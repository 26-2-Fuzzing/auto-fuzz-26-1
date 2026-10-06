"""Offline integration checks for the bounded local paired-cycle workflow."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from a5_0x366_mutator import TargetedMutation
from experiment_runner import (
    ExperimentRunner, build_parser, matched_noop_case, paired_order, run, verify_trial_tx,
)
from experiment_store import ExperimentStore
from mutation_feedback import create_trial_feedback
from strategy_selector import TrialStrategySelector
from trial_models import MutationCase


BASE = bytes.fromhex("00000000200000F0")


class TwoCaseGenerator:
    def __init__(self, _dbc_path, original_payload):
        self.original = bytes(original_payload)

    def generate_profile(self, profile, _undefined_max_bits):
        if profile != "all-0x366":
            raise AssertionError("cycle must enumerate the actual family catalogue once")
        first = bytearray(self.original)
        second = bytearray(self.original)
        first[3] = 1
        second[3] = 2
        return [
            TargetedMutation("signal_single", "CASE_A", self.original, bytes(first)),
            TargetedMutation("signal_combination", "CASE_B", self.original, bytes(second)),
        ]


class QuietManager:
    def __init__(self, config):
        self.config = config

    def close(self):
        pass


def config() -> dict:
    return {
        "remote": {"hosts": {bus: {"name": bus}
                             for bus in ("p_can", "b_can", "i_can")}},
        "target": {"probe_live_payload": True, "probe_samples": 3,
                   "reference_payload": BASE.hex()},
        "trial": {"capture_start_delay_seconds": 0},
        "feedback": {},
    }


class LocalCycleTests(unittest.TestCase):
    @staticmethod
    def make_runner(root: Path):
        dbc = root / "A5.dbc"
        dbc.write_text("fake DBC for offline catalogue tests\n", encoding="utf-8")
        settings = config()
        settings["dbc"] = str(dbc)
        settings["experiments_root"] = str(root)
        store = ExperimentStore(root, 42, settings)
        runner = ExperimentRunner(settings, manager_factory=QuietManager)
        runner.probe_payload = lambda *_args, **_kwargs: BASE
        return runner, store, TrialStrategySelector({}), dbc

    @staticmethod
    def persist_completed_pair(
        store: ExperimentStore, *, reuse_pair_id=None,
        comparability_status="comparable", **kwargs,
    ):
        pairs = store.path / "pairs"
        count = (
            int(reuse_pair_id.removeprefix("pair_")) if reuse_pair_id is not None
            else len(list(pairs.glob("pair_[0-9][0-9][0-9][0-9].json"))) + 1
        )
        pair_id = reuse_pair_id or f"pair_{count:04d}"
        first_trial_id = store.next_trial_id()
        order = paired_order(kwargs["random_seed"], store.experiment_id, count)
        mutation = kwargs["scheduled_mutation"]
        noop_id = first_trial_id if order[0] == "noop" else first_trial_id + 1
        control = matched_noop_case(
            noop_id, kwargs["source_bus"], 0x366, BASE,
            kwargs["random_seed"], mutation,
        )
        for position, kind in enumerate(order, start=1):
            trial_id = first_trial_id + position - 1
            trial_dir = store.create_trial(trial_id)
            case = control if kind == "noop" else mutation
            store.write_json(trial_dir / "metadata.json", {
                "status": "completed", "pair_id": pair_id,
                "pair_position": position, "pair_order": order,
                "cycle_entry": kwargs["cycle_entry"],
            })
            store.write_json(trial_dir / "mutation.json", case.to_dict())
            feedback = create_trial_feedback(trial_id, case, [], 0.6)
            store.write_json(trial_dir / "feedback.json", feedback)
            for name in ("anomalies.json", "tx.jsonl", "p_can.jsonl", "b_can.jsonl", "i_can.jsonl"):
                (trial_dir / name).write_text("{}\n", encoding="utf-8")
            store.record_completed_trial(case, feedback)
        report = {
            "pair_id": pair_id,
            "comparability": {"status": comparability_status},
        }
        if comparability_status == "inconclusive":
            report["tx_comparison"] = {
                "mutation": {"status": "valid"},
                "noop": {"status": "valid"},
            }
        store.write_json(pairs / f"{pair_id}.json", {
            "pair_id": pair_id,
            "status": "completed",
            "comparability_status": comparability_status,
            "pair_report": f"{pair_id}_report.json",
            "cycle_entry": kwargs["cycle_entry"],
            "frozen_mutation": kwargs["scheduled_mutation"].to_dict(),
            "first_trial_id": first_trial_id,
            "second_trial_id": first_trial_id + 1,
            "pair_order": order,
        })
        store.write_json(pairs / f"{pair_id}_report.json", report)
        return report

    def freeze_inconclusive_pair_before_cycle_ledger(
        self, runner, store, selector, dbc,
    ):
        calls = []

        def interrupted(**kwargs):
            index = kwargs["cycle_entry"]["entry_index"]
            calls.append(index)
            report = self.persist_completed_pair(
                comparability_status="inconclusive" if index == 0 else "comparable",
                **kwargs,
            )
            if index == 0:
                raise KeyboardInterrupt()
            return report

        runner.run_paired_set = interrupted
        with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
            with self.assertRaises(KeyboardInterrupt):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
        plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
        self.assertEqual(plan["cursor"], 0)
        return calls

    def test_cycle_cap_and_completion_keep_cursor_after_each_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(**kwargs)

            runner.run_paired_set = completed
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                first = runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
                plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
                self.assertEqual(first["status"], "active")
                self.assertEqual(first["completed_count"], 1)
                self.assertEqual(plan["cursor"], 1)
                self.assertEqual(plan["scheduled_count"], 2)
                second = runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["completed_count"], 2)
            self.assertEqual(calls, [0, 1])
            final = json.loads((store.path / "pairs" / "cycle.json").read_text())
            self.assertEqual([item["entry_index"] for item in final["completed_pairs"]], [0, 1])

    def test_crash_after_pair_report_reconciles_without_repeating_first_case(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def interrupted(**kwargs):
                index = kwargs["cycle_entry"]["entry_index"]
                calls.append(index)
                report = self.persist_completed_pair(**kwargs)
                if index == 0:
                    raise KeyboardInterrupt()
                return report

            runner.run_paired_set = interrupted
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
                frozen = json.loads((store.path / "pairs" / "cycle.json").read_text())
                self.assertEqual(frozen["cursor"], 0)
                resumed = runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(calls, [0, 1])
            self.assertEqual(resumed["reconciled_this_invocation"], 1)
            self.assertEqual(resumed["status"], "completed")

    def test_advisory_resume_reconciles_inconclusive_pair_without_repeating_tx(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = self.freeze_inconclusive_pair_before_cycle_ledger(
                runner, store, selector, dbc,
            )
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                resumed = runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                    continue_inconclusive=True,
                )
            plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
            self.assertEqual(calls, [0, 1])
            self.assertEqual(resumed["reconciled_this_invocation"], 1)
            self.assertEqual(plan["cursor"], 2)
            self.assertEqual(
                plan["completed_pairs"][0]["comparability_status"], "inconclusive",
            )
            self.assertEqual(plan["completed_pairs"][1]["entry_index"], 1)

    def test_strict_resume_keeps_completed_inconclusive_pair_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = self.freeze_inconclusive_pair_before_cycle_ledger(
                runner, store, selector, dbc,
            )
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                with self.assertRaisesRegex(RuntimeError, "inconclusive|paused"):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
            plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
            self.assertEqual(calls, [0])
            self.assertEqual(plan["cursor"], 0)

    def test_advisory_resume_rejects_ledger_status_mismatch_before_next_tx(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(
                    comparability_status="inconclusive", **kwargs,
                )

            runner.run_paired_set = completed
            cycle_path = store.path / "pairs" / "cycle.json"
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                    continue_inconclusive=True,
                )
                plan = json.loads(cycle_path.read_text())
                self.assertEqual(
                    plan["completed_pairs"][0]["comparability_status"], "inconclusive",
                )
                del plan["completed_pairs"][0]["comparability_status"]
                store.write_json(cycle_path, plan)
                with self.assertRaises(RuntimeError):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                        continue_inconclusive=True,
                    )
            self.assertEqual(calls, [0])
            self.assertFalse((store.path / "pairs" / "pair_0002.json").exists())

    def test_advisory_resume_rejects_invalid_or_missing_tx_status_before_next_tx(self):
        for status in ("invalid", "missing"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                runner, store, selector, dbc = self.make_runner(Path(directory))
                calls = self.freeze_inconclusive_pair_before_cycle_ledger(
                    runner, store, selector, dbc,
                )
                report_path = store.path / "pairs" / "pair_0001_report.json"
                report = json.loads(report_path.read_text())
                if status == "invalid":
                    report["tx_comparison"]["mutation"]["status"] = "invalid"
                else:
                    del report["tx_comparison"]["noop"]["status"]
                store.write_json(report_path, report)
                with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                    with self.assertRaisesRegex(RuntimeError, "unverified TX evidence"):
                        runner.run_paired_cycle(
                            store=store, source_bus="b_can", random_seed=366,
                            selector=selector, dbc_path=dbc, max_sets=1,
                            continue_inconclusive=True,
                        )
                plan = json.loads((store.path / "pairs" / "cycle.json").read_text())
                self.assertEqual(plan["cursor"], 0)
                self.assertEqual(calls, [0])
                self.assertFalse((store.path / "pairs" / "pair_0002.json").exists())

    def test_advisory_resume_rejects_missing_trial_evidence_before_next_tx(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(
                    comparability_status="inconclusive", **kwargs,
                )

            runner.run_paired_set = completed
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                    continue_inconclusive=True,
                )
                (store.path / "trial_0001" / "feedback.json").unlink()
                with self.assertRaisesRegex(RuntimeError, "evidence is missing"):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                        continue_inconclusive=True,
                    )
            self.assertEqual(calls, [0])
            self.assertFalse((store.path / "pairs" / "pair_0002.json").exists())

    def test_pending_pair_is_resumed_through_pair_safety_path(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            # Make the second call update the existing pending manifest rather
            # than allocating a new pair; this isolates cycle cursor behavior.
            def resume_stub(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                pair_path = store.path / "pairs" / "pair_0001.json"
                if len(calls) == 1:
                    store.write_json(pair_path, {
                        "pair_id": "pair_0001", "status": "first_completed",
                        "cycle_entry": kwargs["cycle_entry"],
                    })
                    raise KeyboardInterrupt()
                return self.persist_completed_pair(reuse_pair_id="pair_0001", **kwargs)

            runner.run_paired_set = resume_stub
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
                resumed = runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
            self.assertEqual(calls, [0, 0])
            self.assertEqual(resumed["completed_count"], 1)

    def test_cycle_rejects_prior_standalone_trials_before_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            store.create_trial(1)
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                with self.assertRaisesRegex(RuntimeError, "without prior trials"):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
            self.assertFalse((store.path / "pairs" / "cycle.json").exists())

    def test_missing_completed_ledger_trial_blocks_next_case(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(**kwargs)

            runner.run_paired_set = completed
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
                shutil.rmtree(store.path / "trial_0001")
                with self.assertRaisesRegex(RuntimeError, "evidence is missing"):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
            self.assertEqual(calls, [0])

    def test_orphan_trial_without_current_pair_manifest_blocks_reinjection(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(**kwargs)

            runner.run_paired_set = completed
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
                store.create_trial(3)  # TX may have begun; pair_0002.json is missing.
                with self.assertRaisesRegex(RuntimeError, "without a linked pair manifest"):
                    runner.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
            self.assertEqual(calls, [0])

    def test_changed_remote_config_rejects_cycle_resume_before_next_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, store, selector, dbc = self.make_runner(Path(directory))
            calls = []

            def completed(**kwargs):
                calls.append(kwargs["cycle_entry"]["entry_index"])
                return self.persist_completed_pair(**kwargs)

            runner.run_paired_set = completed
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
                changed = json.loads(json.dumps(runner.config))
                changed["remote"]["hosts"]["b_can"]["name"] = "different-board"
                resumed = ExperimentRunner(changed, manager_factory=QuietManager)
                resumed.probe_payload = lambda *_args, **_kwargs: BASE
                with self.assertRaisesRegex(RuntimeError, "different runner settings"):
                    resumed.run_paired_cycle(
                        store=store, source_bus="b_can", random_seed=366,
                        selector=selector, dbc_path=dbc, max_sets=1,
                    )
            self.assertEqual(calls, [0])
            self.assertFalse((store.path / "pairs" / "pair_0002.json").exists())

    def test_cycle_preview_is_read_only_and_reports_bounded_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbc = root / "A5.dbc"
            dbc.write_text("fake DBC\n", encoding="utf-8")
            settings = config()
            settings["dbc"] = str(dbc)
            settings["experiments_root"] = str(root / "experiments")
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(settings), encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--paired-cycle", "--cycle-max-sets", "1",
            ])
            output = io.StringIO()
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator), \
                 patch("experiment_runner.ExperimentRunner", side_effect=AssertionError("SSH opened")), \
                 redirect_stdout(output):
                self.assertEqual(run(args), 0)
            self.assertIn("2 distinct pairs", output.getvalue())
            self.assertIn("this invocation cap 1 pairs", output.getvalue())
            self.assertFalse((root / "experiments").exists())

    def test_existing_cycle_preview_reports_actual_remaining_without_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, store, selector, dbc = self.make_runner(root)
            runner.run_paired_set = lambda **kwargs: self.persist_completed_pair(**kwargs)
            with patch("paired_cycle.A5BlinkmodiMutator", TwoCaseGenerator):
                runner.run_paired_cycle(
                    store=store, source_bus="b_can", random_seed=366,
                    selector=selector, dbc_path=dbc, max_sets=1,
                )
                config_path = root / "runner.yaml"
                config_path.write_text(json.dumps(runner.config), encoding="utf-8")
                args = build_parser().parse_args([
                    "--config", str(config_path), "--paired-cycle", "--experiment-id", "42",
                ])
                output = io.StringIO()
                with patch("experiment_runner.ExperimentRunner",
                           side_effect=AssertionError("SSH opened")), redirect_stdout(output):
                    self.assertEqual(run(args), 0)
            self.assertIn("Frozen experiment 42: 1/2 processed pairs "
                          "(1 comparable, 0 inconclusive), 1 remaining",
                          output.getvalue())

    def test_cycle_rejects_enabled_feedback_even_in_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config()
            settings["feedback"] = {"enabled": True}
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(settings), encoding="utf-8")
            args = build_parser().parse_args(["--config", str(config_path), "--paired-cycle"])
            with self.assertRaisesRegex(Exception, "feedback.enabled: false"):
                run(args)
            self.assertFalse((root / "experiments").exists())

    def test_cycle_temporal_tx_manifest_requires_order_and_two_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = bytes.fromhex("00000020200000F0")
            another = bytes.fromhex("00000010200000F0")
            case = MutationCase(
                mutation_id=1, source_bus="b_can", can_id=0x366,
                operator="TEMPORAL_SEQUENCE", original_payload=BASE,
                mutated_payload=changed, random_seed=366,
                parameters={"cycle_entry_index": 0, "sequence": {
                    "frames": [changed.hex().upper(), another.hex().upper()],
                    "interval_ms": 100,
                }},
            )
            control = matched_noop_case(2, "b_can", 0x366, BASE, 366, case)
            self.assertEqual(control.parameters["cycle_entry_index"], 0)

            def records(mutation: MutationCase, frames: list[str]):
                kind = mutation.trial_kind
                original = BASE.hex().upper()
                result = [{
                    "record_type": "tx_session_start", "tx_session_id": "s1",
                    "trial_contract_version": 1, "trial_kind": kind,
                    "execute": True, "experiment_id": "42",
                    "campaign": {"enabled": True, "normal_data_hex": original},
                    "mutation": {"trial_mutation_id": mutation.mutation_id,
                                 "control_noop": kind == "noop"},
                }]
                result.append({"record_type": "can_tx", "status": "sent", "phase": "normal",
                               "arbitration_id": 0x366, "data_hex": original,
                               "trial_kind": kind})
                result.extend({"record_type": "can_tx", "status": "sent", "phase": "mutation",
                               "arbitration_id": 0x366, "data_hex": payload,
                               "trial_kind": kind} for payload in frames)
                result.append({"record_type": "can_tx", "status": "sent", "phase": "recovery",
                               "kind": "restore", "arbitration_id": 0x366,
                               "data_hex": original, "trial_kind": kind})
                result.append({
                    "record_type": "tx_session_end", "tx_session_id": "s1",
                    "trial_contract_version": 1, "trial_kind": kind,
                    "execute": True, "experiment_id": "42", "status": "completed",
                    "phase_sent": {"normal": 1, "mutation": len(frames)},
                    "restore": {"status": "sent", "sent": 1},
                })
                return result

            tx = root / "tx.jsonl"
            def check(mutation, frames):
                tx.write_text("".join(json.dumps(item) + "\n"
                                      for item in records(mutation, frames)), encoding="utf-8")
                verify_trial_tx(tx, mutation=mutation, experiment_id=42)

            with self.assertRaisesRegex(ValueError, "two complete patterns"):
                check(case, [changed.hex().upper(), another.hex().upper()])
            with self.assertRaisesRegex(ValueError, "sequence order"):
                check(case, [changed.hex().upper(), changed.hex().upper(),
                             another.hex().upper(), changed.hex().upper()])
            check(case, [changed.hex().upper(), another.hex().upper()] * 2)
            check(control, [BASE.hex().upper()] * 4)


if __name__ == "__main__":
    unittest.main()
