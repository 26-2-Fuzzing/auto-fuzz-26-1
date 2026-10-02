from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from distributed_runner import (
    _pair_digest,
    _result_zip,
    _probe_live_payload,
    _sender_command,
    analyze_distributed_trial,
    analyze_distributed_pair,
    inject_trial,
    load_result_bundle,
    load_trial_package,
    prepare_trial_package,
    prepare_pair_package,
    prepare_pair_next_package,
    receive_trial,
)
from experiment_store import ExperimentStore
from strategy_selector import StrategyDecision
from trial_models import MutationCase


BASE = bytes.fromhex("00000000200000F0")


def config(root: Path) -> dict:
    return {
        "experiments_root": str(root),
        "dbc": None,
        "sender_config": "sender_trial.yaml",
        "target": {
            "channel": "can0",
            "reference_payload": BASE.hex(),
            "probe_live_payload": True,
        },
        "trial": {
            "baseline_seconds": 0.5,
            "normal_seconds": 0.5,
            "mutation_seconds": 0.5,
            "post_seconds": 0.2,
            "interval_ms": 50,
            "runner_timeout_seconds": 10,
        },
        "distributed": {
            "receiver_lead_seconds": 1,
            "receiver_tail_seconds": 1,
        },
        "anomaly_thresholds": {
            "minimum_baseline_frames": 5,
            "minimum_timing_intervals": 3,
            "new_message_minimum_frames": 3,
            "payload_novel_ratio": 0.2,
        },
        "feedback": {
            "interesting_score_threshold": 0.6,
            "bit_operation_ratio": 1.0,
            "max_operations": 1,
            "no_anomaly": {"exploration_probability": 1.0},
            "interesting": {"exploitation_probability": 1.0},
        },
    }


def tx_jsonl(trial_kind: str = "mutation", *, status: str = "completed") -> bytes:
    records = [
        {"record_type": "tx_session_start", "trial_contract_version": 1, "trial_kind": trial_kind},
        {"record_type": "tx_phase", "phase": "baseline", "event": "start", "wall_time_ns": 1_000_000_000},
        {"record_type": "tx_phase", "phase": "baseline", "event": "end", "wall_time_ns": 1_500_000_000},
        {"record_type": "tx_phase", "phase": "normal", "event": "start", "wall_time_ns": 1_500_000_000},
        {"record_type": "tx_phase", "phase": "normal", "event": "end", "wall_time_ns": 2_000_000_000},
        {"record_type": "tx_phase", "phase": "mutation", "event": "start", "wall_time_ns": 2_000_000_000},
        {"record_type": "tx_phase", "phase": "mutation", "event": "end", "wall_time_ns": 2_500_000_000},
        {"record_type": "tx_phase", "phase": "recovery", "event": "start", "wall_time_ns": 2_500_000_000},
        {"record_type": "tx_phase", "phase": "recovery", "event": "end", "wall_time_ns": 2_700_000_000},
        {"record_type": "tx_session_end", "status": status,
         "trial_contract_version": 1, "trial_kind": trial_kind},
    ]
    return ("".join(json.dumps(item) + "\n" for item in records)).encode()


def rx_jsonl(
    bus: str, experiment_id: int, changed: bool = False,
    end_ns: int = 2_800_000_000,
) -> bytes:
    records = [{
        "record_type": "session_start", "wall_time_ns": 900_000_000,
        "experiment_id": str(experiment_id), "bus": bus,
    }]
    for index in range(10):
        records.append({
            "record_type": "can_rx", "wall_time_ns": 1_000_000_000 + index * 40_000_000,
            "experiment_id": str(experiment_id), "bus": bus,
            "arbitration_id": 0x123, "is_extended_id": False,
            "is_error_frame": False, "is_remote_frame": False, "data_hex": "00",
        })
    for index in range(10):
        records.append({
            "record_type": "can_rx", "wall_time_ns": 2_000_000_000 + index * 40_000_000,
            "experiment_id": str(experiment_id), "bus": bus,
            "arbitration_id": 0x123, "is_extended_id": False,
            "is_error_frame": False, "is_remote_frame": False,
            "data_hex": "01" if changed else "00",
        })
    records.append({
        "record_type": "session_end", "wall_time_ns": end_ns,
        "experiment_id": str(experiment_id), "bus": bus,
    })
    return ("".join(json.dumps(item) + "\n" for item in records)).encode()


