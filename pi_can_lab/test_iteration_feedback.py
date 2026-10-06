from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiment_store import ExperimentStore
from can_sender import build_parser as build_sender_parser, run as run_sender
from experiment_runner import ExperimentRunner, PairedClockPreflightError
from ssh_manager import CommandResult
from mutation_feedback import create_trial_feedback
from remote_capture import RemoteCapture
from strategy_selector import TrialStrategySelector
from trial_analysis import analyze_trial, validate_capture_log
from trial_models import MutationCase, noop_case


BASE = bytes.fromhex("00000000200000F0")
CLOCKS = {
    bus: {"offset_ms": 0.0, "round_trip_ms": 0.1,
          "alignment_valid": True, "reference_id": "test-controller"}
    for bus in ("p_can", "b_can", "i_can")
}


def mutation(mutation_id: int = 1, parent: int | None = None) -> MutationCase:
    changed = bytearray(BASE)
    changed[3] ^= 0x20
    return MutationCase(
        mutation_id=mutation_id,
        source_bus="b_can",
        can_id=0x366,
        operator="BIT_FLIP",
        original_payload=BASE,
        mutated_payload=bytes(changed),
        random_seed=366,
        parent_mutation_id=parent,
    )


class FeedbackStateTests(unittest.TestCase):
    def test_load_save_and_completed_trial_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, {"target": "0x366"})
            state = store.load_feedback_state()
            self.assertEqual(state["total_trials"], 0)
            anomaly = {
                "target_bus": "I_CAN", "target_id": "0x123",
                "type": "PAYLOAD_CHANGE", "classification": "candidate", "score": 0.87,
                "evidence": {"feedback_eligible": False, "novel_payloads": ["AA"]},
            }
            first = create_trial_feedback(1, mutation(), [anomaly], 0.6, state)
            self.assertEqual(first["verification_status"], "candidate")
            store.record_completed_trial(mutation(), first)
            second_mutation = mutation(2)
            second = create_trial_feedback(
                2, second_mutation, [anomaly], 0.6, store.load_feedback_state()
            )
            self.assertEqual(second["verification_status"], "candidate")
            updated = store.record_completed_trial(second_mutation, second)
            reloaded = store.load_feedback_state()
        self.assertEqual(updated, reloaded)
        self.assertEqual(reloaded["total_trials"], 2)
        self.assertEqual(reloaded["next_mutation_id"], 3)
        self.assertEqual(reloaded["mutation_statistics"]["BIT_FLIP"]["interesting"], 0)
        self.assertEqual(reloaded["interesting_mutations"], [])
        self.assertEqual(reloaded["completed_trial_ids"], [1, 2])
        self.assertEqual(len(reloaded["feedback_candidates"]), 2)

    def test_analyzed_trial_reconciliation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, {"target": "0x366"})
            trial = store.create_trial(1)
            feedback = create_trial_feedback(1, mutation(), [], 0.6)
            store.write_json(trial / "metadata.json", {
                "status": "analyzed", "trial_id": 1,
            })
            store.write_json(trial / "mutation.json", mutation().to_dict())
            store.write_json(trial / "feedback.json", feedback)

            self.assertEqual(store.reconcile_analyzed_trials(), [1])
            self.assertEqual(store.reconcile_analyzed_trials(), [])
            state = store.load_feedback_state()
            metadata = json.loads((trial / "metadata.json").read_text())

        self.assertEqual(state["total_trials"], 1)
        self.assertEqual(state["completed_trial_ids"], [1])
        self.assertEqual(metadata["status"], "completed")

    def test_trial_feedback_maps_one_mutation_to_many_anomalies(self) -> None:
        feedback = create_trial_feedback(3, mutation(84), [
            {"target_bus": "B_CAN", "target_id": "0x456", "type": "TIMING", "score": 0.87},
            {"target_bus": "I_CAN", "target_id": "0x321", "type": "NEW_MESSAGE", "score": 0.72},
        ], 0.6)
        self.assertFalse(feedback["interesting"])
        self.assertEqual(feedback["verification_status"], "none")
        self.assertEqual(len(feedback["mutation_anomaly_mappings"]), 2)
        self.assertTrue(all(item["mutation_id"] == 84 for item in feedback["mutation_anomaly_mappings"]))

    def test_repeat_requires_same_source_and_target_payload(self) -> None:
        anomaly = {
            "target_bus": "P_CAN", "target_id": "0x1F8", "type": "PAYLOAD_CHANGE",
            "classification": "candidate", "score": 1.0,
            "evidence": {"feedback_eligible": False, "novel_payloads": ["01"]},
        }
        first = create_trial_feedback(1, mutation(), [anomaly], 0.6)
        prior = {"feedback_candidates": first["candidate_events"]}
        different_response = {
            **anomaly, "evidence": {"feedback_eligible": False, "novel_payloads": ["02"]}
        }
        second = create_trial_feedback(2, mutation(2), [different_response], 0.6, prior)
        self.assertFalse(second["interesting"])
        self.assertEqual(second["verification_status"], "candidate")

    def test_noop_diagnostics_never_enter_mutation_history_or_selector_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, {})
            before = store.load_feedback_state()
            control = noop_case(1, "b_can", 0x366, BASE, 366)
            anomaly = {
                "target_bus": "P_CAN", "target_id": "0x1F8", "type": "PAYLOAD_CHANGE",
                "classification": "candidate", "score": 0.9,
                "evidence": {"feedback_eligible": False},
            }
            feedback = create_trial_feedback(1, control, [anomaly], 0.6)
            self.assertEqual(len(feedback["control_events"]), 1)
            self.assertEqual(feedback["candidate_events"], [])
            store.record_completed_trial(control, feedback)
            store.record_completed_trial(control, feedback)
            after = store.load_feedback_state()
            self.assertEqual(after["total_control_trials"], 1)
            self.assertEqual(after["control_trial_ids"], [1])
            self.assertEqual(after["total_trials"], 0)
            self.assertEqual(after["mutation_history"], [])
            self.assertEqual(after["feedback_candidates"], [])
            self.assertEqual(store.next_mutation_id(), 1)
            self.assertEqual(
                TrialStrategySelector._rng(5, before).random(),
                TrialStrategySelector._rng(5, after).random(),
            )


