#!/usr/bin/env python3
"""Offline, bounded trial runner for three isolated laptop/Pi pairs.

The B-CAN laptop is the control PC.  It prepares one immutable mutation package,
injects it through the B-CAN Pi, and later analyzes result bundles produced by
the P-CAN and I-CAN laptops.  Feedback is committed only after all three result
bundles (B TX, P RX, I RX) have been collected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional

from can_common import ConfigurationError, load_yaml_config, parse_can_data, parse_int
from experiment_runner import (
    dbc_signal_metadata,
    load_phase_times,
    next_experiment_id,
    ns_to_iso,
    parse_candump_payloads,
)
from experiment_store import ExperimentStore
from mutation_feedback import create_trial_feedback
from remote_watchdog import run_supervised_sender
from ssh_manager import SSHManager, remote_join
from strategy_selector import StrategyDecision, TrialStrategySelector
from trial_analysis import _clock_alignment, analyze_trial, validate_capture_log
from trial_config import TRIAL_CONTRACT_VERSION, trial_settings
from trial_models import MutationCase, noop_case, utc_now


RX_BUSES = ("p_can", "i_can")
ALL_BUSES = ("b_can", *RX_BUSES)
PACKAGE_MEMBERS = {"trial_plan.json", "mutation.json"}
PAIR_COMMIT_KEYS = (
    "schema_version", "pair_id", "experiment_id", "source_bus", "target_id",
    "random_seed", "pair_order", "baseline_payload", "frozen_mutation",
    "strategy", "first_trial_id", "first_package_digest",
)


def normalize_bus(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {"b": "b_can", "p": "p_can", "i": "i_can"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ALL_BUSES:
        raise ConfigurationError("bus must be B_CAN, P_CAN, or I_CAN")
    return normalized


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _package_digest(plan: Mapping[str, Any], mutation: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "package_digest"}
    return _digest_bytes(_canonical_bytes({"plan": unsigned, "mutation": mutation}))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n").encode("utf-8")


def _write_zip(path: Path, members: Mapping[str, bytes]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Result bundles and trial packages are immutable evidence.  ``x`` refuses
    # to overwrite an existing package instead of silently replacing it.
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def load_trial_package(path: Path) -> tuple[dict[str, Any], MutationCase]:
    with zipfile.ZipFile(path.expanduser().resolve(), "r") as archive:
        names = set(archive.namelist())
        if names != PACKAGE_MEMBERS:
            raise ValueError(
                f"Invalid trial package members: expected={sorted(PACKAGE_MEMBERS)}, "
                f"actual={sorted(names)}"
            )
        plan = json.loads(archive.read("trial_plan.json"))
        mutation_data = json.loads(archive.read("mutation.json"))
    expected = str(plan.get("package_digest", ""))
    actual = _package_digest(plan, mutation_data)
    if not expected or expected != actual:
        raise ValueError("Trial package digest mismatch")
    mutation = MutationCase.from_dict(mutation_data)
    if mutation.mutation_id != int(plan["mutation_id"]):
        raise ValueError("Trial package mutation_id mismatch")
    if mutation.source_bus != "b_can":
        raise ValueError("Distributed trial injection source must be B_CAN")
    return plan, mutation


def _validated_contract(plan: Mapping[str, Any], mutation: MutationCase) -> dict[str, Any]:
    """Fail before SSH if a package is not a bounded calibration trial."""
    if plan.get("trial_contract_version") != TRIAL_CONTRACT_VERSION:
        raise ConfigurationError("Trial package does not declare supported safety contract 1")
    if plan.get("trial_kind") != mutation.trial_kind:
        raise ConfigurationError("Trial package kind differs from its mutation record")
    if plan.get("pair_id") is not None:
        order = plan.get("pair_order")
        position = plan.get("pair_position")
        if (not isinstance(order, list) or len(order) != 2
                or not all(isinstance(role, str) for role in order)
                or set(order) != {"mutation", "noop"}
                or position not in (1, 2)
                or plan.get("pair_role") != order[position - 1]
                or plan.get("pair_role") != mutation.trial_kind
                or plan.get("probe_live_payload") is not True):
            raise ConfigurationError("Paired package requires a valid role and live payload probe")
    timing = plan.get("timing")
    if not isinstance(timing, Mapping):
        raise ConfigurationError("Trial package timing is missing")
    settings = trial_settings(timing)
    for name in ("receiver_lead_seconds", "receiver_tail_seconds"):
        try:
            value = float(timing[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"Trial package {name} is invalid") from exc
        if not math.isfinite(value) or value <= 0 or value > 300:
            raise ConfigurationError(f"Trial package {name} must be in (0, 300] seconds")
        settings[name] = value
    return settings


def _resolve_config_path(value: Any, config_path: Optional[Path]) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and config_path is not None:
        path = config_path.parent / path
    return path.resolve()


def _pending_trials(store: ExperimentStore) -> list[int]:
    pending = []
    for metadata_path in sorted(store.path.glob("trial_*/metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("status") not in {"completed", "failed"}:
            pending.append(int(metadata.get("trial_id", -1)))
    return pending


def _matched_noop_parameters(frozen: MutationCase) -> dict[str, Any]:
    sequence = frozen.parameters.get("sequence")
    if sequence is None:
        return {}
    if not isinstance(sequence, Mapping):
        raise ConfigurationError("Frozen temporal sequence metadata is invalid")
    frames = sequence.get("frames")
    try:
        interval_ms = float(sequence["interval_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Frozen temporal interval is invalid") from exc
    if (not isinstance(frames, list) or not frames
            or not math.isfinite(interval_ms) or interval_ms < 50):
        raise ConfigurationError("Paired temporal sequence requires frames and interval >= 50 ms")
    return {"sequence": {
        "name": "MATCHED_NOOP", "interval_ms": interval_ms,
        "frames": [frozen.original_payload.hex().upper()] * len(frames),
        "signals": [], "frame_signals_changed": [[] for _ in frames],
    }}


def _case_matches_frozen(case: MutationCase, frozen: MutationCase) -> bool:
    if (case.source_bus != frozen.source_bus or case.can_id != frozen.can_id
            or case.original_payload != frozen.original_payload
            or case.random_seed != frozen.random_seed):
        return False
    if case.trial_kind == "mutation":
        return case.to_dict() == frozen.to_dict()
    return (case.trial_kind == "noop"
            and case.mutated_payload == frozen.original_payload
            and case.parameters == _matched_noop_parameters(frozen))


def prepare_trial_package(
    *,
    config: Mapping[str, Any],
    config_path: Optional[Path],
    experiment_id: int,
    target_id: int,
    random_seed: int,
    mutation_profile: Optional[str],
    undefined_max_bits: int,
    base_payload: Optional[bytes],
    output: Optional[Path],
    control_noop: bool = False,
    frozen_mutation: Optional[MutationCase] = None,
    pair_metadata: Optional[Mapping[str, Any]] = None,
) -> Path:
    if control_noop and mutation_profile is not None:
        raise ConfigurationError("A no-op control cannot select a mutation profile")
    if target_id != 0x366 and mutation_profile is not None:
        raise ConfigurationError("targeted mutation profiles are only valid for CAN ID 0x366")
    trial_cfg = trial_settings(config.get("trial", {}))
    distributed_cfg = config.get("distributed", {})
    lead = float(distributed_cfg.get("receiver_lead_seconds", 30.0))
    tail = float(distributed_cfg.get("receiver_tail_seconds", 5.0))
    if not math.isfinite(lead) or not math.isfinite(tail) or lead <= 0 or tail <= 0:
        raise ConfigurationError("receiver lead/tail seconds must be positive and finite")
    timing = {**trial_cfg, "receiver_lead_seconds": lead, "receiver_tail_seconds": tail}
    analysis_config = dict(config.get("anomaly_thresholds", {}))
    root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
    snapshot = {
        "mode": "distributed_offline",
        "target_id": f"0x{target_id:X}",
        "source_bus": "B_CAN",
        "random_seed": random_seed,
        "mutation_profile": mutation_profile,
        "undefined_max_bits": undefined_max_bits,
        "trial_kind": "noop" if control_noop else "mutation",
        "runner_config": dict(config),
    }
    store = ExperimentStore(root, experiment_id, snapshot)
    store.reconcile_analyzed_trials()
    pending = _pending_trials(store)
    if pending:
        raise RuntimeError(
            "Previous distributed trial is not completed; analyze or mark it failed first: "
            + ", ".join(map(str, pending))
        )
    if pair_metadata is None:
        for pair_file in sorted((store.path / "pairs").glob("pair_*.json")):
            if re.fullmatch(r"pair_[0-9]{4,}", pair_file.stem):
                pair_status = json.loads(pair_file.read_text(encoding="utf-8")).get("status")
                if pair_status != "reported":
                    raise RuntimeError(
                        f"Paired set {pair_file.stem} is unfinished; complete it before a standalone trial"
                    )

    target_cfg = config.get("target", {})
    original = base_payload
    if original is None:
        reference = target_cfg.get("reference_payload")
        if reference is None:
            raise ConfigurationError("target.reference_payload or --base-payload is required")
        original = parse_can_data(reference)
    if len(original) != 8:
        raise ConfigurationError("0x366 distributed baseline payload must be exactly 8 bytes")

    state = store.load_feedback_state()
    state = {**state, "next_mutation_id": store.next_mutation_id()}
    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = _resolve_config_path(dbc_value, config_path) if dbc_value else None
    trial_id = store.next_trial_id()
    if frozen_mutation is not None and (
        frozen_mutation.trial_kind != "mutation"
        or frozen_mutation.source_bus != "b_can"
        or frozen_mutation.can_id != target_id
        or frozen_mutation.original_payload != original
        or (not control_noop and frozen_mutation.mutation_id != store.next_mutation_id())
    ):
        raise ConfigurationError("Frozen pair mutation does not match this experiment and baseline")
    if pair_metadata is not None:
        expected_role = "noop" if control_noop else "mutation"
        order = pair_metadata.get("pair_order")
        if (set(pair_metadata) != {"pair_id", "pair_position", "pair_order", "pair_role"}
                or not isinstance(order, list) or len(order) != 2
                or not all(isinstance(role, str) for role in order)
                or set(order) != {"mutation", "noop"}
                or pair_metadata.get("pair_role") != expected_role
                or pair_metadata.get("pair_position") not in (1, 2)
                or order[pair_metadata["pair_position"] - 1] != expected_role
                or not isinstance(pair_metadata.get("pair_id"), str)
                or not pair_metadata.get("pair_id")):
            raise ConfigurationError("Invalid pair metadata for trial package")
        if not bool(target_cfg.get("probe_live_payload", True)):
            raise ConfigurationError("Paired trials require a live B-CAN payload probe")
    if control_noop:
        mutation = noop_case(trial_id, "b_can", target_id, original, random_seed)
        if frozen_mutation is not None:
            mutation = replace(mutation, parameters=_matched_noop_parameters(frozen_mutation))
        decision = StrategyDecision("CONTROL", "NOOP", None, None, mutation.generation_reason)
    elif frozen_mutation is not None:
        mutation = frozen_mutation
        decision = StrategyDecision(
            mutation.strategy_mode,
            "FROZEN_PAIR_MUTATION",
            mutation.parent_mutation_id,
            mutation.changed_bytes[0] if mutation.changed_bytes else None,
            mutation.generation_reason,
        )
    else:
        selector = TrialStrategySelector(config.get("feedback", {}))
        mutation, decision = selector.select_mutation(
            state=state,
            original_payload=original,
            source_bus="b_can",
            can_id=target_id,
            random_seed=random_seed,
            mutation_profile=mutation_profile,
            dbc_path=dbc_path,
            undefined_max_bits=undefined_max_bits,
        )
    signal = dbc_signal_metadata(mutation, dbc_path)
    if signal is not None:
        mutation = replace(mutation, signal=signal)

    package_path = output
    if package_path is None:
        package_path = store.path / "outbox" / (
            f"experiment_{experiment_id:04d}_trial_{trial_id:04d}.zip"
        )
    package_path = package_path.expanduser().resolve()
    if package_path.exists():
        raise FileExistsError(f"Refusing to prepare over an existing package: {package_path}")
    trial_dir = store.create_trial(trial_id)

    plan: dict[str, Any] = {
        "schema_version": 1,
        "trial_contract_version": TRIAL_CONTRACT_VERSION,
        "trial_kind": mutation.trial_kind,
        "execution_mode": "distributed_offline_previous_trial_feedback",
        "experiment_id": experiment_id,
        "trial_id": trial_id,
        "mutation_id": mutation.mutation_id,
        "source_bus": "B_CAN",
        "receiver_buses": ["P_CAN", "I_CAN"],
        "target_id": f"0x{target_id:X}",
        "channel": str(target_cfg.get("channel", "can0")),
        "sender_config": str(config.get("sender_config", "sender_trial.yaml")),
        "probe_live_payload": bool(target_cfg.get("probe_live_payload", True)),
        "probe_samples": int(target_cfg.get("probe_samples", 3)),
        "probe_timeout_seconds": float(target_cfg.get("probe_timeout_seconds", 6.0)),
        "timing": timing,
        "analysis_config": analysis_config,
        "clock_warning_threshold_ms": float(
            config.get("time_sync", {}).get("warning_threshold_ms", 50.0)
        ),
        "prepared_at": utc_now(),
        "strategy": decision.__dict__,
        "dbc_path": str(dbc_path) if dbc_path else None,
    }
    if pair_metadata is not None:
        plan.update(dict(pair_metadata))
    mutation_data = mutation.to_dict()
    plan["package_digest"] = _package_digest(plan, mutation_data)
    metadata = {
        "schema_version": 1,
        "status": "prepared_offline",
        "execution_mode": plan["execution_mode"],
        "experiment_id": experiment_id,
        "trial_id": trial_id,
        "target_id": plan["target_id"],
        "source_bus": "B_CAN",
        "mutation_id": mutation.mutation_id,
        "trial_kind": mutation.trial_kind,
        "trial_contract_version": TRIAL_CONTRACT_VERSION,
        "collection_config": dict(timing),
        "analysis_config": analysis_config,
        "dbc_path": str(dbc_path) if dbc_path else None,
        "random_seed": random_seed,
        "feedback_snapshot_total_trials": int(state.get("total_trials", 0)),
        "start_time": utc_now(),
        "strategy": decision.__dict__,
        "required_results": ["B_CAN_TX", "P_CAN_RX", "I_CAN_RX"],
        "package_digest": plan["package_digest"],
    }
    if pair_metadata is not None:
        metadata.update(dict(pair_metadata))
    store.write_json(trial_dir / "mutation.json", mutation_data)
    store.write_json(trial_dir / "trial_plan.json", plan)
    store.write_json(trial_dir / "metadata.json", metadata)

    _write_zip(package_path, {
        "trial_plan.json": _json_bytes(plan),
        "mutation.json": _json_bytes(mutation_data),
    })
    print(f"[PREPARED] Experiment {experiment_id}, Trial {trial_id}")
    print(f"[{'CONTROL' if control_noop else 'MUTATION'}] {mutation.mutation_uid} / {mutation.operator} / {mutation.mutated_payload.hex().upper()}")
    print(f"[PACKAGE]  {package_path.resolve()}")
    print("[FEEDBACK] Observation only; automatic feedback selection is disabled.")
    return package_path.resolve()


def _pair_digest(manifest: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_bytes({key: manifest[key] for key in PAIR_COMMIT_KEYS}))


def _pair_path(store: ExperimentStore, pair_id: str) -> Path:
    if re.fullmatch(r"pair_[0-9]{4,}", pair_id) is None:
        raise ValueError("Invalid pair ID")
    return store.path / "pairs" / f"{pair_id}.json"


def _load_pair_manifest(store: ExperimentStore, pair_id: str) -> dict[str, Any]:
    path = _pair_path(store, pair_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("pair_id") != pair_id or manifest.get("pair_digest") != _pair_digest(manifest):
        raise ValueError("Paired trial manifest digest mismatch")
    return manifest


def _next_pair_id(store: ExperimentStore) -> tuple[str, int]:
    indexes = []
    pair_dir = store.path / "pairs"
    for path in pair_dir.glob("pair_*.json"):
        if re.fullmatch(r"pair_[0-9]{4,}", path.stem):
            indexes.append(int(path.stem.removeprefix("pair_")))
    index = max(indexes, default=0) + 1
    return f"pair_{index:04d}", index


def _pair_order(random_seed: int, experiment_id: int, pair_index: int) -> list[str]:
    # The seed decides the first orientation; consecutive pairs alternate.
    initial = hashlib.sha256(f"{random_seed}:{experiment_id}".encode()).digest()[0] & 1
    noop_first = not bool(initial ^ ((pair_index - 1) & 1))
    return ["noop", "mutation"] if noop_first else ["mutation", "noop"]


def prepare_pair_package(
    *, config: Mapping[str, Any], config_path: Optional[Path], experiment_id: int,
    target_id: int, random_seed: int, mutation_profile: Optional[str],
    undefined_max_bits: int, base_payload: Optional[bytes], output: Optional[Path],
    acknowledge_unverified_source_baseline: bool = False,
) -> tuple[Path, Path]:
    """Freeze a mutation and create the first immutable distributed episode."""
    trial_settings(config.get("trial", {}))
    if not bool(config.get("target", {}).get("probe_live_payload", True)):
        raise ConfigurationError("Paired trials require a live B-CAN payload probe")
    root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
    store = ExperimentStore(root, experiment_id, {
        "mode": "distributed_offline_pair", "target_id": f"0x{target_id:X}",
        "source_bus": "B_CAN", "random_seed": random_seed,
        "mutation_profile": mutation_profile,
        "undefined_max_bits": undefined_max_bits,
        "runner_config": dict(config),
    })
    store.reconcile_analyzed_trials()
    if _pending_trials(store):
        raise RuntimeError("Complete or mark the previous distributed trial failed before preparing a pair")
    pair_id, pair_index = _next_pair_id(store)
    if pair_index > 1:
        previous_report = store.path / "pairs" / f"pair_{pair_index - 1:04d}_report.json"
        if not previous_report.is_file():
            raise RuntimeError("Complete the preceding pair report before preparing another pair")
        previous = json.loads(previous_report.read_text(encoding="utf-8"))
        if (previous.get("pair_id") != f"pair_{pair_index - 1:04d}"
                or previous.get("feedback_eligible") is not False):
            raise ValueError("Previous pair report is invalid")
        states = previous.get("state_comparison") or {}
        if (states.get("first_recovery", {}).get("status") != "stable"
                or states.get("second_recovery", {}).get("status") != "stable"):
            raise RuntimeError("Previous pair recovery is not demonstrably stable")
        comparable = previous.get("comparability") or {}
        if comparable.get("status") != "comparable":
            missing_source_only = set(comparable.get("reasons") or ()) == {
                "mutation: source_target_prestate_unobserved",
                "noop: source_target_prestate_unobserved",
            }
            if not missing_source_only:
                raise RuntimeError("Previous pair has unresolved state or capture quality failures")
            if not acknowledge_unverified_source_baseline:
                raise ConfigurationError(
                    "Previous pair lacks source RX baseline evidence; review it and use "
                    "--acknowledge-unverified-source-baseline to prepare another set"
                )
    order = _pair_order(random_seed, experiment_id, pair_index)
    original = base_payload
    if original is None:
        reference = config.get("target", {}).get("reference_payload")
        if reference is None:
            raise ConfigurationError("target.reference_payload or --base-payload is required")
        original = parse_can_data(reference)
    if len(original) != 8:
        raise ConfigurationError("0x366 distributed baseline payload must be exactly 8 bytes")
    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = _resolve_config_path(dbc_value, config_path) if dbc_value else None
    state = store.load_feedback_state()
    state = {**state, "next_mutation_id": store.next_mutation_id()}
    frozen, decision = TrialStrategySelector(config.get("feedback", {})).select_mutation(
        state=state, original_payload=original, source_bus="b_can", can_id=target_id,
        random_seed=random_seed, mutation_profile=mutation_profile, dbc_path=dbc_path,
        undefined_max_bits=undefined_max_bits,
    )
    _matched_noop_parameters(frozen)
    signal = dbc_signal_metadata(frozen, dbc_path)
    if signal is not None:
        frozen = replace(frozen, signal=signal)
    first_trial_id = store.next_trial_id()
    pair_fields = {
        "pair_id": pair_id, "pair_position": 1, "pair_order": order,
        "pair_role": order[0],
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "pair_id": pair_id,
        "experiment_id": experiment_id,
        "source_bus": "B_CAN",
        "target_id": f"0x{target_id:X}",
        "random_seed": random_seed,
        "pair_order": order,
        "baseline_payload": original.hex().upper(),
        "frozen_mutation": frozen.to_dict(),
        "strategy": decision.__dict__,
        "first_trial_id": first_trial_id,
        "first_package_digest": None,
        "second_trial_id": None,
        "status": "preparing",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    manifest["pair_digest"] = _pair_digest(manifest)
    path = _pair_path(store, pair_id)
    expected_package_path = (output or store.path / "outbox" /
                             f"experiment_{experiment_id:04d}_trial_{first_trial_id:04d}.zip")
    if expected_package_path.expanduser().resolve().exists():
        raise FileExistsError(f"Refusing to prepare over an existing package: {expected_package_path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        package = prepare_trial_package(
            config=config, config_path=config_path, experiment_id=experiment_id,
            target_id=target_id, random_seed=random_seed,
            mutation_profile=mutation_profile if order[0] == "mutation" else None,
            undefined_max_bits=undefined_max_bits, base_payload=original, output=output,
            control_noop=order[0] == "noop",
            frozen_mutation=frozen,
            pair_metadata=pair_fields,
        )
        first_plan, first_case = load_trial_package(package)
        if int(first_plan["trial_id"]) != first_trial_id or first_case.trial_kind != order[0]:
            raise RuntimeError("Prepared pair episode does not match its reserved trial")
    except BaseException:
        manifest.update({"status": "prepare_failed", "updated_at": utc_now()})
        store.write_json(path, manifest)
        raise
    manifest.update({"first_package_digest": first_plan["package_digest"],
                     "status": "first_prepared", "updated_at": utc_now()})
    manifest["pair_digest"] = _pair_digest(manifest)
    store.write_json(path, manifest)
    print(f"[PAIR]    {pair_id}: {order[0]} then {order[1]}")
    print(f"[MANIFEST] {path}")
    return package, path


def prepare_pair_next_package(
    *, config: Mapping[str, Any], config_path: Optional[Path],
    first_package: Path, output: Optional[Path],
) -> Path:
    """Prepare the complementary episode only after a completed stable first."""
    from pair_analysis import recovery_returned_to_prestate

    first_plan, first_case = load_trial_package(first_package)
    pair_id = first_plan.get("pair_id")
    if not isinstance(pair_id, str) or first_plan.get("pair_position") != 1:
        raise ValueError("First package is not a paired first episode")
    root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
    store = ExperimentStore(root, int(first_plan["experiment_id"]), {})
    manifest = _load_pair_manifest(store, pair_id)
    if manifest.get("status") in {"preparing", "prepare_failed"} and manifest.get("first_package_digest") is None:
        # A process may have stopped after committing the first package but
        # before updating the manifest. Reconcile that exact existing package;
        # never create or transmit another trial here.
        frozen_for_recovery = MutationCase.from_dict(manifest["frozen_mutation"])
        stored_plan_path = store.path / f"trial_{int(first_plan['trial_id']):04d}" / "trial_plan.json"
        if (manifest.get("first_trial_id") != first_plan.get("trial_id")
                or manifest.get("pair_order") != first_plan.get("pair_order")
                or first_plan.get("pair_role") != first_case.trial_kind
                or not _case_matches_frozen(first_case, frozen_for_recovery)
                or not stored_plan_path.is_file()
                or json.loads(stored_plan_path.read_text(encoding="utf-8")) != first_plan):
            raise ValueError("Interrupted pair preparation does not match the frozen manifest")
        manifest.update({"first_package_digest": first_plan["package_digest"],
                         "status": "first_prepared", "updated_at": utc_now()})
        manifest["pair_digest"] = _pair_digest(manifest)
        store.write_json(_pair_path(store, pair_id), manifest)
    if (manifest.get("status") != "first_prepared"
            or manifest["first_package_digest"] != first_plan["package_digest"]
            or manifest["first_trial_id"] != first_plan["trial_id"]
            or manifest["pair_order"] != first_plan.get("pair_order")
            or first_case.trial_kind != manifest["pair_order"][0]):
        raise ValueError("First package does not match its pair manifest")
    frozen = MutationCase.from_dict(manifest["frozen_mutation"])
    _matched_noop_parameters(frozen)
    if not _case_matches_frozen(first_case, frozen):
        raise ValueError("First episode differs from the frozen mutation or baseline")
    first_dir = store.path / f"trial_{int(first_plan['trial_id']):04d}"
    metadata = json.loads((first_dir / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("status") != "completed":
        raise RuntimeError("Analyze the first episode before preparing the second")
    if store.next_trial_id() != int(first_plan["trial_id"]) + 1:
        raise RuntimeError("A different trial intervened between pair episodes")
    if _pending_trials(store):
        raise RuntimeError("Complete pending trials before preparing the second episode")
    gate = recovery_returned_to_prestate(store.path, int(first_plan["trial_id"]))
    if gate.get("status") != "stable":
        raise RuntimeError(f"First episode recovery is inconclusive: {gate.get('reasons', [])}")
    timing = trial_settings(config.get("trial", {}))
    distributed = config.get("distributed", {})
    comparable = {**timing,
                  "receiver_lead_seconds": float(distributed.get("receiver_lead_seconds", 30.0)),
                  "receiver_tail_seconds": float(distributed.get("receiver_tail_seconds", 5.0))}
    first_timing = dict(first_plan["timing"])
    if (comparable != first_timing
            or dict(config.get("anomaly_thresholds", {})) != first_plan.get("analysis_config")
            or str(config.get("sender_config", "sender_trial.yaml")) != first_plan["sender_config"]
            or str(config.get("target", {}).get("channel", "can0")) != first_plan["channel"]):
        raise ConfigurationError("Collection or analysis config changed since first pair episode")
    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = _resolve_config_path(dbc_value, config_path) if dbc_value else None
    if (str(dbc_path) if dbc_path else None) != first_plan.get("dbc_path"):
        raise ConfigurationError("DBC path changed since first pair episode")
    reference = config.get("target", {}).get("reference_payload")
    if reference is not None and parse_can_data(reference) != frozen.original_payload:
        raise ConfigurationError("Reference payload changed since first pair episode")
    second_role = manifest["pair_order"][1]
    package = prepare_trial_package(
        config=config, config_path=config_path,
        experiment_id=int(first_plan["experiment_id"]), target_id=frozen.can_id,
        random_seed=frozen.random_seed,
        mutation_profile=None, undefined_max_bits=2,
        base_payload=frozen.original_payload, output=output,
        control_noop=second_role == "noop",
        frozen_mutation=frozen,
        pair_metadata={"pair_id": pair_id, "pair_position": 2,
                       "pair_order": manifest["pair_order"], "pair_role": second_role},
    )
    second_plan, _ = load_trial_package(package)
    manifest.update({"second_trial_id": second_plan["trial_id"],
                     "status": "second_prepared", "updated_at": utc_now(),
                     "recovery_gate": gate})
    store.write_json(_pair_path(store, pair_id), manifest)
    print(f"[PAIR]    {pair_id}: second episode {second_role} is prepared")
    return package


def _node_settings(config: Mapping[str, Any], requested_bus: str) -> tuple[dict[str, Any], dict[str, Any]]:
    node = dict(config.get("node", {}))
    configured_bus = normalize_bus(str(node.get("bus", requested_bus)))
    if configured_bus != requested_bus:
        raise ConfigurationError(
            f"node.bus={configured_bus} does not match requested bus={requested_bus}"
        )
    ssh = node.get("ssh")
    if not isinstance(ssh, Mapping):
        raise ConfigurationError("node.ssh mapping is required")
    return node, dict(ssh)


def _wait_remote(manager: SSHManager, process: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not manager.process_alive(process):
            return
        time.sleep(0.25)
    raise TimeoutError(f"Remote capture exceeded timeout ({timeout:g}s)")


def _print_clock_warning(bus: str, clock: Mapping[str, Any], threshold_ms: float) -> None:
    offset = float(clock.get("offset_ms", 0.0))
    if abs(offset) > threshold_ms:
        print(
            f"[WARN] {bus.upper()} Pi clock offset={offset:.3f} ms "
            f"> {threshold_ms:g} ms"
        )


def _result_zip(
    *,
    output: Path,
    plan: Mapping[str, Any],
    role: str,
    bus: str,
    data_name: str,
    data: bytes,
    clock: Mapping[str, Any],
    clock_reference_id: str | None = None,
    stdout: bytes = b"",
) -> Path:
    # SSHManager.clock_sample measures Pi-vs-this-laptop, not Pi-vs-UTC.
    # Separate laptops cannot subtract those offsets without an explicitly
    # shared reference clock identity.
    reference_id = str(clock_reference_id).strip() if clock_reference_id is not None else ""
    clock_sample = {
        **clock, "reference_id": reference_id or None,
        "alignment_valid": bool(reference_id),
    }
    manifest = {
        "schema_version": 1,
        "experiment_id": int(plan["experiment_id"]),
        "trial_id": int(plan["trial_id"]),
        "mutation_id": int(plan["mutation_id"]),
        "package_digest": str(plan["package_digest"]),
        "role": role,
        "bus": bus.upper(),
        "data_file": data_name,
        "data_sha256": _digest_bytes(data),
        "clock_sample": clock_sample,
        "clock_reference_id": reference_id or None,
        "created_at": utc_now(),
    }
    members = {"result_manifest.json": _json_bytes(manifest), data_name: data}
    if stdout:
        members["remote.stdout.log"] = stdout
    _write_zip(output, members)
    return output.resolve()


def receive_trial(
    *, package: Path, config: Mapping[str, Any], bus: str, output: Optional[Path],
    manager_factory=SSHManager,
) -> Path:
    if bus not in RX_BUSES:
        raise ConfigurationError("receive supports only P_CAN or I_CAN")
    plan, mutation = load_trial_package(package)
    timing = _validated_contract(plan, mutation)
    node, ssh = _node_settings(config, bus)
    duration = sum(float(timing[name]) for name in (
        "receiver_lead_seconds", "baseline_seconds", "normal_seconds",
        "mutation_seconds", "post_seconds", "receiver_tail_seconds",
    ))
    project = str(node.get("project_dir", "/home/pi/auto-fuzz-26-1/pi_can_lab"))
    python = str(node.get("python", remote_join(project, ".venv/bin/python")))
    remote_root = str(node.get("remote_root", "/tmp/auto_fuzz_distributed"))
    remote_dir = remote_join(
        remote_root, f"experiment_{int(plan['experiment_id']):04d}",
        f"trial_{int(plan['trial_id']):04d}", bus, f"attempt_{uuid.uuid4().hex}",
    )
    remote_log = remote_join(remote_dir, f"{bus}.jsonl")
    remote_stdout = remote_join(remote_dir, "capture.stdout.log")
    receiver_config = str(node.get("receiver_config", f"receiver_{bus[0]}_can.yaml"))
    command = [
        python, remote_join(project, "can_receiver.py"),
        "--config", remote_join(project, receiver_config),
        "--bus-name", bus,
        "--output", remote_log,
        "--output-policy", "fail",
        "--experiment-id", str(plan["experiment_id"]),
        "--duration", f"{duration:g}",
        "--print-mode", "none", "--no-report",
    ]
    local_dir = Path(node.get("results_dir", "distributed_results")).expanduser().resolve()
    raw_path = local_dir / f"experiment_{int(plan['experiment_id']):04d}_trial_{int(plan['trial_id']):04d}_{bus}.jsonl"
    stdout_path = raw_path.with_suffix(".stdout.log")
    output_path = output or local_dir / (
        f"experiment_{int(plan['experiment_id']):04d}_trial_{int(plan['trial_id']):04d}_{bus}_result.zip"
    )
    for existing in (raw_path, stdout_path, output_path):
        if existing.exists():
            raise FileExistsError(f"Refusing to repeat capture with existing local evidence: {existing}")
    local_dir.mkdir(parents=True, exist_ok=True)
    manager = manager_factory(ssh)
    process = None
    process_running = False
    try:
        clock = manager.clock_sample()
        _print_clock_warning(bus, clock, float(plan["clock_warning_threshold_ms"]))
        manager.ensure_directory(remote_dir)
        process = manager.start_process(command, remote_stdout)
        process_running = True
        print(f"[CAPTURE] {bus.upper()} started for {duration:g}s")
        print("[ACTION]  Start B-CAN inject command now if both receiver laptops are ready.")
        _wait_remote(manager, process, duration + 30.0)
        process_running = False
        manager.download(remote_log, raw_path)
        manager.download(remote_stdout, stdout_path)
        validate_capture_log(raw_path, int(plan["experiment_id"]))
    except BaseException as exc:
        if process_running and process is not None:
            try:
                manager.stop_process(process)
            except Exception as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Remote receiver stop could not be confirmed: {cleanup_error}")
        raise
    finally:
        manager.close()
    result = _result_zip(
        output=output_path, plan=plan, role="rx", bus=bus,
        data_name=f"{bus}.jsonl", data=raw_path.read_bytes(), clock=clock,
        clock_reference_id=node.get("clock_reference_id"),
        stdout=stdout_path.read_bytes(),
    )
    print(f"[RESULT] {result}")
    return result


def _probe_live_payload(manager: SSHManager, plan: Mapping[str, Any]) -> bytes:
    can_id = parse_int(plan["target_id"], "target ID")
    paired = plan.get("pair_id") is not None
    samples = max(3 if paired else 1, int(plan.get("probe_samples", 3)))
    timeout = float(plan.get("probe_timeout_seconds", 6.0))
    mask = "1FFFFFFF" if can_id > 0x7FF else "7FF"
    result = manager.run([
        "timeout", f"{timeout:g}", "candump", "-L", "-n", str(samples),
        f"{plan.get('channel', 'can0')},{can_id:X}:{mask}",
    ], timeout=timeout + 2.0, check=False)
    payloads = parse_candump_payloads(result.stdout, can_id)
    if not payloads:
        raise RuntimeError(f"No 0x{can_id:X} baseline payload captured on B_CAN")
    counts = {payload: payloads.count(payload) for payload in set(payloads)}
    observed = max(counts, key=counts.get)
    if paired and (len(payloads) < samples or counts[observed] * 2 <= len(payloads)):
        raise RuntimeError(
            f"Paired B-CAN baseline probe is unstable or incomplete: "
            f"captured={len(payloads)}, requested={samples}, modal={counts[observed]}"
        )
    return observed


def _sender_command(
    *, plan: Mapping[str, Any], mutation: MutationCase, node: Mapping[str, Any], remote_tx: str,
) -> list[str]:
    project = str(node.get("project_dir", "/home/pi/auto-fuzz-26-1/pi_can_lab"))
    python = str(node.get("python", remote_join(project, ".venv/bin/python")))
    timing = plan["timing"]
    command = [
        python, remote_join(project, "can_sender.py"),
        "--config", remote_join(project, str(plan["sender_config"])),
        "--bus-name", "b_can", "--id", plan["target_id"],
        "--data", mutation.original_payload.hex(),
        "--experiment-id", str(plan["experiment_id"]),
        "--output", remote_tx, "--output-policy", "fail", "--count", "1",
        "--mutation-data", mutation.mutated_payload.hex(),
        "--mutation-id", str(mutation.mutation_id),
        "--mutation-uid", mutation.mutation_uid,
        "--mutation-operator", mutation.operator,
        "--mutation-metadata-json", json.dumps(
            mutation.parameters, ensure_ascii=True, separators=(",", ":")
        ),
        "--generation-reason", mutation.generation_reason,
        "--random-seed", str(mutation.random_seed),
        "--baseline-duration", str(float(timing["baseline_seconds"])),
        "--normal-duration", str(float(timing["normal_seconds"])),
        "--mutation-duration", str(float(timing["mutation_seconds"])),
        "--recovery-duration", str(float(timing["post_seconds"])),
        "--interval-ms", str(float(timing["interval_ms"])),
        "--trial-contract-version", str(TRIAL_CONTRACT_VERSION),
        "--execute",
    ]
    if mutation.trial_kind == "noop":
        command.append("--control-noop")
    if mutation.parent_mutation_id is not None:
        command.extend(["--parent-mutation-id", str(mutation.parent_mutation_id)])
    return command


def _verify_sender_completion(tx_path: Path, trial_kind: str) -> None:
    starts: list[dict[str, Any]] = []
    ends: list[dict[str, Any]] = []
    with tx_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record_type") == "tx_session_start":
                starts.append(record)
            elif record.get("record_type") == "tx_session_end":
                ends.append(record)
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("TX session must contain exactly one start and one end marker")
    for record in (*starts, *ends):
        if record.get("trial_contract_version") != TRIAL_CONTRACT_VERSION:
            raise ValueError("TX session did not execute safety contract 1")
        if record.get("trial_kind") != trial_kind:
            raise ValueError("TX session trial kind differs from its package")
    if ends[0].get("status") != "completed":
        raise ValueError("TX session did not complete successfully")
    load_phase_times(tx_path)


def inject_trial(
    *, package: Path, config: Mapping[str, Any], output: Optional[Path], execute: bool,
    manager_factory=SSHManager,
) -> Optional[Path]:
    plan, mutation = load_trial_package(package)
    timing = _validated_contract(plan, mutation)
    node, ssh = _node_settings(config, "b_can")
    if not execute:
        print("[SAFE] Preview only; no SSH connection or CAN transmission was started.")
        print(f"[TRIAL] Experiment {plan['experiment_id']}, Trial {plan['trial_id']}")
        print(f"[TX]    0x{mutation.can_id:X}#{mutation.mutated_payload.hex().upper()}")
        print("[SAFE] Add --execute only after P-CAN and I-CAN captures are running.")
        return None
    remote_root = str(node.get("remote_root", "/tmp/auto_fuzz_distributed"))
    remote_dir = remote_join(
        remote_root, f"experiment_{int(plan['experiment_id']):04d}",
        f"trial_{int(plan['trial_id']):04d}", "b_can", f"attempt_{uuid.uuid4().hex}",
    )
    remote_tx = remote_join(remote_dir, "tx.jsonl")
    remote_stdout = remote_join(remote_dir, "sender.stdout.log")
    local_dir = Path(node.get("results_dir", "distributed_results")).expanduser().resolve()
    tx_path = local_dir / f"experiment_{int(plan['experiment_id']):04d}_trial_{int(plan['trial_id']):04d}_tx.jsonl"
    stdout_path = tx_path.with_suffix(".stdout.log")
    output_path = output or local_dir / (
        f"experiment_{int(plan['experiment_id']):04d}_trial_{int(plan['trial_id']):04d}_b_can_tx_result.zip"
    )
    attempt_path = local_dir / (
        f"experiment_{int(plan['experiment_id']):04d}_trial_{int(plan['trial_id']):04d}_b_can_attempt_started.json"
    ) if plan.get("pair_id") else None
    for existing in (tx_path, stdout_path, output_path, attempt_path):
        if existing is None:
            continue
        if existing.exists():
            raise FileExistsError(f"Refusing to repeat injection with existing local evidence: {existing}")
    local_dir.mkdir(parents=True, exist_ok=True)
    if attempt_path is not None:
        # An SSH disconnect can leave a sender running without a downloaded TX
        # log. The exclusive marker prevents an accidental second injection of
        # that package even when no result ZIP was recovered.
        with attempt_path.open("x", encoding="utf-8") as handle:
            json.dump({
                "schema_version": 1,
                "pair_id": plan["pair_id"],
                "experiment_id": plan["experiment_id"],
                "trial_id": plan["trial_id"],
                "package_digest": plan["package_digest"],
                "attempt_started_at": utc_now(),
                "status": "attempt_started",
            }, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    manager = manager_factory(ssh)
    try:
        clock = manager.clock_sample()
        _print_clock_warning("b_can", clock, float(plan["clock_warning_threshold_ms"]))
        manager.ensure_directory(remote_dir)
        if bool(plan.get("probe_live_payload", True)):
            observed = _probe_live_payload(manager, plan)
            if observed != mutation.original_payload:
                raise RuntimeError(
                    "Live B-CAN baseline differs from prepared mutation baseline: "
                    f"live={observed.hex().upper()} prepared={mutation.original_payload.hex().upper()}"
                )
        command = _sender_command(
            plan=plan, mutation=mutation, node=node, remote_tx=remote_tx
        )
        run_supervised_sender(
            manager, command, remote_stdout,
            timeout=timing["runner_timeout_seconds"],
            poll_seconds=timing["watchdog_poll_seconds"],
        )
        manager.download(remote_stdout, stdout_path)
        manager.download(remote_tx, tx_path)
        _verify_sender_completion(tx_path, mutation.trial_kind)
    finally:
        manager.close()
    result_path = _result_zip(
        output=output_path, plan=plan, role="tx", bus="b_can",
        data_name="tx.jsonl", data=tx_path.read_bytes(), clock=clock,
        clock_reference_id=node.get("clock_reference_id"),
        stdout=stdout_path.read_bytes(),
    )
    print(f"[TX COMPLETE] {mutation.mutation_uid}")
    print(f"[RESULT]      {result_path}")
    return result_path


def load_result_bundle(path: Path) -> tuple[dict[str, Any], bytes]:
    with zipfile.ZipFile(path.expanduser().resolve(), "r") as archive:
        names = set(archive.namelist())
        if "result_manifest.json" not in names:
            raise ValueError(f"{path}: result_manifest.json is missing")
        manifest = json.loads(archive.read("result_manifest.json"))
        data_name = str(manifest.get("data_file", ""))
        if not data_name or data_name not in names or Path(data_name).name != data_name:
            raise ValueError(f"{path}: invalid data_file")
        data = archive.read(data_name)
    if _digest_bytes(data) != manifest.get("data_sha256"):
        raise ValueError(f"{path}: result data digest mismatch")
    return manifest, data


def _write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"Refusing to overwrite different raw result: {path}")
        return
    path.write_bytes(data)


def _capture_bounds(path: Path) -> tuple[int, int]:
    started = ended = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record_type") == "session_start":
                started = int(record["wall_time_ns"])
            elif record.get("record_type") == "session_end":
                ended = int(record["wall_time_ns"])
    if started is None or ended is None:
        raise ValueError(f"Capture bounds are missing: {path}")
    return started, ended


def analyze_distributed_trial(
    *, config: Mapping[str, Any], config_path: Optional[Path], package: Path,
    tx_result: Path, p_result: Path, i_result: Path,
) -> dict[str, Any]:
    plan, mutation = load_trial_package(package)
    _validated_contract(plan, mutation)
    supplied = [load_result_bundle(path) for path in (tx_result, p_result, i_result)]
    by_key: dict[tuple[str, str], tuple[dict[str, Any], bytes]] = {}
    for manifest, data in supplied:
        if str(manifest.get("package_digest")) != str(plan["package_digest"]):
            raise ValueError("Result belongs to a different trial package")
        for field in ("experiment_id", "trial_id", "mutation_id"):
            if int(manifest[field]) != int(plan[field]):
                raise ValueError(f"Result {field} mismatch")
        key = (str(manifest["role"]), normalize_bus(str(manifest["bus"])))
        if key in by_key:
            raise ValueError(f"Duplicate result role/bus: {key}")
        by_key[key] = (manifest, data)
    required = {("tx", "b_can"), ("rx", "p_can"), ("rx", "i_can")}
    if set(by_key) != required:
        raise ValueError(f"Required results are {sorted(required)}; received {sorted(by_key)}")

    root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
    store = ExperimentStore(root, int(plan["experiment_id"]), {
        "mode": "distributed_offline", "target_id": plan["target_id"],
        "source_bus": "B_CAN",
    })
    trial_dir = store.path / f"trial_{int(plan['trial_id']):04d}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    existing_mutation = trial_dir / "mutation.json"
    mutation_bytes = _json_bytes(mutation.to_dict())
    _write_immutable(existing_mutation, mutation_bytes)
    _write_immutable(trial_dir / "trial_plan.json", _json_bytes(plan))
    tx_path = trial_dir / "tx.jsonl"
    p_path = trial_dir / "p_can.jsonl"
    i_path = trial_dir / "i_can.jsonl"
    _write_immutable(tx_path, by_key[("tx", "b_can")][1])
    _write_immutable(p_path, by_key[("rx", "p_can")][1])
    _write_immutable(i_path, by_key[("rx", "i_can")][1])

    _verify_sender_completion(tx_path, mutation.trial_kind)
    phases = load_phase_times(tx_path)
    raw_clocks: dict[str, dict[str, Any]] = {}
    reference_ids: set[str] = set()
    references_complete = True
    for key, (manifest, _) in by_key.items():
        sample = manifest.get("clock_sample")
        sample = dict(sample) if isinstance(sample, Mapping) else {}
        reference = manifest.get("clock_reference_id")
        if not isinstance(reference, str) or not reference.strip() or sample.get("reference_id") != reference:
            references_complete = False
        else:
            reference_ids.add(reference)
        raw_clocks[key[1]] = sample
    common_reference = references_complete and len(reference_ids) == 1
    clocks = {
        bus: {**sample, "alignment_valid": common_reference and sample.get("alignment_valid") is True}
        for bus, sample in raw_clocks.items()
    }
    capture_quality = {}
    for bus, path in (("p_can", p_path), ("i_can", i_path)):
        quality = validate_capture_log(path, int(plan["experiment_id"]))
        start_ns, end_ns = _capture_bounds(path)
        correction_ns, uncertainty_ns, alignment = _clock_alignment(bus, "b_can", clocks)
        if end_ns <= start_ns or end_ns - start_ns < int(phases["recovery_end"]) - int(phases["baseline_start"]):
            raise ValueError(f"{bus} capture is too short for TX baseline/recovery windows")
        if alignment == "aligned":
            uncertainty = uncertainty_ns or 0
            if (start_ns + correction_ns + uncertainty > int(phases["baseline_start"])
                    or end_ns + correction_ns - uncertainty < int(phases["recovery_end"])):
                raise ValueError(
                    f"{bus} capture does not cover corrected TX baseline/recovery windows: "
                    f"capture={start_ns}..{end_ns}, correction={correction_ns}"
                )
        capture_quality[bus] = {
            **quality, "capture_start_ns": start_ns, "capture_end_ns": end_ns,
            "clock_alignment": alignment,
            "phase_coverage": "verified" if alignment == "aligned" else "unverified_clock_reference",
        }

    dbc_value = plan.get("dbc_path", config.get("dbc"))
    dbc_path = _resolve_config_path(dbc_value, config_path) if dbc_value else None
    analysis_config = dict(plan.get("analysis_config", {}))
    analysis = analyze_trial(
        rx_paths={"p_can": p_path, "i_can": i_path},
        phase_times_ns=phases,
        mutation=mutation,
        thresholds=analysis_config,
        experiment_dir=store.path,
        current_trial_id=int(plan["trial_id"]),
        dbc_path=dbc_path,
        clock_offsets=clocks,
        trial_kind=mutation.trial_kind,
    )
    anomalies_doc = {
        "schema_version": 1, "trial_id": int(plan["trial_id"]),
        "mutation_id": mutation.mutation_id, **analysis,
    }
    store.write_json(trial_dir / "anomalies.json", anomalies_doc)
    threshold = float(config.get("feedback", {}).get("interesting_score_threshold", 0.6))
    feedback = create_trial_feedback(
        int(plan["trial_id"]), mutation, analysis["anomalies"], threshold,
        prior_state=store.load_feedback_state(),
        trial_kind=mutation.trial_kind,
    )
    store.write_json(trial_dir / "feedback.json", feedback)
    metadata = {
        "schema_version": 1,
        "status": "analyzed",
        "execution_mode": plan["execution_mode"],
        "experiment_id": int(plan["experiment_id"]),
        "trial_id": int(plan["trial_id"]),
        "target_id": plan["target_id"],
        "source_bus": "B_CAN",
        "mutation_id": mutation.mutation_id,
        "trial_kind": mutation.trial_kind,
        "trial_contract_version": TRIAL_CONTRACT_VERSION,
        "dbc_path": str(dbc_path) if dbc_path else None,
        "collection_config": dict(plan["timing"]),
        "analysis_config": analysis_config,
        "start_time": plan["prepared_at"],
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
        "logs": {"tx": "tx.jsonl", "p_can": "p_can.jsonl", "i_can": "i_can.jsonl"},
        "capture_quality": capture_quality,
        "clock_samples": raw_clocks,
        "clock_offsets": clocks,
        "clock_alignment_reference_id": next(iter(reference_ids)) if common_reference else None,
        "clock_alignment_status": "common_reference" if common_reference else "unverified_independent_references",
        "required_results": ["B_CAN_TX", "P_CAN_RX", "I_CAN_RX"],
        "package_digest": plan["package_digest"],
    }
    if plan.get("pair_id") is not None:
        metadata.update({key: plan[key] for key in (
            "pair_id", "pair_position", "pair_order", "pair_role",
        )})
    store.write_json(trial_dir / "metadata.json", metadata)
    store.record_completed_trial(mutation, feedback)
    metadata["status"] = "completed"
    store.write_json(trial_dir / "metadata.json", metadata)
    print(f"[COMPLETED] Experiment {plan['experiment_id']}, Trial {plan['trial_id']}")
    print(f"[ANOMALY]   count={analysis['summary']['anomaly_count']} max={analysis['summary']['maximum_score']:.2f}")
    print(f"[FEEDBACK]  {feedback['verification_status'].upper()}")
    print("[NEXT]      Run prepare again; automatic feedback remains disabled.")
    return feedback


def analyze_distributed_pair(
    *, config: Mapping[str, Any], config_path: Optional[Path],
    first_package: Path, second_package: Path,
) -> dict[str, Any]:
    """Produce one immutable paired verdict after both episodes are complete."""
    from pair_analysis import analyze_trial_pair

    first_plan, first_case = load_trial_package(first_package)
    second_plan, second_case = load_trial_package(second_package)
    pair_id = first_plan.get("pair_id")
    if (not isinstance(pair_id, str)
            or first_plan.get("pair_position") != 1
            or second_plan.get("pair_position") != 2
            or second_plan.get("pair_id") != pair_id
            or first_plan.get("pair_order") != second_plan.get("pair_order")
            or first_plan.get("experiment_id") != second_plan.get("experiment_id")
            or int(second_plan["trial_id"]) != int(first_plan["trial_id"]) + 1):
        raise ValueError("Packages are not the two ordered episodes of one pair")
    root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
    store = ExperimentStore(root, int(first_plan["experiment_id"]), {})
    manifest = _load_pair_manifest(store, pair_id)
    if (manifest.get("status") not in {"second_prepared", "reported"}
            or manifest.get("first_package_digest") != first_plan["package_digest"]
            or manifest.get("first_trial_id") != first_plan["trial_id"]
            or manifest.get("second_trial_id") != second_plan["trial_id"]
            or manifest.get("pair_order") != first_plan.get("pair_order")):
        raise ValueError("Pair packages do not match the frozen manifest")
    frozen = MutationCase.from_dict(manifest["frozen_mutation"])
    for plan, case, position in ((first_plan, first_case, 1), (second_plan, second_case, 2)):
        expected_role = manifest["pair_order"][position - 1]
        if (plan.get("pair_role") != expected_role
                or case.trial_kind != expected_role
                or case.original_payload != frozen.original_payload
                or not _case_matches_frozen(case, frozen)):
            raise ValueError("Pair episode differs from the frozen payload or role")
        trial_dir = store.path / f"trial_{int(plan['trial_id']):04d}"
        stored_plan = json.loads((trial_dir / "trial_plan.json").read_text(encoding="utf-8"))
        metadata = json.loads((trial_dir / "metadata.json").read_text(encoding="utf-8"))
        if stored_plan != plan or metadata.get("status") != "completed":
            raise RuntimeError("Both pair episodes must be analyzed and completed")
    mutation_trial_id = int(first_plan["trial_id"] if first_case.trial_kind == "mutation" else second_plan["trial_id"])
    noop_trial_id = int(first_plan["trial_id"] if first_case.trial_kind == "noop" else second_plan["trial_id"])
    report = analyze_trial_pair(
        store.path, mutation_trial_id, noop_trial_id, pair_id=pair_id,
    )
    path = store.path / "pairs" / f"{pair_id}_report.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != report:
            raise FileExistsError("Existing paired report differs from recomputed evidence")
    else:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    manifest.update({"status": "reported", "pair_report": str(path.relative_to(store.path)),
                     "updated_at": utc_now()})
    store.write_json(_pair_path(store, pair_id), manifest)
    print(f"[PAIR REPORT] {path}")
    print(f"[COMPARABILITY] {report.get('comparability', {}).get('status', 'inconclusive')}")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Distributed B-TX + P/I-RX bounded calibration runner"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="create one immutable next-trial package on B Control PC")
    prepare.add_argument("--config", default="distributed_runner.yaml")
    prepare.add_argument("--experiment-id", type=int)
    prepare.add_argument("--target-id", default="0x366")
    prepare.add_argument("--base-payload")
    prepare.add_argument("--random-seed", type=int, default=366)
    prepare.add_argument("--mutation-profile")
    prepare.add_argument("--control-noop", action="store_true", help="run an unchanged-payload control")
    prepare.add_argument("--undefined-max-bits", type=int, default=2)
    prepare.add_argument("--output")

    prepare_pair = sub.add_parser("prepare-pair", help="freeze a mutation and create the first paired package")
    prepare_pair.add_argument("--config", default="distributed_runner.yaml")
    prepare_pair.add_argument("--experiment-id", type=int)
    prepare_pair.add_argument("--target-id", default="0x366")
    prepare_pair.add_argument("--base-payload")
    prepare_pair.add_argument("--random-seed", type=int, default=366)
    prepare_pair.add_argument("--mutation-profile")
    prepare_pair.add_argument("--undefined-max-bits", type=int, default=2)
    prepare_pair.add_argument("--output")
    prepare_pair.add_argument(
        "--acknowledge-unverified-source-baseline", action="store_true",
        help="after reviewing a prior pair whose sole limitation is missing B-CAN RX history",
    )

    prepare_pair_next = sub.add_parser("prepare-pair-next", help="create complementary package after stable recovery")
    prepare_pair_next.add_argument("--config", default="distributed_runner.yaml")
    prepare_pair_next.add_argument("--first-package", required=True)
    prepare_pair_next.add_argument("--output")

    receive = sub.add_parser("receive", help="capture P_CAN or I_CAN through this laptop's Pi")
    receive.add_argument("--config", default="distributed_node.yaml")
    receive.add_argument("--package", required=True)
    receive.add_argument("--bus", required=True, choices=("P_CAN", "I_CAN", "p_can", "i_can"))
    receive.add_argument("--output")

    inject = sub.add_parser("inject", help="inject the prepared mutation through B_CAN Pi")
    inject.add_argument("--config", default="distributed_node.yaml")
    inject.add_argument("--package", required=True)
    inject.add_argument("--output")
    inject.add_argument("--execute", action="store_true")

    analyze = sub.add_parser("analyze", help="merge B TX + P/I RX and commit feedback")
    analyze.add_argument("--config", default="distributed_runner.yaml")
    analyze.add_argument("--package", required=True)
    analyze.add_argument("--tx-result", required=True)
    analyze.add_argument("--p-result", required=True)
    analyze.add_argument("--i-result", required=True)
    analyze_pair = sub.add_parser("analyze-pair", help="compare two completed paired episodes")
    analyze_pair.add_argument("--config", default="distributed_runner.yaml")
    analyze_pair.add_argument("--first-package", required=True)
    analyze_pair.add_argument("--second-package", required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    config, config_path = load_yaml_config(args.config)
    if args.command == "prepare":
        root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
        experiment_id = args.experiment_id or next_experiment_id(root)
        prepare_trial_package(
            config=config, config_path=config_path, experiment_id=experiment_id,
            target_id=parse_int(args.target_id, "target ID"), random_seed=args.random_seed,
            mutation_profile=args.mutation_profile,
            undefined_max_bits=args.undefined_max_bits,
            base_payload=parse_can_data(args.base_payload) if args.base_payload else None,
            output=Path(args.output) if args.output else None,
            control_noop=args.control_noop,
        )
    elif args.command == "prepare-pair":
        root = _resolve_config_path(config.get("experiments_root", "experiments"), config_path)
        experiment_id = args.experiment_id or next_experiment_id(root)
        prepare_pair_package(
            config=config, config_path=config_path, experiment_id=experiment_id,
            target_id=parse_int(args.target_id, "target ID"), random_seed=args.random_seed,
            mutation_profile=args.mutation_profile,
            undefined_max_bits=args.undefined_max_bits,
            base_payload=parse_can_data(args.base_payload) if args.base_payload else None,
            output=Path(args.output) if args.output else None,
            acknowledge_unverified_source_baseline=args.acknowledge_unverified_source_baseline,
        )
    elif args.command == "prepare-pair-next":
        prepare_pair_next_package(
            config=config, config_path=config_path,
            first_package=Path(args.first_package),
            output=Path(args.output) if args.output else None,
        )
    elif args.command == "receive":
        receive_trial(
            package=Path(args.package), config=config, bus=normalize_bus(args.bus),
            output=Path(args.output) if args.output else None,
        )
    elif args.command == "inject":
        inject_trial(
            package=Path(args.package), config=config,
            output=Path(args.output) if args.output else None, execute=args.execute,
        )
    elif args.command == "analyze":
        analyze_distributed_trial(
            config=config, config_path=config_path, package=Path(args.package),
            tx_result=Path(args.tx_result), p_result=Path(args.p_result),
            i_result=Path(args.i_result),
        )
    elif args.command == "analyze-pair":
        analyze_distributed_pair(
            config=config, config_path=config_path,
            first_package=Path(args.first_package), second_package=Path(args.second_package),
        )
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