class DistributedPackageTests(unittest.TestCase):
    def test_existing_output_is_rejected_before_trial_is_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing.zip"
            output.write_bytes(b"user-owned package")
            with self.assertRaisesRegex(FileExistsError, "existing package"):
                prepare_trial_package(
                    config=config(root / "experiments"), config_path=None,
                    experiment_id=42, target_id=0x366, random_seed=366,
                    mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=output,
                )
            trial = root / "experiments" / "experiment_0042" / "trial_0001"
            self.assertFalse(trial.exists())
            self.assertEqual(output.read_bytes(), b"user-owned package")

    def test_package_is_immutable_and_digest_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = prepare_trial_package(
                config=config(root / "experiments"), config_path=None,
                experiment_id=42, target_id=0x366, random_seed=366,
                mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            plan, mutation = load_trial_package(package)
            self.assertEqual(plan["receiver_buses"], ["P_CAN", "I_CAN"])
            self.assertEqual(mutation.source_bus, "b_can")
            self.assertEqual(plan["trial_contract_version"], 1)
            self.assertEqual(plan["timing"]["interval_ms"], 50)
            self.assertEqual(plan["analysis_config"]["minimum_baseline_frames"], 5)

            with zipfile.ZipFile(package, "r") as source:
                plan_bytes = source.read("trial_plan.json")
                mutation_data = json.loads(source.read("mutation.json"))
            mutation_data["mutated_payload"] = BASE.hex()
            bad = root / "tampered.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("trial_plan.json", plan_bytes)
                archive.writestr("mutation.json", json.dumps(mutation_data))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_trial_package(bad)

    def test_pending_trial_prevents_next_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial1.zip",
            )
            with self.assertRaisesRegex(RuntimeError, "not completed"):
                prepare_trial_package(
                    config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                    random_seed=366, mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "trial2.zip",
                )

    def test_noop_control_keeps_original_and_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = prepare_trial_package(
                config=config(root / "experiments"), config_path=None,
                experiment_id=42, target_id=0x366, random_seed=366,
                mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "control.zip", control_noop=True,
            )
            plan, control = load_trial_package(package)
            self.assertEqual(plan["trial_kind"], "noop")
            self.assertEqual(control.original_payload, control.mutated_payload)
            self.assertEqual(control.mutation_uid, "CTRL-000001")
            command = _sender_command(
                plan=plan, mutation=control,
                node={"project_dir": "/lab", "python": "python3"},
                remote_tx="/tmp/tx.jsonl",
            )
            self.assertIn("--control-noop", command)
            self.assertEqual(command[command.index("--trial-contract-version") + 1], "1")
            self.assertEqual(command[command.index("--interval-ms") + 1], "50.0")

    def test_unsafe_trial_timing_is_rejected_before_store_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            cfg["trial"]["normal_seconds"] = 5
            cfg["trial"]["mutation_seconds"] = 5
            with self.assertRaisesRegex(Exception, "limits mutation exposure"):
                prepare_trial_package(
                    config=cfg, config_path=None, experiment_id=42,
                    target_id=0x366, random_seed=366,
                    mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "unsafe.zip",
                )
            self.assertFalse((root / "experiments" / "experiment_0042").exists())