class StrategySelectionTests(unittest.TestCase):
    def selector(self, **overrides) -> TrialStrategySelector:
        config = {
            "bit_operation_ratio": 1.0,
            "max_operations": 1,
            "no_anomaly": {"exploration_probability": 1.0},
            "interesting": {"exploitation_probability": 1.0},
        }
        config.update(overrides)
        return TrialStrategySelector(config)

    def test_no_feedback_initial_trial_uses_original_mutator(self) -> None:
        state = {
            "total_trials": 0, "next_mutation_id": 1,
            "interesting_mutations": [], "mutation_history": [], "last_feedback": None,
        }
        selected, decision = self.selector().select_mutation(
            state=state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=366,
        )
        self.assertEqual(decision.strategy, "INITIAL")
        self.assertEqual(selected.strategy_mode, "EXPLORE")
        self.assertNotEqual(selected.original_payload, selected.mutated_payload)
        self.assertIsNone(selected.parent_mutation_id)

    def test_non_semantic_timestamps_do_not_change_selection(self) -> None:
        first_state = {
            "total_trials": 0, "next_mutation_id": 1,
            "interesting_mutations": [], "mutation_history": [],
            "last_feedback": None, "updated_at": "2026-01-01T00:00:00Z",
        }
        second_state = {**first_state, "updated_at": "2026-09-07T00:00:00Z"}
        selector = self.selector()
        first, _ = selector.select_mutation(
            state=first_state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=366,
        )
        second, _ = selector.select_mutation(
            state=second_state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=366,
        )
        self.assertEqual(first.mutated_payload, second.mutated_payload)

    def test_no_anomaly_selects_exploration(self) -> None:
        previous = mutation().to_dict()
        state = {
            "total_trials": 1, "next_mutation_id": 2,
            "interesting_mutations": [], "mutation_history": [previous],
            "last_feedback": {"interesting": False},
        }
        selected, decision = self.selector().select_mutation(
            state=state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=7,
        )
        self.assertEqual(decision.mode, "EXPLORE")
        self.assertIsNone(selected.parent_mutation_id)

    def test_old_verified_label_does_not_trigger_automatic_exploitation(self) -> None:
        previous = mutation(84).to_dict()
        state = {
            "total_trials": 10, "next_mutation_id": 85,
            "mutation_history": [previous],
            "interesting_mutations": [{
                "mutation_id": 84, "score": 0.87,
                "mutation": previous, "anomaly_types": ["TIMING"], "trial_id": 10,
                "verification_status": "verified",
            }],
            "last_feedback": {"interesting": True},
        }
        selector = self.selector()
        first, decision = selector.select_mutation(
            state=state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=42,
        )
        second, _ = selector.select_mutation(
            state=state, original_payload=BASE, source_bus="b_can", can_id=0x366, random_seed=42,
        )
        self.assertEqual(decision.mode, "EXPLORE")
        self.assertIsNone(first.parent_mutation_id)
        self.assertEqual(first.mutated_payload, second.mutated_payload)
        self.assertNotEqual(first.mutated_payload, BASE)

    def test_enabling_old_automatic_feedback_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Automatic feedback is disabled"):
            self.selector(enabled=True)

    def test_legacy_unverified_score_cannot_be_exploited(self) -> None:
        previous = mutation(84).to_dict()
        state = {
            "total_trials": 1, "next_mutation_id": 85,
            "mutation_history": [previous],
            "interesting_mutations": [{"mutation_id": 84, "score": 1.0, "mutation": previous}],
            "last_feedback": {"interesting": True, "anomaly_score": 1.0},
        }
        decision = self.selector().decide(state, 42)
        self.assertEqual(decision.mode, "EXPLORE")

    def test_automatic_feedback_disabled_by_default_even_with_old_verified_state(self) -> None:
        previous = mutation(84).to_dict()
        state = {
            "total_trials": 10, "next_mutation_id": 85,
            "mutation_history": [previous],
            "interesting_mutations": [{
                "mutation_id": 84, "score": 1.0, "mutation": previous,
                "verification_status": "verified",
            }],
        }
        self.assertEqual(TrialStrategySelector({}).decide(state, 42).mode, "EXPLORE")


