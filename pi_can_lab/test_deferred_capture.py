"""Offline checks for capture-first paired-cycle orchestration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a5_0x366_mutator import TargetedMutation
from experiment_runner import ExperimentRunner, build_parser, run
from experiment_store import ExperimentStore
from paired_cycle import advance_cycle
from strategy_selector import TrialStrategySelector


BASE = bytes.fromhex("00000000200000F0")


class TwoCaseGenerator:
    def __init__(self, _dbc_path, original_payload):
        self.original = bytes(original_payload)

    def generate_profile(self, profile, _undefined_max_bits):
        if profile != "all-0x366":
            raise AssertionError("unexpected mutation profile")
        first = bytearray(self.original)
        second = bytearray(self.original)
        first[3] = 1
        second[3] = 2
        return [
            TargetedMutation("signal_single", "CASE_A", self.original, bytes(first)),
            TargetedMutation("signal_combination", "CASE_B", self.original, bytes(second)),
        ]


class QuietManager:
    def __init__(self, _config):
        pass

    def close(self):
        pass


def settings() -> dict:
    return {
        "remote": {"hosts": {bus: {"name": bus}
                             for bus in ("p_can", "b_can", "i_can")}},
        "target": {"reference_payload": BASE.hex()},
        "trial": {"capture_start_delay_seconds": 0},
        "feedback": {"enabled": False},
    }


class DeferredCaptureTests(unittest.TestCase):
    def make_runner(self, root: Path):
        dbc = root / "A5.dbc"
        dbc.write_text("offline fixture\n", encoding="utf-8")
        config = settings()
        runner = ExperimentRunner(config, manager_factory=QuietManager)
        runner.probe_payload = lambda *_args, **_kwargs: BASE
        store = ExperimentStore(root, 42, config)
        calls = []

        def fake_capture(**kwargs):
            trial_id = kwargs["expected_trial_id"]
            calls.append(trial_id)
            trial_dir = store.create_trial(trial_id)
            store.write_json(trial_dir / "metadata.json", {
                "status": "captured", "trial_id": trial_id,
                "pair_id": kwargs["pair_id"],
                "pair_position": kwargs["pair_position"],
                "pair_order": kwargs["pair_order"],
                "pair_role": "noop" if kwargs["control_noop"] else "mutation",
                "cycle_entry": kwargs["cycle_entry"],
            })
            store.write_json(trial_dir / "mutation.json",
                             kwargs["prepared_case"].to_dict())
            for name in ("tx.jsonl", "p_can.jsonl", "b_can.jsonl", "i_can.jsonl"):
                (trial_dir / name).write_text("{}\n", encoding="utf-8")
            self.assertTrue(kwargs["defer_analysis"])
            return {"status": "captured", "trial_id": trial_id}

        runner.run_trial = fake_capture
        return runner, store, dbc, calls

    def test_capture_cycle_advances_without_trial_or_pair_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            stable = {"status": "stable", "observed_change": False, "reasons": []}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery", return_value=stable), \
                 patch("experiment_runner.analyze_trial", side_effect=AssertionError("runtime analysis")), \
                 patch("experiment_runner.analyze_trial_pair", side_effect=AssertionError("pair analysis")):
                first = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
                second = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
                resumed = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(first["captured_count"], 1)
            self.assertEqual(second["status"], "capture_complete")
            self.assertEqual(resumed["executed_this_invocation"], 0)
            self.assertEqual(calls, [1, 2, 3, 4])
            plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
            self.assertEqual(len(plan["captured_pairs"]), 2)
            self.assertEqual(plan["completed_pairs"], [])
            self.assertEqual(store.load_feedback_state()["total_trials"], 0)
            self.assertFalse(list(store.path.glob("pairs/*_report.json")))
            self.assertFalse(list(store.path.glob("trial_*/anomalies.json")))

    def test_exterior_light_fault_observation_does_not_stop_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
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
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery",
                       return_value=light_fault), \
                 patch("experiment_runner.analyze_trial",
                       side_effect=AssertionError("runtime analysis")), \
                 patch("experiment_runner.analyze_trial_pair",
                       side_effect=AssertionError("runtime pair analysis")):
                first = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
                resumed = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(first["captured_count"], 1)
            self.assertEqual(resumed["status"], "capture_complete")
            self.assertEqual(calls, [1, 2, 3, 4])
            for number in (1, 2):
                pair = json.loads((store.path / "pairs" / f"pair_{number:04d}.json").read_text())
                self.assertEqual(pair["status"], "captured")
                self.assertEqual(pair["recovery_gates"]["first"], light_fault)
                self.assertEqual(pair["recovery_gates"]["second"], light_fault)

    def test_lock_review_is_recorded_without_stopping_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            lock_review = {
                "status": "review_required", "observed_change": True,
                "reasons": ["B_CAN 0x184 ZV_FT_verriegeln late recovery changed"],
                "capture_integrity_status": "stable", "capture_integrity_reasons": [],
                "checks": {"watched_ids": {"b_can": {"0x184": {
                    "signal_changes": [{"signal": "ZV_FT_verriegeln",
                                        "kind": "repeated_activity"}],
                }}}},
            }
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery",
                       return_value=lock_review):
                result = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=2,
                )
            self.assertEqual(result["status"], "capture_complete")
            self.assertEqual(calls, [1, 2, 3, 4])
            for number in (1, 2):
                pair = json.loads((store.path / "pairs" / f"pair_{number:04d}.json").read_text())
                self.assertEqual(pair["status"], "captured")
                self.assertEqual(pair["recovery_gates"]["first"], lock_review)
                self.assertEqual(pair["recovery_gates"]["second"], lock_review)

    def test_non_exempt_lock_advisory_cannot_claim_stable_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            contradictory = {
                "status": "stable", "observed_change": True, "reasons": [],
                "capture_integrity_status": "stable", "capture_integrity_reasons": [],
                "advisory_observations": [
                    "B_CAN 0x184 ZV_FT_verriegeln late recovery changed",
                ],
            }
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery",
                       return_value=contradictory):
                with self.assertRaisesRegex(RuntimeError, "recovery needs review"):
                    runner.run_deferred_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                    )
            self.assertEqual(calls, [1])

    def test_crash_after_pair_capture_reconciles_without_reinjection(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            stable = {"status": "stable", "observed_change": False, "reasons": []}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery", return_value=stable):
                with patch("experiment_runner.advance_captured_cycle",
                           side_effect=KeyboardInterrupt("after capture")):
                    with self.assertRaises(KeyboardInterrupt):
                        runner.run_deferred_paired_cycle(
                            store=store, source_bus="b_can", random_seed=366,
                            selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                        )
                self.assertEqual(calls, [1, 2])
                plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
                self.assertEqual(plan["captured_pairs"], [])
                resumed = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(calls, [1, 2, 3, 4])
            self.assertEqual(resumed["reconciled_this_invocation"], 1)
            self.assertEqual(resumed["captured_count"], 2)

    def test_recovery_review_stops_before_second_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            review = {"status": "review_required", "observed_change": True,
                      "reasons": ["lighting changed"]}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery", return_value=review):
                with self.assertRaisesRegex(RuntimeError, "recovery needs review"):
                    runner.run_deferred_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                    )
                with self.assertRaises(RuntimeError):
                    runner.run_deferred_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                    )
            self.assertEqual(calls, [1])
            pair = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(pair["status"], "blocked")
            self.assertEqual(pair["recovery_gates"]["first"]["status"], "review_required")

    def test_second_recovery_review_keeps_both_captures_for_offline_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            stable = {"status": "stable", "observed_change": False, "reasons": []}
            review = {"status": "review_required", "observed_change": True,
                      "reasons": ["lighting changed"]}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery",
                       side_effect=[stable, review]):
                with self.assertRaisesRegex(RuntimeError, "recovery needs review"):
                    runner.run_deferred_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=2,
                    )
            self.assertEqual(calls, [1, 2])
            plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
            self.assertEqual(len(plan["captured_pairs"]), 1)
            pair = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(pair["status"], "captured")
            self.assertEqual(pair["recovery_gates"]["second"]["status"], "review_required")

    def test_inconsistent_stable_gate_cannot_authorize_next_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            inconsistent = {"status": "stable", "observed_change": True,
                            "reasons": []}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery",
                       return_value=inconsistent):
                with self.assertRaisesRegex(RuntimeError, "recovery needs review"):
                    runner.run_deferred_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=2,
                    )
            self.assertEqual(calls, [1])

    def test_finalize_cli_does_not_construct_remote_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings()
            config["experiments_root"] = str(root / "experiments")
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            store = ExperimentStore(root / "experiments", 42, config)
            pairs = store.path / "pairs"
            pairs.mkdir(exist_ok=True)
            (pairs / "cycle.json").write_text("{}\n", encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--experiment-id", "42",
                "--finalize-analysis",
            ])
            with patch("experiment_runner.ExperimentRunner") as runner_class, \
                 patch("deferred_finalizer.finalize_deferred_cycle",
                       return_value={"status": "completed"}) as finalizer:
                self.assertEqual(run(args), 0)
            runner_class.assert_not_called()
            finalizer.assert_called_once()

    def test_partial_offline_review_does_not_block_later_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, dbc, calls = self.make_runner(Path(directory))
            stable = {"status": "stable", "observed_change": False, "reasons": []}
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("minimal_recovery_gate.check_minimal_recovery", return_value=stable):
                runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
                pairs = store.path / "pairs"
                pair_path = pairs / "pair_0001.json"
                pair = json.loads(pair_path.read_text())
                pair.update(status="completed", pair_report="pair_0001_report.json",
                            comparability_status="comparable")
                store.write_json(pair_path, pair)
                store.write_json(pairs / "pair_0001_report.json", {
                    "pair_id": "pair_0001",
                    "comparability": {"status": "comparable"},
                    "next_pair_gate": {"status": "review_required"},
                })
                for trial_id in (1, 2):
                    metadata_path = store.path / f"trial_{trial_id:04d}" / "metadata.json"
                    metadata = json.loads(metadata_path.read_text())
                    metadata["status"] = "completed"
                    store.write_json(metadata_path, metadata)
                cycle_path = pairs / "cycle.json"
                plan = json.loads(cycle_path.read_text())
                plan = advance_cycle(plan, plan["captured_pairs"][0]["entry_index"],
                                     "pair_0001")
                store.write_json(cycle_path, plan)
                result = runner.run_deferred_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=TrialStrategySelector({}), dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(result["status"], "capture_complete")
            self.assertEqual(calls, [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
