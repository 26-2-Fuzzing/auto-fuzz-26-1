#!/usr/bin/env python3
"""Control-PC orchestrator for completed-trial iteration-based CAN fuzzing."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from can_common import ConfigurationError, load_dbc, load_yaml_config, parse_can_data, parse_int
from experiment_store import ExperimentStore
from mutation_feedback import create_trial_feedback
from remote_capture import RemoteCapture
from ssh_manager import SSHManager, remote_join
from strategy_selector import StrategyDecision, TrialStrategySelector
from trial_analysis import analyze_trial, validate_capture_log
from trial_models import MutationCase, utc_now


BUS_ALIASES = {
    "p": "p_can", "p-can": "p_can", "p_can": "p_can",
    "b": "b_can", "b-can": "b_can", "b_can": "b_can",
    "i": "i_can", "i-can": "i_can", "i_can": "i_can",
}
RECEIVER_CONFIGS = {
    "p_can": "receiver_p_can.yaml",
    "b_can": "receiver_b_can.yaml",
    "i_can": "receiver_i_can.yaml",
}
PHASE_NAMES = {"baseline", "normal", "mutation", "recovery"}


def normalize_bus(value: str) -> str:
    try:
        return BUS_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise ConfigurationError("source bus must be P_CAN, B_CAN, or I_CAN") from exc


def ns_to_iso(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1e9, tz=timezone.utc).isoformat()


def next_experiment_id(root: Path) -> int:
    indexes = []
    if root.exists():
        for path in root.iterdir():
            match = re.fullmatch(r"experiment_(\d+)", path.name)
            if path.is_dir() and match:
                indexes.append(int(match.group(1)))
    return max(indexes, default=0) + 1


def load_phase_times(tx_path: Path) -> dict[str, int]:
    phases: dict[str, int] = {}
    completed = False
    with tx_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record_type") != "tx_phase":
                if (
                    record.get("record_type") == "tx_session_end"
                    and record.get("status") == "completed"
                ):
                    completed = True
                continue
            phase = str(record.get("phase"))
            event = str(record.get("event"))
            if phase in PHASE_NAMES and event in {"start", "end"}:
                phases[f"{phase}_{event}"] = int(record["wall_time_ns"])
    required = {"baseline_start", "baseline_end", "mutation_start", "mutation_end"}
    missing = sorted(required - phases.keys())
    if missing:
        raise ValueError("TX phase markers are missing: " + ", ".join(missing))
    if not completed:
        raise ValueError("TX session did not contain a completed end marker")
    return phases


def parse_candump_payloads(output: str, can_id: int) -> list[bytes]:
    payloads = []
    pattern = re.compile(r"(?:^|\s)([0-9A-Fa-f]{3,8})#([0-9A-Fa-f]+)(?:\s|$)")
    for line in output.splitlines():
        match = pattern.search(line)
        if match and int(match.group(1), 16) == can_id:
            payloads.append(bytes.fromhex(match.group(2)))
    return payloads


def dbc_signal_metadata(mutation: MutationCase, dbc_path: Optional[Path]) -> Optional[dict[str, Any]]:
    if dbc_path is None:
        return None
    try:
        message = load_dbc(dbc_path).get_message_by_frame_id(mutation.can_id)
        before = message.decode(mutation.original_payload, decode_choices=False, scaling=True)
        after = message.decode(mutation.mutated_payload, decode_choices=False, scaling=True)
    except Exception:
        return None
    definitions = {signal.name: signal for signal in message.signals}
    changes = []
    for name, original_value in before.items():
        if name not in after or after[name] == original_value:
            continue
        signal = definitions[name]
        changes.append({
            "signal_name": name,
            "start_bit": int(signal.start),
            "length": int(signal.length),
            "original_value": original_value,
            "mutated_value": after[name],
        })
    return {
        "message_name": message.name,
        "signal_name": changes[0]["signal_name"] if changes else None,
        "changes": changes,
    }


class ExperimentRunner:
    def __init__(self, config: Mapping[str, Any], *, manager_factory=SSHManager):
        self.config = dict(config)
        remote = self.config.get("remote", {})
        hosts = remote.get("hosts", {})
        missing = sorted(set(RECEIVER_CONFIGS) - set(hosts))
        if missing:
            raise ConfigurationError("remote.hosts is missing: " + ", ".join(missing))
        self.managers = {
            bus: manager_factory(hosts[bus]) for bus in RECEIVER_CONFIGS
        }
        default_project = str(remote.get("project_dir", "/home/pi/auto-fuzz-26/pi_can_lab"))
        default_python = str(remote.get("python", "python3"))
        self.project_dirs = {
            bus: str(hosts[bus].get("project_dir", default_project)) for bus in RECEIVER_CONFIGS
        }
        self.python_commands = {
            bus: str(hosts[bus].get("python", default_python)) for bus in RECEIVER_CONFIGS
        }
        receiver_configs = dict(RECEIVER_CONFIGS)
        receiver_configs.update(remote.get("receiver_configs", {}))
        self.capture = RemoteCapture(
            self.managers,
            self.project_dirs,
            self.python_commands,
            receiver_configs,
            str(remote.get("capture_root", "/tmp/auto_fuzz_trials")),
        )

    def close(self) -> None:
        for manager in self.managers.values():
            manager.close()

    def check_clocks(self) -> dict[str, Any]:
        sync = self.config.get("time_sync", {})
        sample_count = max(1, int(sync.get("samples", 3)))
        warning_ms = float(sync.get("warning_threshold_ms", 50.0))

        def one(bus: str) -> tuple[str, dict[str, Any]]:
            samples = [self.managers[bus].clock_sample() for _ in range(sample_count)]
            best = min(samples, key=lambda item: item["round_trip_ms"])
            return bus, {**best, "samples": samples, "warning": abs(best["offset_ms"]) > warning_ms}

        with ThreadPoolExecutor(max_workers=3) as executor:
            values = dict(executor.map(one, self.managers))
        for bus, value in values.items():
            if value["warning"]:
                print(f"[WARN] {bus} clock offset={value['offset_ms']:.3f} ms > {warning_ms:g} ms")
        return values

    def probe_payload(self, source_bus: str, can_id: int) -> bytes:
        target = self.config.get("target", {})
        if not bool(target.get("probe_live_payload", True)):
            configured = target.get("reference_payload")
            if configured is None:
                raise ConfigurationError("target.reference_payload is required when live probing is disabled")
            return parse_can_data(configured)
        channel = str(target.get("channel", "can0"))
        samples = max(1, int(target.get("probe_samples", 3)))
        timeout_seconds = float(target.get("probe_timeout_seconds", 6.0))
        mask = "1FFFFFFF" if can_id > 0x7FF else "7FF"
        result = self.managers[source_bus].run([
            "timeout", f"{timeout_seconds:g}", "candump", "-L", "-n", str(samples),
            f"{channel},{can_id:X}:{mask}",
        ], timeout=timeout_seconds + 2.0, check=False)
        payloads = parse_candump_payloads(result.stdout, can_id)
        if not payloads:
            configured = target.get("reference_payload")
            if configured is not None:
                print("[WARN] live payload probe failed; using configured reference_payload")
                return parse_can_data(configured)
            raise RuntimeError(f"No 0x{can_id:X} payload captured from {source_bus}")
        counts = {payload: payloads.count(payload) for payload in set(payloads)}
        return max(counts, key=counts.get)

    def _sender_command(
        self,
        source_bus: str,
        experiment_id: int,
        mutation: MutationCase,
        remote_tx: str,
        trial_cfg: Mapping[str, Any],
    ) -> list[str]:
        project = self.project_dirs[source_bus]
        command = [
            self.python_commands[source_bus], remote_join(project, "can_sender.py"),
            "--config", remote_join(project, str(self.config.get("sender_config", "sender_trial.yaml"))),
            "--bus-name", source_bus,
            "--id", f"0x{mutation.can_id:X}",
            "--data", mutation.original_payload.hex(),
            "--experiment-id", str(experiment_id),
            "--output", remote_tx, "--output-policy", "fail",
            "--count", "1",
            "--mutation-data", mutation.mutated_payload.hex(),
            "--mutation-id", str(mutation.mutation_id),
            "--mutation-operator", mutation.operator,
            "--generation-reason", mutation.generation_reason,
            "--random-seed", str(mutation.random_seed),
            "--baseline-duration", str(float(trial_cfg.get("baseline_seconds", 10.0))),
            "--normal-duration", str(float(trial_cfg.get("normal_seconds", 5.0))),
            "--mutation-duration", str(float(trial_cfg.get("mutation_seconds", 5.0))),
            "--recovery-duration", str(float(trial_cfg.get("post_seconds", 10.0))),
            "--execute",
        ]
        if mutation.parent_mutation_id is not None:
            command.extend(["--parent-mutation-id", str(mutation.parent_mutation_id)])
        return command

    def run_trial(
        self,
        *,
        store: ExperimentStore,
        source_bus: str,
        can_id: int,
        random_seed: int,
        selector: TrialStrategySelector,
        dbc_path: Optional[Path],
        reproduce_mutation_id: Optional[int] = None,
    ) -> dict[str, Any]:
        state = store.load_feedback_state()
        state = {**state, "next_mutation_id": store.next_mutation_id()}
        trial_id = store.next_trial_id()
        trial_dir = store.create_trial(trial_id)
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "status": "preparing",
            "experiment_id": store.experiment_id,
            "trial_id": trial_id,
            "target_id": f"0x{can_id:X}",
            "source_bus": source_bus.upper(),
            "random_seed": random_seed,
            "feedback_snapshot_total_trials": int(state.get("total_trials", 0)),
            "start_time": utc_now(),
        }
        store.write_json(trial_dir / "metadata.json", metadata)
        try:
            original = self.probe_payload(source_bus, can_id)
            if reproduce_mutation_id is not None:
                parent_data = next((
                    item for item in state.get("mutation_history", [])
                    if int(item.get("mutation_id", -1)) == reproduce_mutation_id
                ), None)
                if parent_data is None:
                    raise ConfigurationError(
                        f"mutation {reproduce_mutation_id} is not in completed mutation_history"
                    )
                parent = MutationCase.from_dict(parent_data)
                if parent.source_bus != source_bus or parent.can_id != can_id:
                    raise ConfigurationError("reproduction source bus/target ID differs from the parent mutation")
                if original != parent.original_payload:
                    raise ConfigurationError(
                        "live baseline differs from the parent mutation; refusing unsafe reproduction"
                    )
                mutation = replace(
                    parent,
                    mutation_id=int(state["next_mutation_id"]),
                    parent_mutation_id=parent.mutation_id,
                    reproduction_of_mutation_id=parent.mutation_id,
                    generation_reason=f"Explicit reproduction of mutation {parent.mutation_id}",
                    strategy_mode="REPRODUCE",
                    random_seed=random_seed,
                    created_at=utc_now(),
                )
                decision = StrategyDecision(
                    "REPRODUCE", "EXACT_REPRODUCTION", parent.mutation_id,
                    parent.changed_bytes[0] if parent.changed_bytes else None,
                    mutation.generation_reason,
                )
            else:
                mutation, decision = selector.select_mutation(
                    state=state, original_payload=original, source_bus=source_bus,
                    can_id=can_id, random_seed=random_seed,
                )
            signal = dbc_signal_metadata(mutation, dbc_path)
            if signal is not None:
                mutation = replace(mutation, signal=signal)
            store.write_json(trial_dir / "mutation.json", mutation.to_dict())

            clocks = self.check_clocks()
            metadata.update({
                "status": "prepared",
                "mutation_id": mutation.mutation_id,
                "strategy": decision.__dict__,
                "clock_offsets": clocks,
            })
            store.write_json(trial_dir / "metadata.json", metadata)
        except Exception as exc:
            metadata["status"] = "failed"
            metadata["end_time"] = utc_now()
            metadata["error"] = f"{type(exc).__name__}: {exc}"
            store.write_json(trial_dir / "metadata.json", metadata)
            raise

        remote_cfg = self.config.get("remote", {})
        remote_dir = remote_join(
            str(remote_cfg.get("capture_root", "/tmp/auto_fuzz_trials")),
            f"experiment_{store.experiment_id:04d}", f"trial_{trial_id:04d}",
        )
        remote_tx = remote_join(remote_dir, "tx.jsonl")
        source_manager = self.managers[source_bus]
        trial_cfg = self.config.get("trial", {})
        captured = False
        feedback_committed = False
        try:
            self.capture.start_all(store.experiment_id, trial_id)
            captured = True
            time.sleep(max(0.0, float(trial_cfg.get("capture_start_delay_seconds", 1.0))))
            self.capture.assert_all_running()
            metadata["status"] = "running"
            store.write_json(trial_dir / "metadata.json", metadata)
            command = self._sender_command(
                source_bus, store.experiment_id, mutation, remote_tx, trial_cfg
            )
            result = source_manager.run(command, timeout=float(trial_cfg.get("runner_timeout_seconds", 300.0)))
            (trial_dir / "sender.stdout.log").write_text(result.stdout, encoding="utf-8")
            (trial_dir / "sender.stderr.log").write_text(result.stderr, encoding="utf-8")
            rx_paths = self.capture.stop_and_download(trial_dir)
            captured = False
            source_manager.download(remote_tx, trial_dir / "tx.jsonl")

            phases = load_phase_times(trial_dir / "tx.jsonl")
            capture_quality = {
                bus: validate_capture_log(path, store.experiment_id)
                for bus, path in rx_paths.items()
            }
            metadata.update({
                "status": "captured",
                "end_time": utc_now(),
                "phase_times_ns": phases,
                "baseline_start": ns_to_iso(phases.get("baseline_start")),
                "baseline_end": ns_to_iso(phases.get("baseline_end")),
                "normal_start": ns_to_iso(phases.get("normal_start")),
                "normal_end": ns_to_iso(phases.get("normal_end")),
                "mutation_start": ns_to_iso(phases.get("mutation_start")),
                "mutation_end": ns_to_iso(phases.get("mutation_end")),
                "post_start": ns_to_iso(phases.get("recovery_start")),
                "post_end": ns_to_iso(phases.get("recovery_end")),
                "logs": {bus: str(path.name) for bus, path in rx_paths.items()},
                "capture_quality": capture_quality,
            })
            store.write_json(trial_dir / "metadata.json", metadata)

            analysis = analyze_trial(
                rx_paths=rx_paths,
                phase_times_ns=phases,
                mutation=mutation,
                thresholds=self.config.get("anomaly_thresholds", {}),
            )
            anomalies_doc = {
                "schema_version": 1,
                "trial_id": trial_id,
                "mutation_id": mutation.mutation_id,
                **analysis,
            }
            store.write_json(trial_dir / "anomalies.json", anomalies_doc)
            threshold = float(self.config.get("feedback", {}).get("interesting_score_threshold", 0.6))
            feedback = create_trial_feedback(trial_id, mutation, analysis["anomalies"], threshold)
            store.write_json(trial_dir / "feedback.json", feedback)
            metadata["status"] = "analyzed"
            store.write_json(trial_dir / "metadata.json", metadata)
            new_state = store.record_completed_trial(mutation, feedback)
            feedback_committed = True
            metadata["status"] = "completed"
            store.write_json(trial_dir / "metadata.json", metadata)
            self.print_summary(trial_id, mutation, analysis, feedback, selector.decide(new_state, random_seed))
            return feedback
        except Exception as exc:
            metadata["status"] = "analyzed" if feedback_committed else "failed"
            metadata["end_time"] = utc_now()
            error_field = "completion_error" if feedback_committed else "error"
            metadata[error_field] = f"{type(exc).__name__}: {exc}"
            store.write_json(trial_dir / "metadata.json", metadata)
            if captured:
                try:
                    self.capture.stop_and_download(trial_dir)
                except Exception as capture_exc:
                    metadata["capture_cleanup_error"] = str(capture_exc)
                    store.write_json(trial_dir / "metadata.json", metadata)
            raise

    @staticmethod
    def print_summary(
        trial_id: int,
        mutation: MutationCase,
        analysis: Mapping[str, Any],
        feedback: Mapping[str, Any],
        next_decision: StrategyDecision,
    ) -> None:
        byte = mutation.changed_bytes[0] if mutation.changed_bytes else "-"
        bit = mutation.changed_bits[0][1] if mutation.changed_bits else "-"
        summary = analysis["summary"]
        print("=" * 48)
        print(f"Trial {trial_id:02d} completed\n")
        print("Mutation")
        print(f"  ID       : {mutation.mutation_id}")
        print(f"  CAN ID   : 0x{mutation.can_id:X}")
        print(f"  Operator : {mutation.operator}")
        print(f"  Byte     : {byte}")
        print(f"  Bit      : {bit}\n")
        print("Anomaly")
        print(f"  Count          : {summary['anomaly_count']}")
        print(f"  Max Score      : {summary['maximum_score']:.2f}")
        print(f"  Cross-Bus      : {'YES' if summary['cross_bus'] else 'NO'}")
        print(f"  Interesting    : {'YES' if feedback['interesting'] else 'NO'}\n")
        print("Next Strategy")
        print(f"  Mode           : {next_decision.mode}")
        print(f"  Focus Byte     : {next_decision.focus_byte if next_decision.focus_byte is not None else '-'}")
        print(f"  Parent Mutation: {next_decision.parent_mutation_id if next_decision.parent_mutation_id is not None else '-'}")
        print("=" * 48)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Iteration-based three-Pi CAN fuzzing runner")
    parser.add_argument("--config", default="experiment_runner.yaml", help="SSH/trial YAML")
    parser.add_argument("--experiment-id", type=int, help="resume/create numeric experiment ID")
    parser.add_argument("--target-id", default="0x366")
    parser.add_argument("--source-bus", default="B_CAN")
    parser.add_argument("--trials", type=int, default=1, help="number of new completed trials")
    parser.add_argument("--random-seed", type=int, default=366)
    parser.add_argument("--reproduce-mutation-id", type=int, help="repeat one completed mutation exactly")
    parser.add_argument("--execute", action="store_true", help="connect over SSH and transmit CAN frames")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.trials < 1:
        raise ConfigurationError("trials must be at least 1")
    config, config_path = load_yaml_config(args.config)
    if not args.execute:
        print("[SAFE] Preview only: no SSH connection or CAN transmission was started.")
        print("[SAFE] Add --execute after reviewing experiment_runner.yaml and the isolated bench.")
        return 0
    source_bus = normalize_bus(args.source_bus)
    target_id = parse_int(args.target_id, "target ID")
    root_value = config.get("experiments_root", "experiments")
    root = Path(root_value).expanduser()
    if not root.is_absolute() and config_path is not None:
        root = config_path.parent / root
    root = root.resolve()
    experiment_id = args.experiment_id or next_experiment_id(root)
    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = Path(dbc_value).expanduser()
    if not dbc_path.is_absolute() and config_path is not None:
        dbc_path = config_path.parent / dbc_path
    dbc_path = dbc_path.resolve() if dbc_value else None
    snapshot = {
        "target_id": f"0x{target_id:X}", "source_bus": source_bus.upper(),
        "random_seed": args.random_seed, "runner_config": config,
    }
    store = ExperimentStore(root, experiment_id, snapshot)
    reconciled = store.reconcile_analyzed_trials()
    if reconciled:
        print("[RECOVER] completed analyzed trials: " + ", ".join(map(str, reconciled)))
    selector = TrialStrategySelector(config.get("feedback", {}))
    runner = ExperimentRunner(config)
    print(f"[Experiment Start] id={experiment_id}, source={source_bus}, target=0x{target_id:X}")
    try:
        for _ in range(args.trials):
            runner.run_trial(
                store=store, source_bus=source_bus, can_id=target_id,
                random_seed=args.random_seed, selector=selector, dbc_path=dbc_path,
                reproduce_mutation_id=args.reproduce_mutation_id,
            )
        store.complete()
    finally:
        runner.close()
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