class DistributedPairTests(unittest.TestCase):
    @staticmethod
    def complete_prepared_trial(root: Path, trial_id: int) -> None:
        path = root / "experiments" / "experiment_0042" / f"trial_{trial_id:04d}" / "metadata.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["status"] = "completed"
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_pair_freezes_payload_and_balances_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, manifest_path = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            first_plan, first_case = load_trial_package(first)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(first_plan["pair_id"], "pair_0001")
            self.assertEqual(first_plan["pair_position"], 1)
            self.assertEqual(first_plan["pair_role"], first_case.trial_kind)
            self.assertEqual(first_plan["pair_order"], manifest["pair_order"])
            frozen = manifest["frozen_mutation"]
            self.assertEqual(first_case.original_payload, BASE)
            self.assertEqual(frozen["original_payload"].replace(" ", ""), BASE.hex().upper())

            with self.assertRaisesRegex(RuntimeError, "Analyze the first"):
                prepare_pair_next_package(
                    config=cfg, config_path=None, first_package=first,
                    output=root / "second.zip",
                )
            self.complete_prepared_trial(root, 1)
            with patch("pair_analysis.recovery_returned_to_prestate", return_value={
                "status": "inconclusive", "reasons": ["state drift"]
            }):
                with self.assertRaisesRegex(RuntimeError, "recovery is inconclusive"):
                    prepare_pair_next_package(
                        config=cfg, config_path=None, first_package=first,
                        output=root / "second.zip",
                    )
            self.assertFalse((root / "second.zip").exists())
            with patch("pair_analysis.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": []
            }):
                second = prepare_pair_next_package(
                    config=cfg, config_path=None, first_package=first,
                    output=root / "second.zip",
                )
            second_plan, second_case = load_trial_package(second)
            self.assertEqual(second_plan["pair_position"], 2)
            self.assertNotEqual(first_case.trial_kind, second_case.trial_kind)
            self.assertEqual(second_case.original_payload, BASE)
            mutated = first_case if first_case.trial_kind == "mutation" else second_case
            self.assertEqual(mutated.to_dict(), frozen)
            self.assertEqual(second_plan["pair_role"], second_case.trial_kind)
            self.assertEqual(json.loads(manifest_path.read_text())["status"], "second_prepared")

            self.complete_prepared_trial(root, 2)
            with patch("pair_analysis.analyze_trial_pair", return_value={
                "pair_id": "pair_0001",
                "comparability": {"status": "inconclusive", "reasons": [
                    "mutation: source_target_prestate_unobserved",
                    "noop: source_target_prestate_unobserved",
                ]},
                "state_comparison": {
                    "first_recovery": {"status": "stable"},
                    "second_recovery": {"status": "stable"},
                },
                "verification_status": "unverified",
                "feedback_eligible": False,
            }) as analyzer:
                report = analyze_distributed_pair(
                    config=cfg, config_path=None,
                    first_package=first, second_package=second,
                )
            self.assertEqual(report["comparability"]["status"], "inconclusive")
            report_path = manifest_path.parent / "pair_0001_report.json"
            self.assertTrue(report_path.is_file())
            self.assertEqual(analyzer.call_args.kwargs["pair_id"], "pair_0001")

            interrupted = json.loads(manifest_path.read_text(encoding="utf-8"))
            interrupted["status"] = "second_prepared"
            manifest_path.write_text(json.dumps(interrupted), encoding="utf-8")
            with patch("pair_analysis.analyze_trial_pair", return_value=report):
                self.assertEqual(analyze_distributed_pair(
                    config=cfg, config_path=None,
                    first_package=first, second_package=second,
                ), report)
            self.assertEqual(json.loads(manifest_path.read_text())["status"], "reported")

            unstable = json.loads(report_path.read_text(encoding="utf-8"))
            unstable["state_comparison"]["second_recovery"]["status"] = "inconclusive"
            report_path.write_text(json.dumps(unstable), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "recovery is not demonstrably stable"):
                prepare_pair_package(
                    config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                    random_seed=366, mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "next-first.zip",
                    acknowledge_unverified_source_baseline=True,
                )
            report_path.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "acknowledge-unverified-source-baseline"):
                prepare_pair_package(
                    config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                    random_seed=366, mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "next-first.zip",
                )

            next_first, next_manifest_path = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "next-first.zip",
                acknowledge_unverified_source_baseline=True,
            )
            next_plan, _ = load_trial_package(next_first)
            self.assertEqual(next_plan["pair_id"], "pair_0002")
            self.assertEqual(
                json.loads(next_manifest_path.read_text())["pair_order"],
                list(reversed(manifest["pair_order"])),
            )

    def test_pair_manifest_and_config_drift_are_rejected_before_second(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, manifest_path = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            self.complete_prepared_trial(root, 1)
            manifest = json.loads(manifest_path.read_text())
            manifest["frozen_mutation"]["mutated_payload"] = BASE.hex().upper()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
                prepare_pair_next_package(
                    config=cfg, config_path=None, first_package=first,
                    output=root / "second.zip",
                )
            self.assertFalse((root / "second.zip").exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, _ = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            self.complete_prepared_trial(root, 1)
            drifted = config(root / "experiments")
            drifted["trial"]["normal_seconds"] = 0.7
            with patch("pair_analysis.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": []
            }):
                with self.assertRaisesRegex(Exception, "config changed"):
                    prepare_pair_next_package(
                        config=drifted, config_path=None, first_package=first,
                        output=root / "second.zip",
                    )
            self.assertFalse((root / "second.zip").exists())

    def test_pair_rejects_disabled_live_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            cfg["target"]["probe_live_payload"] = False
            with self.assertRaisesRegex(Exception, "live B-CAN payload probe"):
                prepare_pair_package(
                    config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                    random_seed=366, mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "first.zip",
                )
            self.assertFalse((root / "first.zip").exists())

    def test_pair_metadata_survives_trial_analysis_and_blocks_intervening_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, _ = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            tx, p, i = DistributedAnalysisTests().make_results(
                root, first, ("same-clock", "same-clock", "same-clock")
            )
            analyze_distributed_trial(
                config=cfg, config_path=None, package=first,
                tx_result=tx, p_result=p, i_result=i,
            )
            metadata_path = root / "experiments" / "experiment_0042" / "trial_0001" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["pair_id"], "pair_0001")
            self.assertEqual(metadata["pair_position"], 1)
            self.assertEqual(metadata["pair_role"], metadata["trial_kind"])
            with self.assertRaisesRegex(RuntimeError, "unfinished"):
                prepare_trial_package(
                    config=cfg, config_path=None, experiment_id=42,
                    target_id=0x366, random_seed=366, mutation_profile=None,
                    undefined_max_bits=2, base_payload=BASE, output=root / "intervening.zip",
                )
            self.assertFalse((root / "intervening.zip").exists())

    def test_paired_live_probe_requires_three_samples_and_majority(self) -> None:
        class ProbeManager:
            def __init__(self, output: str):
                self.output = output
                self.command = None

            def run(self, command, **_kwargs):
                self.command = command
                return SimpleNamespace(stdout=self.output)

        plan = {"target_id": "0x366", "pair_id": "pair_0001",
                "probe_samples": 3, "probe_timeout_seconds": 6,
                "channel": "can0"}
        one = "(1.0) can0 366#00000000200000F0\n"
        two = "(1.1) can0 366#00000000200000F1\n"
        incomplete = ProbeManager(one + two)
        with self.assertRaisesRegex(RuntimeError, "unstable or incomplete"):
            _probe_live_payload(incomplete, plan)
        self.assertEqual(incomplete.command[incomplete.command.index("-n") + 1], "3")
        tie = ProbeManager(one + two + "(1.2) can0 366#00000000200000F2\n")
        with self.assertRaisesRegex(RuntimeError, "unstable or incomplete"):
            _probe_live_payload(tie, plan)
        majority = ProbeManager(one + one + two)
        self.assertEqual(_probe_live_payload(majority, plan), BASE)

    def test_temporal_pair_noop_uses_frozen_mutation_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            sequence = {"name": "TEST", "interval_ms": 100,
                        "frames": ["00000000200000F1", "00000000200000F2", "00000000200000F3"]}
            frozen = MutationCase(
                mutation_id=1, source_bus="b_can", can_id=0x366,
                operator="TEMPORAL_SEQUENCE", original_payload=BASE,
                mutated_payload=bytes.fromhex("00000000200000F1"), random_seed=366,
                parameters={"sequence": sequence},
            )
            decision = StrategyDecision("EXPLORE", "INITIAL", None, None, "test fixture")
            with patch("distributed_runner.TrialStrategySelector.select_mutation", return_value=(frozen, decision)):
                first, _ = prepare_pair_package(
                    config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                    random_seed=366, mutation_profile=None, undefined_max_bits=2,
                    base_payload=BASE, output=root / "first.zip",
                )
            self.complete_prepared_trial(root, 1)
            with patch("pair_analysis.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": []
            }):
                second = prepare_pair_next_package(
                    config=cfg, config_path=None, first_package=first,
                    output=root / "second.zip",
                )
            cases = [load_trial_package(first)[1], load_trial_package(second)[1]]
            control = next(case for case in cases if case.trial_kind == "noop")
            self.assertEqual(control.parameters["sequence"]["interval_ms"], 100.0)
            self.assertEqual(control.parameters["sequence"]["frames"], [BASE.hex().upper()] * 3)
            self.assertEqual(len(control.parameters["sequence"]["frame_signals_changed"]), 3)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            too_fast = MutationCase(
                mutation_id=1, source_bus="b_can", can_id=0x366,
                operator="TEMPORAL_SEQUENCE", original_payload=BASE,
                mutated_payload=bytes.fromhex("00000000200000F1"), random_seed=366,
                parameters={"sequence": {"interval_ms": 10, "frames": ["00000000200000F1"]}},
            )
            with patch("distributed_runner.TrialStrategySelector.select_mutation", return_value=(too_fast, decision)):
                with self.assertRaisesRegex(Exception, "interval >= 50 ms"):
                    prepare_pair_package(
                        config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                        random_seed=366, mutation_profile=None, undefined_max_bits=2,
                        base_payload=BASE, output=root / "first.zip",
                    )
            self.assertFalse((root / "first.zip").exists())

    def test_paired_injection_never_retries_after_uncertain_remote_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, _ = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            result_dir = root / "results"
            node_cfg = {"node": {"bus": "b_can", "ssh": {"host": "fake"},
                                 "results_dir": str(result_dir)}}
            calls = []
            def cannot_connect(_ssh):
                calls.append(1)
                raise OSError("connection lost")

            with self.assertRaisesRegex(OSError, "connection lost"):
                inject_trial(
                    package=first, config=node_cfg, output=None, execute=True,
                    manager_factory=cannot_connect,
                )
            marker = result_dir / "experiment_0042_trial_0001_b_can_attempt_started.json"
            self.assertTrue(marker.is_file())
            self.assertEqual(json.loads(marker.read_text())["package_digest"],
                             load_trial_package(first)[0]["package_digest"])
            with self.assertRaisesRegex(FileExistsError, "existing local evidence"):
                inject_trial(
                    package=first, config=node_cfg, output=None, execute=True,
                    manager_factory=cannot_connect,
                )
            self.assertEqual(len(calls), 1)

    def test_first_package_failure_preserves_frozen_pair_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            with patch("distributed_runner.prepare_trial_package", side_effect=OSError("disk stopped")):
                with self.assertRaisesRegex(OSError, "disk stopped"):
                    prepare_pair_package(
                        config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                        random_seed=366, mutation_profile=None, undefined_max_bits=2,
                        base_payload=BASE, output=root / "first.zip",
                    )
            manifest_path = root / "experiments" / "experiment_0042" / "pairs" / "pair_0001.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "prepare_failed")
            self.assertIsNone(manifest["first_package_digest"])
            self.assertEqual(manifest["frozen_mutation"]["original_payload"].replace(" ", ""), BASE.hex().upper())
            self.assertFalse((root / "first.zip").exists())

    def test_existing_first_package_reconciles_interrupted_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            first, manifest_path = prepare_pair_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "first.zip",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({"status": "preparing", "first_package_digest": None})
            manifest["pair_digest"] = _pair_digest(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.complete_prepared_trial(root, 1)
            with patch("pair_analysis.recovery_returned_to_prestate", return_value={
                "status": "stable", "reasons": []
            }):
                second = prepare_pair_next_package(
                    config=cfg, config_path=None, first_package=first,
                    output=root / "second.zip",
                )
            self.assertTrue(second.is_file())
            recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["status"], "second_prepared")
            self.assertEqual(recovered["first_package_digest"],
                             load_trial_package(first)[0]["package_digest"])


