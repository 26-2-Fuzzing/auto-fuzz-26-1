from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pair_analysis import (
    PAIR_RAW_STEP_TOLERANCE, _candidate_recovery_checks, _compare_windows,
    _paired_background_envelopes,
    _paired_background_trends, _paired_step_tolerances,
    _paired_tolerances_report, _within_trial_envelopes,
    analyze_trial_pair, recovery_returned_to_prestate,
)
from trial_analysis import SignalDefinition
from trial_models import MutationCase


NS = 1_000_000_000
ORIGINAL = bytes.fromhex("00000000200000F0")
CHANGED = bytes.fromhex("0000000020000018")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def _phases(offset: int) -> dict[str, int]:
    return {
        "baseline_start": offset, "baseline_end": offset + 30 * NS,
        "normal_start": offset + 30 * NS, "normal_end": offset + 40 * NS,
        "mutation_start": offset + 40 * NS, "mutation_end": offset + 41 * NS,
        "recovery_start": offset + 41 * NS, "recovery_end": offset + 61 * NS,
    }


def _capture(phases: dict[str, int], bus: str, *, recovery_hz: int = 20,
             normal_hz: int = 20) -> list[dict]:
    start = phases["baseline_start"]
    session = "rx-session"
    common = {"bus": bus, "experiment_id": 1, "session_id": session}
    records = [{"record_type": "session_start", "wall_time_ns": start - NS, **common}]
    for phase, hz in (("baseline", 20), ("normal", normal_hz), ("recovery", recovery_hz)):
        window_start = phases[f"{phase}_end"] - 5 * NS
        for index in range(5 * hz):
            records.append({
                "record_type": "can_rx", "wall_time_ns": window_start + (index * NS // hz) + 1,
                "arbitration_id": 0x2A0, "is_extended_id": False,
                "data_hex": "00", "is_error_frame": False, "is_remote_frame": False,
                **common,
            })
        for index in range(50):
            records.append({
                "record_type": "can_rx", "wall_time_ns": window_start + index * 100_000_000 + 3,
                "arbitration_id": 0x1F8, "data_hex": "00",
                "is_extended_id": False, "is_error_frame": False,
                "is_remote_frame": False, **common,
            })
        for second in range(5):
            records.append({
                "record_type": "can_rx", "wall_time_ns": window_start + second * NS + 2,
                "arbitration_id": 0x366, "data_hex": ORIGINAL.hex().upper(),
                "is_extended_id": False, "is_error_frame": False,
                "is_remote_frame": False, **common,
            })
    records.append({"record_type": "session_end",
                    "wall_time_ns": phases["recovery_end"] + NS, **common})
    return [records[0], *sorted(records[1:-1], key=lambda item: item["wall_time_ns"]), records[-1]]


def _tx(phases: dict[str, int], kind: str, payload: bytes, trial_id: int) -> list[dict]:
    session = f"{kind}-tx"
    common = {"experiment_id": 1, "trial_kind": kind,
              "tx_session_id": session, "execute": True}
    records = [{
        "record_type": "tx_session_start", "trial_contract_version": 1,
        "transmission": {"interval_ms": 50},
        "campaign": {"enabled": True, "normal_data_hex": ORIGINAL.hex().upper()},
        "mutation": {"control_noop": kind == "noop", "trial_mutation_id": trial_id},
        **common,
    }]
    for phase in ("baseline", "normal", "mutation", "recovery"):
        records.append({"record_type": "tx_phase", "phase": phase, "event": "start",
                        "wall_time_ns": phases[f"{phase}_start"], **common})
        if phase in {"normal", "mutation"}:
            amount = 200 if phase == "normal" else 20
            data = ORIGINAL if phase == "normal" else payload
            for index in range(amount):
                records.append({
                    "record_type": "can_tx", "phase": phase, "status": "sent",
                    "wall_time_ns": phases[f"{phase}_start"] + index * 50_000_000,
                    "arbitration_id": 0x366, "data_hex": data.hex().upper(), **common,
                })
        if phase == "recovery":
            records.append({
                "record_type": "can_tx", "phase": "recovery", "status": "sent",
                "wall_time_ns": phases["recovery_start"] + 1_000_000,
                "arbitration_id": 0x366, "data_hex": ORIGINAL.hex().upper(),
                "kind": "restore", **common,
            })
        records.append({"record_type": "tx_phase", "phase": phase, "event": "end",
                        "wall_time_ns": phases[f"{phase}_end"], **common})
    records.append({
        "record_type": "tx_session_end", "trial_contract_version": 1,
        "status": "completed", "phase_sent": {"normal": 200, "mutation": 20},
        "restore": {"status": "sent", "sent": 1}, **common,
    })
    return records


def _episode(root: Path, trial_id: int, kind: str, position: int, *,
             recovery_hz: int = 20, normal_hz: int = 20,
             candidate: bool = False) -> Path:
    path = root / f"trial_{trial_id:04d}"
    path.mkdir()
    phases = _phases(trial_id * 100 * NS)
    payload = ORIGINAL if kind == "noop" else CHANGED
    mutation = MutationCase(
        mutation_id=trial_id, source_bus="b_can", can_id=0x366,
        operator="TEST", original_payload=ORIGINAL, mutated_payload=payload,
        random_seed=trial_id, trial_kind=kind,
    )
    _write_json(path / "mutation.json", mutation.to_dict())
    order = ["mutation", "noop"]
    _write_json(path / "metadata.json", {
        "status": "completed", "experiment_id": 1, "trial_id": trial_id,
        "trial_kind": kind, "pair_id": "pair_0001", "pair_role": kind,
        "pair_position": position, "pair_order": order,
        "source_bus": "B_CAN", "target_id": "0x366", "phase_times_ns": phases,
        "logs": {bus: f"{bus}.jsonl" for bus in ("p_can", "b_can", "i_can")},
        "clock_offsets": {bus: {"reference_id": "shared-controller",
                                "alignment_valid": True, "offset_ms": 0,
                                "uncertainty_ms": 1}
                          for bus in ("p_can", "b_can", "i_can")},
        "collection_config": {
            "baseline_seconds": 30, "normal_seconds": 10,
            "mutation_seconds": 1, "post_seconds": 20, "interval_ms": 50,
        },
    })
    for bus in ("p_can", "b_can", "i_can"):
        _write_jsonl(path / f"{bus}.jsonl", _capture(
            phases, bus, recovery_hz=recovery_hz, normal_hz=normal_hz,
        ))
    _write_jsonl(path / "tx.jsonl", _tx(phases, kind, payload, trial_id))
    anomalies = []
    if candidate:
        anomalies.append({
            "target_bus": "B_CAN", "target_id": "0x1F8", "type": "PAYLOAD_CHANGE",
            "classification": "candidate", "score": .8,
            "evidence": {"signal_name": "Status", "observed_value": 1,
                         "feedback_eligible": False, "recovery_state": "restored"},
        })
    _write_json(path / "anomalies.json", {
        "schema_version": 3, "trial_id": trial_id, "anomalies": anomalies,
    })
    return path


class PairAnalysisTests(unittest.TestCase):
    def test_multimodal_timing_median_shift_does_not_fail_recovery(self) -> None:
        def timing_trial(normal_gaps, recovery_gaps):
            def frames(start, gaps):
                timestamps = [start]
                for gap in gaps:
                    timestamps.append(timestamps[-1] + gap * 1_000_000)
                return [{"id": 0x3C1, "time_ns": timestamp, "payload": b"\0"}
                        for timestamp in timestamps]

            return {
                "phases": {"normal_start": 0, "normal_end": 10 * NS,
                           "recovery_start": 11 * NS, "recovery_end": 31 * NS},
                "frames": {"i_can": frames(5 * NS, normal_gaps)
                           + frames(26 * NS, recovery_gaps)},
                "metadata": {"dbc_path": None},
                "analysis": {"anomalies": [{
                    "classification": "candidate", "target_bus": "I_CAN",
                    "target_id": "0x3C1", "type": "TIMING",
                }]},
            }

        normal = [30] * 3 + [60] * 3 + [90] * 4 + [120] * 3
        shifted_median = [30] * 3 + [60] * 4 + [90] * 3 + [120] * 3
        check = _candidate_recovery_checks(timing_trial(normal, shifted_median))[0]
        self.assertEqual(check["status"], "restored")
        self.assertGreater(abs(check["normal_median_ms"] - check["recovery_median_ms"]), 20)
        self.assertLess(abs(check["normal_mean_ms"] - check["recovery_mean_ms"]), 3)
        self.assertEqual(_candidate_recovery_checks(timing_trial(
            [90] * 13, [60] * 13,
        ))[0]["status"], "changed")
        self.assertEqual(_candidate_recovery_checks(timing_trial(
            [75] * 13, [50, 100] * 6 + [50],
        ))[0]["status"], "changed")

    def test_pre_exposure_envelope_accepts_supported_values_only(self) -> None:
        signal = SignalDefinition("MO_Mom_Begr_dyn", 0, 16, 1, False)
        layouts = {0xA8: (signal,)}

        def frames(values, offset=0):
            return [{"id": 0xA8, "extended": False,
                     "time_ns": offset + index * 500_000_000,
                     "payload": value.to_bytes(2, "little")}
                    for index, value in enumerate(values)]

        def episode(values, offset=0):
            return {"phases": {"baseline_start": offset, "normal_end": offset + 40 * NS},
                    "mutation": MutationCase(
                        mutation_id=1, source_bus="b_can", can_id=0x366,
                        operator="TEST", original_payload=ORIGINAL,
                        mutated_payload=CHANGED, random_seed=1,
                    ), "frames": {"b_can": frames(values, offset)}}

        mutation = episode([665] * 30 + [664] * 20 + [665] * 30)
        noop = episode([664] * 30 + [665] * 20 + [664] * 30, 100 * NS)
        within = _within_trial_envelopes(noop, layouts)
        paired = _paired_background_envelopes(mutation, noop, layouts)
        before, after = {"b_can": frames([665] * 10)}, {"b_can": frames([664] * 10)}
        self.assertEqual(_compare_windows(
            before, after, 0x366, layouts, "paired baseline",
        )["status"], "inconclusive")
        accepted = _compare_windows(
            before, after, 0x366, layouts, "paired baseline",
            background_envelopes=paired,
        )
        self.assertEqual(accepted["status"], "stable")
        self.assertEqual(accepted["markers"][0]["background_signal_envelope"][0]
                         ["pre_exposure_evidence"]["noop"]["supported_values"], [664, 665])
        self.assertEqual(_compare_windows(
            after, before, 0x366, layouts, "normal-to-recovery",
            background_envelopes=within,
        )["status"], "stable")
        self.assertEqual(_compare_windows(
            after, {"b_can": frames([663] * 10)}, 0x366, layouts,
            "normal-to-recovery", background_envelopes=within,
        )["status"], "inconclusive")
        # A rare one-frame value does not become part of the accepted envelope.
        noisy = episode([664] * 35 + [665] + [664] * 44, 100 * NS)
        self.assertFalse(_paired_background_envelopes(mutation, noisy, layouts))

    def test_pre_exposure_fuel_motion_is_scoped_to_paired_state(self) -> None:
        signal = SignalDefinition("KBI_Tankinhalt_hochaufl", 0, 16, 1, False)
        layouts = {0x6B8: (signal,)}

        def frames(values, offset=0):
            return [{"id": 0x6B8, "extended": False,
                     "time_ns": offset + index * 500_000_000,
                     "payload": value.to_bytes(2, "little")}
                    for index, value in enumerate(values)]

        def episode(values, offset=0):
            return {"phases": {"baseline_start": offset, "normal_end": offset + 40 * NS},
                    "mutation": MutationCase(
                        mutation_id=1, source_bus="b_can", can_id=0x366,
                        operator="TEST", original_payload=ORIGINAL,
                        mutated_payload=CHANGED, random_seed=1,
                    ), "frames": {"b_can": frames(values, offset)}}

        mutation = episode([1479] * 30 + [1478] * 50)
        noop = episode([1466] * 70 + [1465] * 10, 476 * NS)
        trends = _paired_background_trends(mutation, noop, layouts)
        before, after = {"b_can": frames([1478] * 10)}, {"b_can": frames([1466] * 10)}
        paired = _compare_windows(
            before, after, 0x366, layouts, "paired baseline", background_trends=trends,
        )
        self.assertEqual(paired["status"], "stable")
        self.assertEqual(paired["markers"][0]["background_signal_drift"][0]
                         ["pre_exposure_evidence"]["mutation"]["direction"], "down")
        self.assertLessEqual(trends[("b_can", 0x6B8, signal.name)]["observed_shift_raw"],
                             trends[("b_can", 0x6B8, signal.name)]["max_shift_raw"])
        self.assertIn(("b_can", 0x6B8, signal.name),
                      _paired_background_trends(noop, mutation, layouts))
        short_but_sustained = episode([1466] * 71 + [1465] * 9, 476 * NS)
        self.assertIn(("b_can", 0x6B8, signal.name),
                      _paired_background_trends(mutation, short_but_sustained, layouts))
        one_off = episode([1466] * 76 + [1465] * 4, 476 * NS)
        self.assertFalse(_paired_background_trends(mutation, one_off, layouts))
        # A change first seen after exposure supplies no pre-exposure trend.
        no_trend = _paired_background_trends(
            mutation, episode([1466] * 80, 476 * NS), layouts,
        )
        self.assertFalse(no_trend)
        self.assertEqual(_compare_windows(
            before, after, 0x366, layouts, "paired baseline",
            background_trends=no_trend,
        )["status"], "inconclusive")
        # A much larger state shift is not explained by the measured drift rate.
        self.assertFalse(_paired_background_trends(
            mutation, episode([1370] * 70 + [1369] * 10, 476 * NS), layouts,
        ))

    def test_climate_one_step_tolerance_applies_only_between_episodes(self) -> None:
        signal = SignalDefinition("KL_Anf_KL", 0, 8, 1, False)
        layouts = {0x3B5: (signal,)}

        def window(value):
            return {"b_can": [{"id": 0x3B5, "extended": False,
                               "time_ns": index * 500_000_000,
                               "payload": bytes([value])} for index in range(10)]}

        before = window(113)
        self.assertEqual(_compare_windows(
            before, window(114), 0x366, layouts, "recovery",
        )["status"], "inconclusive")
        allowed = _compare_windows(
            before, window(114), 0x366, layouts, "paired baseline",
            extra_step_tolerance=PAIR_RAW_STEP_TOLERANCE,
        )
        self.assertEqual(allowed["status"], "stable")
        self.assertEqual(allowed["markers"][0]["tolerated_signal_drift"][0]["after"], 114)
        self.assertEqual(_compare_windows(
            before, window(115), 0x366, layouts, "paired baseline",
            extra_step_tolerance=PAIR_RAW_STEP_TOLERANCE,
        )["status"], "inconclusive")

    def test_pair_tolerances_apply_to_named_signals_and_log_excess(self) -> None:
        cases = (
            (0x6B8, "KBI_Tankinhalt_hochaufl", 1404, 2),
            (0x3B5, "KL_Anf_KL", 113, 1),
            (0x6B0, "FS_Taupunkt", 445, 4),
            (0x6B0, "FS_Luftfeuchte_rel", 77, 2),
            (0xA8, "MO_Mom_Begr_dyn", 665, 1),
            (0x154, "MO_Mom_Begr_Schalt", 665, 1),
        )

        def window(can_id, value):
            return {"b_can": [{"id": can_id, "extended": False,
                               "time_ns": index * 500_000_000,
                               "payload": value.to_bytes(2, "little")}
                              for index in range(10)]}

        for can_id, name, before_value, limit in cases:
            with self.subTest(signal=name):
                signal = SignalDefinition(name, 0, 16, 1, False)
                layouts = {can_id: (signal,)}
                key = "b_can", can_id, name
                before = window(can_id, before_value)
                within = _compare_windows(
                    before, window(can_id, before_value - limit), 0x366,
                    layouts, "paired baseline", extra_step_tolerance=PAIR_RAW_STEP_TOLERANCE,
                )
                self.assertEqual(within["status"], "stable")
                self.assertEqual(within["markers"][0]["tolerated_signal_drift"][0]
                                 ["tolerance_raw"], limit)
                excess = _compare_windows(
                    before, window(can_id, before_value - limit - 1), 0x366,
                    layouts, "paired baseline", extra_step_tolerance=PAIR_RAW_STEP_TOLERANCE,
                    background_trends={key: {"direction": "down"}},
                    background_envelopes={key: {
                        "before_values": [before_value],
                        "after_values": [before_value - limit - 1],
                        "max_shift_raw": limit + 1,
                        "pre_exposure_evidence": {},
                    }},
                )
                self.assertEqual(excess["status"], "inconclusive")
                change = excess["markers"][0]["signal_changes"][0]
                self.assertEqual(change["delta_raw"], limit + 1)
                self.assertEqual(change["tolerance_raw"], limit)
                self.assertIn(f"{limit + 1} raw > tolerance {limit} raw",
                              excess["reasons"][0])

        # A named signal does not make its whole CAN ID eligible for drift.
        unrelated = SignalDefinition("OTHER", 0, 16, 1, False)
        self.assertEqual(_compare_windows(
            window(0x6B8, 1404), window(0x6B8, 1403), 0x366,
            {0x6B8: (unrelated,)}, "paired baseline",
            extra_step_tolerance=PAIR_RAW_STEP_TOLERANCE,
        )["status"], "inconclusive")

    def test_fuel_pair_tolerance_scales_with_elapsed_time_and_is_capped(self) -> None:
        fuel = (0x6B8, "KBI_Tankinhalt_hochaufl")
        self.assertEqual(_paired_step_tolerances(0)[fuel], 2)
        self.assertEqual(_paired_step_tolerances(93)[fuel], 6)
        self.assertEqual(_paired_step_tolerances(476)[fuel], 18)
        self.assertEqual(_paired_step_tolerances(3600)[fuel], 20)
        summary = next(row for row in _paired_tolerances_report(
            _paired_step_tolerances(476), 476,
        ) if row["signal"] == fuel[1])
        self.assertEqual(summary["tolerance_physical"], 0.18)
        self.assertEqual(summary["unit"], "L")
        self.assertEqual(summary["elapsed_fuel_allowance"]["modeled_rate_l_per_hour"], 1.2)

        signal = SignalDefinition(fuel[1], 0, 16, 1, False)

        def window(value):
            return {"b_can": [{"id": fuel[0], "extended": False,
                               "time_ns": index * 500_000_000,
                               "payload": value.to_bytes(2, "little")}
                              for index in range(10)]}

        before = window(1478)
        for elapsed, after, expected in ((93, 1476, "stable"),
                                         (476, 1465, "stable"),
                                         (476, 1459, "inconclusive")):
            with self.subTest(elapsed=elapsed, after=after):
                comparison = _compare_windows(
                    before, window(after), 0x366, {fuel[0]: (signal,)},
                    "paired baseline", extra_step_tolerance=_paired_step_tolerances(elapsed),
                )
                self.assertEqual(comparison["status"], expected)
                if expected == "inconclusive":
                    change = comparison["markers"][0]["signal_changes"][0]
                    self.assertEqual(change["delta_physical"], 0.19)
                    self.assertEqual(change["tolerance_physical"], 0.18)

    def test_one_raw_fuel_step_is_tolerated_but_larger_change_stops_pair(self) -> None:
        signal = SignalDefinition("KBI_Tankinhalt_hochaufl", 0, 16, 1, False)

        def window(value):
            return [{"id": 0x6B8, "extended": False,
                     "time_ns": index * 500_000_000,
                     "payload": value.to_bytes(2, "little")}
                    for index in range(10)]

        before = {"b_can": window(1478)}
        one_step = _compare_windows(
            before, {"b_can": window(1477)}, 0x366, {0x6B8: (signal,)}, "recovery"
        )
        two_steps = _compare_windows(
            before, {"b_can": window(1476)}, 0x366, {0x6B8: (signal,)}, "recovery"
        )
        self.assertEqual(one_step["status"], "stable")
        self.assertEqual(one_step["markers"][0]["tolerated_signal_drift"][0]["after"], 1477)
        self.assertEqual(two_steps["status"], "inconclusive")

    def test_recovery_gate_and_mutation_only_are_observational(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _episode(root, 1, "mutation", 1, candidate=True)
            _episode(root, 2, "noop", 2)
            gate = recovery_returned_to_prestate(root, 1)
            self.assertEqual(gate["status"], "stable")
            report = analyze_trial_pair(root, 1, 2, pair_id="pair_0001")
        self.assertEqual(report["comparability"]["status"], "comparable")
        self.assertEqual(len(report["event_comparison"]["mutation_only"]), 1)
        self.assertEqual(report["event_comparison"]["mutation_only"][0]["verification_status"],
                         "unverified")
        self.assertFalse(report["feedback_eligible"])

    def test_shared_noop_event_is_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _episode(root, 1, "mutation", 1, candidate=True)
            _episode(root, 2, "noop", 2, candidate=True)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(len(report["event_comparison"]["noop_shared"]), 1)
        self.assertEqual(report["verification_status"], "unverified")
        self.assertFalse(report["event_comparison"]["feedback_eligible"])

    def test_unrestored_0x2a0_rate_blocks_second_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1, recovery_hz=5, candidate=True)
            _episode(root, 2, "noop", 2)
            gate = recovery_returned_to_prestate(first)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(gate["status"], "inconclusive")
        self.assertTrue(any("0x2A0 rate/mode" in reason for reason in gate["reasons"]))
        self.assertEqual(report["comparability"]["status"], "inconclusive")
        self.assertFalse(report["event_comparison"]["mutation_only"])

    def test_second_normal_state_mismatch_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _episode(root, 1, "mutation", 1)
            _episode(root, 2, "noop", 2, normal_hz=5)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(report["comparability"]["status"], "inconclusive")
        self.assertTrue(any("paired normal" in reason for reason in report["comparability"]["reasons"]))

    def test_partial_tx_and_missing_rx_end_are_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _episode(root, 1, "mutation", 1)
            second = _episode(root, 2, "noop", 2)
            tx = [json.loads(line) for line in (second / "tx.jsonl").read_text().splitlines()]
            tx[-1]["status"] = "interrupted"
            _write_jsonl(second / "tx.jsonl", tx)
            rx = [json.loads(line) for line in (second / "b_can.jsonl").read_text().splitlines()]
            _write_jsonl(second / "b_can.jsonl", rx[:-1])
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(report["comparability"]["status"], "inconclusive")
        self.assertTrue(any("TX session did not complete" in reason for reason in report["comparability"]["reasons"]))
        self.assertTrue(any("invalid receiver capture" in reason for reason in report["comparability"]["reasons"]))

    def test_temporal_payload_sequence_with_matched_noop_cadence_is_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1)
            second = _episode(root, 2, "noop", 2)
            mutation_path = first / "mutation.json"
            data = json.loads(mutation_path.read_text())
            alternate = bytes.fromhex("0000000020000038").hex().upper()
            data["parameters"] = {"sequence": {
                "frames": [CHANGED.hex().upper(), alternate], "interval_ms": 100,
            }}
            _write_json(mutation_path, data)
            noop_path = second / "mutation.json"
            noop_data = json.loads(noop_path.read_text())
            noop_data["parameters"] = {"sequence": {
                "frames": [ORIGINAL.hex().upper(), ORIGINAL.hex().upper()],
                "interval_ms": 100,
            }}
            _write_json(noop_path, noop_data)
            for path, allowed in ((first, [CHANGED.hex().upper(), alternate]),
                                  (second, [ORIGINAL.hex().upper(), ORIGINAL.hex().upper()])):
                tx_path = path / "tx.jsonl"
                tx = [json.loads(line) for line in tx_path.read_text().splitlines()]
                changed = []
                index = 0
                for record in tx:
                    if record.get("record_type") == "can_tx" and record.get("phase") == "mutation":
                        if index % 2:
                            index += 1
                            continue
                        record["data_hex"] = allowed[(index // 2) % len(allowed)]
                        index += 1
                    elif record.get("record_type") == "tx_session_end":
                        record["phase_sent"]["mutation"] = 10
                    changed.append(record)
                _write_jsonl(tx_path, changed)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(report["comparability"]["status"], "comparable")

    def test_missing_required_bus_and_foreign_pair_id_are_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _episode(root, 1, "mutation", 1)
            second = _episode(root, 2, "noop", 2)
            metadata_path = second / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            del metadata["logs"]["p_can"]
            metadata["pair_id"] = "another_pair"
            _write_json(metadata_path, metadata)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(report["comparability"]["status"], "inconclusive")
        self.assertTrue(any("p_can: required receiver capture" in reason
                            for reason in report["comparability"]["reasons"]))
        self.assertTrue(any("matching recorded pair ID" in reason
                            for reason in report["comparability"]["reasons"]))

    def test_bursty_unrelated_id_and_nested_changed_bits_do_not_break_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1, candidate=True)
            _episode(root, 2, "noop", 2)
            analysis_path = first / "anomalies.json"
            analysis = json.loads(analysis_path.read_text())
            analysis["anomalies"][0]["evidence"] = {
                "signal_name": None, "changed_bits": [[0, 1], [1, 2]],
                "feedback_eligible": False, "recovery_state": "restored",
            }
            _write_json(analysis_path, analysis)
            for trial in (first, root / "trial_0002"):
                path = trial / "b_can.jsonl"
                records = [json.loads(line) for line in path.read_text().splitlines()]
                start = json.loads((trial / "metadata.json").read_text())[
                    "phase_times_ns"]["normal_end"] - 5 * NS
                burst = [{
                    "record_type": "can_rx", "bus": "b_can", "experiment_id": 1,
                    "session_id": "rx-session", "wall_time_ns": start + index * 1_000_000,
                    "arbitration_id": 0x555, "data_hex": "00",
                } for index in range(20)]
                middle = sorted([*records[1:-1], *burst], key=lambda row: row["wall_time_ns"])
                _write_jsonl(path, [records[0], *middle, records[-1]])
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(report["comparability"]["status"], "comparable")
        self.assertEqual(len(report["event_comparison"]["mutation_only"]), 1)

    def test_source_original_drift_and_persistent_candidate_block_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1, candidate=True)
            _episode(root, 2, "noop", 2)
            capture_path = first / "b_can.jsonl"
            capture = [json.loads(line) for line in capture_path.read_text().splitlines()]
            phases = json.loads((first / "metadata.json").read_text())["phase_times_ns"]
            for record in capture:
                if (record.get("record_type") == "can_rx"
                        and record.get("arbitration_id") == 0x366
                        and phases["normal_end"] - 5 * NS <= record["wall_time_ns"] < phases["normal_end"]):
                    record["data_hex"] = CHANGED.hex().upper()
                    break
            for record in capture:
                if (record.get("record_type") == "can_rx"
                        and record.get("arbitration_id") == 0x1F8
                        and phases["recovery_end"] - 5 * NS <= record["wall_time_ns"] < phases["recovery_end"]):
                    record["data_hex"] = "01"
            _write_jsonl(capture_path, capture)
            analysis_path = first / "anomalies.json"
            analysis = json.loads(analysis_path.read_text())
            analysis["anomalies"][0]["evidence"]["recovery_state"] = "persistent"
            _write_json(analysis_path, analysis)
            gate = recovery_returned_to_prestate(root, 1)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(gate["status"], "inconclusive")
        self.assertTrue(any("source target payload drifted" in reason for reason in gate["reasons"]))
        self.assertTrue(any("late recovery changed" in reason for reason in gate["reasons"]))
        self.assertEqual(report["comparability"]["status"], "inconclusive")

    def test_distributed_without_source_rx_can_gate_recovery_but_not_pair_contrast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1, candidate=True)
            second = _episode(root, 2, "noop", 2)
            analysis_path = first / "anomalies.json"
            analysis = json.loads(analysis_path.read_text())
            analysis["anomalies"][0]["target_bus"] = "P_CAN"
            _write_json(analysis_path, analysis)
            for path in (first, second):
                metadata_path = path / "metadata.json"
                metadata = json.loads(metadata_path.read_text())
                metadata["execution_mode"] = "distributed_offline"
                del metadata["logs"]["b_can"]
                _write_json(metadata_path, metadata)
            gate = recovery_returned_to_prestate(root, 1)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(gate["status"], "stable")
        self.assertEqual(report["comparability"]["status"], "inconclusive")
        self.assertTrue(any("source_target_prestate_unobserved" in reason
                            for reason in report["comparability"]["reasons"]))
        self.assertEqual(len(report["event_comparison"]["mutation_candidates"]), 1)
        self.assertFalse(report["event_comparison"]["mutation_only"])

    def test_configured_dbc_missing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _episode(root, 1, "mutation", 1)
            _episode(root, 2, "noop", 2)
            metadata_path = first / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["dbc_path"] = str(root / "missing.dbc")
            _write_json(metadata_path, metadata)
            gate = recovery_returned_to_prestate(root, 1)
            report = analyze_trial_pair(root, 1, 2)
        self.assertEqual(gate["status"], "inconclusive")
        self.assertTrue(any("configured DBC is unavailable" in reason
                            for reason in gate["reasons"]))
        self.assertEqual(report["comparability"]["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
