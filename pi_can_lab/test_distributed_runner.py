from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from distributed_runner import (
    _result_zip,
    _sender_command,
    analyze_distributed_trial,
    load_trial_package,
    prepare_trial_package,
)
from experiment_store import ExperimentStore


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
            "normal_seconds": 0.2,
            "mutation_seconds": 0.5,
            "post_seconds": 0.2,
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


def tx_jsonl() -> bytes:
    records = [
        {"record_type": "tx_phase", "phase": "baseline", "event": "start", "wall_time_ns": 1_000_000_000},
        {"record_type": "tx_phase", "phase": "baseline", "event": "end", "wall_time_ns": 1_500_000_000},
        {"record_type": "tx_phase", "phase": "normal", "event": "start", "wall_time_ns": 1_500_000_000},
        {"record_type": "tx_phase", "phase": "normal", "event": "end", "wall_time_ns": 1_700_000_000},
        {"record_type": "tx_phase", "phase": "mutation", "event": "start", "wall_time_ns": 2_000_000_000},
        {"record_type": "tx_phase", "phase": "mutation", "event": "end", "wall_time_ns": 2_500_000_000},
        {"record_type": "tx_phase", "phase": "recovery", "event": "start", "wall_time_ns": 2_500_000_000},
        {"record_type": "tx_phase", "phase": "recovery", "event": "end", "wall_time_ns": 2_700_000_000},
        {"record_type": "tx_session_end", "status": "completed"},
    ]
    return ("".join(json.dumps(item) + "\n" for item in records)).encode()


def rx_jsonl(bus: str, experiment_id: int, changed: bool = False) -> bytes:
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
        "record_type": "session_end", "wall_time_ns": 2_800_000_000,
        "experiment_id": str(experiment_id), "bus": bus,
    })
    return ("".join(json.dumps(item) + "\n" for item in records)).encode()


class DistributedPackageTests(unittest.TestCase):
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


class DistributedAnalysisTests(unittest.TestCase):
    def make_results(self, root: Path, package: Path):
        plan, _ = load_trial_package(package)
        clock = {"offset_ms": 0.0, "round_trip_ms": 1.0}
        tx = _result_zip(
            output=root / "tx.zip", plan=plan, role="tx", bus="b_can",
            data_name="tx.jsonl", data=tx_jsonl(), clock=clock,
        )
        p = _result_zip(
            output=root / "p.zip", plan=plan, role="rx", bus="p_can",
            data_name="p_can.jsonl", data=rx_jsonl("p_can", 42, changed=True), clock=clock,
        )
        i = _result_zip(
            output=root / "i.zip", plan=plan, role="rx", bus="i_can",
            data_name="i_can.jsonl", data=rx_jsonl("i_can", 42), clock=clock,
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
            self.assertTrue(feedback["interesting"])
            self.assertEqual(state["total_trials"], 1)
            self.assertTrue((trial / "p_can.jsonl").is_file())
            self.assertTrue((trial / "i_can.jsonl").is_file())
            self.assertFalse((trial / "b_can.jsonl").exists())

            second = prepare_trial_package(
                config=cfg, config_path=None, experiment_id=42, target_id=0x366,
                random_seed=366, mutation_profile=None, undefined_max_bits=2,
                base_payload=BASE, output=root / "trial2.zip",
            )
            _, next_mutation = load_trial_package(second)
            self.assertEqual(next_mutation.parent_mutation_id, 1)

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


if __name__ == "__main__":
    unittest.main()