class DistributedAnalysisTests(unittest.TestCase):
    def make_results(
        self, root: Path, package: Path, references=(None, None, None),
        p_end_ns: int = 2_800_000_000,
    ):
        plan, mutation = load_trial_package(package)
        clock = {"offset_ms": 0.0, "round_trip_ms": 1.0}
        tx = _result_zip(
            output=root / "tx.zip", plan=plan, role="tx", bus="b_can",
            data_name="tx.jsonl", data=tx_jsonl(mutation.trial_kind), clock=clock,
            clock_reference_id=references[0],
        )
        p = _result_zip(
            output=root / "p.zip", plan=plan, role="rx", bus="p_can",
            data_name="p_can.jsonl", data=rx_jsonl("p_can", 42, changed=True, end_ns=p_end_ns), clock=clock,
            clock_reference_id=references[1],
        )
        i = _result_zip(
            output=root / "i.zip", plan=plan, role="rx", bus="i_can",
            data_name="i_can.jsonl", data=rx_jsonl("i_can", 42), clock=clock,
            clock_reference_id=references[2],
        )
        return tx, p, i

    def test_analysis_requires_b_tx_and_both_rx_then_commits_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial1.zip",
            )
            tx, p, i = self.make_results(root, package)
            feedback = analyze_distributed_trial(
                config=cfg, config_path=None, package=package,
                tx_result=tx, p_result=p, i_result=i,
            )
            store = ExperimentStore(root / "experiments", 42, {})
            state = store.load_feedback_state()
            trial = store.path / "trial_0001"
            # One short baseline window cannot validate a payload change; a
            # high raw anomaly score alone must not drive the next mutation.
            self.assertFalse(feedback["interesting"])
            self.assertEqual(feedback["verification_status"], "none")
            self.assertEqual(state["total_trials"], 1)
            self.assertTrue((trial / "p_can.jsonl").is_file())
            self.assertTrue((trial / "i_can.jsonl").is_file())
            self.assertFalse((trial / "b_can.jsonl").exists())
            metadata = json.loads((trial / "metadata.json").read_text())
            self.assertEqual(metadata["clock_alignment_status"], "unverified_independent_references")
            anomalies = json.loads((trial / "anomalies.json").read_text())
            self.assertEqual(anomalies["clock_quality"]["p_can"]["status"], "invalid_reference")

            second = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial2.zip",
            )
            _, next_mutation = load_trial_package(second)
            self.assertIsNone(next_mutation.parent_mutation_id)

    def test_shared_reference_is_required_for_cross_host_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            tx, p, i = self.make_results(root, package, ("clock-a", "clock-a", "clock-b"))
            analyze_distributed_trial(
                config=cfg, config_path=None, package=package,
                tx_result=tx, p_result=p, i_result=i,
            )
            trial = root / "experiments" / "experiment_0042" / "trial_0001"
            metadata = json.loads((trial / "metadata.json").read_text())
            self.assertIsNone(metadata["clock_alignment_reference_id"])
            self.assertEqual(metadata["capture_quality"]["p_can"]["phase_coverage"], "unverified_clock_reference")
            anomalies = json.loads((trial / "anomalies.json").read_text())
            self.assertEqual(anomalies["clock_quality"]["p_can"]["status"], "invalid_reference")

    def test_noop_result_is_control_not_mutation_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "control.zip", control_noop=True,
            )
            tx, p, i = self.make_results(root, package, ("same", "same", "same"))
            feedback = analyze_distributed_trial(
                config=cfg, config_path=None, package=package,
                tx_result=tx, p_result=p, i_result=i,
            )
            self.assertEqual(feedback["verification_status"], "control")
            store = ExperimentStore(root / "experiments", 42, {})
            state = store.load_feedback_state()
            self.assertEqual(state["total_trials"], 0)
            self.assertEqual(state["total_control_trials"], 1)
            trial = store.path / "trial_0001"
            metadata = json.loads((trial / "metadata.json").read_text())
            self.assertEqual(metadata["clock_alignment_reference_id"], "same")
            anomalies = json.loads((trial / "anomalies.json").read_text())
            self.assertEqual(anomalies["clock_quality"]["p_can"]["status"], "aligned")

    def test_analysis_uses_prepared_thresholds_not_later_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            tx, p, i = self.make_results(root, package)
            cfg["anomaly_thresholds"]["minimum_baseline_frames"] = 100
            analyze_distributed_trial(
                config=cfg, config_path=None, package=package,
                tx_result=tx, p_result=p, i_result=i,
            )
            trial = root / "experiments" / "experiment_0042" / "trial_0001"
            anomalies = json.loads((trial / "anomalies.json").read_text())
            metadata = json.loads((trial / "metadata.json").read_text())
            self.assertEqual(anomalies["thresholds"]["minimum_baseline_frames"], 5)
            self.assertEqual(metadata["analysis_config"]["minimum_baseline_frames"], 5)

    def test_aligned_capture_must_cover_recovery_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            tx, p, i = self.make_results(
                root, package, ("same", "same", "same"), p_end_ns=2_600_000_000,
            )
            with self.assertRaisesRegex(ValueError, "baseline/recovery windows"):
                analyze_distributed_trial(
                    config=cfg, config_path=None, package=package,
                    tx_result=tx, p_result=p, i_result=i,
                )

    def test_duplicate_or_missing_receiver_role_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial1.zip",
            )
            tx, p, _ = self.make_results(root, package)
            with self.assertRaisesRegex(ValueError, "Duplicate result"):
                analyze_distributed_trial(
                    config=cfg, config_path=None, package=package,
                    tx_result=tx, p_result=p, i_result=p,
                )

    def test_sender_command_has_no_b_can_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = prepare_trial_package(
                config=config(root / "experiments"), config_path=None,
                experiment_id=42, target_id=0x366, random_seed=366,
                mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            plan, mutation = load_trial_package(package)
            command = _sender_command(
                plan=plan, mutation=mutation,
                node={"project_dir": "/lab", "python": "python3"},
                remote_tx="/tmp/tx.jsonl",
            )
            self.assertIn("/lab/can_sender.py", command)
            self.assertNotIn("can_receiver.py", " ".join(command))


class DistributedInjectionTests(unittest.TestCase):
    def test_injection_uses_watchdog_and_rejects_incomplete_tx(self) -> None:
        class FakeManager:
            def __init__(self, _ssh, tx_data: bytes):
                self.tx_data = tx_data
                self.started = None
                self.closed = False

            def clock_sample(self):
                return {"offset_ms": 0.0, "round_trip_ms": 1.0}

            def ensure_directory(self, _path):
                pass

            def start_process(self, command, stdout_path):
                self.started = list(command)
                self.stdout_path = stdout_path
                return object()

            def process_alive(self, _process):
                return False

            def download(self, remote_path, local_path):
                local_path.write_bytes(self.tx_data if remote_path.endswith("tx.jsonl") else b"sender output")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root / "experiments")
            cfg["target"]["probe_live_payload"] = False
            package = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            node_cfg = {"node": {
                "bus": "b_can", "ssh": {"host": "fake"},
                "project_dir": "/lab", "python": "python3",
                "results_dir": str(root / "results"), "clock_reference_id": "laptop-a",
            }}
            first = FakeManager({}, tx_jsonl())
            result = inject_trial(
                package=package, config=node_cfg, output=root / "tx_result.zip",
                execute=True, manager_factory=lambda _ssh: first,
            )
            self.assertTrue(first.closed)
            self.assertEqual(first.started[:4], ["timeout", "--signal=TERM", "--kill-after=5s", "10s"])
            self.assertIn("--trial-contract-version", first.started)
            manifest, _ = load_result_bundle(result)
            self.assertEqual(manifest["clock_reference_id"], "laptop-a")

            second = FakeManager({}, tx_jsonl(status="aborted"))
            second_node_cfg = {"node": {**node_cfg["node"], "results_dir": str(root / "results2")}}
            with self.assertRaisesRegex(ValueError, "did not complete"):
                inject_trial(
                    package=package, config=second_node_cfg, output=root / "aborted.zip",
                    execute=True, manager_factory=lambda _ssh: second,
                )
            self.assertTrue(second.closed)
            self.assertFalse((root / "aborted.zip").exists())


