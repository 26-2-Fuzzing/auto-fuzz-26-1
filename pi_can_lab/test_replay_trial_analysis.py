from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from replay_trial_analysis import (
    main, parse_trial_specs, passive_phase_quartets, replay_experiment,
)
from trial_models import MutationCase


NS = 1_000_000_000


def _frame(stamp: int, bus: str, can_id: int, payload: str) -> str:
    return json.dumps({
        "record_type": "can_rx", "wall_time_ns": stamp, "bus": bus,
        "arbitration_id": can_id, "is_extended_id": False,
        "is_error_frame": False, "is_remote_frame": False, "data_hex": payload,
    }) + "\n"


def _fixture(base: Path, *, no_op: bool = False) -> Path:
    root = base / "experiment_0001"
    trial = root / "trial_0001"
    trial.mkdir(parents=True)
    (root / "experiment.json").write_text(json.dumps({
        "experiment_id": 1, "config": {"runner_config": {
            "anomaly_thresholds": {"minimum_baseline_frames": 5},
        }},
    }), encoding="utf-8")
    original = bytes.fromhex("00000000200000F0")
    changed = original if no_op else bytes.fromhex("0000000020000018")
    mutation = MutationCase(
        mutation_id=1, source_bus="b_can", can_id=0x366, operator="TEST",
        original_payload=original, mutated_payload=changed, random_seed=1,
        trial_kind="noop" if no_op else "mutation",
    )
    (trial / "mutation.json").write_text(json.dumps(mutation.to_dict()), encoding="utf-8")
    phases = {
        "baseline_start": NS, "baseline_end": 11 * NS,
        "normal_start": 11 * NS, "normal_end": 16 * NS,
        "mutation_start": 16 * NS, "mutation_end": 17 * NS,
        "recovery_start": 17 * NS, "recovery_end": 27 * NS,
    }
    (trial / "metadata.json").write_text(json.dumps({
        "status": "completed", "trial_id": 1, "trial_kind": "no_op" if no_op else "mutation",
        "phase_times_ns": phases, "logs": {"b_can": "b_can.jsonl", "i_can": "i_can.jsonl"},
        "clock_offsets": {
            "b_can": {"offset_ms": 100.0, "round_trip_ms": 1.0},
            "i_can": {"offset_ms": 110.0, "round_trip_ms": 1.0},
        },
    }), encoding="utf-8")
    b_lines = []
    i_lines = []
    for second in range(26):
        stamp = (second + 1) * NS + 100_000_000
        b_lines.append(_frame(stamp, "b_can", 0x2A0, "00"))
        i_lines.append(_frame(stamp, "i_can", 0x456, "00" if second < 15 else "01"))
    (trial / "b_can.jsonl").write_text("".join(b_lines), encoding="utf-8")
    (trial / "i_can.jsonl").write_text("".join(i_lines), encoding="utf-8")
    return root


