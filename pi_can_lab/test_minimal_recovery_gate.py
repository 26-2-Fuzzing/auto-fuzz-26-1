from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minimal_recovery_gate import check_minimal_recovery
from test_pair_analysis import NS, _episode, _write_json, _write_jsonl


def _captured_trial(root: Path) -> Path:
    trial = _episode(root, 1, "mutation", 1)
    metadata_path = trial / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "captured"
    _write_json(metadata_path, metadata)
    (trial / "anomalies.json").unlink()
    phases = metadata["phase_times_ns"]
    path = trial / "b_can.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    common = {"record_type": "can_rx", "bus": "b_can", "experiment_id": 1,
              "session_id": "rx-session", "is_extended_id": False,
              "is_error_frame": False, "is_remote_frame": False}
    for phase in ("baseline", "normal", "recovery"):
        start = phases[f"{phase}_end"] - 5 * NS
        for index in range(50):
            records.append({**common, "wall_time_ns": start + index * NS // 10 + 5,
                            "arbitration_id": 0x3D6, "data_hex": "0000000000000000"})
        for index in range(25):
            records.append({**common, "wall_time_ns": start + index * NS // 5 + 6,
                            "arbitration_id": 0x583, "data_hex": "0000000000000000"})
        for index in range(5):
            records.append({**common, "wall_time_ns": start + index * NS + 7,
                            "arbitration_id": 0x184, "data_hex": "0000000000000000"})
    start_record = next(item for item in records if item["record_type"] == "session_start")
    end_record = next(item for item in records if item["record_type"] == "session_end")
    frames = sorted((item for item in records if item["record_type"] == "can_rx"),
                    key=lambda item: item["wall_time_ns"])
    _write_jsonl(path, [start_record, *frames, end_record])
    return trial


def _change_recovery_bits(trial: Path, can_id: int, bit: int, count: int) -> None:
    path = trial / "b_can.jsonl"
    metadata = json.loads((trial / "metadata.json").read_text(encoding="utf-8"))
    start = metadata["phase_times_ns"]["recovery_end"] - 5 * NS
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    changed = 0
    for record in records:
        if (record.get("record_type") == "can_rx"
                and record.get("arbitration_id") == can_id
                and start <= record["wall_time_ns"] < start + 5 * NS
                and changed < count):
            payload = bytearray.fromhex(record["data_hex"])
            payload[bit // 8] |= 1 << (bit % 8)
            record["data_hex"] = payload.hex().upper()
            changed += 1
    assert changed == count
    _write_jsonl(path, records)


class MinimalRecoveryGateTests(unittest.TestCase):
    def test_captured_trial_passes_without_anomaly_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "stable", report["reasons"])
        self.assertEqual(report["capture_integrity_status"], "stable")
        self.assertEqual(report["capture_integrity_reasons"], [])
        self.assertFalse(report["observed_change"])
        self.assertEqual(report["checks"]["tx"]["status"], "valid")

    def test_repeated_rear_light_activity_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            _change_recovery_bits(trial, 0x3D6, 8, 4)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["capture_integrity_status"], "stable")
        self.assertEqual(report["capture_integrity_reasons"], [])
        self.assertTrue(report["observed_change"])
        self.assertTrue(any("LH_Standlicht_H_aktiv" in reason for reason in report["reasons"]))

    def test_exterior_light_fault_is_advisory_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            _change_recovery_bits(trial, 0x3D6, 7, 50)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "stable", report["reasons"])
        self.assertEqual(report["capture_integrity_status"], "stable")
        self.assertTrue(report["observed_change"])
        self.assertEqual(len(report["advisory_observations"]), 1)
        self.assertIn("LH_Aussenlicht_def", report["advisory_observations"][0])
        signal_changes = report["checks"]["watched_ids"]["b_can"]["0x3D6"]["signal_changes"]
        self.assertEqual(signal_changes[0]["signal"], "LH_Aussenlicht_def")
        self.assertFalse(signal_changes[0]["review_required"])

    def test_lamp_activity_still_requires_review_with_fault_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            _change_recovery_bits(trial, 0x3D6, 7, 50)
            _change_recovery_bits(trial, 0x3D6, 8, 4)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertTrue(any("LH_Standlicht_H_aktiv" in reason for reason in report["reasons"]))
        self.assertFalse(any("LH_Aussenlicht_def" in reason for reason in report["reasons"]))
        self.assertTrue(any("LH_Aussenlicht_def" in observation for observation
                            in report["advisory_observations"]))

    def test_repeated_lock_command_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            _change_recovery_bits(trial, 0x184, 12, 2)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["capture_integrity_status"], "stable")
        self.assertEqual(report["capture_integrity_reasons"], [])
        self.assertTrue(report["observed_change"])
        self.assertTrue(any("ZV_FT_verriegeln" in reason for reason in report["reasons"]))

    def test_source_payload_drift_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            path = trial / "b_can.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            phases = json.loads((trial / "metadata.json").read_text(encoding="utf-8"))[
                "phase_times_ns"]
            for record in records:
                if (record.get("record_type") == "can_rx"
                        and record.get("arbitration_id") == 0x366
                        and phases["recovery_end"] - 5 * NS <= record["wall_time_ns"]
                        < phases["recovery_end"]):
                    record["data_hex"] = "FFFFFFFFFFFFFFFF"
                    break
            _write_jsonl(path, records)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["capture_integrity_status"], "review_required")
        self.assertTrue(any("source target payload drifted" in reason for reason
                            in report["capture_integrity_reasons"]))
        self.assertTrue(report["observed_change"])
        self.assertTrue(any("source target payload drifted" in reason for reason
                            in report["reasons"]))

    def test_missing_capture_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            (trial / "i_can.jsonl").unlink()
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["capture_integrity_status"], "review_required")
        self.assertTrue(any("i_can: invalid receiver capture" in reason for reason
                            in report["capture_integrity_reasons"]))
        self.assertFalse(report["observed_change"])
        self.assertTrue(any("i_can: invalid receiver capture" in reason for reason
                            in report["reasons"]))

    def test_too_few_rear_light_frames_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = _captured_trial(Path(directory))
            path = trial / "b_can.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            phases = json.loads((trial / "metadata.json").read_text(encoding="utf-8"))[
                "phase_times_ns"]
            windows = {phase: (phases[f"{phase}_end"] - 5 * NS,
                               phases[f"{phase}_end"])
                       for phase in ("normal", "recovery")}
            kept = {phase: 0 for phase in windows}
            filtered = []
            for record in records:
                if record.get("record_type") == "can_rx" and record.get("arbitration_id") == 0x3D6:
                    for phase, (start, end) in windows.items():
                        if start <= record["wall_time_ns"] < end:
                            kept[phase] += 1
                            if kept[phase] > 4:
                                break
                    else:
                        filtered.append(record)
                    continue
                filtered.append(record)
            _write_jsonl(path, filtered)
            report = check_minimal_recovery(trial)
        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["capture_integrity_status"], "stable")
        self.assertEqual(report["capture_integrity_reasons"], [])
        self.assertFalse(report["observed_change"])
        self.assertTrue(any("rear-light state 0x3D6 is not observed" in reason
                            for reason in report["reasons"]))


if __name__ == "__main__":
    unittest.main()