class DistributedReceiveTests(unittest.TestCase):
    def test_receive_uses_unique_remote_attempts_and_refuses_existing_evidence(self) -> None:
        class FakeManager:
            def __init__(self, _ssh):
                self.started = None
                self.closed = False

            def clock_sample(self):
                return {"offset_ms": 0.0, "round_trip_ms": 1.0}

            def ensure_directory(self, _path):
                pass

            def start_process(self, command, _stdout_path):
                self.started = list(command)
                return object()

            def process_alive(self, _process):
                return False

            def download(self, remote_path, local_path):
                local_path.write_bytes(rx_jsonl("p_can", 42) if remote_path.endswith("p_can.jsonl") else b"receiver output")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = prepare_trial_package(
                config=config(root / "experiments"), config_path=None,
                experiment_id=42, target_id=0x366, random_seed=366,
                mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            node_cfg = {"node": {
                "bus": "p_can", "ssh": {"host": "fake"},
                "results_dir": str(root / "results1"),
            }}
            managers = []
            def factory(ssh):
                instance = FakeManager(ssh)
                managers.append(instance)
                return instance

            receive_trial(
                package=package, config=node_cfg, bus="p_can", output=root / "first.zip",
                manager_factory=factory,
            )
            self.assertTrue(managers[0].closed)
            with self.assertRaisesRegex(FileExistsError, "existing local evidence"):
                receive_trial(
                    package=package, config=node_cfg, bus="p_can", output=root / "again.zip",
                    manager_factory=factory,
                )
            self.assertEqual(len(managers), 1)
            other_cfg = {"node": {**node_cfg["node"], "results_dir": str(root / "results2")}}
            receive_trial(
                package=package, config=other_cfg, bus="p_can", output=root / "second.zip",
                manager_factory=factory,
            )
            first_path = managers[0].started[managers[0].started.index("--output") + 1]
            second_path = managers[1].started[managers[1].started.index("--output") + 1]
            self.assertNotEqual(first_path, second_path)
            self.assertIn("/attempt_", first_path)
            self.assertIn("/attempt_", second_path)

    def test_receive_interrupt_stops_remote_process(self) -> None:
        class InterruptedManager:
            def __init__(self, _ssh):
                self.stopped = False
                self.closed = False

            def clock_sample(self):
                return {"offset_ms": 0.0, "round_trip_ms": 1.0}

            def ensure_directory(self, _path):
                pass

            def start_process(self, _command, _stdout_path):
                return object()

            def process_alive(self, _process):
                raise KeyboardInterrupt()

            def stop_process(self, _process):
                self.stopped = True

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = prepare_trial_package(
                config=config(root / "experiments"), config_path=None,
                experiment_id=42, target_id=0x366, random_seed=366,
                mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial.zip",
            )
            manager = InterruptedManager({})
            with self.assertRaises(KeyboardInterrupt):
                receive_trial(
                    package=package,
                    config={"node": {"bus": "p_can", "ssh": {"host": "fake"},
                                     "results_dir": str(root / "results")}},
                    bus="p_can", output=root / "rx.zip",
                    manager_factory=lambda _ssh: manager,
                )
            self.assertTrue(manager.stopped)
            self.assertTrue(manager.closed)
            self.assertFalse((root / "rx.zip").exists())


if __name__ == "__main__":
    unittest.main()
