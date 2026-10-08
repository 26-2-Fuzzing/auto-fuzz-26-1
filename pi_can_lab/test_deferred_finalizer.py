"""Offline commit checks for the captured paired-cycle workflow."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a5_0x366_mutator import TargetedMutation
from deferred_finalizer import finalize_deferred_cycle
from experiment_store import ExperimentStore
from paired_cycle import build_cycle_plan, make_cycle_mutation
from trial_models import noop_case


BASE = bytes.fromhex("00000000200000F0")
PHASES = {
    "baseline_start": 1_000_000_000, "baseline_end": 2_000_000_000,
    "normal_start": 2_000_000_000, "normal_end": 3_000_000_000,
    "mutation_start": 3_000_000_000, "mutation_end": 4_000_000_000,
    "recovery_start": 4_000_000_000, "recovery_end": 5_000_000_000,
}


class DeferredFinalizerTests(unittest.TestCase):
    def make_capture(self, root: Path) -> tuple[ExperimentStore, Path]:
        dbc = root / "A5.dbc"
        dbc.write_text("offline frozen catalogue fixture\n", encoding="utf-8")
        store = ExperimentStore(root, 12, {
            "runner_config": {"feedback": {"interesting_score_threshold": 0.9}},
        })
        class OneCaseGenerator:
            def __init__(self, _dbc, original):
                self.original = bytes(original)

            def generate_profile(self, _profile, _undefined_max_bits):
                changed = bytearray(self.original)
                changed[3] = 1
                return [TargetedMutation(
                    "signal_combination", "CASE_B", self.original, bytes(changed),
                )]

        with patch("paired_cycle.A5BlinkmodiMutator", OneCaseGenerator):
            plan = build_cycle_plan(
                dbc, BASE, source_bus="b_can", random_seed=366,
                selected_family="signal_combination",
            )
        self.assertEqual(plan["scheduled_count"], 1)
        entry = next(row for row in plan["entries"]
                     if row["disposition"] == "scheduled")
        plan.update(deferred_analysis=True, status="active", captured_pairs=[{
            "entry_index": entry["index"], "pair_id": "pair_0001",
        }])
        pairs = store.path / "pairs"
        pairs.mkdir()
        store.write_json(pairs / "cycle.json", plan)
        link = {
            "catalog_sha256": plan["catalog_sha256"],
            "entry_index": entry["index"], "entry_id": entry["entry_id"],
            "family": entry["family"], "case": entry["case"],
            "tx_fingerprint": entry["tx_fingerprint"],
        }
        mutation = make_cycle_mutation(
            entry, mutation_id=1, source_bus="b_can",
            original_payload=BASE, random_seed=366,
        )
        control = noop_case(2, "b_can", 0x366, BASE, 366)
        order = ["mutation", "noop"]
        store.write_json(pairs / "pair_0001.json", {
            "schema_version": 1, "pair_id": "pair_0001",
            "status": "captured", "analysis_mode": "deferred",
            "pair_order": order, "source_bus": "b_can",
            "target_id": "0x366", "random_seed": 366,
            "collection_config": {}, "analysis_config": {},
            "baseline_payload": BASE.hex().upper(), "dbc_path": str(dbc),
            "cycle_entry": link, "first_trial_id": 1, "second_trial_id": 2,
            "frozen_mutation": mutation.to_dict(),
            "frozen_noop": control.to_dict(),
            "recovery_gates": {
                "first": {"status": "stable", "observed_change": False, "reasons": []},
                "second": {"status": "stable", "observed_change": False, "reasons": []},
            },
        })
        for position, case in enumerate((mutation, control), start=1):
            trial = store.create_trial(position)
            store.write_json(trial / "mutation.json", case.to_dict())
            store.write_json(trial / "metadata.json", {
                "status": "captured", "experiment_id": 12,
                "trial_id": position, "pair_id": "pair_0001",
                "pair_position": position, "pair_order": order,
                "pair_role": order[position - 1],
                "trial_kind": order[position - 1],
                "source_bus": "B_CAN", "target_id": "0x366",
                "dbc_path": str(dbc), "cycle_entry": link,
                "phase_times_ns": PHASES,
                "logs": {bus: f"{bus}.jsonl"
                         for bus in ("p_can", "b_can", "i_can")},
                "collection_config": {}, "analysis_config": {}, "clock_offsets": {},
            })
            for name in ("tx.jsonl", "p_can.jsonl", "b_can.jsonl", "i_can.jsonl"):
                (trial / name).write_text("{}\n", encoding="utf-8")
        return store, pairs

    @staticmethod
    def fake_analysis(**kwargs):
        anomalies = ([{
            "classification": "candidate", "score": 0.8,
            "target_bus": "P_CAN", "target_id": "0x3D6",
            "type": "payload_signal", "evidence": {},
        }] if kwargs["mutation"].trial_kind == "mutation" else [])
        return {
            "anomalies": anomalies,
            "trial_kind": kwargs["mutation"].trial_kind,
            "phase_times_ns": kwargs["phase_times_ns"],
        }

    @staticmethod
    def fake_pair(_root, mutation_id, noop_id, *, pair_id):
        return {
            "pair_id": pair_id,
            "mutation_trial_id": mutation_id,
            "noop_trial_id": noop_id,
            "comparability": {"status": "comparable", "reasons": []},
            "next_pair_gate": {"status": "ready"},
            "verification_status": "unverified", "feedback_eligible": False,
        }

    def test_finalize_commits_once_and_second_call_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pairs = self.make_capture(Path(directory))
            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial", side_effect=self.fake_analysis) as trial_analysis,
                  patch("deferred_finalizer.analyze_trial_pair", side_effect=self.fake_pair) as pair_analysis):
                first = finalize_deferred_cycle(store)
                self.assertEqual(first["status"], "completed")
                self.assertEqual(first["analyzed_count"], 1)
                self.assertEqual(first["analyzed_this_invocation"], 1)
                self.assertFalse(first["review_required"])
                second = finalize_deferred_cycle(store)
                self.assertEqual(second["analyzed_this_invocation"], 0)
                self.assertEqual(trial_analysis.call_count, 2)
                pair_analysis.assert_called_once()
            plan = json.loads((pairs / "cycle.json").read_text())
            self.assertEqual(plan["completed_pairs"][0]["pair_id"], "pair_0001")
            self.assertEqual(store.load_feedback_state()["completed_trial_ids"], [1])
            self.assertEqual(store.load_feedback_state()["control_trial_ids"], [2])
            feedback = json.loads((store.path / "trial_0001" / "feedback.json").read_text())
            self.assertEqual(feedback["candidate_events"], [])
            self.assertEqual(json.loads((store.path / "experiment.json").read_text())["status"], "completed")

    def test_interrupted_second_analysis_resumes_without_reanalyzing_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _pairs = self.make_capture(Path(directory))
            calls = []

            def interrupted(**kwargs):
                calls.append(kwargs["current_trial_id"])
                if kwargs["current_trial_id"] == 2 and calls.count(2) == 1:
                    raise KeyboardInterrupt("interrupted offline analysis")
                return self.fake_analysis(**kwargs)

            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial", side_effect=interrupted),
                  patch("deferred_finalizer.analyze_trial_pair", side_effect=self.fake_pair)):
                with self.assertRaises(KeyboardInterrupt):
                    finalize_deferred_cycle(store)
                self.assertEqual(store.load_feedback_state()["completed_trial_ids"], [1])
                resumed = finalize_deferred_cycle(store)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(calls, [1, 2, 2])

    def test_missing_capture_file_or_gate_is_rejected_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pairs = self.make_capture(Path(directory))
            (store.path / "trial_0002" / "i_can.jsonl").unlink()
            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial") as analyzer):
                with self.assertRaisesRegex(RuntimeError, "lacks required capture files"):
                    finalize_deferred_cycle(store)
                analyzer.assert_not_called()
            (store.path / "trial_0002" / "i_can.jsonl").write_text("{}\n")
            pair_path = pairs / "pair_0001.json"
            pair = json.loads(pair_path.read_text())
            pair["recovery_gates"].pop("second")
            store.write_json(pair_path, pair)
            with patch("deferred_finalizer._validate_capture_evidence"):
                with self.assertRaisesRegex(RuntimeError, "lacks both saved recovery gates"):
                    finalize_deferred_cycle(store)

    def test_review_finding_is_reported_without_discarding_completed_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pairs = self.make_capture(Path(directory))
            pair_path = pairs / "pair_0001.json"
            pair = json.loads(pair_path.read_text())
            for gate in pair["recovery_gates"].values():
                gate.update(
                    status="review_required", observed_change=True,
                    reasons=["lock command changed"],
                    capture_integrity_status="stable", capture_integrity_reasons=[],
                )
            store.write_json(pair_path, pair)
            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial", side_effect=self.fake_analysis),
                  patch("deferred_finalizer.analyze_trial_pair", side_effect=self.fake_pair)):
                result = finalize_deferred_cycle(store)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["review_pairs"], ["pair_0001"])
            self.assertTrue(result["review_required"])

    def test_exterior_light_fault_observation_finalizes_after_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pairs = self.make_capture(Path(directory))
            pair_path = pairs / "pair_0001.json"
            pair = json.loads(pair_path.read_text())
            light_fault = {
                "status": "stable", "observed_change": True, "reasons": [],
                "capture_integrity_status": "stable", "capture_integrity_reasons": [],
                "advisory_observations": [
                    "B_CAN 0x3D6 LH_Aussenlicht_def late recovery changed "
                    "(0/50 vs 50/50 active)",
                ],
                "checks": {"watched_ids": {"b_can": {"0x3D6": {
                    "normal_count": 50, "recovery_count": 50,
                    "signal_changes": [{"signal": "LH_Aussenlicht_def",
                                        "normal_active": 0, "recovery_active": 50,
                                        "kind": "stable_state",
                                        "review_required": False}],
                }}}},
            }
            pair["recovery_gates"] = {"first": light_fault, "second": light_fault}
            store.write_json(pair_path, pair)
            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial", side_effect=self.fake_analysis),
                  patch("deferred_finalizer.analyze_trial_pair", side_effect=self.fake_pair)):
                result = finalize_deferred_cycle(store)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["review_pairs"], [])
            self.assertFalse(result["review_required"])
            self.assertEqual(json.loads(pair_path.read_text())["recovery_gates"]["first"],
                             light_fault)

    def test_non_exempt_advisory_is_not_accepted_as_stable_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pairs = self.make_capture(Path(directory))
            pair_path = pairs / "pair_0001.json"
            pair = json.loads(pair_path.read_text())
            pair["recovery_gates"]["first"].update(
                status="stable", observed_change=True, reasons=[],
                capture_integrity_status="stable", capture_integrity_reasons=[],
                advisory_observations=[
                    "B_CAN 0x184 ZV_FT_verriegeln late recovery changed",
                ],
            )
            store.write_json(pair_path, pair)
            with patch("deferred_finalizer._validate_capture_evidence"), \
                 patch("deferred_finalizer.analyze_trial") as analyzer:
                with self.assertRaisesRegex(RuntimeError, "first recovery gate is invalid"):
                    finalize_deferred_cycle(store)
                analyzer.assert_not_called()

    def test_detailed_recovery_review_is_reported_after_all_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _pairs = self.make_capture(Path(directory))

            def detailed_review(root, mutation_id, noop_id, *, pair_id):
                report = self.fake_pair(root, mutation_id, noop_id, pair_id=pair_id)
                report["next_pair_gate"] = {
                    "status": "review_required", "reasons": ["light state changed"],
                }
                return report

            with (patch("deferred_finalizer._validate_capture_evidence"),
                  patch("deferred_finalizer.analyze_trial", side_effect=self.fake_analysis),
                  patch("deferred_finalizer.analyze_trial_pair", side_effect=detailed_review)):
                result = finalize_deferred_cycle(store)
            self.assertEqual(result["analyzed_count"], 1)
            self.assertEqual(result["review_pairs"], ["pair_0001"])
            self.assertTrue(result["review_required"])


if __name__ == "__main__":
    unittest.main()