class TrialAnalysisTests(unittest.TestCase):
    @staticmethod
    def record(stamp: int, bus: str, can_id: int, payload: str) -> str:
        return json.dumps({
            "record_type": "can_rx", "wall_time_ns": stamp,
            "bus": bus, "arbitration_id": can_id,
            "is_extended_id": False, "data_hex": payload,
            "is_error_frame": False, "is_remote_frame": False,
        }) + "\n"

    def test_trial_result_metrics_and_cross_bus_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "i_can.jsonl"
            lines = []
            for index in range(50):
                lines.append(self.record(1_000_000_000 + index * 20_000_000, "i_can", 0x456, "00"))
            for index in range(50):
                lines.append(self.record(2_000_000_000 + index * 20_000_000, "i_can", 0x456, "00"))
            for index in range(25):
                lines.append(self.record(3_000_000_000 + index * 40_000_000, "i_can", 0x456, "01"))
            for index in range(3):
                lines.append(self.record(3_100_000_000 + index * 20_000_000, "i_can", 0x321, "AA"))
                lines.append(self.record(4_100_000_000 + index * 20_000_000, "i_can", 0x321, "AA"))
            for index in range(25):
                lines.append(self.record(4_000_000_000 + index * 40_000_000, "i_can", 0x456, "01"))
            path.write_text("".join(lines), encoding="utf-8")
            result = analyze_trial(
                rx_paths={"i_can": path},
                phase_times_ns={
                    "baseline_start": 1_000_000_000, "baseline_end": 2_000_000_000,
                    "normal_start": 2_000_000_000, "normal_end": 3_000_000_000,
                    "mutation_start": 3_000_000_000, "mutation_end": 4_000_000_000,
                    "recovery_start": 4_000_000_000, "recovery_end": 5_000_000_000,
                },
                mutation=mutation(),
                thresholds={
                    "minimum_baseline_frames": 5, "minimum_timing_intervals": 3,
                    "new_message_minimum_frames": 3, "timing_relative_change": 0.25,
                    "frequency_relative_change": 0.4, "message_loss_ratio": 0.1,
                    "payload_novel_ratio": 0.2,
                },
                clock_offsets=CLOCKS,
            )
        anomaly_types = {item["type"] for item in result["anomalies"]}
        self.assertIn("FREQUENCY_CHANGE", anomaly_types)
        self.assertIn("NEW_MESSAGE", anomaly_types)
        self.assertTrue(all(item["cross_bus"] for item in result["anomalies"]))
        self.assertNotIn("CROSS_BUS", anomaly_types)
        metric = next(item for item in result["metrics"] if item["can_id"] == "0x456")
        self.assertEqual(metric["baseline"]["message_count"], 50)
        self.assertAlmostEqual(metric["baseline"]["mean_cycle_time_ms"], 20.0)

    def test_incomplete_capture_is_rejected_before_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            path.write_text(self.record(1_000_000_000, "i_can", 0x123, "00"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Incomplete capture"):
                validate_capture_log(path, 42)

    def test_jitter_from_zero_baseline_stddev_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            baseline = [1_000_000_000 + index * 20_000_000 for index in range(5)]
            mutation_stamps = [
                2_000_000_000, 2_010_000_000, 2_040_000_000,
                2_050_000_000, 2_080_000_000,
            ]
            path.write_text("".join(
                self.record(stamp, "i_can", 0x123, "00")
                for stamp in baseline + mutation_stamps
            ), encoding="utf-8")
            result = analyze_trial(
                rx_paths={"i_can": path},
                phase_times_ns={
                    "baseline_start": 1_000_000_000,
                    "baseline_end": 1_500_000_000,
                    "mutation_start": 2_000_000_000,
                    "mutation_end": 2_500_000_000,
                },
                mutation=mutation(),
                thresholds={
                    "minimum_baseline_frames": 5,
                    "minimum_timing_intervals": 3,
                    "timing_relative_change": 0.25,
                    "timing_stddev_absolute_ms": 2.0,
                },
                clock_offsets=CLOCKS,
            )
        timing = next(item for item in result["anomalies"] if item["type"] == "TIMING")
        self.assertEqual(timing["evidence"]["control_stddev_ms"], 0.0)
        self.assertEqual(timing["evidence"]["mutation_stddev_ms"], 10.0)

    def test_exact_target_routing_on_other_bus_is_evidence_not_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            routed = mutation().mutated_payload.hex().upper()
            lines = [
                self.record(1_000_000_000 + index * 20_000_000, "i_can", 0x366, BASE.hex())
                for index in range(10)
            ]
            lines.extend(
                self.record(2_000_000_000 + index * 20_000_000, "i_can", 0x366, routed)
                for index in range(10)
            )
            path.write_text("".join(lines), encoding="utf-8")
            result = analyze_trial(
                rx_paths={"i_can": path},
                phase_times_ns={
                    "baseline_start": 1_000_000_000,
                    "baseline_end": 1_500_000_000,
                    "mutation_start": 2_000_000_000,
                    "mutation_end": 2_500_000_000,
                },
                mutation=mutation(),
                thresholds={"new_message_minimum_frames": 3},
            )
        self.assertEqual(result["anomalies"], [])
        self.assertEqual(result["summary"]["propagated_payloads"], [
            {"bus": "I_CAN", "matches": 10}
        ])

    def test_payload_candidate_uses_equal_length_pre_mutation_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p_can.jsonl"
            lines = [
                self.record(1_000_000_000 + index * 100_000_000, "p_can", 0x1F8, "00")
                for index in range(100)
            ]
            lines += [
                self.record(11_000_000_000 + index * 100_000_000, "p_can", 0x1F8, "00")
                for index in range(50)
            ]
            lines += [
                self.record(16_000_000_000 + index * 100_000_000, "p_can", 0x1F8,
                            "01" if index >= 6 else "00")
                for index in range(10)
            ]
            lines += [
                self.record(17_000_000_000 + index * 100_000_000, "p_can", 0x1F8, "01")
                for index in range(10)
            ]
            path.write_text("".join(lines), encoding="utf-8")
            phases = {
                "baseline_start": 1_000_000_000, "baseline_end": 11_000_000_000,
                "normal_start": 11_000_000_000, "normal_end": 16_000_000_000,
                "mutation_start": 16_000_000_000, "mutation_end": 17_000_000_000,
                "recovery_start": 17_000_000_000, "recovery_end": 18_000_000_000,
            }
            result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={}, clock_offsets=CLOCKS,
            )
            candidate = next(item for item in result["anomalies"] if item["type"] == "PAYLOAD_CHANGE")
            self.assertEqual(candidate["target_id"], "0x1F8")
            self.assertFalse(candidate["evidence"]["feedback_eligible"])
            self.assertEqual(candidate["evidence"]["persistence_frames"], 14)

            # The same value in a completed earlier trial's pre-mutation traffic
            # makes it an already-observed state, not a novel reaction.
            previous = Path(directory) / "trial_0001"
            previous.mkdir()
            (previous / "metadata.json").write_text(json.dumps({
                "status": "completed", "phase_times_ns": phases,
                "logs": {"p_can": "p_can.jsonl"},
                "clock_offsets": CLOCKS,
            }), encoding="utf-8")
            (previous / "p_can.jsonl").write_text(
                self.record(1_000_000_000, "p_can", 0x1F8, "01"), encoding="utf-8"
            )
            historical_result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={},
                experiment_dir=Path(directory), current_trial_id=2,
                clock_offsets=CLOCKS,
            )
            historical_candidate = next(item for item in historical_result["anomalies"]
                                        if item["type"] == "PAYLOAD_CHANGE")
            self.assertTrue(historical_candidate["evidence"]["historically_seen_payload"])
            self.assertEqual(historical_result["summary"]["historical_control_trials"], 1)

            # A prior occurrence only during injection is not a natural-state
            # control; it may instead be a reproducible response.
            (previous / "p_can.jsonl").write_text(
                self.record(16_500_000_000, "p_can", 0x1F8, "01"), encoding="utf-8"
            )
            prior_injection_result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={},
                experiment_dir=Path(directory), current_trial_id=2,
                clock_offsets=CLOCKS,
            )
            candidate = next(
                item for item in prior_injection_result["anomalies"]
                if item["type"] == "PAYLOAD_CHANGE"
            )
            self.assertTrue(candidate["evidence"]["historically_seen_payload"])
            self.assertEqual(candidate["classification"], "candidate")

            (previous / "metadata.json").write_text(json.dumps({
                "status": "failed", "phase_times_ns": phases,
                "logs": {"p_can": "p_can.jsonl"},
            }), encoding="utf-8")
            failed_history_result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={},
                experiment_dir=Path(directory), current_trial_id=2,
                clock_offsets=CLOCKS,
            )
            self.assertEqual(failed_history_result["summary"]["historical_control_trials"], 0)

            # An occurrence during this Trial's Normal phase has the same effect.
            path.write_text("".join(lines) + self.record(
                15_900_000_000, "p_can", 0x1F8, "01"
            ), encoding="utf-8")
            normal_result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={}, clock_offsets=CLOCKS,
            )
            self.assertFalse(any(item["type"] == "PAYLOAD_CHANGE" for item in normal_result["anomalies"]))

    def test_previous_sparse_message_is_not_new_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "trial_0001"
            previous.mkdir()
            phases = {
                "baseline_start": 1_000_000_000, "baseline_end": 11_000_000_000,
                "mutation_start": 16_000_000_000, "mutation_end": 17_000_000_000,
            }
            (previous / "metadata.json").write_text(json.dumps({
                "status": "completed", "phase_times_ns": phases,
                "logs": {"p_can": "p_can.jsonl"},
            }), encoding="utf-8")
            (previous / "p_can.jsonl").write_text(
                self.record(2_000_000_000, "p_can", 0x17332811, "AA"), encoding="utf-8"
            )
            current = root / "current.jsonl"
            current.write_text("".join(
                self.record(16_000_000_000 + index * 10_000_000, "p_can", 0x17332811, "AA")
                for index in range(3)
            ), encoding="utf-8")
            result = analyze_trial(
                rx_paths={"p_can": current}, phase_times_ns=phases,
                mutation=mutation(), thresholds={},
                experiment_dir=root, current_trial_id=2,
            )
            self.assertNotIn("NEW_MESSAGE", {item["type"] for item in result["anomalies"]})