def _noop_tx_records(phases: dict[str, int]) -> list[dict]:
    session = "completed-noop-session"
    common = {"tx_session_id": session, "trial_kind": "noop", "execute": True,
              "experiment_id": 1}
    records = [{
        "record_type": "tx_session_start", **common, "trial_contract_version": 1,
        "campaign": {"enabled": True, "normal_data_hex": "00000000200000F0"},
        "mutation": {"control_noop": True, "trial_mutation_id": 1},
        "transmission": {"interval_ms": 50.0},
    }]
    def marker(phase: str, event: str) -> dict:
        return {"record_type": "tx_phase", **common, "phase": phase,
                "event": event, "wall_time_ns": phases[f"{phase}_{event}"]}
    records += [marker("baseline", "start"), marker("baseline", "end")]
    for phase in ("normal", "mutation"):
        records.append(marker(phase, "start"))
        for index in range((phases[f"{phase}_end"] - phases[f"{phase}_start"]) // 50_000_000):
            records.append({
                "record_type": "can_tx", **common, "status": "sent", "phase": phase,
                "arbitration_id": 0x366, "data_hex": "00000000200000F0",
                "wall_time_ns": phases[f"{phase}_start"] + index * 50_000_000,
            })
        records.append(marker(phase, "end"))
    records.append({
        "record_type": "can_tx", **common, "status": "sent", "phase": "recovery",
        "kind": "restore", "arbitration_id": 0x366,
        "data_hex": "00000000200000F0", "wall_time_ns": phases["mutation_end"] + 1_000_000,
    })
    records += [marker("recovery", "start"), marker("recovery", "end")]
    records.append({
        "record_type": "tx_session_end", **common, "trial_contract_version": 1,
        "status": "completed", "phase_sent": {"normal": 100, "mutation": 20},
        "restore": {"status": "sent", "sent": 1},
    })
    return records


def _synthetic_candidate(**kwargs):
    candidate = {
        "target_bus": "I_CAN", "target_id": "0x456", "type": "PAYLOAD_CHANGE",
        "classification": "candidate", "score": 0.9,
        "evidence": {"feedback_eligible": False, "verification_candidate": True},
    }
    return {
        "anomalies": [candidate], "observations": [
            {"classification": "inconclusive", "target_bus": "P_CAN", "target_id": "0x100"},
        ],
        "summary": {"candidate_count": 1, "inconclusive_count": 1, "incomparable_count": 2},
    }


class ReplayTrialAnalysisTests(unittest.TestCase):
    def test_trial_selector_and_passive_quartets(self) -> None:
        self.assertEqual(parse_trial_specs(["1,3-4", "7"]), {1, 3, 4, 7})
        with self.assertRaises(ValueError):
            parse_trial_specs(["4-2"])
        phases = {
            "baseline_start": NS, "baseline_end": 11 * NS,
            "mutation_start": 16 * NS, "mutation_end": 17 * NS,
            "recovery_start": 17 * NS, "recovery_end": 27 * NS,
        }
        quartets = passive_phase_quartets(phases, 1.0)
        self.assertEqual(len(quartets), 2)
        self.assertTrue(all(item["recovery_end"] <= phases["baseline_end"] for item in quartets))
        self.assertTrue(all(left["recovery_end"] <= right["baseline_start"] for left, right in zip(quartets, quartets[1:])))
        self.assertEqual(passive_phase_quartets(phases, 5.0), [])
        with self.assertRaises(ValueError):
            passive_phase_quartets(phases, 0.001)

    def test_replay_forwards_controls_and_counts_no_op_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory), no_op=True)
            metadata_path = root / "trial_0001" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["analysis_config"] = {"comparison_window_seconds": 1.0}
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate) as analyze:
                report = replay_experiment(root, trial_ids={1}, passive_windows=(1.0, 5.0))
        trial = report["trials"][0]
        self.assertEqual(trial["trial_kind"], "no_op")
        self.assertEqual(trial["analysis"]["candidate_count"], 1)
        self.assertEqual(trial["analysis"]["no_op_false_alert_count"], 1)
        self.assertEqual(trial["analysis"]["inconclusive_count"], 1)
        self.assertEqual(trial["analysis"]["incomparable_count"], 2)
        self.assertEqual(report["aggregate"]["no_op_trials_with_false_alert"], 1)
        self.assertEqual(report["aggregate"]["incomparable_no_op_trial_count"], 1)
        self.assertEqual(trial["tx_evidence"]["control_status"], "unassessed")
        self.assertEqual(report["aggregate"]["passive_negative_controls"][0]["dependent_group_count_for_diagnostics_only"], 2)
        self.assertEqual(report["aggregate"]["passive_negative_controls"][1]["trials_without_usable_groups"], 1)
        self.assertEqual(analyze.call_count, 3)
        self.assertEqual(analyze.call_args_list[0].kwargs["trial_kind"], "no_op")
        self.assertEqual(analyze.call_args_list[0].kwargs["thresholds"],
                         {"comparison_window_seconds": 1.0})
        self.assertEqual(analyze.call_args_list[0].kwargs["clock_offsets"]["i_can"]["offset_ms"], 110.0)
        for call in analyze.call_args_list[1:]:
            self.assertEqual(call.kwargs["trial_kind"], "calibration")
            self.assertEqual(call.kwargs["experiment_dir"], root)
            self.assertEqual(call.kwargs["current_trial_id"], 1)
            self.assertLessEqual(call.kwargs["phase_times_ns"]["recovery_end"], 11 * NS)

    def test_no_op_tx_schedule_must_match_to_be_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory), no_op=True)
            tx_path = root / "trial_0001" / "tx.jsonl"
            metadata = json.loads((root / "trial_0001" / "metadata.json").read_text(encoding="utf-8"))
            records = _noop_tx_records(metadata["phase_times_ns"])
            tx_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root)
            self.assertEqual(report["trials"][0]["tx_evidence"]["control_status"], "comparable")
            self.assertEqual(report["aggregate"]["comparable_no_op_trials_with_false_alert"], 1)
            records = [item for item in records if not (
                item.get("record_type") == "can_tx" and item.get("phase") == "mutation"
                and item["wall_time_ns"] >= metadata["phase_times_ns"]["mutation_start"] + 900_000_000
            )]
            tx_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root)
            self.assertEqual(report["trials"][0]["tx_evidence"]["control_status"], "invalid")
            self.assertEqual(report["aggregate"]["incomparable_no_op_trial_count"], 1)

    def test_no_op_bursty_or_partial_session_is_not_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory), no_op=True)
            tx_path = root / "trial_0001" / "tx.jsonl"
            phases = json.loads((root / "trial_0001" / "metadata.json").read_text(
                encoding="utf-8"))["phase_times_ns"]
            records = _noop_tx_records(phases)
            for item in records:
                if item.get("record_type") == "can_tx" and item.get("phase") == "mutation":
                    item["wall_time_ns"] = phases["mutation_start"] + (
                        item["wall_time_ns"] - phases["mutation_start"]
                    ) // 10
            tx_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root)
            self.assertEqual(report["trials"][0]["tx_evidence"]["control_status"], "invalid")
            self.assertIn("bursty", report["trials"][0]["tx_evidence"]["reason"])
            self.assertEqual(report["aggregate"]["comparable_no_op_trial_count"], 0)
            records[-1]["status"] = "interrupted"
            tx_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root)
            self.assertEqual(report["trials"][0]["tx_evidence"]["control_status"], "invalid")
            self.assertIn("contract", report["trials"][0]["tx_evidence"]["reason"])

    def test_explicit_trial_kind_conflict_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory), no_op=True)
            mutation_path = root / "trial_0001" / "mutation.json"
            mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
            mutation["trial_kind"] = "mutation"
            mutation_path.write_text(json.dumps(mutation), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicts"):
                replay_experiment(root)

    def test_clock_report_hides_invalid_reference_and_legacy_distributed_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory))
            metadata_path = root / "trial_0001" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["execution_mode"] = "distributed_offline_previous_trial_feedback"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate) as analyze:
                report = replay_experiment(root)
            self.assertFalse(analyze.call_args.kwargs["clock_offsets"]["i_can"]["alignment_valid"])
            self.assertEqual(report["trials"][0]["clock_alignment"]["i_can"]["status"],
                             "invalid_reference")
            self.assertIsNone(report["trials"][0]["clock_alignment"]["i_can"][
                "correction_to_source_ms"])
            metadata["clock_offsets"]["b_can"].update(reference_id="host-a", alignment_valid=True)
            metadata["clock_offsets"]["i_can"].update(reference_id="host-b", alignment_valid=True)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root)
            self.assertEqual(report["trials"][0]["clock_alignment"]["i_can"]["status"],
                             "invalid_reference")
            self.assertIsNone(report["trials"][0]["clock_alignment"]["i_can"][
                "correction_to_source_ms"])

    def test_synthetic_positive_is_reported_without_feedback_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory))
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                report = replay_experiment(root, trial_ids={1})
            after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(report["trials"][0]["analysis"]["candidates"][0]["target_id"], "0x456")
        self.assertEqual(report["aggregate"]["mutation_trial_count"], 1)
        self.assertEqual(report["aggregate"]["no_op_false_alert_events"], 0)
        self.assertEqual(report["trials"][0]["clock_alignment"]["i_can"]["status"], "aligned")
        self.assertEqual(report["trials"][0]["clock_alignment"]["i_can"][
            "correction_to_source_ms"], -10.0)
        self.assertEqual(report["aggregate"]["incomparable_comparison_count"], 2)

    def test_real_detector_replays_a_synthetic_persistent_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _fixture(Path(directory))
            path = root / "trial_0001" / "i_can.jsonl"
            lines = []
            for phase_start, duration, payload in ((NS, 10, "00"), (11 * NS, 5, "00"),
                                                   (16 * NS, 1, "01"), (17 * NS, 10, "01")):
                for index in range(duration * 10):
                    lines.append(_frame(phase_start + index * 100_000_000 + 50_000_000,
                                        "i_can", 0x456, payload))
            with path.open("a", encoding="utf-8") as handle:
                handle.writelines(lines)
            report = replay_experiment(root, include_history=False)
        refs = [(item["target_bus"], item["target_id"], item["type"])
                for item in report["trials"][0]["analysis"]["candidates"]]
        self.assertIn(("I_CAN", "0x456", "PAYLOAD_CHANGE"), refs)
        self.assertEqual(report["aggregate"]["no_op_trial_count"], 0)

    def test_cli_writes_only_an_explicit_new_external_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = _fixture(base)
            output = base / "report.json"
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main([str(root), "--trial", "1", "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["aggregate"]["completed_trial_count"], 1)
            with patch("replay_trial_analysis.analyze_trial", side_effect=_synthetic_candidate):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main([str(root), "--output", str(output)])
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main([str(root), "--output", str(root / "report.json")])
            self.assertFalse((root / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
