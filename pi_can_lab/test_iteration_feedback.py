from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from experiment_store import ExperimentStore
from can_sender import build_parser as build_sender_parser, run as run_sender
from experiment_runner import ExperimentRunner
from ssh_manager import CommandResult
from mutation_feedback import create_trial_feedback
from remote_capture import RemoteCapture
from strategy_selector import TrialStrategySelector
from trial_analysis import analyze_trial, validate_capture_log
from trial_models import MutationCase


BASE = bytes.fromhex("00000000200000F0")


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
            feedback = create_trial_feedback(1, mutation(), [{
                "target_bus": "I_CAN", "target_id": "0x123",
                "type": "TIMING", "score": 0.87,
            }], 0.6)
            updated = store.record_completed_trial(mutation(), feedback)
            reloaded = store.load_feedback_state()
        self.assertEqual(updated, reloaded)
        self.assertEqual(reloaded["total_trials"], 1)
        self.assertEqual(reloaded["next_mutation_id"], 2)
        self.assertEqual(reloaded["mutation_statistics"]["BIT_FLIP"]["interesting"], 1)
        self.assertEqual(reloaded["interesting_mutations"][0]["mutation_id"], 1)

    def test_trial_feedback_maps_one_mutation_to_many_anomalies(self) -> None:
        feedback = create_trial_feedback(3, mutation(84), [
            {"target_bus": "B_CAN", "target_id": "0x456", "type": "TIMING", "score": 0.87},
            {"target_bus": "I_CAN", "target_id": "0x321", "type": "NEW_MESSAGE", "score": 0.72},
        ], 0.6)
        self.assertTrue(feedback["interesting"])
        self.assertEqual(len(feedback["mutation_anomaly_mappings"]), 2)
        self.assertTrue(all(item["mutation_id"] == 84 for item in feedback["mutation_anomaly_mappings"]))


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

    def test_interesting_feedback_exploits_and_records_parent(self) -> None:
        previous = mutation(84).to_dict()
        state = {
            "total_trials": 10, "next_mutation_id": 85,
            "mutation_history": [previous],
            "interesting_mutations": [{
                "mutation_id": 84, "score": 0.87,
                "mutation": previous, "anomaly_types": ["TIMING"], "trial_id": 10,
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
        self.assertEqual(decision.mode, "EXPLOIT")
        self.assertEqual(first.parent_mutation_id, 84)
        self.assertEqual(first.mutated_payload, second.mutated_payload)
        self.assertIn(first.operator, {"BOUNDARY", "BIT_FLIP", "NEIGHBOR"})


class TrialAnalysisTests(unittest.TestCase):
    @staticmethod
    def record(stamp: int, bus: str, can_id: int, payload: str) -> str:
        return json.dumps({
            "record_type": "can_rx", "wall_time_ns": stamp,
            "bus": bus, "arbitration_id": can_id,
            "is_extended_id": False, "data_hex": payload,
            "is_error_frame": False, "is_remote_frame": False,
        }) + "\n"

    def test_trial_result_metrics_and_cross_bus_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "i_can.jsonl"
            lines = []
            for index in range(10):
                lines.append(self.record(1_000_000_000 + index * 20_000_000, "i_can", 0x456, "00"))
            for index in range(5):
                lines.append(self.record(2_000_000_000 + index * 40_000_000, "i_can", 0x456, "01"))
            for index in range(3):
                lines.append(self.record(2_100_000_000 + index * 20_000_000, "i_can", 0x321, "AA"))
            path.write_text("".join(lines), encoding="utf-8")
            result = analyze_trial(
                rx_paths={"i_can": path},
                phase_times_ns={
                    "baseline_start": 1_000_000_000, "baseline_end": 1_500_000_000,
                    "mutation_start": 2_000_000_000, "mutation_end": 2_500_000_000,
                },
                mutation=mutation(),
                thresholds={
                    "minimum_baseline_frames": 5, "minimum_timing_intervals": 3,
                    "new_message_minimum_frames": 3, "timing_relative_change": 0.25,
                    "frequency_relative_change": 0.4, "message_loss_ratio": 0.1,
                    "payload_novel_ratio": 0.2,
                },
            )
        anomaly_types = {item["type"] for item in result["anomalies"]}
        self.assertIn("TIMING", anomaly_types)
        self.assertIn("NEW_MESSAGE", anomaly_types)
        self.assertIn("PAYLOAD_CHANGE", anomaly_types)
        self.assertIn("CROSS_BUS", anomaly_types)
        metric = next(item for item in result["metrics"] if item["can_id"] == "0x456")
        self.assertEqual(metric["baseline"]["message_count"], 10)
        self.assertAlmostEqual(metric["baseline"]["mean_cycle_time_ms"], 20.0)

    def test_incomplete_capture_is_rejected_before_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            path.write_text(self.record(1_000_000_000, "i_can", 0x123, "00"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Incomplete capture"):
                validate_capture_log(path, 42)


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
        return CommandResult("sender complete\n", "", 0)

    def clock_sample(self):
        return {
            "offset_ms": 0.1, "round_trip_ms": 0.2,
            "chrony_available": True, "chrony_tracking": "synchronised",
        }

    def download(self, remote_path, local_path):
        if remote_path.endswith("tx.jsonl"):
            records = [{"record_type": "tx_session_start", "wall_time_ns": 900_000_000}]
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
            records.append({"record_type": "tx_session_end", "status": "completed", "wall_time_ns": 5_000_000_000})
            local_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            return
        lines = [json.dumps({
            "record_type": "session_start", "experiment_id": 42,
            "wall_time_ns": 900_000_000,
        }) + "\n"]
        for index in range(10):
            lines.append(TrialAnalysisTests.record(1_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        for index in range(10):
            lines.append(TrialAnalysisTests.record(3_000_000_000 + index * 50_000_000, self.bus, 0x123, "00"))
        lines.append(json.dumps({
            "record_type": "session_end", "experiment_id": 42,
            "wall_time_ns": 5_100_000_000,
        }) + "\n")
        local_path.write_text("".join(lines), encoding="utf-8")

    def close(self):
        pass


class ExperimentRunnerIntegrationTests(unittest.TestCase):
    def test_completed_trial_updates_state_only_after_collection_and_analysis(self) -> None:
        config = {
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


if __name__ == "__main__":
    unittest.main()
