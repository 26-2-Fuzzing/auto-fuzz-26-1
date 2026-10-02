#!/usr/bin/env python3
"""Control-PC orchestrator for completed-trial iteration-based CAN fuzzing."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Mapping, Optional

from a5_0x366_mutator import A5BlinkmodiMutator, BASELINE_PAYLOAD, PROFILE_FAMILIES
from can_common import ConfigurationError, load_dbc, load_yaml_config, parse_can_data, parse_int
from experiment_store import ExperimentStore
from mutation_feedback import create_trial_feedback
from pair_analysis import analyze_trial_pair, recovery_returned_to_prestate
from paired_cycle import (
    advance_cycle, build_cycle_plan, make_cycle_mutation,
    next_cycle_entry, validate_cycle_plan,
)
from remote_capture import RemoteCapture
from remote_watchdog import run_supervised_sender
from ssh_manager import SSHManager, remote_join
from strategy_selector import StrategyDecision, TrialStrategySelector
from trial_analysis import _clock_alignment, analyze_trial, validate_capture_log
from trial_models import MutationCase, noop_case, utc_now
from trial_config import TRIAL_CONTRACT_VERSION, trial_settings


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


def paired_order(random_seed: int, experiment_id: int, pair_index: int) -> list[str]:
    """Alternate the order in two-set blocks, with a reproducible first side."""
    if pair_index < 1:
        raise ValueError("pair_index must be positive")
    first_bit = hashlib.sha256(f"{random_seed}:{experiment_id}".encode()).digest()[0] & 1
    mutation_first = bool(first_bit ^ ((pair_index - 1) & 1))
    return ["mutation", "noop"] if mutation_first else ["noop", "mutation"]


def matched_noop_case(
    trial_id: int, source_bus: str, can_id: int,
    original: bytes, random_seed: int, mutation: MutationCase,
) -> MutationCase:
    """Freeze an original-payload control with the mutation's TX cadence."""
    control = noop_case(trial_id, source_bus, can_id, original, random_seed)
    sequence = mutation.parameters.get("sequence")
    if sequence is None:
        return control
    if not isinstance(sequence, Mapping):
        raise ConfigurationError("Temporal pair sequence must be an object")
    frames = sequence.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ConfigurationError("Temporal pair sequence needs nonempty frames")
    try:
        interval_ms = float(sequence["interval_ms"])
        parsed = [parse_can_data(str(value)) for value in frames]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Temporal pair sequence has invalid timing or payloads") from exc
    if (not math.isfinite(interval_ms) or interval_ms < 50
            or any(len(payload) != len(original) for payload in parsed)):
        raise ConfigurationError("Temporal pair violates the 50 ms or payload-length contract")
    parameters: dict[str, Any] = {
        "sequence": {
            "name": "MATCHED_NOOP",
            "interval_ms": interval_ms,
            "frames": [original.hex().upper()] * len(parsed),
        }
    }
    for key in ("cycle_entry_index", "cycle_entry_id"):
        if key in mutation.parameters:
            parameters[key] = mutation.parameters[key]
    return replace(control, parameters=parameters)


