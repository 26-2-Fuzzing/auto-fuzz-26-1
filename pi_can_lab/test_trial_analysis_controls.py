"""Regression tests for state-aware, single-trial CAN observations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trial_analysis import _clock_alignment, analyze_trial, validate_capture_log
from trial_models import MutationCase


NS = 1_000_000_000
DBC = Path(__file__).resolve().parents[1] / "A5.dbc"
CLOCKS = {
    "b_can": {"offset_ms": 0, "round_trip_ms": 1, "alignment_valid": True, "reference_id": "lab"},
    "i_can": {"offset_ms": 0, "round_trip_ms": 1, "alignment_valid": True, "reference_id": "lab"},
    "p_can": {"offset_ms": 0, "round_trip_ms": 1, "alignment_valid": True, "reference_id": "lab"},
}


def mutation() -> MutationCase:
    return MutationCase(
        mutation_id=1, source_bus="b_can", can_id=0x366, operator="SIGNAL_SINGLE",
        original_payload=bytes.fromhex("00000000200000F0"),
        mutated_payload=bytes.fromhex("0000000020000018"), random_seed=366,
    )


def record(bus: str, can_id: int, payload: str, stamp_ns: int) -> str:
    return json.dumps({
        "record_type": "can_rx", "bus": bus, "arbitration_id": can_id,
        "is_extended_id": can_id > 0x7FF, "data_hex": payload,
        "wall_time_ns": stamp_ns, "is_error_frame": False, "is_remote_frame": False,
    }) + "\n"


def regular(bus: str, can_id: int, payload: str, start_ns: int, count: int, step_ns: int) -> list[str]:
    return [record(bus, can_id, payload, start_ns + index * step_ns) for index in range(count)]


def write_frames(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines), encoding="utf-8")


class StateAwareAnalysisTests(unittest.TestCase):
    def test_invalid_clock_sample_does_not_produce_false_alignment(self) -> None:
        clocks = {
            **CLOCKS,
            "i_can": {**CLOCKS["i_can"], "offset_ms": float("inf")},
        }
        self.assertEqual(_clock_alignment("i_can", "b_can", clocks)[2], "unknown")
        clocks["i_can"] = {**CLOCKS["i_can"], "uncertainty_ms": -1}
        self.assertEqual(_clock_alignment("i_can", "b_can", clocks)[2], "uncertain")

    def test_capture_markers_report_bounds_and_reject_bad_order(self) -> None:
        start = {"record_type": "session_start", "experiment_id": 42, "wall_time_ns": 100}
        frame = {"record_type": "can_rx", "experiment_id": 42, "wall_time_ns": 150}
        end = {"record_type": "session_end", "experiment_id": 42, "wall_time_ns": 200}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            path.write_text("".join(json.dumps(item) + "\n" for item in (start, frame, end)), encoding="utf-8")
            quality = validate_capture_log(path, 42)
            self.assertEqual((quality["session_start_ns"], quality["session_end_ns"]), (100, 200))

            path.write_text("".join(json.dumps(item) + "\n" for item in (frame, start, end)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "out of order"):
                validate_capture_log(path, 42)

            reversed_end = {**end, "wall_time_ns": 90}
            path.write_text("".join(json.dumps(item) + "\n" for item in (start, frame, reversed_end)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "timestamps are out of order"):
                validate_capture_log(path, 42)

            late_frame = {**frame, "wall_time_ns": 250}
            path.write_text("".join(json.dumps(item) + "\n" for item in (start, late_frame, end)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside session markers"):
                validate_capture_log(path, 42)

            different_session = {**end, "session_id": "other"}
            path.write_text("".join(json.dumps(item) + "\n" for item in (
                {**start, "session_id": "original"}, frame, different_session
            )), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "session identifiers do not match"):
                validate_capture_log(path, 42)

    def test_mode_transition_before_mutation_is_preexisting(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        lines = regular("b_can", 0x2A0, "00" * 8, 10 * NS, 100, NS // 10)
        lines += regular("b_can", 0x2A0, "00" * 8, 20 * NS, 200, NS // 50)
        lines += regular("b_can", 0x2A0, "00" * 8, 24 * NS, 5, NS // 5)
        lines += regular("b_can", 0x2A0, "00" * 8, 25 * NS, 5, NS // 5)
        lines += regular("b_can", 0x2A0, "00" * 8, 26 * NS, 10, NS // 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"b_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, dbc_path=DBC, clock_offsets=CLOCKS,
            )
        self.assertFalse(any(item["target_id"] == "0x2A0" for item in result["anomalies"]))
        self.assertTrue(any(
            item["target_id"] == "0x2A0" and item["classification"] == "preexisting"
            for item in result["observations"]
        ))

    def test_prior_mutation_only_new_id_does_not_hide_repeat_candidate(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        emitted = regular("b_can", 0x456, "AA", 25 * NS, 3, NS // 5)
        emitted += regular("b_can", 0x456, "AA", 26 * NS, 3, NS // 5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "trial_0001"
            prior.mkdir()
            (prior / "metadata.json").write_text(json.dumps({
                "status": "completed", "source_bus": "B_CAN",
                "phase_times_ns": phases, "logs": {"b_can": "b_can.jsonl"},
            }), encoding="utf-8")
            write_frames(prior / "b_can.jsonl", emitted)
            current = root / "current.jsonl"
            write_frames(current, emitted)
            result = analyze_trial(
                rx_paths={"b_can": current}, phase_times_ns=phases,
                mutation=mutation(), thresholds={}, experiment_dir=root,
                current_trial_id=2, clock_offsets=CLOCKS,
            )
        found = next(item for item in result["anomalies"] if item["target_id"] == "0x456")
        self.assertEqual(found["type"], "NEW_MESSAGE")
        self.assertTrue(found["evidence"]["historically_seen"])

    def test_old_distributed_history_without_clock_reference_is_not_untreated_control(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        emitted = regular("p_can", 0x456, "AA", 25 * NS + 10_000_000, 3, NS // 5)
        emitted += regular("p_can", 0x456, "AA", 26 * NS, 3, NS // 5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "trial_0001"
            prior.mkdir()
            (prior / "metadata.json").write_text(json.dumps({
                "status": "completed", "execution_mode": "distributed_offline_previous_trial_feedback",
                "source_bus": "B_CAN", "phase_times_ns": phases,
                "logs": {"p_can": "p_can.jsonl"},
                "clock_offsets": {
                    "b_can": {"offset_ms": 0, "round_trip_ms": 1},
                    "p_can": {"offset_ms": 0, "round_trip_ms": 1},
                },
            }), encoding="utf-8")
            write_frames(prior / "p_can.jsonl", [record("p_can", 0x456, "AA", 11 * NS)])
            current = root / "current.jsonl"
            write_frames(current, emitted)
            result = analyze_trial(
                rx_paths={"p_can": current}, phase_times_ns=phases,
                mutation=mutation(), thresholds={}, experiment_dir=root,
                current_trial_id=2, clock_offsets=CLOCKS,
            )
        self.assertTrue(any(item["target_id"] == "0x456" and item["type"] == "NEW_MESSAGE"
                            for item in result["anomalies"]))
        self.assertEqual(result["summary"]["historical_control_trials"], 0)

    def test_capture_bus_label_mismatch_is_rejected(self) -> None:
        phases = {
            "baseline_start": NS, "baseline_end": 2 * NS,
            "mutation_start": 2 * NS, "mutation_end": 3 * NS,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, [record("i_can", 0x123, "AA", NS)])
            with self.assertRaisesRegex(ValueError, "CAN bus does not match"):
                analyze_trial(
                    rx_paths={"b_can": path}, phase_times_ns=phases,
                    mutation=mutation(), thresholds={},
                )

    def test_navigation_value_seen_before_baseline_is_background(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        prefix = "1AEA3D9A3A8E3C"
        lines = regular("i_can", 0x486, prefix + "E8", 9 * NS, 4, NS // 5)
        lines += regular("i_can", 0x486, prefix + "EA", 10 * NS, 10, NS // 5)
        lines += regular("i_can", 0x486, prefix + "EC", 12 * NS, 10, NS // 5)
        lines += regular("i_can", 0x486, prefix + "EE", 14 * NS, 10, NS // 5)
        lines += regular("i_can", 0x486, prefix + "EC", 20 * NS, 25, NS // 5)
        lines += regular("i_can", 0x486, prefix + "EC", 25 * NS, 3, NS // 5)
        lines += regular("i_can", 0x486, prefix + "E8", 25 * NS + 3 * NS // 5, 2, NS // 5)
        lines += regular("i_can", 0x486, prefix + "E8", 26 * NS, 10, NS // 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"i_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, dbc_path=DBC, clock_offsets=CLOCKS,
            )
        self.assertFalse(any(item["target_id"] == "0x486" for item in result["anomalies"]))
        background = next(item for item in result["observations"] if
                          item["target_id"] == "0x486" and item["evidence"].get("signal_name") == "NP_Sat")
        self.assertEqual(background["classification"], "background")
        self.assertIn(20, background["evidence"]["pre_injection_values"])
        self.assertTrue(background["evidence"]["pre_injection_exact_payload"])

    def test_known_historical_payload_can_still_be_a_transition_candidate(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        old = "0000E00100134ADE"
        new = "0000F00100134ADE"
        current = regular("p_can", 0x1F8, old, 10 * NS, 100, NS // 10)
        current += regular("p_can", 0x1F8, old, 20 * NS, 50, NS // 10)
        current += regular("p_can", 0x1F8, old, 25 * NS, 6, NS // 10)
        current += regular("p_can", 0x1F8, new, 25 * NS + 6 * NS // 10, 4, NS // 10)
        current += regular("p_can", 0x1F8, new, 26 * NS, 20, NS // 10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "trial_0001"
            prior.mkdir()
            (prior / "metadata.json").write_text(json.dumps({
                "status": "completed", "source_bus": "B_CAN",
                "phase_times_ns": phases, "logs": {"p_can": "p_can.jsonl"},
            }), encoding="utf-8")
            write_frames(prior / "p_can.jsonl", [record("p_can", 0x1F8, new, 11 * NS)])
            path = root / "current.jsonl"
            write_frames(path, current)
            result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, experiment_dir=root, current_trial_id=2,
                clock_offsets=CLOCKS,
            )
            noop = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, experiment_dir=root, current_trial_id=2,
                clock_offsets=CLOCKS, trial_kind="noop",
            )
            invalid = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, clock_offsets={
                    **CLOCKS, "p_can": {**CLOCKS["p_can"], "alignment_valid": False}
                },
            )
        candidate = next(item for item in result["anomalies"] if item["target_id"] == "0x1F8")
        self.assertEqual(candidate["classification"], "candidate")
        self.assertTrue(candidate["evidence"]["historically_seen_payload"])
        self.assertFalse(candidate["evidence"]["feedback_eligible"])
        self.assertEqual(candidate["evidence"]["persistence_frames"], 24)
        self.assertEqual(noop["summary"]["false_alert_count"], 1)
        self.assertFalse(noop["anomalies"][0]["evidence"]["verification_candidate"])
        self.assertFalse(invalid["anomalies"])
        self.assertTrue(any(item["classification"] == "inconclusive" for item in invalid["observations"]))

    def test_onset_near_mutation_end_with_clock_error_is_inconclusive(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        old, new = "0000E00100134ADE", "0000F00100134ADE"
        lines = regular("p_can", 0x1F8, old, 10 * NS, 100, NS // 10)
        lines += regular("p_can", 0x1F8, old, 20 * NS, 50, NS // 10)
        lines += regular("p_can", 0x1F8, old, 25 * NS, 8, NS // 10)
        lines += [record("p_can", 0x1F8, new, 25 * NS + offset * 1_000_000)
                  for offset in (850, 950)]
        lines += regular("p_can", 0x1F8, new, 26 * NS, 10, NS // 10)
        clocks = {bus: {**sample, "round_trip_ms": 200}
                  for bus, sample in CLOCKS.items()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"p_can": path}, phase_times_ns=phases,
                mutation=mutation(), thresholds={}, clock_offsets=clocks,
            )
        self.assertFalse(any(item["target_id"] == "0x1F8" and item["type"] == "PAYLOAD_CHANGE"
                             for item in result["anomalies"]))
        self.assertTrue(any(item["target_id"] == "0x1F8" and item["type"] == "PAYLOAD_CHANGE" and
                            item["evidence"].get("reason") == "onset_overlaps_mutation_boundary_uncertainty"
                            for item in result["observations"]))

    def test_stable_signal_and_rate_change_are_observed(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        fixed = "1AEA3D9A3A8E3CEC"
        changed = "1AEA3D9A3A8E3C2C"  # NP_Fix 3 -> 0; NP_Sat remains 22.
        lines = regular("b_can", 0x486, fixed, 10 * NS, 50, NS // 5)
        lines += regular("b_can", 0x486, fixed, 20 * NS, 25, NS // 5)
        lines += regular("b_can", 0x486, changed, 25 * NS, 5, NS // 5)
        lines += regular("b_can", 0x486, changed, 26 * NS, 10, NS // 5)
        lines += regular("b_can", 0x123, "AA", 10 * NS, 100, NS // 10)
        lines += regular("b_can", 0x123, "AA", 20 * NS, 50, NS // 10)
        lines += regular("b_can", 0x123, "AA", 25 * NS, 5, NS // 5)
        lines += regular("b_can", 0x123, "AA", 26 * NS, 20, NS // 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"b_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, dbc_path=DBC, clock_offsets=CLOCKS,
            )
        kinds = {(item["target_id"], item["type"]) for item in result["anomalies"]}
        self.assertIn(("0x486", "PAYLOAD_CHANGE"), kinds)
        self.assertIn(("0x123", "FREQUENCY_CHANGE"), kinds)
        self.assertTrue(all(not item["evidence"]["feedback_eligible"] for item in result["anomalies"]))

    def test_sparse_and_unsustained_burst_are_inconclusive(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
        }
        lines = [record("b_can", 0x321, "00", 20 * NS),
                 record("b_can", 0x321, "01", 25 * NS)]
        lines += regular("b_can", 0x17332811, "AA", 25 * NS + NS // 2, 3, NS // 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"b_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, clock_offsets=CLOCKS, trial_kind="calibration",
            )
        self.assertEqual(result["summary"]["candidate_count"], 0)
        self.assertEqual(result["summary"]["false_alert_count"], 0)
        self.assertEqual(result["summary"]["incomparable_count"], 2)
        self.assertTrue(any(item["target_id"] == "0x321" and
                            item["classification"] == "inconclusive" for item in result["observations"]))
        self.assertTrue(any(item["target_id"] == "0x17332811" and
                            item["classification"] == "inconclusive" for item in result["observations"]))

    def test_bursty_control_is_not_a_periodic_timing_candidate(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 24 * NS,
            "mutation_start": 24 * NS, "mutation_end": 26 * NS,
        }
        control_ms = (100, 180, 260, 340, 1950)
        mutation_ms = (100, 150, 200, 250, 300, 1950)
        lines = [record("b_can", 0x17331110, "AA", 22 * NS + value * 1_000_000)
                 for value in control_ms]
        lines += [record("b_can", 0x17331110, "AA", 24 * NS + value * 1_000_000)
                  for value in mutation_ms]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"b_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, clock_offsets=CLOCKS, trial_kind="calibration",
            )
        self.assertEqual(result["summary"]["candidate_count"], 0)
        timing = next(item for item in result["observations"] if item["type"] == "TIMING")
        self.assertEqual(timing["evidence"]["reason"], "bursty_control_not_periodic")

    def test_dbc_crc_and_counter_rotation_is_not_payload_candidate(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 25 * NS,
            "mutation_start": 25 * NS, "mutation_end": 26 * NS,
            "recovery_start": 26 * NS, "recovery_end": 28 * NS,
        }
        lines = []
        for start, count in ((10 * NS, 50), (20 * NS, 25), (25 * NS, 5), (26 * NS, 10)):
            for index in range(count):
                payload = bytes((index & 0xFF, index & 0x0F)) + bytes(6)
                lines.append(record("b_can", 0x2A0, payload.hex(), start + index * NS // 5))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"b_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, dbc_path=DBC, clock_offsets=CLOCKS,
            )
        self.assertFalse(any(item["target_id"] == "0x2A0" and item["type"] == "PAYLOAD_CHANGE"
                             for item in result["anomalies"]))

    def test_equal_frame_count_can_still_have_new_timing_jitter(self) -> None:
        phases = {
            "baseline_start": 1 * NS, "baseline_end": 1 * NS + NS // 2,
            "normal_start": 1 * NS + NS // 2, "normal_end": 2 * NS,
            "mutation_start": 2 * NS, "mutation_end": 2 * NS + NS // 2,
        }
        lines = regular("i_can", 0x123, "AA", 1 * NS, 5, 20_000_000)
        lines += regular("i_can", 0x123, "AA", 1 * NS + NS // 2, 5, 20_000_000)
        lines += [record("i_can", 0x123, "AA", 2 * NS + offset * 1_000_000)
                  for offset in (0, 10, 40, 50, 80)]
        clocks = {
            bus: {**sample, "round_trip_ms": 0.1}
            for bus, sample in CLOCKS.items()
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "i_can.jsonl"
            write_frames(path, lines)
            result = analyze_trial(
                rx_paths={"i_can": path}, phase_times_ns=phases, mutation=mutation(),
                thresholds={}, clock_offsets=clocks,
            )
        timing = next(item for item in result["anomalies"] if item["type"] == "TIMING")
        self.assertEqual(timing["classification"], "candidate")
        self.assertEqual(timing["evidence"]["control_count"], 5)
        self.assertEqual(timing["evidence"]["mutation_count"], 5)
        self.assertAlmostEqual(timing["evidence"]["mutation_stddev_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