class FakeManager:
    def __init__(self, bus: str):
        self.bus = bus
        self.started = []
        self.stopped = []

    def ensure_directory(self, path):
        self.remote_dir = path

    def start_process(self, command, stdout_path):
        self.started.append(command)
        return SimpleNamespace(pid=100 + len(self.started), command=tuple(command), stdout_path=stdout_path)

    def stop_process(self, process):
        self.stopped.append(process.pid)

    def process_alive(self, process):
        return process.pid not in self.stopped

    def download(self, remote_path, local_path):
        local_path.write_text(self.bus, encoding="utf-8")


class RemoteCaptureTests(unittest.TestCase):
    def test_capture_waits_for_session_marker_before_ready(self) -> None:
        class DelayedManager(FakeManager):
            def __init__(self, bus):
                super().__init__(bus)
                self.probes = 0

            def run(self, command, timeout=None, check=True):
                self.probes += 1
                if self.probes == 1:
                    return CommandResult("", "not ready", 1)
                marker = {"record_type": "session_start", "experiment_id": 42, "bus": self.bus}
                return CommandResult(json.dumps(marker) + "\n", "", 0)

        managers = {bus: DelayedManager(bus) for bus in ("p_can", "b_can", "i_can")}
        capture = RemoteCapture(
            managers,
            {bus: "/project/pi_can_lab" for bus in managers},
            {bus: "python3" for bus in managers},
            {bus: f"receiver_{bus}.yaml" for bus in managers},
            "/tmp/trials",
        )
        capture.start_all(42, 1)
        capture.wait_ready(42, timeout_seconds=1)
        self.assertTrue(all(manager.probes == 2 for manager in managers.values()))

    def test_three_pi_capture_is_mockable_and_collected(self) -> None:
        managers = {bus: FakeManager(bus) for bus in ("p_can", "b_can", "i_can")}
        capture = RemoteCapture(
            managers,
            {bus: "/project/pi_can_lab" for bus in managers},
            {bus: "python3" for bus in managers},
            {bus: f"receiver_{bus[0]}_can.yaml" for bus in managers},
            "/tmp/trials",
        )
        with tempfile.TemporaryDirectory() as directory:
            capture.start_all(42, 1)
            capture.assert_all_running()
            paths = capture.stop_and_download(Path(directory))
            self.assertEqual(set(paths), set(managers))
            self.assertTrue(all(path.is_file() for path in paths.values()))
        self.assertTrue(all(manager.stopped for manager in managers.values()))

    def test_partial_start_failure_stops_late_successes(self) -> None:
        class StartManager(FakeManager):
            def __init__(self, bus: str, *, fail=False, delay=0.0):
                super().__init__(bus)
                self.fail = fail
                self.delay = delay

            def ensure_directory(self, path):
                if self.delay:
                    time.sleep(self.delay)
                if self.fail:
                    raise RuntimeError("start failed")
                super().ensure_directory(path)

        managers = {
            "p_can": StartManager("p_can", fail=True),
            "b_can": StartManager("b_can", delay=0.05),
            "i_can": StartManager("i_can", delay=0.05),
        }
        capture = RemoteCapture(
            managers,
            {bus: "/project/pi_can_lab" for bus in managers},
            {bus: "python3" for bus in managers},
            {bus: f"receiver_{bus[0]}_can.yaml" for bus in managers},
            "/tmp/trials",
        )
        with self.assertRaisesRegex(RuntimeError, "Capture start failure"):
            capture.start_all(42, 1)
        self.assertEqual(managers["b_can"].stopped, [101])
        self.assertEqual(managers["i_can"].stopped, [101])
        self.assertFalse(capture.handles)