def _with_trial_lock(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        store = kwargs.get("store")
        if not isinstance(store, ExperimentStore):
            raise TypeError("Trial execution requires an ExperimentStore")
        with self._execution_lock(store):
            return method(self, *args, **kwargs)
    return wrapped


def cycle_execution_context(
    config: Mapping[str, Any], *, source_bus: str, random_seed: int,
    undefined_max_bits: int, dbc_path: Path,
) -> dict[str, Any]:
    """Bind a resumed cycle to the same physical and analysis configuration."""
    return {
        "source_bus": source_bus,
        "target_id": "0x366",
        "random_seed": random_seed,
        "undefined_max_bits": undefined_max_bits,
        "collection_config": dict(config["trial"]),
        "analysis_config": dict(config.get("anomaly_thresholds", {})),
        "dbc_path": str(dbc_path.resolve()),
        "runner_config_sha256": hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest(),
    }


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
                key = f"{phase}_{event}"
                if key in phases:
                    raise ValueError(f"Duplicate TX phase marker: {key}")
                phases[key] = int(record["wall_time_ns"])
    required = {f"{phase}_{event}" for phase in PHASE_NAMES for event in ("start", "end")}
    missing = sorted(required - phases.keys())
    if missing:
        raise ValueError("TX phase markers are missing: " + ", ".join(missing))
    if not completed:
        raise ValueError("TX session did not contain a completed end marker")
    ordered = [phases[key] for key in (
        "baseline_start", "baseline_end", "normal_start", "normal_end",
        "mutation_start", "mutation_end", "recovery_start", "recovery_end",
    )]
    if any(start >= end for start, end in zip(ordered[::2], ordered[1::2])):
        raise ValueError("TX phases must have positive durations")
    if any(end > next_start for end, next_start in zip(ordered[1:-1:2], ordered[2::2])):
        raise ValueError("TX phases overlap or are out of order")
    return phases


def verify_capture_coverage(
    quality: Mapping[str, Any], phases: Mapping[str, int],
    *, bus: str, source_bus: str, clocks: Mapping[str, Any],
) -> dict[str, Any]:
    """Reject incomplete aligned captures before a partial log can imply loss."""
    started, ended = quality.get("session_start_ns"), quality.get("session_end_ns")
    if started is None or ended is None:
        raise ValueError(f"{bus} capture lacks timestamped session bounds")
    correction, uncertainty, alignment = _clock_alignment(bus, source_bus, clocks)
    covered = {
        **quality, "clock_alignment": alignment,
        "phase_coverage": "unverified_clock_reference",
    }
    if alignment in {"source_clock", "aligned"}:
        margin = uncertainty or 0
        if (int(started) + correction + margin > int(phases["baseline_start"])
                or int(ended) + correction - margin < int(phases["recovery_end"])):
            raise ValueError(f"{bus} capture does not cover baseline through recovery")
        covered["phase_coverage"] = "verified"
    return covered


def verify_trial_tx(
    tx_path: Path, *, mutation: MutationCase, experiment_id: int,
) -> None:
    """Require the sender's bounded, executed manifest before committing data."""
    starts: list[dict[str, Any]] = []
    ends: list[dict[str, Any]] = []
    sent: dict[str, list[dict[str, Any]]] = {"normal": [], "mutation": [], "recovery": []}
    with tx_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            kind = record.get("record_type")
            if kind == "tx_session_start":
                starts.append(record)
            elif kind == "tx_session_end":
                ends.append(record)
            elif kind == "can_tx":
                if record.get("status") != "sent":
                    raise ValueError("TX manifest contains an unsent frame")
                phase = record.get("phase")
                if phase not in sent:
                    raise ValueError(f"Unexpected TX phase: {phase}")
                sent[phase].append(record)
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("TX manifest must contain exactly one session start and end")
    start, end = starts[0], ends[0]
    session_id = start.get("tx_session_id")
    if not session_id or end.get("tx_session_id") != session_id:
        raise ValueError("TX manifest session ID mismatch")
    for item in (start, end):
        if (item.get("trial_contract_version") != TRIAL_CONTRACT_VERSION
                or item.get("trial_kind") != mutation.trial_kind
                or item.get("execute") is not True
                or str(item.get("experiment_id")) != str(experiment_id)):
            raise ValueError("TX manifest does not match the executed trial contract")
    if end.get("status") != "completed":
        raise ValueError("TX manifest did not complete")
    campaign = start.get("campaign") or {}
    stimulus = start.get("mutation") or {}
    if (campaign.get("enabled") is not True
            or int(stimulus.get("trial_mutation_id", -1)) != mutation.mutation_id
            or bool(stimulus.get("control_noop")) != (mutation.trial_kind == "noop")
            or campaign.get("normal_data_hex") != mutation.original_payload.hex().upper()):
        raise ValueError("TX manifest payload or trial ID differs from the prepared trial")
    sent_counts = end.get("phase_sent") or {}
    for phase in ("normal", "mutation"):
        if not sent[phase] or int(sent_counts.get(phase, -1)) != len(sent[phase]):
            raise ValueError(f"TX manifest {phase} frame count is missing or inconsistent")
    if len(sent["normal"]) > 200 or len(sent["mutation"]) > 20:
        raise ValueError("TX manifest exceeds calibration frame limits")
    expected_normal = mutation.original_payload.hex().upper()
    sequence = mutation.parameters.get("sequence")
    allowed_mutation = (
        {str(value).replace(" ", "").upper() for value in sequence.get("frames", [])}
        if isinstance(sequence, Mapping) else {mutation.mutated_payload.hex().upper()}
    )
    if mutation.trial_kind == "noop":
        allowed_mutation = {expected_normal}
    if isinstance(sequence, Mapping):
        raw_frames = sequence.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise ValueError("TX temporal sequence has no ordered frame plan")
        planned_order = [str(value).replace(" ", "").upper() for value in raw_frames]
        observed_order = [str(record.get("data_hex", "")).upper() for record in sent["mutation"]]
        if any(payload != planned_order[index % len(planned_order)]
               for index, payload in enumerate(observed_order)):
            raise ValueError("TX temporal sequence order differs from the frozen trial plan")
        if ("cycle_entry_index" in mutation.parameters
                and len(observed_order) < 2 * len(planned_order)):
            raise ValueError("TX cycle temporal case sent fewer than two complete patterns")
    for phase, records, allowed in (
        ("normal", sent["normal"], {expected_normal}),
        ("mutation", sent["mutation"], allowed_mutation),
    ):
        if any(
            int(record.get("arbitration_id", -1)) != mutation.can_id
            or str(record.get("data_hex", "")).upper() not in allowed
            or record.get("trial_kind") != mutation.trial_kind
            for record in records
        ):
            raise ValueError(f"TX manifest {phase} payload differs from prepared trial")
    restore = end.get("restore") or {}
    restored = [record for record in sent["recovery"] if record.get("kind") == "restore"]
    if (restore.get("status") != "sent" or int(restore.get("sent", 0)) != 1
            or len(restored) != 1
            or str(restored[0].get("data_hex", "")).upper() != expected_normal):
        raise ValueError("TX manifest lacks the required original-payload restore")


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
        self.config["trial"] = trial_settings(self.config.get("trial", {}))
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
        self._execution_mutex = threading.RLock()
        self._execution_lock_depth = 0
        self._locked_experiment_path: Optional[Path] = None

    @contextmanager
    def _execution_lock(self, store: ExperimentStore):
        """Serialize local trial planning and sending across runner processes."""
        with self._execution_mutex:
            lock_file = None
            if self._execution_lock_depth == 0:
                pairs_dir = store.path / "pairs"
                pairs_dir.mkdir(exist_ok=True)
                lock_file = (pairs_dir / ".runner.lock").open("a+")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    lock_file.close()
                    raise RuntimeError("Another local trial or pair is already running") from exc
                self._locked_experiment_path = store.path
            elif self._locked_experiment_path != store.path:
                raise RuntimeError("A runner cannot hold locks for two experiments at once")
            self._execution_lock_depth += 1
            try:
                yield
            finally:
                self._execution_lock_depth -= 1
                if lock_file is not None:
                    self._locked_experiment_path = None
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()

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
            return bus, {
                **best, "samples": samples, "warning": abs(best["offset_ms"]) > warning_ms,
                "reference_id": "shared-controller", "alignment_valid": True,
            }

        with ThreadPoolExecutor(max_workers=3) as executor:
            values = dict(executor.map(one, self.managers))
        for bus, value in values.items():
            if value["warning"]:
                print(f"[WARN] {bus} clock offset={value['offset_ms']:.3f} ms > {warning_ms:g} ms")
        return values

    def probe_payload(self, source_bus: str, can_id: int, *, require_live: bool = False) -> bytes:
        target = self.config.get("target", {})
        if not bool(target.get("probe_live_payload", True)):
            if require_live:
                raise ConfigurationError("Paired trials require a live source payload probe")
            configured = target.get("reference_payload")
            if configured is None:
                raise ConfigurationError("target.reference_payload is required when live probing is disabled")
            return parse_can_data(configured)
        channel = str(target.get("channel", "can0"))
        samples = max(1, int(target.get("probe_samples", 3)))
        if require_live and samples < 3:
            raise ConfigurationError("Paired trials require at least three live payload samples")
        timeout_seconds = float(target.get("probe_timeout_seconds", 6.0))
        mask = "1FFFFFFF" if can_id > 0x7FF else "7FF"
        result = self.managers[source_bus].run([
            "timeout", f"{timeout_seconds:g}", "candump", "-L", "-n", str(samples),
            f"{channel},{can_id:X}:{mask}",
        ], timeout=timeout_seconds + 2.0, check=False)
        payloads = parse_candump_payloads(result.stdout, can_id)
        if not payloads:
            if require_live:
                raise RuntimeError("Live source payload probe returned no frames for the pair")
            configured = target.get("reference_payload")
            if configured is not None:
                print("[WARN] live payload probe failed; using configured reference_payload")
                return parse_can_data(configured)
            raise RuntimeError(f"No 0x{can_id:X} payload captured from {source_bus}")
        counts = {payload: payloads.count(payload) for payload in set(payloads)}
        if require_live and (
            len(payloads) < samples or max(counts.values()) <= samples // 2
        ):
            raise RuntimeError("Live source payload samples have no stable majority")
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
        trial_cfg = trial_settings(trial_cfg)
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
            "--mutation-uid", mutation.mutation_uid,
            "--mutation-operator", mutation.operator,
            "--mutation-metadata-json", json.dumps(
                mutation.parameters, ensure_ascii=True, separators=(",", ":")
            ),
            "--generation-reason", mutation.generation_reason,
            "--random-seed", str(mutation.random_seed),
            "--baseline-duration", str(trial_cfg["baseline_seconds"]),
            "--normal-duration", str(trial_cfg["normal_seconds"]),
            "--mutation-duration", str(trial_cfg["mutation_seconds"]),
            "--recovery-duration", str(trial_cfg["post_seconds"]),
            "--interval-ms", str(trial_cfg["interval_ms"]),
            "--trial-contract-version", str(TRIAL_CONTRACT_VERSION),
            "--execute",
        ]
        if mutation.trial_kind == "noop":
            command.append("--control-noop")
        if mutation.parent_mutation_id is not None:
            command.extend(["--parent-mutation-id", str(mutation.parent_mutation_id)])
        return command

    @_with_trial_lock
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
        mutation_profile: Optional[str] = None,
        undefined_max_bits: int = 2,
        control_noop: bool = False,
        pair_id: Optional[str] = None,
        pair_position: Optional[int] = None,
        pair_order: Optional[list[str]] = None,
        expected_trial_id: Optional[int] = None,
        expected_original_payload: Optional[bytes] = None,
        prepared_case: Optional[MutationCase] = None,
        prepared_decision: Optional[StrategyDecision] = None,
        cycle_entry: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if control_noop and (reproduce_mutation_id is not None or mutation_profile is not None):
            raise ConfigurationError("A no-op control cannot reproduce or select a mutation profile")
        if pair_id is not None:
            if (pair_position not in (1, 2) or pair_order is None
                    or len(pair_order) != 2 or pair_order[pair_position - 1] != (
                        "noop" if control_noop else "mutation"
                    ) or expected_trial_id is None or expected_original_payload is None
                    or prepared_case is None or prepared_decision is None):
                raise ConfigurationError("A paired episode needs a complete frozen trial plan")
        elif any(value is not None for value in (
            pair_position, pair_order, expected_trial_id, expected_original_payload,
            prepared_case, prepared_decision, cycle_entry,
        )):
            raise ConfigurationError("Frozen trial plan fields require pair_id")
        cycle_path = store.path / "pairs" / "cycle.json"
        if cycle_path.is_file() and cycle_entry is None:
            raise RuntimeError("This experiment belongs to a paired cycle")
        if cycle_entry is not None and not cycle_path.is_file():
            raise RuntimeError("Cycle episode has no frozen cycle plan")
        if pair_id is None:
            pairs_dir = store.path / "pairs"
            for path in pairs_dir.glob("pair_*.json"):
                if not re.fullmatch(r"pair_\d+\.json", path.name):
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    if json.load(handle).get("status") != "completed":
                        raise RuntimeError("An unfinished pair must be resolved before a single trial")
        state = store.load_feedback_state()
        state = {**state, "next_mutation_id": store.next_mutation_id()}
        trial_id = store.next_trial_id()
        if expected_trial_id is not None and trial_id != expected_trial_id:
            raise RuntimeError(
                f"Pair expected trial {expected_trial_id}, but next available trial is {trial_id}"
            )
        trial_dir = store.create_trial(trial_id)
        metadata: dict[str, Any] = {
            "schema_version": 3,
            "status": "preparing",
            "experiment_id": store.experiment_id,
            "trial_id": trial_id,
            "target_id": f"0x{can_id:X}",
            "source_bus": source_bus.upper(),
            "random_seed": random_seed,
            "feedback_snapshot_total_trials": int(state.get("total_trials", 0)),
            "start_time": utc_now(),
            "trial_kind": "noop" if control_noop else "mutation",
            "collection_config": dict(self.config["trial"]),
            "analysis_config": dict(self.config.get("anomaly_thresholds", {})),
            "trial_contract_version": TRIAL_CONTRACT_VERSION,
            "dbc_path": str(dbc_path) if dbc_path else None,
        }
        if pair_id is not None:
            metadata.update({
                "pair_id": pair_id,
                "pair_position": pair_position,
                "pair_order": list(pair_order),
                "pair_role": "noop" if control_noop else "mutation",
            })
            if cycle_entry is not None:
                metadata["cycle_entry"] = dict(cycle_entry)
        store.write_json(trial_dir / "metadata.json", metadata)
        try:
            original = (
                self.probe_payload(source_bus, can_id, require_live=True)
                if pair_id is not None else self.probe_payload(source_bus, can_id)
            )
            if expected_original_payload is not None and original != expected_original_payload:
                raise RuntimeError("Live source payload differs from the frozen pair reference")
            if prepared_case is not None:
                if (prepared_case.trial_kind != ("noop" if control_noop else "mutation")
                        or prepared_case.source_bus != source_bus
                        or prepared_case.can_id != can_id
                        or prepared_case.original_payload != original
                        or (control_noop and prepared_case.mutation_id != trial_id)
                        or (not control_noop and prepared_case.mutation_id != state["next_mutation_id"])):
                    raise RuntimeError("Frozen pair case no longer matches the source or mutation sequence")
                mutation = prepared_case
                decision = prepared_decision
            elif control_noop:
                mutation = noop_case(trial_id, source_bus, can_id, original, random_seed)
                decision = StrategyDecision("CONTROL", "NOOP", None, None, mutation.generation_reason)
            elif reproduce_mutation_id is not None:
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
                    mutation_profile=mutation_profile,
                    dbc_path=dbc_path,
                    undefined_max_bits=undefined_max_bits,
                )
            signal = dbc_signal_metadata(mutation, dbc_path)
            if signal is not None and prepared_case is None:
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
        except BaseException as exc:
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
        remote_stdout = remote_join(remote_dir, "sender.stdout.log")
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
            run_supervised_sender(
                source_manager, command, remote_stdout,
                timeout=trial_cfg["runner_timeout_seconds"],
                poll_seconds=trial_cfg["watchdog_poll_seconds"],
                health_check=self.capture.assert_all_running,
            )
            source_manager.download(remote_stdout, trial_dir / "sender.stdout.log")
            rx_paths = self.capture.stop_and_download(trial_dir)
            captured = False
            source_manager.download(remote_tx, trial_dir / "tx.jsonl")

            verify_trial_tx(
                trial_dir / "tx.jsonl", mutation=mutation,
                experiment_id=store.experiment_id,
            )
            phases = load_phase_times(trial_dir / "tx.jsonl")
            capture_quality = {
                bus: verify_capture_coverage(
                    validate_capture_log(path, store.experiment_id), phases,
                    bus=bus, source_bus=source_bus, clocks=clocks,
                )
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
                experiment_dir=store.path,
                current_trial_id=trial_id,
                dbc_path=dbc_path,
                clock_offsets=clocks,
                trial_kind=mutation.trial_kind,
            )
            anomalies_doc = {
                "schema_version": 1,
                "trial_id": trial_id,
                "mutation_id": mutation.mutation_id,
                **analysis,
            }
            store.write_json(trial_dir / "anomalies.json", anomalies_doc)
            threshold = float(self.config.get("feedback", {}).get("interesting_score_threshold", 0.6))
            feedback = create_trial_feedback(
                trial_id, mutation, analysis["anomalies"], threshold, prior_state=state
            )
            store.write_json(trial_dir / "feedback.json", feedback)
            metadata["status"] = "analyzed"
            store.write_json(trial_dir / "metadata.json", metadata)
            new_state = store.record_completed_trial(mutation, feedback)
            feedback_committed = True
            metadata["status"] = "completed"
            store.write_json(trial_dir / "metadata.json", metadata)
            self.print_summary(trial_id, mutation, analysis, feedback, selector.decide(new_state, random_seed))
            return feedback
        except BaseException as exc:
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
            for remote_path, local_name in ((remote_tx, "tx.jsonl"), (remote_stdout, "sender.stdout.log")):
                try:
                    if not (trial_dir / local_name).exists():
                        source_manager.download(remote_path, trial_dir / local_name)
                except Exception:
                    pass  # The failed metadata remains authoritative if collection is unavailable.
            raise

    @_with_trial_lock
    def run_paired_set(
        self,
        *,
        store: ExperimentStore,
        source_bus: str,
        can_id: int,
        random_seed: int,
        selector: TrialStrategySelector,
        dbc_path: Optional[Path],
        mutation_profile: Optional[str] = None,
        undefined_max_bits: int = 2,
        scheduled_mutation: Optional[MutationCase] = None,
        cycle_entry: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute or reconcile one frozen pair without repeating an uncertain TX."""
        pairs_dir = store.path / "pairs"
        pairs_dir.mkdir(exist_ok=True)
        cycle_path = pairs_dir / "cycle.json"
        if cycle_path.is_file() != (cycle_entry is not None):
            raise RuntimeError("Cycle and standalone pairs cannot share one experiment")
        if (scheduled_mutation is None) != (cycle_entry is None):
            raise ConfigurationError("A scheduled mutation needs a cycle entry")
        indexed_paths = sorted(
            (int(match.group(1)), path)
            for path in pairs_dir.iterdir()
            if (match := re.fullmatch(r"pair_(\d+)\.json", path.name))
        )
        pending = []
        for _, path in indexed_paths:
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if document.get("status") != "completed":
                pending.append((path, document))
        if len(pending) > 1:
            raise RuntimeError("Multiple unfinished pairs need manual inspection")
        if not pending and indexed_paths:
            latest_path = indexed_paths[-1][1]
            with latest_path.open("r", encoding="utf-8") as handle:
                latest = json.load(handle)
            comparability = latest.get("comparability_status")
            if comparability is None:
                report_name = latest.get("pair_report")
                if not isinstance(report_name, str) or Path(report_name).name != report_name:
                    raise RuntimeError("Latest pair has no safe comparison report")
                with (pairs_dir / report_name).open("r", encoding="utf-8") as handle:
                    comparability = (json.load(handle).get("comparability") or {}).get("status")
            if comparability != "comparable":
                raise RuntimeError("Latest pair comparison is inconclusive; campaign is paused")

        if pending:
            pair_path, pair = pending[0]
            pair_id = pair_path.stem
            if pair.get("cycle_entry") != (dict(cycle_entry) if cycle_entry else None):
                raise RuntimeError(f"{pair_id} belongs to a different cycle entry")
            expected = {
                "source_bus": source_bus,
                "target_id": f"0x{can_id:X}",
                "random_seed": random_seed,
                "mutation_profile": mutation_profile,
                "undefined_max_bits": undefined_max_bits,
                "collection_config": dict(self.config["trial"]),
                "analysis_config": dict(self.config.get("anomaly_thresholds", {})),
                "dbc_path": str(dbc_path) if dbc_path else None,
            }
            if any(pair.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"{pair_id} was prepared with different settings")
        else:
            pair_index = indexed_paths[-1][0] + 1 if indexed_paths else 1
            pair_id = f"pair_{pair_index:04d}"
            pair_path = pairs_dir / f"{pair_id}.json"
            first_trial_id = store.next_trial_id()
            state = store.load_feedback_state()
            state = {**state, "next_mutation_id": store.next_mutation_id()}
            original = self.probe_payload(source_bus, can_id, require_live=True)
            if scheduled_mutation is None:
                mutation, decision = selector.select_mutation(
                    state=state, original_payload=original, source_bus=source_bus,
                    can_id=can_id, random_seed=random_seed,
                    mutation_profile=mutation_profile,
                    dbc_path=dbc_path, undefined_max_bits=undefined_max_bits,
                )
            else:
                mutation = scheduled_mutation
                decision = StrategyDecision(
                    "EXPLORE", "PAIRED_CYCLE", None, None,
                    f"Scheduled {cycle_entry['family']} case {cycle_entry['case']}",
                )
            if (mutation.trial_kind != "mutation" or mutation.original_payload != original
                    or mutation.source_bus != source_bus or mutation.can_id != can_id
                    or mutation.mutation_id != state["next_mutation_id"]):
                raise RuntimeError("The selected mutation does not match the frozen pair reference")
            signal = dbc_signal_metadata(mutation, dbc_path)
            if signal is not None:
                mutation = replace(mutation, signal=signal)
            order = paired_order(random_seed, store.experiment_id, pair_index)
            noop_trial_id = first_trial_id if order[0] == "noop" else first_trial_id + 1
            control = matched_noop_case(
                noop_trial_id, source_bus, can_id, original, random_seed, mutation
            )
            pair = {
                "schema_version": 1,
                "pair_id": pair_id,
                "status": "prepared",
                "pair_order": order,
                "source_bus": source_bus,
                "target_id": f"0x{can_id:X}",
                "random_seed": random_seed,
                "mutation_profile": mutation_profile,
                "undefined_max_bits": undefined_max_bits,
                "collection_config": dict(self.config["trial"]),
                "analysis_config": dict(self.config.get("anomaly_thresholds", {})),
                "dbc_path": str(dbc_path) if dbc_path else None,
                "baseline_payload": original.hex().upper(),
                "frozen_mutation": mutation.to_dict(),
                "frozen_noop": control.to_dict(),
                "strategy": decision.__dict__,
                "cycle_entry": dict(cycle_entry) if cycle_entry else None,
                "first_trial_id": first_trial_id,
                "second_trial_id": first_trial_id + 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            store.write_json(pair_path, pair)

        def save_pair(status: str, **updates: Any) -> None:
            pair.update(updates)
            pair["status"] = status
            pair["updated_at"] = utc_now()
            store.write_json(pair_path, pair)

        if pair.get("pair_id") != pair_id or pair.get("pair_order") not in (
            ["noop", "mutation"], ["mutation", "noop"]
        ):
            raise RuntimeError("Pair manifest identity or order is invalid")
        mutation = MutationCase.from_dict(pair["frozen_mutation"])
        original = bytes.fromhex(pair["baseline_payload"])
        if (mutation.trial_kind != "mutation" or mutation.original_payload != original
                or mutation.source_bus != source_bus or mutation.can_id != can_id):
            raise RuntimeError("Frozen mutation and pair reference disagree")
        control = MutationCase.from_dict(pair["frozen_noop"])
        decision = StrategyDecision(**pair["strategy"])
        first_trial_id = int(pair["first_trial_id"])
        second_trial_id = int(pair["second_trial_id"])
        if second_trial_id != first_trial_id + 1:
            raise RuntimeError("Pair trial IDs must be consecutive")
        expected_noop_id = first_trial_id if pair["pair_order"][0] == "noop" else second_trial_id
        expected_control = matched_noop_case(
            expected_noop_id, source_bus, can_id, original, random_seed, mutation
        )
        if (control.trial_kind != "noop" or control.mutation_id != expected_noop_id
                or control.original_payload != original
                or control.parameters != expected_control.parameters):
            raise RuntimeError("Frozen no-op schedule does not match the mutation schedule")

        def completed_episode(position: int, kind: str, trial_id: int) -> bool:
            trial_dir = store.path / f"trial_{trial_id:04d}"
            if not trial_dir.exists():
                return False
            try:
                with (trial_dir / "metadata.json").open("r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                with (trial_dir / "mutation.json").open("r", encoding="utf-8") as handle:
                    recorded = MutationCase.from_dict(json.load(handle))
                files = ("feedback.json", "anomalies.json", "tx.jsonl",
                         "p_can.jsonl", "b_can.jsonl", "i_can.jsonl")
                if not all((trial_dir / file).is_file() for file in files):
                    raise RuntimeError("completed episode lacks required evidence files")
                if (metadata.get("status") != "completed" or metadata.get("pair_id") != pair_id
                        or metadata.get("pair_position") != position
                        or metadata.get("pair_order") != pair["pair_order"]
                        or metadata.get("pair_role") != kind
                        or metadata.get("cycle_entry") != pair.get("cycle_entry")
                        or recorded.trial_kind != kind or recorded.original_payload != original
                        or recorded != (mutation if kind == "mutation" else control)):
                    raise RuntimeError("existing episode does not match the frozen pair plan")
                state = store.load_feedback_state()
                ledger = (
                    state.get("control_trial_ids", []) if kind == "noop"
                    else state.get("completed_trial_ids", [])
                )
                if trial_id not in {int(value) for value in ledger}:
                    raise RuntimeError("episode completion is absent from the feedback ledger")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Trial {trial_id} exists but cannot be safely resumed") from exc
            return True

        for position, (kind, trial_id) in enumerate(zip(
            pair["pair_order"], (first_trial_id, second_trial_id)
        ), start=1):
            if completed_episode(position, kind, trial_id):
                if position == 1 and pair["status"] == "prepared":
                    save_pair("first_completed")
                continue
            if (position == 1 and pair["status"] in {
                    "first_completed", "analysis_pending", "completed"
                }) or (position == 2 and pair["status"] in {
                    "analysis_pending", "completed"
                }):
                raise RuntimeError(
                    f"{pair_id} is missing a previously completed trial; refusing reinjection"
                )
            # A crash after a fully completed first episode can safely resume
            # at the second episode. An incomplete trial or failed state gate
            # never gets another automatic injection attempt.
            resume_after_completed_first = (
                position == 2 and pair["status"] == "blocked"
                and "recovery_gate" not in pair
            )
            if pair["status"] == "blocked" and not resume_after_completed_first:
                raise RuntimeError(f"{pair_id} is blocked; incomplete trials are never reinjected")
            if position == 2:
                try:
                    gate = recovery_returned_to_prestate(store.path, first_trial_id)
                except BaseException as exc:
                    save_pair("blocked", recovery_gate={
                        "status": "inconclusive", "reasons": [f"{type(exc).__name__}: {exc}"],
                    })
                    raise
                save_pair("first_completed", recovery_gate=gate)
                if gate.get("status") != "stable":
                    save_pair("blocked")
                    raise RuntimeError(f"{pair_id} first recovery did not pass the prestate gate")
            case = control if kind == "noop" else mutation
            episode_decision = (
                StrategyDecision("CONTROL", "NOOP", None, None, case.generation_reason)
                if kind == "noop" else decision
            )
            try:
                self.run_trial(
                    store=store, source_bus=source_bus, can_id=can_id,
                    random_seed=random_seed, selector=selector, dbc_path=dbc_path,
                    control_noop=kind == "noop", pair_id=pair_id,
                    pair_position=position, pair_order=list(pair["pair_order"]),
                    expected_trial_id=trial_id, expected_original_payload=original,
                    prepared_case=case, prepared_decision=episode_decision,
                    cycle_entry=pair.get("cycle_entry"),
                )
            except BaseException as exc:
                save_pair("blocked", interruption=f"{type(exc).__name__}: {exc}")
                raise
            save_pair("first_completed" if position == 1 else "analysis_pending")

        mutation_trial_id = (
            first_trial_id if pair["pair_order"][0] == "mutation" else second_trial_id
        )
        noop_trial_id = (
            first_trial_id if pair["pair_order"][0] == "noop" else second_trial_id
        )
        report = analyze_trial_pair(
            store.path, mutation_trial_id, noop_trial_id, pair_id=pair_id
        )
        report_path = pairs_dir / f"{pair_id}_report.json"
        store.write_json(report_path, report)
        comparability = (report.get("comparability") or {}).get("status", "inconclusive")
        save_pair("completed", pair_report=report_path.name,
                  comparability_status=comparability)
        print(f"[PAIR] {pair_id}: {pair['pair_order'][0]} → {pair['pair_order'][1]}; "
              f"comparison={comparability}")
        if comparability != "comparable":
            raise RuntimeError(f"{pair_id} comparison is inconclusive; campaign is paused")
        return report

    @_with_trial_lock
    def run_paired_cycle(
        self,
        *,
        store: ExperimentStore,
        source_bus: str,
        random_seed: int,
        selector: TrialStrategySelector,
        dbc_path: Path,
        undefined_max_bits: int = 2,
        max_sets: int = 10,
    ) -> dict[str, Any]:
        """Run a bounded slice of one frozen eight-family 0x366 catalogue."""
        if max_sets < 1:
            raise ConfigurationError("cycle-max-sets must be at least 1")
        if dbc_path is None or not dbc_path.is_file():
            raise ConfigurationError("Paired cycle requires an available 0x366 DBC")
        pairs_dir = store.path / "pairs"
        pairs_dir.mkdir(exist_ok=True)
        cycle_path = pairs_dir / "cycle.json"
        context = cycle_execution_context(
            self.config, source_bus=source_bus, random_seed=random_seed,
            undefined_max_bits=undefined_max_bits, dbc_path=dbc_path,
        )
        if cycle_path.is_file():
            reconciled_trials = store.reconcile_analyzed_trials()
            if reconciled_trials:
                print("[RECOVER] completed analyzed trials: "
                      + ", ".join(map(str, reconciled_trials)))
            with cycle_path.open("r", encoding="utf-8") as handle:
                plan = json.load(handle)
            validate_cycle_plan(plan, dbc_path=dbc_path)
            if plan.get("execution_context") != context:
                raise RuntimeError("Frozen cycle was prepared with different runner settings")
            original = bytes.fromhex(plan["baseline_payload"])
            rebuilt = build_cycle_plan(
                dbc_path, original, source_bus=source_bus,
                random_seed=random_seed, undefined_max_bits=undefined_max_bits,
                mutation_duration_s=self.config["trial"]["mutation_seconds"],
                mutation_interval_ms=self.config["trial"]["interval_ms"],
            )
            if rebuilt["catalog_sha256"] != plan["catalog_sha256"]:
                raise RuntimeError("The candidate catalogue changed since cycle creation")
        else:
            existing_trials = any(path.is_dir() for path in store.path.glob("trial_*"))
            existing_pairs = any(
                re.fullmatch(r"pair_\d+\.json", path.name)
                for path in pairs_dir.iterdir() if path.is_file()
            )
            if existing_trials or existing_pairs:
                raise RuntimeError("A new paired cycle needs an experiment without prior trials or pairs")
            original = self.probe_payload(source_bus, 0x366, require_live=True)
            plan = build_cycle_plan(
                dbc_path, original, source_bus=source_bus,
                random_seed=random_seed, undefined_max_bits=undefined_max_bits,
                mutation_duration_s=self.config["trial"]["mutation_seconds"],
                mutation_interval_ms=self.config["trial"]["interval_ms"],
            )
            plan["execution_context"] = context
            plan["cycle_id"] = "cycle_0001"
            plan["created_at"] = utc_now()
            store.write_json(cycle_path, plan)

        def cycle_link(entry: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "catalog_sha256": plan["catalog_sha256"],
                "entry_index": entry["index"],
                "entry_id": entry["entry_id"],
                "family": entry["family"],
                "case": entry["case"],
                "tx_fingerprint": entry["tx_fingerprint"],
            }

        def pair_for_entry(link: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
            matches = []
            for path in pairs_dir.iterdir():
                if not path.is_file() or not re.fullmatch(r"pair_\d+\.json", path.name):
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    document = json.load(handle)
                recorded = document.get("cycle_entry")
                if not isinstance(recorded, Mapping):
                    raise RuntimeError(f"{path.name} is not linked to the frozen cycle")
                if recorded.get("catalog_sha256") != plan["catalog_sha256"]:
                    raise RuntimeError(f"{path.name} belongs to another candidate catalogue")
                if recorded.get("entry_index") == link["entry_index"]:
                    if recorded != link:
                        raise RuntimeError(f"{path.name} conflicts with the scheduled cycle entry")
                    matches.append((path, document))
            if len(matches) > 1:
                raise RuntimeError("Two pairs claim the same cycle entry")
            return matches[0] if matches else None

        def reconcile_completed_pair(
            existing: tuple[Path, dict[str, Any]], entry: Mapping[str, Any]
        ) -> str:
            path, document = existing
            if document.get("status") != "completed":
                raise RuntimeError(f"{path.stem} is unfinished and cannot advance the cycle")
            if document.get("comparability_status") != "comparable":
                raise RuntimeError(f"{path.stem} is inconclusive; cycle remains paused")
            report_name = document.get("pair_report")
            if not isinstance(report_name, str) or Path(report_name).name != report_name:
                raise RuntimeError(f"{path.stem} has no safe comparison report")
            with (pairs_dir / report_name).open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            if (report.get("pair_id") != path.stem
                    or (report.get("comparability") or {}).get("status") != "comparable"):
                raise RuntimeError(f"{path.stem} report does not confirm comparability")
            frozen = MutationCase.from_dict(document["frozen_mutation"])
            expected = make_cycle_mutation(
                entry, mutation_id=frozen.mutation_id,
                source_bus=source_bus, original_payload=original,
                random_seed=random_seed,
            )
            if (frozen.source_bus != expected.source_bus
                    or frozen.can_id != expected.can_id
                    or frozen.original_payload != expected.original_payload
                    or frozen.mutated_payload != expected.mutated_payload
                    or frozen.operator != expected.operator
                    or frozen.parameters != expected.parameters):
                raise RuntimeError(f"{path.stem} mutation differs from the scheduled case")
            first_id = int(document.get("first_trial_id", -1))
            second_id = int(document.get("second_trial_id", -1))
            if first_id < 1 or second_id != first_id + 1:
                raise RuntimeError(f"{path.stem} has invalid recorded trial IDs")
            order = document.get("pair_order")
            if order not in (["noop", "mutation"], ["mutation", "noop"]):
                raise RuntimeError(f"{path.stem} has invalid recorded episode order")
            required_files = (
                "metadata.json", "mutation.json", "feedback.json", "anomalies.json",
                "tx.jsonl", "p_can.jsonl", "b_can.jsonl", "i_can.jsonl",
            )
            for position, trial_id in enumerate((first_id, second_id), start=1):
                trial_dir = store.path / f"trial_{trial_id:04d}"
                if not all((trial_dir / name).is_file() for name in required_files):
                    raise RuntimeError(f"{path.stem} trial {trial_id} evidence is missing")
                with (trial_dir / "metadata.json").open("r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                with (trial_dir / "mutation.json").open("r", encoding="utf-8") as handle:
                    recorded_case = MutationCase.from_dict(json.load(handle))
                if (metadata.get("status") != "completed"
                        or metadata.get("pair_id") != path.stem
                        or metadata.get("pair_position") != position
                        or metadata.get("pair_order") != order
                        or metadata.get("cycle_entry") != document.get("cycle_entry")
                        or recorded_case.trial_kind != order[position - 1]
                        or recorded_case.original_payload != original
                        or (recorded_case.trial_kind == "mutation" and recorded_case != frozen)
                        or (recorded_case.trial_kind == "noop"
                            and recorded_case.mutated_payload != original)):
                    raise RuntimeError(f"{path.stem} trial {trial_id} record no longer matches")
            return path.stem

        # The cursor alone is not evidence: audit every recorded completion
        # before starting the next scheduled exposure.
        for recorded in plan["completed_pairs"]:
            entry = plan["entries"][recorded["entry_index"]]
            pair_id = recorded["pair_id"]
            if not re.fullmatch(r"pair_\d+", pair_id):
                raise RuntimeError("Cycle ledger contains an invalid pair ID")
            path = pairs_dir / f"{pair_id}.json"
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if document.get("cycle_entry") != cycle_link(entry):
                raise RuntimeError(f"{pair_id} no longer matches the cycle ledger")
            reconcile_completed_pair((path, document), entry)

        newly_executed = 0
        reconciled = 0
        while newly_executed < max_sets:
            entry = next_cycle_entry(plan)
            if entry is None:
                break
            link = cycle_link(entry)
            existing = pair_for_entry(link)
            if existing is None or existing[1].get("status") != "completed":
                if (existing is None and store.next_trial_id()
                        != 2 * len(plan["completed_pairs"]) + 1):
                    raise RuntimeError(
                        "Cycle trial evidence exists without a linked pair manifest"
                    )
                mutation = make_cycle_mutation(
                    entry, mutation_id=store.next_mutation_id(),
                    source_bus=source_bus, original_payload=original,
                    random_seed=random_seed,
                )
                self.run_paired_set(
                    store=store, source_bus=source_bus, can_id=0x366,
                    random_seed=random_seed, selector=selector, dbc_path=dbc_path,
                    undefined_max_bits=undefined_max_bits,
                    scheduled_mutation=mutation, cycle_entry=link,
                )
                newly_executed += 1
                existing = pair_for_entry(link)
                if existing is None:
                    raise RuntimeError("Completed cycle pair has no persisted manifest")
            else:
                reconciled += 1
            pair_id = reconcile_completed_pair(existing, entry)
            plan = advance_cycle(plan, entry["index"], pair_id)
            plan["updated_at"] = utc_now()
            store.write_json(cycle_path, plan)
        remaining = plan["scheduled_count"] - len(plan["completed_pairs"])
        print(f"[CYCLE] {len(plan['completed_pairs'])}/{plan['scheduled_count']} comparable pairs; "
              f"{remaining} remaining; {newly_executed} executed this invocation")
        return {
            "status": plan["status"],
            "scheduled_count": plan["scheduled_count"],
            "completed_count": len(plan["completed_pairs"]),
            "remaining_count": remaining,
            "executed_this_invocation": newly_executed,
            "reconciled_this_invocation": reconciled,
            "cycle_manifest": str(cycle_path),
        }

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
        print("No-op control" if mutation.trial_kind == "noop" else "Mutation")
        print(f"  ID       : {mutation.mutation_id}")
        print(f"  CAN ID   : 0x{mutation.can_id:X}")
        print(f"  Operator : {mutation.operator}")
        print(f"  Byte     : {byte}")
        print(f"  Bit      : {bit}\n")
        print("Observed changes")
        print(f"  Candidates     : {summary['candidate_count']}")
        print(f"  Inconclusive   : {summary['inconclusive_count']}")
        print(f"  Max Score      : {summary['maximum_score']:.2f}")
        print(f"  Cross-Bus      : {'YES' if summary['cross_bus'] else 'NO'}")
        print(f"  Validation     : {feedback.get('verification_status', 'legacy').upper()}")
        print(f"  Feedback Used  : NO\n")
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
    parser.add_argument("--trials", type=int, help="number of new completed single trials (default: 1)")
    parser.add_argument("--paired-sets", type=int, default=0,
                        help="run two full no-op/mutation episodes per set, with balanced order")
    parser.add_argument("--paired-cycle", action="store_true",
                        help="resume a frozen catalogue of distinct 0x366 family pairs")
    parser.add_argument("--cycle-max-sets", type=int,
                        help="maximum newly executed pairs this invocation (default: 10)")
    parser.add_argument("--random-seed", type=int, default=366)
    parser.add_argument("--reproduce-mutation-id", type=int, help="repeat one completed mutation exactly")
    parser.add_argument("--control-noop", action="store_true", help="send the original payload in the comparison slot; never add control feedback")
    parser.add_argument(
        "--mutation-profile", choices=tuple(PROFILE_FAMILIES),
        help="DBC-driven 0x366 campaign; omitted preserves the existing generic mutator",
    )
    parser.add_argument(
        "--undefined-max-bits", type=int, default=2,
        help="maximum changed bits in an undefined_bit_multi case (default: 2)",
    )
    parser.add_argument(
        "--print-0x366-map", action="store_true",
        help="print the DBC-derived 64-bit occupancy and enum report, then exit",
    )
    parser.add_argument("--execute", action="store_true", help="connect over SSH and transmit CAN frames")
    return parser


def run(args: argparse.Namespace) -> int:
    explicit_trials = args.trials
    trial_count = 1 if explicit_trials is None else explicit_trials
    if trial_count < 1:
        raise ConfigurationError("trials must be at least 1")
    paired_sets = getattr(args, "paired_sets", 0)
    paired_cycle = getattr(args, "paired_cycle", False)
    cycle_max_sets = getattr(args, "cycle_max_sets", None)
    if paired_sets < 0:
        raise ConfigurationError("paired-sets cannot be negative")
    if paired_sets and (explicit_trials is not None or args.control_noop
                        or args.reproduce_mutation_id is not None):
        raise ConfigurationError(
            "--paired-sets cannot be combined with --trials, --control-noop, or reproduction"
        )
    if paired_cycle and (paired_sets or explicit_trials is not None or args.control_noop
                         or args.reproduce_mutation_id is not None
                         or args.mutation_profile is not None or args.print_0x366_map):
        raise ConfigurationError(
            "--paired-cycle cannot be combined with another trial mode or mutation profile"
        )
    if cycle_max_sets is not None and not paired_cycle:
        raise ConfigurationError("--cycle-max-sets requires --paired-cycle")
    cycle_max_sets = 10 if cycle_max_sets is None else cycle_max_sets
    if cycle_max_sets < 1:
        raise ConfigurationError("cycle-max-sets must be at least 1")
    if args.undefined_max_bits < 2:
        raise ConfigurationError("undefined-max-bits must be at least 2")
    config, config_path = load_yaml_config(args.config)
    config["trial"] = trial_settings(config.get("trial", {}))
    if paired_cycle and config.get("feedback", {}).get("enabled", False) is not False:
        raise ConfigurationError("Paired cycle requires feedback.enabled: false")
    if args.control_noop and (args.reproduce_mutation_id is not None or args.mutation_profile is not None):
        raise ConfigurationError("--control-noop cannot be combined with reproduction or a mutation profile")
    target_id = parse_int(args.target_id, "target ID")
    root_value = config.get("experiments_root", "experiments")
    root = Path(root_value).expanduser()
    if not root.is_absolute() and config_path is not None:
        root = config_path.parent / root
    root = root.resolve()
    experiment_id = args.experiment_id or next_experiment_id(root)
    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = Path(dbc_value).expanduser() if dbc_value else None
    if dbc_path is not None and not dbc_path.is_absolute() and config_path is not None:
        dbc_path = config_path.parent / dbc_path
    dbc_path = dbc_path.resolve() if dbc_value else None
    if args.print_0x366_map:
        if dbc_path is None:
            raise ConfigurationError("--print-0x366-map requires dbc in runner config")
        target = A5BlinkmodiMutator(dbc_path)
        print(target.occupancy_text())
        print("\nDBC timing")
        print(json.dumps(target.timing, ensure_ascii=False, indent=2))
        print("\nUndefined enum report")
        print(json.dumps(target.enum_report(), ensure_ascii=False, indent=2))
        return 0
    if args.mutation_profile is not None and target_id != 0x366:
        raise ConfigurationError("--mutation-profile is dedicated to CAN ID 0x366")
    if paired_cycle and target_id != 0x366:
        raise ConfigurationError("--paired-cycle is dedicated to CAN ID 0x366")
    source_bus = normalize_bus(args.source_bus)
    if not args.execute:
        print("[SAFE] Preview only: no SSH connection or CAN transmission was started.")
        print("[SAFE] Add --execute after reviewing experiment_runner.yaml and the isolated bench.")
        if paired_sets:
            phase = config["trial"]
            print(
                f"[PAIR] {paired_sets} paired set(s), two complete episodes per set "
                f"({phase['baseline_seconds']}/"
                f"{phase['normal_seconds']}/"
                f"{phase['mutation_seconds']}/"
                f"{phase['post_seconds']} seconds each)"
            )
        if paired_cycle:
            if dbc_path is None or not dbc_path.is_file():
                raise ConfigurationError("Paired cycle preview requires an available 0x366 DBC")
            cycle_file = (
                root / f"experiment_{args.experiment_id:04d}" / "pairs" / "cycle.json"
                if args.experiment_id is not None else None
            )
            if cycle_file is not None and cycle_file.is_file():
                with cycle_file.open("r", encoding="utf-8") as handle:
                    preview_plan = json.load(handle)
                validate_cycle_plan(preview_plan, dbc_path=dbc_path)
                preview_context = cycle_execution_context(
                    config, source_bus=source_bus, random_seed=args.random_seed,
                    undefined_max_bits=args.undefined_max_bits, dbc_path=dbc_path,
                )
                if preview_plan.get("execution_context") != preview_context:
                    raise RuntimeError("Frozen cycle was prepared with different runner settings")
                completed = len(preview_plan["completed_pairs"])
                remaining = preview_plan["scheduled_count"] - completed
                phase_settings = preview_plan["execution_context"]["collection_config"]
                print(f"[CYCLE] Frozen experiment {args.experiment_id}: "
                      f"{completed}/{preview_plan['scheduled_count']} comparable pairs, "
                      f"{remaining} remaining")
            else:
                reference = config.get("target", {}).get("reference_payload")
                preview_payload = (
                    parse_can_data(reference) if reference is not None else BASELINE_PAYLOAD
                )
                preview_plan = build_cycle_plan(
                    dbc_path, preview_payload, source_bus=source_bus,
                    random_seed=args.random_seed, undefined_max_bits=args.undefined_max_bits,
                    mutation_duration_s=config["trial"]["mutation_seconds"],
                    mutation_interval_ms=config["trial"]["interval_ms"],
                )
                remaining = preview_plan["scheduled_count"]
                phase_settings = config["trial"]
                payload_source = (
                    "configured reference" if reference is not None else "canonical 0x366 reference"
                )
                print(
                    f"[CYCLE] Provisional from {payload_source}: "
                    f"{preview_plan['scheduled_count']} distinct pairs, "
                    f"{preview_plan['skipped_count']} skipped duplicates/unsafe cases"
                )
            phase_min = sum(phase_settings[key] for key in (
                "baseline_seconds", "normal_seconds", "mutation_seconds", "post_seconds"
            )) * 2
            total_min = remaining * phase_min
            print(f"[CYCLE] Minimum phase time {total_min / 3600:.2f} hours "
                  f"({phase_min:g} seconds per pair); this invocation cap {cycle_max_sets} pairs")
        if args.mutation_profile:
            print(f"[PROFILE] {args.mutation_profile} / undefined-max-bits={args.undefined_max_bits}")
        return 0
    snapshot = {
        "target_id": f"0x{target_id:X}", "source_bus": source_bus.upper(),
        "random_seed": args.random_seed,
        "mutation_profile": args.mutation_profile,
        "undefined_max_bits": args.undefined_max_bits,
        "runner_config": config,
        "trial_kind": "paired_cycle" if paired_cycle else "paired" if paired_sets
                      else "noop" if args.control_noop else "mutation",
    }
    store = ExperimentStore(root, experiment_id, snapshot)
    selector = TrialStrategySelector(config.get("feedback", {}))
    runner = ExperimentRunner(config)
    print(f"[Experiment Start] id={experiment_id}, source={source_bus}, target=0x{target_id:X}")
    try:
        with runner._execution_lock(store):
            if paired_cycle:
                result = runner.run_paired_cycle(
                    store=store, source_bus=source_bus, random_seed=args.random_seed,
                    selector=selector, dbc_path=dbc_path,
                    undefined_max_bits=args.undefined_max_bits, max_sets=cycle_max_sets,
                )
                if result["status"] == "completed":
                    store.complete()
            else:
                reconciled = store.reconcile_analyzed_trials()
                if reconciled:
                    print("[RECOVER] completed analyzed trials: "
                          + ", ".join(map(str, reconciled)))
                for _ in range(paired_sets or trial_count):
                    if paired_sets:
                        runner.run_paired_set(
                            store=store, source_bus=source_bus, can_id=target_id,
                            random_seed=args.random_seed, selector=selector, dbc_path=dbc_path,
                            mutation_profile=args.mutation_profile,
                            undefined_max_bits=args.undefined_max_bits,
                        )
                    else:
                        runner.run_trial(
                            store=store, source_bus=source_bus, can_id=target_id,
                            random_seed=args.random_seed, selector=selector, dbc_path=dbc_path,
                            reproduce_mutation_id=args.reproduce_mutation_id,
                            mutation_profile=args.mutation_profile,
                            undefined_max_bits=args.undefined_max_bits,
                            control_noop=args.control_noop,
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
