"""Offline checks for paired local episodes and fail-closed resumption."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiment_runner import ExperimentRunner, build_parser, paired_order, run
from experiment_store import ExperimentStore
from mutation_feedback import create_trial_feedback
from strategy_selector import StrategyDecision, TrialStrategySelector
from trial_models import MutationCase, noop_case


BASE = bytes.fromhex("00000000200000F0")


class QuietManager:
    def __init__(self, config):
        self.name = config["name"]
        self.started = 0
        self.stdout = ""

    def run(self, command, timeout=None, check=True):
        del command, timeout, check
        return SimpleNamespace(stdout=self.stdout)

    def start_process(self, command, stdout_path):
        del command, stdout_path
        self.started += 1
        raise AssertionError("no remote process should have started")

    def clock_sample(self):
        return {"offset_ms": 0.0, "round_trip_ms": 0.2}

    def close(self):
        pass


def config() -> dict:
    return {
        "remote": {
            "hosts": {bus: {"name": bus} for bus in ("p_can", "b_can", "i_can")},
        },
        "target": {"probe_live_payload": True, "probe_samples": 3},
        "trial": {"capture_start_delay_seconds": 0},
        "feedback": {},
    }


class PairRunnerTests(unittest.TestCase):
    def make_runner(self, root: Path):
        settings = config()
        store = ExperimentStore(root, 42, settings)
        runner = ExperimentRunner(settings, manager_factory=QuietManager)
        selector = TrialStrategySelector({})
        runner.probe_payload = lambda *_args, **_kwargs: BASE
        return runner, store, selector

    @staticmethod
    def write_completed_episode(**kwargs):
        store = kwargs["store"]
        trial_id = kwargs["expected_trial_id"]
        case = kwargs["prepared_case"]
        trial_dir = store.create_trial(trial_id)
        metadata = {
            "status": "completed",
            "pair_id": kwargs["pair_id"],
            "pair_position": kwargs["pair_position"],
            "pair_order": kwargs["pair_order"],
            "pair_role": case.trial_kind,
        }
        store.write_json(trial_dir / "metadata.json", metadata)
        store.write_json(trial_dir / "mutation.json", case.to_dict())
        for filename in (
            "feedback.json", "anomalies.json", "tx.jsonl",
            "p_can.jsonl", "b_can.jsonl", "i_can.jsonl",
        ):
            (trial_dir / filename).write_text("{}\n", encoding="utf-8")
        store.record_completed_trial(case, create_trial_feedback(trial_id, case, [], 0.6))
        return {"trial_id": trial_id}

    def test_two_sets_alternate_order_and_preserve_frozen_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs)
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "comparable"},
            }):
                for _ in range(2):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            manifests = [
                json.loads((store.path / "pairs" / f"pair_{index:04d}.json").read_text())
                for index in (1, 2)
            ]
            self.assertEqual(manifests[0]["pair_order"], paired_order(366, 42, 1))
            self.assertEqual(manifests[1]["pair_order"], manifests[0]["pair_order"][::-1])
            self.assertEqual([item["status"] for item in manifests], ["completed", "completed"])
            self.assertEqual([item["expected_trial_id"] for item in calls], [1, 2, 3, 4])
            for manifest in manifests:
                self.assertTrue((store.path / "pairs" / manifest["pair_report"]).is_file())
                pair_calls = [item for item in calls if item["pair_id"] == manifest["pair_id"]]
                self.assertEqual(len(pair_calls), 2)
                self.assertEqual(pair_calls[0]["prepared_case"].original_payload, BASE)
                self.assertEqual(pair_calls[1]["prepared_case"].original_payload, BASE)
                self.assertEqual(
                    [item["prepared_case"].trial_kind for item in pair_calls],
                    manifest["pair_order"],
                )
                mutant = next(item["prepared_case"] for item in pair_calls
                              if item["prepared_case"].trial_kind == "mutation")
                self.assertEqual(mutant.to_dict(), manifest["frozen_mutation"])
                control = next(item["prepared_case"] for item in pair_calls
                               if item["prepared_case"].trial_kind == "noop")
                self.assertEqual(control.original_payload, control.mutated_payload)
            self.assertEqual(store.load_feedback_state()["total_trials"], 2)
            self.assertEqual(store.load_feedback_state()["total_control_trials"], 2)

    def test_inconclusive_recovery_blocks_second_episode_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs)
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "inconclusive", "reasons": ["source mode changed"],
            }):
                with self.assertRaisesRegex(RuntimeError, "prestate gate"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                self.assertEqual(len(calls), 1)
                with self.assertRaisesRegex(RuntimeError, "blocked"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertEqual(len(calls), 1)
            self.assertFalse((store.path / "trial_0002").exists())
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["status"], "blocked")

    def test_advisory_inconclusive_recovery_is_recorded_and_second_episode_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            output = io.StringIO()
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "inconclusive", "reasons": ["source mode changed"],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "inconclusive", "reasons": ["source mode changed"]},
            }), redirect_stdout(output):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    continue_inconclusive=True,
                )

            self.assertEqual(calls, [1, 2])
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["recovery_gate"], {
                "status": "inconclusive", "reasons": ["source mode changed"],
            })
            self.assertEqual(manifest["comparability_status"], "inconclusive")
            self.assertTrue((store.path / "pairs" / manifest["pair_report"]).is_file())
            self.assertIn("[WARN] pair_0001 first recovery inconclusive", output.getvalue())
            recovery_advisory = next(item for item in manifest["advisories"]
                                     if item["stage"] == "first_recovery")
            self.assertEqual(recovery_advisory["reasons"], ["source mode changed"])

    def test_advisory_resume_after_strict_recovery_block_does_not_reinject_first(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "inconclusive", "reasons": ["source mode changed"],
            }) as recovery, patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "inconclusive", "reasons": ["source mode changed"]},
            }):
                with self.assertRaisesRegex(RuntimeError, "prestate gate"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                self.assertEqual(calls, [1])
                blocked = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(blocked["recovery_gate"]["status"], "inconclusive")

                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    continue_inconclusive=True,
                )

            self.assertEqual(recovery.call_count, 2)
            self.assertEqual(calls, [1, 2])
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["comparability_status"], "inconclusive")
            self.assertTrue((store.path / "pairs" / manifest["pair_report"]).is_file())
            recovery_advisory = next(item for item in manifest["advisories"]
                                     if item["stage"] == "first_recovery")
            self.assertEqual(recovery_advisory["reasons"], ["source mode changed"])

    def test_completed_first_episode_can_resume_without_reinjection(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def interrupt_after_first(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                result = self.write_completed_episode(**kwargs)
                if kwargs["pair_position"] == 1:
                    raise KeyboardInterrupt()
                return result

            runner.run_trial = interrupt_after_first
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "comparable"},
            }):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                )
            self.assertEqual(calls, [1, 2])
            self.assertEqual(store.load_feedback_state()["total_trials"], 1)
            self.assertEqual(store.load_feedback_state()["total_control_trials"], 1)

    def test_inconclusive_pair_report_pauses_later_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "inconclusive", "reasons": ["second recovery drifted"]},
            }):
                with self.assertRaisesRegex(RuntimeError, "campaign is paused"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                with self.assertRaisesRegex(RuntimeError, "campaign is paused"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertEqual(calls, [1, 2])
            self.assertFalse((store.path / "pairs" / "pair_0002.json").exists())
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["comparability_status"], "inconclusive")

    def test_comparable_pair_with_post_exposure_change_still_pauses_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "comparable", "reasons": []},
                "next_pair_gate": {"status": "review_required", "reasons": ["light fault"]},
            }):
                with self.assertRaisesRegex(RuntimeError, "recovery requires review"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
                with self.assertRaisesRegex(RuntimeError, "recovery requires review"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertEqual(calls, [1, 2])
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["comparability_status"], "comparable")

    def test_continue_inconclusive_does_not_bypass_observed_recovery_change(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "inconclusive", "observed_change": True,
                "reasons": ["B_CAN 0x3D6 LH_Aussenlicht_def state changed"],
            }):
                with self.assertRaisesRegex(RuntimeError, "observed change"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                        continue_inconclusive=True,
                    )
            self.assertEqual(calls, [1])

    def test_continue_inconclusive_does_not_bypass_post_exposure_review(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "comparable", "reasons": []},
                "next_pair_gate": {"status": "review_required", "reasons": ["light fault"]},
            }):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "recovery requires review"):
                        runner.run_paired_set(
                            store=store, source_bus="b_can", can_id=0x366,
                            random_seed=366, selector=selector, dbc_path=None,
                            continue_inconclusive=True,
                        )
            self.assertEqual(calls, [1, 2])

    def test_legacy_post_exposure_report_requires_review_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "schema_version": 1,
                "comparability": {"status": "inconclusive", "reasons": ["light fault"]},
                "state_comparison": {"second_recovery": {"status": "inconclusive"}},
            }):
                with self.assertRaisesRegex(RuntimeError, "recovery requires review"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                        continue_inconclusive=True,
                    )
            with self.assertRaisesRegex(RuntimeError, "Latest pair recovery requires review"):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    continue_inconclusive=True,
                )
            self.assertEqual(calls, [1, 2])

    def test_advisory_inconclusive_report_allows_next_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "inconclusive", "reasons": ["background drift"]},
            }):
                for _ in range(2):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                        continue_inconclusive=True,
                    )

            self.assertEqual(calls, [1, 2, 3, 4])
            for index in (1, 2):
                manifest = json.loads((store.path / "pairs" / f"pair_{index:04d}.json").read_text())
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["comparability_status"], "inconclusive")
                self.assertTrue((store.path / "pairs" / manifest["pair_report"]).is_file())

    def test_incomplete_episode_is_never_injected_again(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def abort(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                store.create_trial(kwargs["expected_trial_id"])
                raise RuntimeError("capture lost")

            runner.run_trial = abort
            with self.assertRaisesRegex(RuntimeError, "capture lost"):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                )
            with self.assertRaises(RuntimeError):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                )
            self.assertEqual(calls, [1])

    def test_advisory_mode_still_blocks_incomplete_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def abort(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                store.create_trial(kwargs["expected_trial_id"])
                raise RuntimeError("capture lost")

            runner.run_trial = abort
            with self.assertRaisesRegex(RuntimeError, "capture lost"):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    continue_inconclusive=True,
                )
            with self.assertRaisesRegex(RuntimeError, "cannot be safely resumed"):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    continue_inconclusive=True,
                )
            self.assertEqual(calls, [1])
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["status"], "blocked")

    def test_missing_first_completed_trial_never_reinjects(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            calls = []

            def record(**kwargs):
                calls.append(kwargs["expected_trial_id"])
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": [],
            }), patch("experiment_runner.analyze_trial_pair", return_value={
                "comparability": {"status": "comparable"},
            }):
                runner.run_paired_set(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                )
                manifest_path = store.path / "pairs" / "pair_0001.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["status"] = "first_completed"
                store.write_json(manifest_path, manifest)
                shutil.rmtree(store.path / "trial_0001")
                with self.assertRaisesRegex(RuntimeError, "missing a previously completed trial"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertEqual(calls, [1, 2])

    def test_pair_reprobe_mismatch_fails_before_remote_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            runner.probe_payload = lambda *_args, **_kwargs: bytes.fromhex("FFFFFFFFFFFFFFFF")
            case = noop_case(1, "b_can", 0x366, BASE, 366)
            with self.assertRaisesRegex(RuntimeError, "frozen pair reference"):
                runner.run_trial(
                    store=store, source_bus="b_can", can_id=0x366,
                    random_seed=366, selector=selector, dbc_path=None,
                    control_noop=True, pair_id="pair_0001", pair_position=1,
                    pair_order=["noop", "mutation"], expected_trial_id=1,
                    expected_original_payload=BASE, prepared_case=case,
                    prepared_decision=SimpleNamespace(
                        mode="CONTROL", strategy="NOOP", parent_mutation_id=None,
                        focus_byte=None, reason="control",
                    ),
                )
            self.assertEqual(
                json.loads((store.path / "trial_0001" / "metadata.json").read_text())["status"],
                "failed",
            )
            self.assertEqual(sum(manager.started for manager in runner.managers.values()), 0)

    def test_temporal_noop_uses_same_sequence_length_and_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            temporal = MutationCase(
                mutation_id=1, source_bus="b_can", can_id=0x366,
                operator="TEMPORAL_SEQUENCE", original_payload=BASE,
                mutated_payload=bytes.fromhex("00000020200000F0"), random_seed=366,
                parameters={"sequence": {"frames": ["00000020200000F0", "00000010200000F0"],
                                         "interval_ms": 100}},
            )
            decision = StrategyDecision("EXPLORE", "TEMPORAL", None, None, "test")
            calls = []

            def record(**kwargs):
                calls.append(kwargs)
                return self.write_completed_episode(**kwargs)

            runner.run_trial = record
            with patch.object(selector, "select_mutation", return_value=(temporal, decision)):
                with patch("experiment_runner.recovery_returned_to_prestate", return_value={
                    "status": "stable", "reasons": [],
                }), patch("experiment_runner.analyze_trial_pair", return_value={
                    "comparability": {"status": "comparable"},
                }):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            control = next(call["prepared_case"] for call in calls
                           if call["prepared_case"].trial_kind == "noop")
            sequence = control.parameters["sequence"]
            self.assertEqual(sequence["interval_ms"], 100.0)
            self.assertEqual(sequence["frames"], [BASE.hex().upper()] * 2)
            sender_command = runner._sender_command(
                "b_can", store.experiment_id, control, "/tmp/test/tx.jsonl",
                runner.config["trial"],
            )
            self.assertIn("--control-noop", sender_command)
            sent_metadata = json.loads(sender_command[
                sender_command.index("--mutation-metadata-json") + 1
            ])
            self.assertEqual(sent_metadata["sequence"], sequence)
            manifest = json.loads((store.path / "pairs" / "pair_0001.json").read_text())
            self.assertEqual(manifest["frozen_noop"], control.to_dict())

    def test_temporal_interval_below_contract_is_rejected_before_transmission(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            temporal = MutationCase(
                mutation_id=1, source_bus="b_can", can_id=0x366,
                operator="TEMPORAL_SEQUENCE", original_payload=BASE,
                mutated_payload=bytes.fromhex("00000020200000F0"), random_seed=366,
                parameters={"sequence": {"frames": ["00000020200000F0"],
                                         "interval_ms": 10}},
            )
            decision = StrategyDecision("EXPLORE", "TEMPORAL", None, None, "test")
            with patch.object(selector, "select_mutation", return_value=(temporal, decision)):
                with self.assertRaisesRegex(Exception, "50 ms"):
                    runner.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertFalse((store.path / "trial_0001").exists())
            self.assertFalse((store.path / "pairs" / "pair_0001.json").exists())
            self.assertEqual(sum(manager.started for manager in runner.managers.values()), 0)

    def test_pair_live_probe_refuses_reference_fallback_and_sparse_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, _, _ = self.make_runner(Path(directory))
            runner.probe_payload = ExperimentRunner.probe_payload.__get__(runner)
            runner.config["target"]["reference_payload"] = BASE.hex()
            with self.assertRaisesRegex(RuntimeError, "no frames"):
                runner.probe_payload("b_can", 0x366, require_live=True)
            self.assertEqual(runner.probe_payload("b_can", 0x366), BASE)
            runner.managers["b_can"].stdout = "(1.0) can0 366#00000000200000F0\n"
            with self.assertRaisesRegex(RuntimeError, "stable majority"):
                runner.probe_payload("b_can", 0x366, require_live=True)
            runner.config["target"]["probe_live_payload"] = False
            with self.assertRaisesRegex(Exception, "live source payload probe"):
                runner.probe_payload("b_can", 0x366, require_live=True)

    def test_paired_cli_rejects_explicit_single_trial_combination(self):
        args = build_parser().parse_args(["--paired-sets", "1", "--trials", "1"])
        with self.assertRaisesRegex(Exception, "cannot be combined"):
            run(args)

    def test_second_runner_cannot_plan_over_an_active_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector = self.make_runner(Path(directory))
            other = ExperimentRunner(config(), manager_factory=QuietManager)
            other.probe_payload = lambda *_args, **_kwargs: BASE
            with runner._execution_lock(store):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    other.run_paired_set(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=selector, dbc_path=None,
                    )
            self.assertFalse((store.path / "pairs" / "pair_0001.json").exists())


if __name__ == "__main__":
    unittest.main()