class TrialSenderTests(unittest.TestCase):
    def test_explicit_trial_mutation_preserves_identity_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sender.yaml"
            config.write_text("""
bus:
  interface: virtual
  channel: trial
sender:
  bus_name: b_can
  id: 0x366
  data: 00000000200000F0
  output: tx.jsonl
  mutation:
    enabled: true
  transmit:
    count: 1
    interval_ms: 10
    restore_original: false
  safety:
    max_count: 1
    max_duration_seconds: 10
    min_interval_ms: 10
""".strip(), encoding="utf-8")
            args = build_sender_parser().parse_args([
                "--config", str(config),
                "--mutation-data", "00000020200000F0",
                "--mutation-id", "85",
                "--parent-mutation-id", "84",
                "--mutation-operator", "BIT_FLIP",
                "--generation-reason", "completed Trial 10 anomaly",
                "--random-seed", "42",
            ])
            self.assertEqual(run_sender(args), 0)
            records = [json.loads(line) for line in (root / "tx.jsonl").read_text().splitlines()]
        frame = next(item for item in records if item.get("record_type") == "can_tx")
        self.assertEqual(frame["mutation"]["mutation_id"], 85)
        self.assertEqual(frame["mutation"]["parent_mutation_id"], 84)
        self.assertEqual(frame["mutation"]["operators"], ["BIT_FLIP"])


