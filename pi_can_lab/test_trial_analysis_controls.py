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

    def test_multimodal_median_shift_needs_mean_rate_change(self) -> None:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 20 * NS,
            "normal_start": 20 * NS, "normal_end": 22 * NS,
            "mutation_start": 22 * NS, "mutation_end": 23 * NS,
        }

        def window(start_ns: int, gaps_ms: list[int]) -> list[str]:
            stamp = start_ns + 5_000_000
            stamps = [stamp]
            for gap in gaps_ms:
                stamp += gap * 1_000_000
                stamps.append(stamp)
            return [record("b_can", 0x3C1, "AA", value) for value in stamps]

        multimodal_control = [30] * 4 + [60] * 2 + [90] + [100] * 2 + [110] * 3 + [115]
        multimodal_mutation = [30] * 3 + [60] * 4 + [100] * 3 + [115] * 3
        cases = (
            ("phase_shift", multimodal_control, multimodal_mutation, False),
            ("rate_shift", [50] * 13, [75] * 13, True),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b_can.jsonl"
            for name, control_gaps, mutation_gaps, should_be_candidate in cases:
                with self.subTest(name=name):
                    write_frames(path, (
                        window(20 * NS, control_gaps)
                        + window(21 * NS, control_gaps)
                        + window(22 * NS, mutation_gaps)
                    ))
                    result = analyze_trial(
                        rx_paths={"b_can": path}, phase_times_ns=phases,
                        mutation=mutation(), thresholds={}, clock_offsets=CLOCKS,
                    )
                    candidates = [item for item in result["anomalies"]
                                  if item["target_id"] == "0x3C1" and item["type"] == "TIMING"]
                    self.assertEqual(bool(candidates), should_be_candidate)
                    if should_be_candidate:
                        self.assertGreaterEqual(
                            candidates[0]["evidence"]["mean_relative_change"], 0.25
                        )
                    else:
                        timing = next(item for item in result["observations"]
                                      if item["target_id"] == "0x3C1"
                                      and item["type"] == "TIMING")
                        self.assertEqual(timing["classification"], "inconclusive")
                        self.assertEqual(
                            timing["evidence"]["reason"],
                            "median_shift_without_mean_rate_change",
                        )
                        self.assertAlmostEqual(timing["evidence"]["control_mean_ms"], 75)
                        self.assertAlmostEqual(timing["evidence"]["mutation_mean_ms"], 75)


class LaterPersistentChangeTests(unittest.TestCase):
    """Use inert metadata and synthetic logs to test observation sequences."""

    def analyze_sequence(
        self, values: list[int], *, dbc: bool, recovery: list[int] | None = None,
        pre: list[int] | None = None, minimum_persistence: int = 3,
        clocks: dict | None = None, control_count: int = 10,
    ) -> dict:
        phases = {
            "baseline_start": 10 * NS, "baseline_end": 12 * NS,
            "normal_start": 12 * NS, "normal_end": 14 * NS,
            "mutation_start": 14 * NS, "mutation_end": 15 * NS,
            "recovery_start": 15 * NS, "recovery_end": 17 * NS,
        }
        pre = [0] * 40 if pre is None else pre
        recovery = [values[-1]] * 20 if recovery is None else recovery
        lines = []
        for start, samples in ((10 * NS, pre), (14 * NS, values), (15 * NS, recovery)):
            for index, value in enumerate(samples):
                if start == 10 * NS and 30 + control_count <= index:
                    continue
                lines.append(record("i_can", 0x510, f"{value:02X}",
                                    start + 20_000_000 + index * NS // 10))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "i_can.jsonl"
            write_frames(path, lines)
            dbc_path = root / "toy.dbc"
            dbc_path.write_text(
                'BO_ 1296 ToyState: 1 Toy\n SG_ State : 0|8@1+ (1,0) [0|255] "" Toy\n',
                encoding="ascii",
            )
            return analyze_trial(
                rx_paths={"i_can": path}, phase_times_ns=phases,
                mutation=MutationCase(
                    mutation_id=1, source_bus="b_can", can_id=0x700,
                    operator="OFFLINE_CALIBRATION", original_payload=b"\x00",
                    mutated_payload=b"\x00", random_seed=1, trial_kind="noop",
                ),
                thresholds={"minimum_persistence_frames": minimum_persistence,
                            "comparison_window_seconds": 1},
                dbc_path=dbc_path if dbc else None,
                clock_offsets=CLOCKS if clocks is None else clocks,
                trial_kind="calibration",
            )

    @staticmethod
    def payload_candidates(result: dict) -> list[dict]:
        return [row for row in result["anomalies"] if row["type"] == "PAYLOAD_CHANGE"]

    def test_glitch_does_not_hide_later_different_or_same_value(self) -> None:
        for dbc in (False, True):
            for changed in (1, 2):
                with self.subTest(dbc=dbc, changed=changed):
                    result = self.analyze_sequence([0, 1, 0] + [changed] * 7, dbc=dbc)
                    candidates = self.payload_candidates(result)
                    self.assertEqual(len(candidates), 1)
                    evidence = candidates[0]["evidence"]
                    self.assertEqual(evidence["onset_ms_from_mutation"], 320)
                    self.assertEqual(evidence["persistence_frames"], 27)
                    self.assertFalse(evidence["feedback_eligible"])
                    self.assertFalse(evidence["verification_candidate"])
                    if dbc:
                        self.assertEqual(evidence["observed_value"], changed)
                    else:
                        self.assertEqual(evidence["changed_bits"], [changed - 1])
                    self.assertTrue(any(
                        row["type"] == "PAYLOAD_CHANGE"
                        and row["classification"] == "inconclusive"
                        and row["evidence"]["onset_ms_from_mutation"] == 120
                        and row["evidence"]["persistence_frames"] == 1
                        for row in result["observations"]
                    ))

    def test_multiple_persistent_runs_keep_their_own_onsets(self) -> None:
        for dbc in (False, True):
            for later in (1, 2):
                with self.subTest(dbc=dbc, later=later):
                    result = self.analyze_sequence(
                        [0, 1, 1, 1, 0, later, later, later, 0, 0], dbc=dbc,
                    )
                    candidates = self.payload_candidates(result)
                    self.assertEqual([row["evidence"]["onset_ms_from_mutation"]
                                      for row in candidates], [120, 520])
                    self.assertEqual([row["evidence"]["persistence_frames"]
                                      for row in candidates], [3, 3])

    def test_separated_glitches_do_not_accumulate_persistence(self) -> None:
        for dbc in (False, True):
            with self.subTest(dbc=dbc):
                result = self.analyze_sequence([0, 1, 0, 1, 0, 1, 0, 1, 0, 0], dbc=dbc)
                self.assertFalse(self.payload_candidates(result))

    def test_configured_persistence_applies_to_later_runs(self) -> None:
        for dbc in (False, True):
            with self.subTest(dbc=dbc):
                result = self.analyze_sequence(
                    [0, 1, 1, 1, 0, 2, 2, 2, 2, 0], dbc=dbc, minimum_persistence=4,
                )
                candidates = self.payload_candidates(result)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["evidence"]["onset_ms_from_mutation"], 520)
                self.assertEqual(candidates[0]["evidence"]["persistence_frames"], 4)
                first = next(row for row in result["observations"] if row["type"] == "PAYLOAD_CHANGE")
                self.assertEqual(first["classification"], "inconclusive")
                if not dbc:
                    self.assertEqual(first["evidence"]["reason"], "short")

    def test_earlier_glitch_cannot_supply_a_later_runs_window_support(self) -> None:
        for dbc in (False, True):
            with self.subTest(dbc=dbc):
                result = self.analyze_sequence([0, 1] + [0] * 7 + [1], dbc=dbc)
                self.assertFalse(self.payload_candidates(result))
                later = next(row for row in result["observations"]
                             if row["type"] == "PAYLOAD_CHANGE"
                             and row["evidence"]["onset_ms_from_mutation"] == 920)
                self.assertEqual(later["classification"], "inconclusive")
                self.assertEqual(later["evidence"]["mutation_support_frames"], 1)
                self.assertEqual(later["evidence"]["persistence_frames"], 21)

    def test_recovery_only_change_is_not_a_candidate(self) -> None:
        for dbc in (False, True):
            with self.subTest(dbc=dbc):
                result = self.analyze_sequence([0, 1] + [0] * 8, dbc=dbc, recovery=[2] * 20)
                self.assertFalse(self.payload_candidates(result))

    def test_later_run_still_needs_control_recovery_and_valid_clock(self) -> None:
        invalid_clocks = {**CLOCKS, "i_can": {**CLOCKS["i_can"], "alignment_valid": False}}
        for dbc in (False, True):
            for guard in ({"control_count": 2}, {"recovery": [2, 2]}, {"clocks": invalid_clocks}):
                with self.subTest(dbc=dbc, guard=guard):
                    result = self.analyze_sequence([0, 1, 0] + [2] * 7, dbc=dbc, **guard)
                    self.assertFalse(self.payload_candidates(result))
                    self.assertTrue(any(
                        row["type"] == "PAYLOAD_CHANGE"
                        and row["classification"] == "inconclusive"
                        and row["evidence"]["onset_ms_from_mutation"] == 320
                        for row in result["observations"]
                    ))

    def test_later_onset_uses_its_own_clock_boundary_check(self) -> None:
        clocks = {bus: {**sample, "round_trip_ms": 200} for bus, sample in CLOCKS.items()}
        for dbc in (False, True):
            with self.subTest(dbc=dbc):
                # First persistent run starts inside clock uncertainty; second is well inside the window.
                result = self.analyze_sequence([1, 1, 1, 0] + [2] * 6, dbc=dbc, clocks=clocks)
                candidates = self.payload_candidates(result)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["evidence"]["onset_ms_from_mutation"], 420)
                self.assertTrue(any(
                    row["evidence"].get("reason") == "onset_overlaps_mutation_boundary_uncertainty"
                    for row in result["observations"]
                ))
                near_end = self.analyze_sequence([0, 1] + [0] * 6 + [2, 2], dbc=dbc, clocks=clocks)
                self.assertFalse(self.payload_candidates(near_end))
                self.assertTrue(any(
                    row["evidence"].get("onset_ms_from_mutation") == 820
                    and row["evidence"].get("reason") == "onset_overlaps_mutation_boundary_uncertainty"
                    for row in near_end["observations"]
                ))

    def test_known_state_does_not_hide_later_novel_signal_state(self) -> None:
        result = self.analyze_sequence(
            [1, 1, 1, 0] + [4] * 6, dbc=True, pre=[1] * 10 + [0] * 30,
        )
        candidates = self.payload_candidates(result)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["evidence"]["observed_value"], 4)
        self.assertTrue(any(row["classification"] == "background"
                            and row["evidence"].get("observed_value") == 1
                            for row in result["observations"]))


if __name__ == "__main__":
    unittest.main()
