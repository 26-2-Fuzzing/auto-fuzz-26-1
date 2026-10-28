"""Offline contracts for replaying one saved trial through the full runner."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from can_common import ConfigurationError
from experiment_runner import (
    ExperimentRunner, build_parser, load_replay_source, run,
)
from experiment_store import ExperimentStore
from strategy_selector import TrialStrategySelector
from test_iteration_feedback import FakeRunnerManager
from trial_models import MutationCase, noop_case


ORIGINAL = bytes.fromhex("00000000200000F0")
MUTATED = bytes.fromhex("00008002200000F0")
BUSES = ("p_can", "b_can", "i_can")
COLLECTION = {
    "baseline_seconds": 30.0,
    "normal_seconds": 10.0,
    "mutation_seconds": 1.0,
    "post_seconds": 20.0,
    "interval_ms": 50.0,
    "capture_start_delay_seconds": 0.0,
    "runner_timeout_seconds": 120.0,
    "watchdog_poll_seconds": 0.5,
}


def replay_config(root: Path) -> dict:
    return {
        "experiments_root": str(root / "experiments"),
        "remote": {"hosts": {bus: {"name": bus} for bus in BUSES}},
        "target": {"probe_live_payload": True, "probe_samples": 3,
                   "reference_payload": ORIGINAL.hex().upper()},
        "trial": dict(COLLECTION),
        "feedback": {"enabled": False},
    }


def saved_trial(root: Path) -> Path:
    """Build a completed exp 5/trial 6 with a coherent executed TX manifest."""
    trial_dir = root / "experiments" / "experiment_0005" / "trial_0006"
    trial_dir.mkdir(parents=True)
    case = MutationCase(
        mutation_id=3, source_bus="b_can", can_id=0x366,
        operator="STATE_CONTRADICTION", original_payload=ORIGINAL,
        mutated_payload=MUTATED, random_seed=366,
        parameters={"mutation_family": "state_contradiction",
                    "case": "LEFT_FZG_KOMBI_MISMATCH_A"},
    )
    (trial_dir / "mutation.json").write_text(
        json.dumps(case.to_dict()), encoding="utf-8"
    )
    phases = {
        "baseline_start": 1_000_000_000,
        "baseline_end": 31_000_000_000,
        "normal_start": 31_000_000_000,
        "normal_end": 41_000_000_000,
        "mutation_start": 41_000_000_000,
        "mutation_end": 42_000_000_000,
        "recovery_start": 42_100_000_000,
        "recovery_end": 62_100_000_000,
    }
    metadata = {
        "schema_version": 3, "status": "completed", "experiment_id": 5,
        "trial_id": 6, "trial_kind": "mutation", "mutation_id": 3,
        "source_bus": "B_CAN", "target_id": "0x366",
        "pair_id": "pair_0003", "pair_position": 2,
        "pair_order": ["noop", "mutation"], "pair_role": "mutation",
        "collection_config": dict(COLLECTION), "phase_times_ns": phases,
        "logs": {bus: f"{bus}.jsonl" for bus in BUSES},
        "capture_quality": {bus: {
            "clock_alignment": "source_clock" if bus == "b_can" else "aligned",
            "phase_coverage": "verified", "frame_count": 40,
            "session_start_count": 1, "session_end_count": 1,
            "session_start_ns": 900_000_000,
            "session_end_ns": 63_000_000_000,
        } for bus in BUSES},
    }
    (trial_dir / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    source_experiment = {"schema_version": 1, "experiment_id": 5,
                         "status": "completed"}
    (trial_dir.parent / "experiment.json").write_text(
        json.dumps(source_experiment), encoding="utf-8"
    )
    for bus in BUSES:
        capture = [
            {"record_type": "session_start", "experiment_id": 5,
             "bus": bus, "wall_time_ns": 900_000_000},
            {"record_type": "session_end", "experiment_id": 5,
             "bus": bus, "wall_time_ns": 63_000_000_000},
        ]
        (trial_dir / f"{bus}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in capture), encoding="utf-8"
        )
    tx = [{
        "record_type": "tx_session_start", "tx_session_id": "source-session",
        "trial_contract_version": 1, "trial_kind": "mutation",
        "experiment_id": "5", "execute": True,
        "campaign": {"enabled": True, "normal_data_hex": ORIGINAL.hex().upper(),
                     "baseline_duration_seconds": 30.0,
                     "normal_duration_seconds": 10.0,
                     "mutation_duration_seconds": 1.0,
                     "recovery_duration_seconds": 20.0},
        "mutation": {"trial_mutation_id": 3, "control_noop": False},
        "transmission": {"interval_ms": 50.0},
    }]
    for phase in ("baseline", "normal", "mutation", "recovery"):
        for event in ("start", "end"):
            tx.append({"record_type": "tx_phase", "phase": phase, "event": event,
                       "wall_time_ns": phases[f"{phase}_{event}"]})
    for phase, data, count in (
        ("normal", ORIGINAL, 200), ("mutation", MUTATED, 20)
    ):
        tx.extend({
            "record_type": "can_tx", "status": "sent", "phase": phase,
            "arbitration_id": 0x366, "data_hex": data.hex().upper(),
            "trial_kind": "mutation", "sequence": index + 1,
        } for index in range(count))
    tx.extend((
        {"record_type": "can_tx", "status": "sent", "phase": "recovery",
         "kind": "restore", "arbitration_id": 0x366,
         "data_hex": ORIGINAL.hex().upper(), "trial_kind": "mutation"},
        {"record_type": "tx_session_end", "tx_session_id": "source-session",
         "trial_contract_version": 1, "trial_kind": "mutation",
         "experiment_id": "5", "execute": True, "status": "completed",
         "phase_sent": {"normal": 200, "mutation": 20},
         "restore": {"status": "sent", "sent": 1}},
    ))
    (trial_dir / "tx.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in tx), encoding="utf-8"
    )
    return trial_dir


def saved_noop_trial(root: Path) -> Path:
    """Build the completed original-only control from exp 5/trial 4."""
    trial_dir = saved_trial(root)
    noop_dir = trial_dir.with_name("trial_0004")
    trial_dir.rename(noop_dir)
    control = noop_case(4, "b_can", 0x366, ORIGINAL, 366)
    (noop_dir / "mutation.json").write_text(
        json.dumps(control.to_dict()), encoding="utf-8"
    )
    metadata_path = noop_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update({
        "trial_id": 4, "trial_kind": "noop", "mutation_id": 4,
        "pair_id": "pair_0002", "pair_role": "noop",
        "pair_order": ["mutation", "noop"],
    })
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    tx_path = noop_dir / "tx.jsonl"
    records = [json.loads(line) for line in tx_path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record.get("record_type") in {"tx_session_start", "tx_session_end", "can_tx"}:
            record["trial_kind"] = "noop"
        if record.get("record_type") == "tx_session_start":
            record["mutation"] = {"trial_mutation_id": 4, "control_noop": True}
        elif record.get("record_type") == "can_tx" and record.get("phase") == "mutation":
            record["data_hex"] = ORIGINAL.hex().upper()
    tx_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return noop_dir


class ReplaySourceTests(unittest.TestCase):
    def test_completed_source_preserves_exact_stimulus_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            case, provenance = load_replay_source(
                source, replay_config(root), "b_can", 0x366
            )
        self.assertEqual(case.source_bus, "b_can")
        self.assertEqual(case.can_id, 0x366)
        self.assertEqual(case.original_payload, ORIGINAL)
        self.assertEqual(case.mutated_payload, MUTATED)
        self.assertEqual(case.operator, "STATE_CONTRADICTION")
        self.assertEqual(case.parameters["case"], "LEFT_FZG_KOMBI_MISMATCH_A")
        self.assertIsInstance(provenance, dict)

    def test_incomplete_or_changed_source_cannot_authorize_replay(self) -> None:
        mutators = {
            "not_completed": lambda source: rewrite_json(
                source / "metadata.json", status="captured"),
            "capture_missing": lambda source: (source / "i_can.jsonl").unlink(),
            "capture_unverified": lambda source: rewrite_capture_quality(
                source / "metadata.json", "i_can", "unverified_clock_reference"),
            "capture_from_other_trial": redirect_capture_to_foreign_file,
            "tx_missing": lambda source: (source / "tx.jsonl").unlink(),
            "tx_payload_changed": lambda source: replace_tx_payload(source / "tx.jsonl"),
            "tx_phase_duration_changed": change_tx_phase_duration,
            "wrong_schedule": lambda source: rewrite_collection(
                source / "metadata.json", "mutation_seconds", 2.0),
        }
        for name, mutate in mutators.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = saved_trial(root)
                mutate(source)
                with self.assertRaises((ValueError, RuntimeError, ConfigurationError, OSError)):
                    load_replay_source(source, replay_config(root), "b_can", 0x366)

    def test_source_bus_and_target_are_fixed_by_saved_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            for bus, can_id in (("p_can", 0x366), ("b_can", 0x367)):
                with self.subTest(bus=bus, can_id=can_id):
                    with self.assertRaises((ValueError, RuntimeError, ConfigurationError)):
                        load_replay_source(source, replay_config(root), bus, can_id)


def rewrite_json(path: Path, **fields) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(fields)
    path.write_text(json.dumps(document), encoding="utf-8")


def rewrite_collection(path: Path, field: str, value: float) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["collection_config"][field] = value
    path.write_text(json.dumps(document), encoding="utf-8")


def rewrite_capture_quality(path: Path, bus: str, value: str) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["capture_quality"][bus]["phase_coverage"] = value
    path.write_text(json.dumps(document), encoding="utf-8")


def redirect_capture_to_foreign_file(source: Path) -> None:
    foreign = source.parent / "foreign_i_can.jsonl"
    foreign.write_bytes((source / "i_can.jsonl").read_bytes())
    metadata_path = source / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["logs"]["i_can"] = str(foreign)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def replace_tx_payload(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = text.replace(MUTATED.hex().upper(), "FFFFFFFFFFFFFFFF", 1)
    path.write_text(text, encoding="utf-8")


def change_tx_phase_duration(source: Path) -> None:
    """Preserve the declared schedule while the executed mutation runs 2 seconds."""
    replacements = {
        42_000_000_000: 43_000_000_000,
        42_100_000_000: 43_100_000_000,
        62_100_000_000: 63_100_000_000,
    }
    tx_path = source / "tx.jsonl"
    rows = [json.loads(line) for line in tx_path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row.get("record_type") == "tx_phase":
            row["wall_time_ns"] = replacements.get(row["wall_time_ns"], row["wall_time_ns"])
    tx_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    metadata_path = source / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["phase_times_ns"] = {
        key: replacements.get(value, value)
        for key, value in metadata["phase_times_ns"].items()
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


class ReplayExecutionTests(unittest.TestCase):
    def test_preview_validates_source_without_remote_connection_or_new_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(replay_config(root)), encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--experiment-id", "42",
                "--source-bus", "B_CAN", "--target-id", "0x366",
                "--replay-trial", str(source),
            ])
            with patch("experiment_runner.ExperimentRunner") as runner_class, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            runner_class.assert_not_called()
            self.assertFalse((root / "experiments" / "experiment_0042").exists())

    def test_cli_execution_records_capture_only_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(replay_config(root)), encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--experiment-id", "42",
                "--source-bus", "B_CAN", "--target-id", "0x366",
                "--replay-trial", str(source), "--execute",
            ])
            real_runner_class = ExperimentRunner
            def offline_runner(config):
                return real_runner_class(config, manager_factory=FakeRunnerManager)

            with patch("experiment_runner.ExperimentRunner", side_effect=offline_runner), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            experiment = root / "experiments" / "experiment_0042"
            summary = json.loads((experiment / "experiment.json").read_text())
            metadata = json.loads((experiment / "trial_0001" / "metadata.json").read_text())
            self.assertEqual(summary["status"], "captured")
            self.assertEqual(metadata["status"], "captured")
            self.assertFalse((experiment / "trial_0001" / "anomalies.json").exists())
            self.assertFalse((experiment / "trial_0001" / "feedback.json").exists())

    def test_full_runner_captures_three_buses_with_saved_tx_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            config = replay_config(root)
            case, provenance = load_replay_source(source, config, "b_can", 0x366)
            store = ExperimentStore(root / "experiments", 42, config)
            runner = ExperimentRunner(config, manager_factory=FakeRunnerManager)
            try:
                with redirect_stdout(io.StringIO()):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector({}),
                        dbc_path=None, replay_source=case,
                        replay_provenance=provenance,
                        capture_tail_seconds=0.02,
                        defer_analysis=True,
                    )
            finally:
                runner.close()
            trial = store.path / "trial_0001"
            metadata = json.loads((trial / "metadata.json").read_text())
            sent = json.loads((trial / "mutation.json").read_text())
            command = next(
                cmd for cmd in runner.managers["b_can"].started
                if "--trial-contract-version" in cmd
            )
            for option, value in (
                ("--baseline-duration", "30.0"),
                ("--normal-duration", "10.0"),
                ("--mutation-duration", "1.0"),
                ("--recovery-duration", "20.0"),
                ("--interval-ms", "50.0"),
            ):
                self.assertEqual(command[command.index(option) + 1], value)
            self.assertEqual(metadata["status"], "captured")
            self.assertEqual(metadata["capture_tail_seconds"], 0.02)
            self.assertEqual(set(metadata["logs"]), set(BUSES))
            self.assertEqual(
                {bus: metadata["capture_quality"][bus]["phase_coverage"]
                 for bus in BUSES},
                {bus: "verified" for bus in BUSES},
            )
            self.assertTrue(all((trial / f"{bus}.jsonl").is_file() for bus in BUSES))
            self.assertEqual(sent["original_payload"].replace(" ", ""), ORIGINAL.hex().upper())
            self.assertEqual(sent["mutated_payload"].replace(" ", ""), MUTATED.hex().upper())
            self.assertEqual(sent["operator"], "STATE_CONTRADICTION")
            self.assertFalse((trial / "anomalies.json").exists())
            self.assertFalse((trial / "feedback.json").exists())

    def test_changed_live_original_stops_before_any_capture_or_sender(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_trial(root)
            config = replay_config(root)
            case, provenance = load_replay_source(source, config, "b_can", 0x366)
            store = ExperimentStore(root / "experiments", 42, config)

            class OfflineManager:
                def __init__(self, _config):
                    pass

                def close(self):
                    pass

            runner = ExperimentRunner(config, manager_factory=OfflineManager)
            clock_calls = []
            live_probe_calls = []

            def checked_clocks(*, paired_source_bus=None):
                clock_calls.append(paired_source_bus)
                return {bus: {} for bus in BUSES}

            def changed_live_payload(_bus, _can_id, *, require_live=False):
                live_probe_calls.append(require_live)
                return b"\xff" * 8

            runner.check_clocks = checked_clocks
            runner.probe_payload = changed_live_payload
            with patch.object(runner.capture, "start_all") as start_capture, \
                 patch("experiment_runner.run_supervised_sender") as sender:
                with self.assertRaises((RuntimeError, ConfigurationError)):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector({}),
                        dbc_path=None, replay_source=case,
                        replay_provenance=provenance,
                    )
            start_capture.assert_not_called()
            sender.assert_not_called()
            self.assertEqual(clock_calls, ["b_can"])
            self.assertEqual(live_probe_calls, [True])


class NoopReplayTests(unittest.TestCase):
    def test_completed_noop_source_is_accepted_as_original_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            case, provenance = load_replay_source(
                source, replay_config(root), "b_can", 0x366
            )
        self.assertEqual(provenance["trial_id"], 4)
        self.assertEqual(case.trial_kind, "noop")
        self.assertEqual(case.operator, "NOOP")
        self.assertEqual(case.mutation_id, 4)
        self.assertEqual(case.original_payload, ORIGINAL)
        self.assertEqual(case.mutated_payload, ORIGINAL)
        self.assertEqual(case.changed_bytes, ())

    def test_noop_source_with_any_changed_transmitted_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            tx_path = source / "tx.jsonl"
            records = [json.loads(line) for line in tx_path.read_text(encoding="utf-8").splitlines()]
            changed = next(
                row for row in records
                if row.get("record_type") == "can_tx" and row.get("phase") == "mutation"
            )
            changed["data_hex"] = MUTATED.hex().upper()
            tx_path.write_text(
                "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
            )
            with self.assertRaises((ValueError, RuntimeError, ConfigurationError)):
                load_replay_source(source, replay_config(root), "b_can", 0x366)

    def test_noop_preview_does_not_connect_or_create_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(replay_config(root)), encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--experiment-id", "42",
                "--source-bus", "B_CAN", "--target-id", "0x366",
                "--replay-trial", str(source),
            ])
            output = io.StringIO()
            with patch("experiment_runner.ExperimentRunner") as runner_class, \
                 redirect_stdout(output):
                self.assertEqual(run(args), 0)
            runner_class.assert_not_called()
            self.assertFalse((root / "experiments" / "experiment_0042").exists())
            self.assertIn("noop", output.getvalue().lower())
            self.assertIn(ORIGINAL.hex().upper(), output.getvalue())

    def test_noop_replay_sends_original_in_both_slots_and_captures_three_buses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            config = replay_config(root)
            case, provenance = load_replay_source(source, config, "b_can", 0x366)
            store = ExperimentStore(root / "experiments", 42, config)
            runner = ExperimentRunner(config, manager_factory=FakeRunnerManager)
            try:
                with redirect_stdout(io.StringIO()):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector({}),
                        dbc_path=None, replay_source=case,
                        replay_provenance=provenance,
                        capture_tail_seconds=0.02,
                        defer_analysis=True,
                    )
            finally:
                runner.close()
            trial = store.path / "trial_0001"
            metadata = json.loads((trial / "metadata.json").read_text(encoding="utf-8"))
            sent = json.loads((trial / "mutation.json").read_text(encoding="utf-8"))
            tx = [json.loads(line) for line in (trial / "tx.jsonl").read_text(encoding="utf-8").splitlines()]
            command = next(
                cmd for cmd in runner.managers["b_can"].started
                if "--trial-contract-version" in cmd
            )
            self.assertIn("--control-noop", command)
            self.assertEqual(command[command.index("--data") + 1].upper(), ORIGINAL.hex().upper())
            self.assertEqual(
                command[command.index("--mutation-data") + 1].upper(), ORIGINAL.hex().upper()
            )
            self.assertEqual(metadata["status"], "captured")
            self.assertEqual(metadata["trial_kind"], "noop")
            self.assertEqual(metadata["capture_tail_seconds"], 0.02)
            self.assertEqual(set(metadata["logs"]), set(BUSES))
            self.assertEqual(
                {bus: metadata["capture_quality"][bus]["phase_coverage"] for bus in BUSES},
                {bus: "verified" for bus in BUSES},
            )
            self.assertTrue(all((trial / f"{bus}.jsonl").is_file() for bus in BUSES))
            self.assertEqual(sent["trial_kind"], "noop")
            self.assertEqual(sent["operator"], "NOOP")
            self.assertEqual(sent["original_payload"], sent["mutated_payload"])
            self.assertEqual(
                {record["data_hex"] for record in tx if record.get("record_type") == "can_tx"},
                {ORIGINAL.hex().upper()},
            )
            self.assertFalse((trial / "anomalies.json").exists())
            self.assertFalse((trial / "feedback.json").exists())

    def test_noop_live_original_mismatch_stops_before_capture_or_tx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            config = replay_config(root)
            case, provenance = load_replay_source(source, config, "b_can", 0x366)
            store = ExperimentStore(root / "experiments", 42, config)

            class OfflineManager:
                def __init__(self, _config):
                    pass

                def close(self):
                    pass

            runner = ExperimentRunner(config, manager_factory=OfflineManager)
            clock_calls = []
            live_probe_calls = []

            def checked_clocks(*, paired_source_bus=None):
                clock_calls.append(paired_source_bus)
                return {bus: {} for bus in BUSES}

            def changed_live_payload(_bus, _can_id, *, require_live=False):
                live_probe_calls.append(require_live)
                return b"\xff" * 8

            runner.check_clocks = checked_clocks
            runner.probe_payload = changed_live_payload
            with patch.object(runner.capture, "start_all") as start_capture, \
                 patch("experiment_runner.run_supervised_sender") as sender:
                with self.assertRaises((RuntimeError, ConfigurationError)):
                    runner.run_trial(
                        store=store, source_bus="b_can", can_id=0x366,
                        random_seed=366, selector=TrialStrategySelector({}),
                        dbc_path=None, replay_source=case,
                        replay_provenance=provenance,
                    )
            start_capture.assert_not_called()
            sender.assert_not_called()
            self.assertEqual(clock_calls, ["b_can"])
            self.assertEqual(live_probe_calls, [True])

    def test_noop_cli_execution_is_capture_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = saved_noop_trial(root)
            config_path = root / "runner.yaml"
            config_path.write_text(json.dumps(replay_config(root)), encoding="utf-8")
            args = build_parser().parse_args([
                "--config", str(config_path), "--experiment-id", "42",
                "--source-bus", "B_CAN", "--target-id", "0x366",
                "--replay-trial", str(source), "--execute",
            ])
            real_runner_class = ExperimentRunner

            def offline_runner(config):
                return real_runner_class(config, manager_factory=FakeRunnerManager)

            with patch("experiment_runner.ExperimentRunner", side_effect=offline_runner), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            experiment = root / "experiments" / "experiment_0042"
            summary = json.loads((experiment / "experiment.json").read_text(encoding="utf-8"))
            metadata = json.loads((experiment / "trial_0001" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "captured")
            self.assertEqual(metadata["status"], "captured")
            self.assertEqual(metadata["trial_kind"], "noop")
            self.assertFalse((experiment / "trial_0001" / "anomalies.json").exists())
            self.assertFalse((experiment / "trial_0001" / "feedback.json").exists())


if __name__ == "__main__":
    unittest.main()