class FakeRunnerManager(FakeManager):
    def __init__(self, config):
        super().__init__(config["name"])

    def run(self, command, timeout=None, check=True):
        del timeout, check
        if "candump" in command:
            return CommandResult("(1.0) can0 366#00000000200000F0\n" * 3, "", 0)
        if command[0] == "head":
            marker = {"record_type": "session_start", "experiment_id": 42, "bus": self.bus}
            return CommandResult(json.dumps(marker) + "\n", "", 0)
        return CommandResult("sender complete\n", "", 0)

    def clock_sample(self):
        return {
            "offset_ms": 0.1, "round_trip_ms": 0.2,
            "chrony_available": True, "chrony_tracking": "synchronised",
        }

    def process_alive(self, process):
        if any(str(part).endswith("can_sender.py") for part in process.command):
            return False
        return super().process_alive(process)

    def download(self, remote_path, local_path):
        if remote_path.endswith("sender.stdout.log"):
            local_path.write_text("sender complete\n", encoding="utf-8")
            return
        if remote_path.endswith("tx.jsonl"):
            sender = next(command for command in reversed(self.started)
                          if "--trial-contract-version" in command)
            trial_kind = "noop" if "--control-noop" in sender else "mutation"
            mutation_payload = sender[sender.index("--mutation-data") + 1].upper()
            mutation_id = int(sender[sender.index("--mutation-id") + 1])
            original_payload = BASE.hex().upper()
            records = [{
                "record_type": "tx_session_start", "wall_time_ns": 900_000_000,
                "tx_session_id": "fake-session", "trial_contract_version": 1,
                "trial_kind": trial_kind, "experiment_id": "42", "execute": True,
                "campaign": {"enabled": True, "normal_data_hex": original_payload},
                "mutation": {"trial_mutation_id": mutation_id, "control_noop": trial_kind == "noop"},
            }]
            for phase, start, end in (
                ("baseline", 1_000_000_000, 2_000_000_000),
                ("normal", 2_000_000_000, 3_000_000_000),
                ("mutation", 3_000_000_000, 4_000_000_000),
                ("recovery", 4_000_000_000, 5_000_000_000),
            ):
                records.extend([
                    {"record_type": "tx_phase", "phase": phase, "event": "start", "wall_time_ns": start},
                    {"record_type": "tx_phase", "phase": phase, "event": "end", "wall_time_ns": end},
                ])
            for phase, payload in (("normal", original_payload), ("mutation", mutation_payload)):
                for index in range(20):
                    records.append({
                        "record_type": "can_tx", "status": "sent", "phase": phase,
                        "arbitration_id": 0x366, "data_hex": payload,
                        "trial_kind": trial_kind, "sequence": index + 1,
                    })
            records.append({
                "record_type": "can_tx", "status": "sent", "phase": "recovery",
                "kind": "restore", "arbitration_id": 0x366,
                "data_hex": original_payload, "trial_kind": trial_kind,
            })
            records.append({
                "record_type": "tx_session_end", "status": "completed",
                "wall_time_ns": 5_000_000_000, "tx_session_id": "fake-session",
                "trial_contract_version": 1, "trial_kind": trial_kind,
                "experiment_id": "42", "execute": True,
                "phase_sent": {"normal": 20, "mutation": 20},
                "restore": {"status": "sent", "sent": 1},
            })
            local_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            return
        lines = [json.dumps({
            "record_type": "session_start", "experiment_id": 42,
            "wall_time_ns": 900_000_000,
        }) + "\n"]
        for index in range(10):
            lines.append(TrialAnalysisTests.record(1_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        for index in range(10):
            lines.append(TrialAnalysisTests.record(2_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        for index in range(10):
            lines.append(TrialAnalysisTests.record(3_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        for index in range(10):
            lines.append(TrialAnalysisTests.record(4_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        lines.append(json.dumps({
            "record_type": "session_end", "experiment_id": 42,
            "wall_time_ns": 5_100_000_000,
        }) + "\n")
        local_path.write_text("".join(lines), encoding="utf-8")

    def close(self):
        pass


class AdjustableClockManager(FakeRunnerManager):
    def __init__(self, config):
        super().__init__(config)
        self.round_trip_ms = config.get("round_trip_ms", 0.2)
        self.round_trip_sequence = config.get("round_trip_sequence")
        self.clock_calls = 0

    def clock_sample(self):
        self.clock_calls += 1
        sequence = self.round_trip_sequence
        rtt = (sequence[min(self.clock_calls - 1, len(sequence) - 1)]
               if sequence else self.round_trip_ms)
        return {"offset_ms": 0.0, "round_trip_ms": rtt}


class ExperimentRunnerIntegrationTests(unittest.TestCase):
    @staticmethod
    def config() -> dict:
        return {
            "remote": {
                "project_dir": "/project/pi_can_lab",
                "capture_root": "/tmp/trials",
                "hosts": {bus: {"name": bus} for bus in ("p_can", "b_can", "i_can")},
            },
            "target": {"probe_live_payload": True, "probe_samples": 3},
            "trial": {
                "capture_start_delay_seconds": 0,
                "baseline_seconds": 1, "normal_seconds": 1,
                "mutation_seconds": 1, "post_seconds": 1,
            },
            "time_sync": {"samples": 1, "warning_threshold_ms": 50},
            "feedback": {
                "interesting_score_threshold": 0.6,
                "no_anomaly": {"exploration_probability": 1.0},
                "interesting": {"exploitation_probability": 1.0},
            },
            "anomaly_thresholds": {"minimum_baseline_frames": 5},
        }

    def test_preparation_failure_is_recorded_without_feedback(self) -> None:
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=FakeRunnerManager)

            def fail_probe(*args):
                del args
                raise RuntimeError("probe failed")

            runner.probe_payload = fail_probe
            try:
                with self.assertRaisesRegex(RuntimeError, "probe failed"):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366,
                        selector=TrialStrategySelector(config["feedback"]),
                        dbc_path=None,
                    )
            finally:
                runner.close()
            metadata = json.loads(
                (store.path / "trial_0001" / "metadata.json").read_text()
            )
            state = store.load_feedback_state()

        self.assertEqual(metadata["status"], "failed")
        self.assertIn("probe failed", metadata["error"])
        self.assertEqual(state["total_trials"], 0)

    def test_paired_clock_sampling_retries_until_all_receivers_align(self) -> None:
        config = self.config()
        config["remote"]["hosts"]["b_can"]["round_trip_sequence"] = [120.0]
        config["remote"]["hosts"]["p_can"]["round_trip_sequence"] = [160.0, 60.0]
        config["remote"]["hosts"]["i_can"]["round_trip_sequence"] = [30.0]
        runner = ExperimentRunner(config, manager_factory=AdjustableClockManager)
        try:
            standalone = runner.check_clocks()
            self.assertEqual(standalone["p_can"]["round_trip_ms"], 160.0)
            self.assertEqual(runner.managers["p_can"].clock_calls, 1)
        finally:
            runner.close()

        runner = ExperimentRunner(config, manager_factory=AdjustableClockManager)
        try:
            paired = runner.check_clocks(paired_source_bus="b_can")
            self.assertEqual(paired["p_can"]["round_trip_ms"], 60.0)
            self.assertEqual(len(paired["p_can"]["samples"]), 2)
            self.assertEqual(
                {bus: manager.clock_calls for bus, manager in runner.managers.items()},
                {"p_can": 2, "b_can": 2, "i_can": 2},
            )
        finally:
            runner.close()

    def test_paired_clock_failure_is_retryable_without_trial_or_transmission(self) -> None:
        config = self.config()
        for host in config["remote"]["hosts"].values():
            host["round_trip_ms"] = 250.0
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=AdjustableClockManager)
            selector = TrialStrategySelector(config["feedback"])
            try:
                with self.assertRaisesRegex(PairedClockPreflightError, "before transmission"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                pair_path = store.path / "pairs" / "pair_0001.json"
                pair = json.loads(pair_path.read_text())
                self.assertEqual(pair["status"], "prepared")
                self.assertEqual(pair["last_clock_preflight_failure"]["trial_id"], 1)
                self.assertFalse((store.path / "trial_0001").exists())
                self.assertTrue(all(not manager.started for manager in runner.managers.values()))
                self.assertTrue(all(manager.clock_calls == 4
                                    for manager in runner.managers.values()))

                for manager in runner.managers.values():
                    manager.round_trip_ms = 0.2
                with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                    "status": "stable", "reasons": [],
                }), patch("experiment_runner.analyze_trial_pair", return_value={
                    "comparability": {"status": "comparable"},
                }):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                completed = json.loads(pair_path.read_text())
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["first_trial_id"], pair["first_trial_id"])
                self.assertEqual(completed["frozen_mutation"], pair["frozen_mutation"])
            finally:
                runner.close()

    def test_second_paired_clock_failure_preserves_completed_first_episode(self) -> None:
        config = self.config()
        for host in config["remote"]["hosts"].values():
            host["round_trip_sequence"] = [0.2, 250.0, 250.0, 250.0, 250.0]
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=AdjustableClockManager)
            selector = TrialStrategySelector(config["feedback"])
            try:
                with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                    "status": "stable", "reasons": [],
                }), patch("experiment_runner.analyze_trial_pair", return_value={
                    "comparability": {"status": "comparable"},
                }):
                    with self.assertRaises(PairedClockPreflightError):
                        runner.run_paired_set(
                            store=store, source_bus="b_can", can_id=0x366,
                            random_seed=366, selector=selector, dbc_path=None,
                        )
                    pair_path = store.path / "pairs" / "pair_0001.json"
                    paused = json.loads(pair_path.read_text())
                    first_record = (store.path / "trial_0001" / "metadata.json").read_bytes()
                    self.assertEqual(paused["status"], "first_completed")
                    self.assertFalse((store.path / "trial_0002").exists())
                    sender_commands = [
                        command for command in runner.managers["b_can"].started
                        if "--trial-contract-version" in command
                    ]
                    self.assertEqual(len(sender_commands), 1)

                    for manager in runner.managers.values():
                        manager.round_trip_sequence = None
                        manager.round_trip_ms = 0.2
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                self.assertEqual(json.loads(pair_path.read_text())["status"], "completed")
                self.assertEqual(
                    (store.path / "trial_0001" / "metadata.json").read_bytes(), first_record
                )
                sender_commands = [
                    command for command in runner.managers["b_can"].started
                    if "--trial-contract-version" in command
                ]
                self.assertEqual(len(sender_commands), 2)
            finally:
                runner.close()

    def test_completed_trial_updates_state_only_after_collection_and_analysis(self) -> None:
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=FakeRunnerManager)
            try:
                runner.run_trial(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                    dbc_path=None,
                )
                runner.run_trial(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                    dbc_path=None, reproduce_mutation_id=1,
                )
            finally:
                runner.close()
            state = store.load_feedback_state()
            trial = store.path / "trial_0001"
            metadata = json.loads((trial / "metadata.json").read_text())
            feedback_exists = (trial / "feedback.json").is_file()
            logs_exist = all(
                (trial / f"{bus}.jsonl").is_file()
                for bus in ("p_can", "b_can", "i_can")
            )
            reproduced = json.loads(
                (store.path / "trial_0002" / "mutation.json").read_text()
            )
        self.assertEqual(state["total_trials"], 2)
        self.assertEqual(metadata["status"], "completed")
        self.assertTrue(feedback_exists)
        self.assertTrue(logs_exist)
        self.assertEqual(reproduced["reproduction_of_mutation_id"], 1)
        self.assertEqual(reproduced["parent_mutation_id"], 1)

    def test_noop_trial_is_recorded_without_consuming_mutation_id(self) -> None:
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=FakeRunnerManager)
            try:
                runner.run_trial(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                    dbc_path=None, control_noop=True,
                )
                control_state = store.load_feedback_state()
                control = json.loads((store.path / "trial_0001" / "mutation.json").read_text())
                command = next(
                    item for item in runner.managers["b_can"].started
                    if "--trial-contract-version" in item
                )
                self.assertIn("--control-noop", command)
                self.assertEqual(command[command.index("--interval-ms") + 1], "50.0")
                runner.run_trial(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                    dbc_path=None,
                )
            finally:
                runner.close()
            mutation_doc = json.loads((store.path / "trial_0002" / "mutation.json").read_text())
            final_state = store.load_feedback_state()
        self.assertEqual(control_state["total_control_trials"], 1)
        self.assertEqual(control_state["total_trials"], 0)
        self.assertEqual(control["trial_kind"], "noop")
        self.assertEqual(control["original_payload"], control["mutated_payload"])
        self.assertEqual(mutation_doc["mutation_id"], 1)
        self.assertEqual(mutation_doc["trial_kind"], "mutation")
        self.assertEqual(final_state["total_trials"], 1)

    def test_incomplete_sender_is_not_committed_as_analyzed(self) -> None:
        class IncompleteSenderManager(FakeRunnerManager):
            def download(self, remote_path, local_path):
                if remote_path.endswith("tx.jsonl"):
                    local_path.write_text(json.dumps({
                        "record_type": "tx_session_end", "status": "aborted",
                    }) + "\n", encoding="utf-8")
                    return
                super().download(remote_path, local_path)

        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=IncompleteSenderManager)
            try:
                with self.assertRaisesRegex(ValueError, "TX manifest must contain exactly one"):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                        dbc_path=None,
                    )
            finally:
                runner.close()
            metadata = json.loads((store.path / "trial_0001" / "metadata.json").read_text())
            state = store.load_feedback_state()
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(state["total_trials"], 0)

    def test_receiver_missing_recovery_is_not_committed(self) -> None:
        class EarlyReceiverManager(FakeRunnerManager):
            def download(self, remote_path, local_path):
                super().download(remote_path, local_path)
                if remote_path.endswith("b_can.jsonl"):
                    content = local_path.read_text(encoding="utf-8")
                    local_path.write_text(
                        content.replace('"wall_time_ns": 5100000000', '"wall_time_ns": 4900000000'),
                        encoding="utf-8",
                    )

        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=EarlyReceiverManager)
            try:
                with self.assertRaisesRegex(ValueError, "does not cover baseline through recovery"):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                        dbc_path=None,
                    )
            finally:
                runner.close()
            metadata = json.loads((store.path / "trial_0001" / "metadata.json").read_text())
            state = store.load_feedback_state()
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(state["total_trials"], 0)

    def test_noop_with_changed_tx_payload_is_rejected(self) -> None:
        class ChangedNoopManager(FakeRunnerManager):
            def download(self, remote_path, local_path):
                super().download(remote_path, local_path)
                if remote_path.endswith("tx.jsonl"):
                    records = [json.loads(line) for line in local_path.read_text().splitlines()]
                    changed = next(record for record in records if
                                   record.get("record_type") == "can_tx"
                                   and record.get("phase") == "mutation")
                    changed["data_hex"] = "FF" * 8
                    local_path.write_text("".join(json.dumps(item) + "\n" for item in records))

        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            store = ExperimentStore(Path(directory), 42, config)
            runner = ExperimentRunner(config, manager_factory=ChangedNoopManager)
            try:
                with self.assertRaisesRegex(ValueError, "mutation payload differs"):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector(config["feedback"]),
                        dbc_path=None, control_noop=True,
                    )
            finally:
                runner.close()
            self.assertEqual(store.load_feedback_state()["total_control_trials"], 0)


if __name__ == "__main__":
    unittest.main()
