"""Offline analysis and commit of a deferred paired-cycle capture.

Only persisted local evidence is read.  This module deliberately has no
orchestrator, SSH, capture, or sender imports.
"""

from __future__ import annotations

import json
import fcntl
import re
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from experiment_store import ExperimentStore
from minimal_recovery_gate import fault_only_advisory
from mutation_feedback import create_trial_feedback
from pair_analysis import (
    _clocked_captures, _phase_times, _tx_schedule, analyze_trial_pair,
)
from paired_cycle import advance_cycle, make_cycle_mutation, validate_deferred_cycle
from trial_analysis import analyze_trial
from trial_models import MutationCase, noop_case, utc_now


_CAPTURE_FILES = ("metadata.json", "mutation.json", "tx.jsonl",
                  "p_can.jsonl", "b_can.jsonl", "i_can.jsonl")


def _with_offline_lock(function):
    """Use the runner's local lock, so capture and finalization cannot overlap."""
    @wraps(function)
    def wrapped(store: ExperimentStore, *args, **kwargs):
        lock_path = store.path / "pairs" / ".runner.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("A capture or finalization is already running") from exc
            try:
                return function(store, *args, **kwargs)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return wrapped


def _read_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _cycle_link(plan: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "catalog_sha256": plan["catalog_sha256"],
        "entry_index": entry["index"],
        "entry_id": entry["entry_id"],
        "family": entry["family"],
        "case": entry["case"],
        "tx_fingerprint": entry["tx_fingerprint"],
    }


def _captured_prefix(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    if plan.get("deferred_analysis") is not True:
        raise ValueError("cycle was not prepared for deferred analysis")
    captured = plan.get("captured_pairs")
    if not isinstance(captured, list):
        raise ValueError("deferred cycle has no captured-pair ledger")
    scheduled = [entry for entry in plan["entries"]
                 if entry["disposition"] == "scheduled"]
    if len(captured) > len(scheduled):
        raise ValueError("captured-pair ledger exceeds the scheduled catalogue")
    ids: set[str] = set()
    for position, (item, entry) in enumerate(zip(captured, scheduled), start=1):
        if (not isinstance(item, dict)
                or item.get("entry_index") != entry["index"]
                or not isinstance(item.get("pair_id"), str)
                or not re.fullmatch(r"pair_\d+", item["pair_id"])
                or item["pair_id"] != f"pair_{position:04d}"
                or item["pair_id"] in ids):
            raise ValueError("captured-pair ledger is not the frozen scheduled prefix")
        ids.add(item["pair_id"])
    completed = plan["completed_pairs"]
    if len(completed) > len(captured) or any(
        item["entry_index"] != captured[index]["entry_index"]
        or item["pair_id"] != captured[index]["pair_id"]
        for index, item in enumerate(completed)
    ):
        raise ValueError("analysis ledger is not a prefix of captured pairs")
    return captured


def _expected_control(
    trial_id: int, mutation: MutationCase, *, random_seed: int,
) -> MutationCase:
    control = noop_case(
        trial_id, mutation.source_bus, mutation.can_id,
        mutation.original_payload, random_seed,
    )
    sequence = mutation.parameters.get("sequence")
    if sequence is None:
        return control
    if not isinstance(sequence, Mapping) or not isinstance(sequence.get("frames"), list):
        raise ValueError("frozen temporal mutation has an invalid sequence")
    parameters: dict[str, Any] = {
        "sequence": {
            "name": "MATCHED_NOOP",
            "interval_ms": float(sequence["interval_ms"]),
            "frames": [mutation.original_payload.hex().upper()] * len(sequence["frames"]),
        }
    }
    for key in ("cycle_entry_index", "cycle_entry_id"):
        if key in mutation.parameters:
            parameters[key] = mutation.parameters[key]
    return replace(control, parameters=parameters)


def _validate_capture_evidence(
    trial_dir: Path, metadata: Mapping[str, Any], mutation: MutationCase,
) -> None:
    """Recheck saved TX, receiver clocks, and capture bounds before writing analysis."""
    phases = _phase_times(metadata.get("phase_times_ns"))
    tx, tx_errors = _tx_schedule(
        trial_dir, phases, mutation, metadata.get("experiment_id")
    )
    _frames, _clock_quality, capture_errors = _clocked_captures(
        trial_dir, metadata, phases, mutation
    )
    errors = tx_errors + capture_errors
    if tx.get("status") != "valid":
        errors.append("TX evidence is not valid")
    if errors:
        raise RuntimeError(f"{trial_dir.name} capture evidence invalid: {'; '.join(errors)}")


def _validate_pair(
    store: ExperimentStore, plan: Mapping[str, Any],
    captured: Mapping[str, Any], entry: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], list[tuple[int, MutationCase, dict[str, Any]]]]:
    pair_id = captured["pair_id"]
    pair_path = store.path / "pairs" / f"{pair_id}.json"
    pair = _read_object(pair_path)
    link = _cycle_link(plan, entry)
    order = pair.get("pair_order")
    first = pair.get("first_trial_id")
    second = pair.get("second_trial_id")
    if (pair.get("pair_id") != pair_id
            or pair.get("status") not in {"captured", "completed"}
            or pair.get("analysis_mode") != "deferred"
            or pair.get("cycle_entry") != link
            or order not in (["noop", "mutation"], ["mutation", "noop"])
            or type(first) is not int or first < 1
            or type(second) is not int or second != first + 1
            or first != 2 * (int(pair_id.removeprefix("pair_")) - 1) + 1
            or pair.get("source_bus") != plan["source_bus"]
            or pair.get("target_id") != plan["target_id"]
            or pair.get("random_seed") != plan["random_seed"]
            or pair.get("baseline_payload") != plan["baseline_payload"]
            or pair.get("dbc_path") != plan["dbc_path"]):
        raise RuntimeError(f"{pair_id} does not match the frozen cycle and trial order")

    gates = pair.get("recovery_gates")
    if not isinstance(gates, Mapping) or set(gates) != {"first", "second"}:
        raise RuntimeError(f"{pair_id} lacks both saved recovery gates")
    for position in ("first", "second"):
        gate = gates[position]
        reasons = gate.get("reasons") if isinstance(gate, Mapping) else None
        advisory = gate.get("advisory_observations", []) if isinstance(gate, Mapping) else None
        if (not isinstance(gate, Mapping)
                or gate.get("status") not in {"stable", "inconclusive", "review_required"}
                or type(gate.get("observed_change")) is not bool
                or not isinstance(reasons, list)
                or not isinstance(advisory, list)
                or (gate.get("status") == "stable") != (not reasons)
                or (gate["observed_change"] and not reasons
                    and not fault_only_advisory(gate))):
            raise RuntimeError(f"{pair_id} {position} recovery gate is invalid")
        if "capture_integrity_status" in gate and (
            gate.get("capture_integrity_status") not in {"stable", "review_required"}
            or not isinstance(gate.get("capture_integrity_reasons"), list)
            or (gate["capture_integrity_status"] == "stable")
            != (not gate["capture_integrity_reasons"])
        ):
            raise RuntimeError(f"{pair_id} {position} capture integrity record is invalid")
    first_gate = gates["first"]
    first_integrity_passed = (
        first_gate.get("capture_integrity_status") == "stable"
        and first_gate.get("capture_integrity_reasons") == []
        if "capture_integrity_status" in first_gate
        else (first_gate["status"] == "stable"
              and first_gate["observed_change"] is False
              and first_gate["reasons"] == [])
    )
    if not first_integrity_passed:
        raise RuntimeError(f"{pair_id} first recovery did not pass before the second episode")

    mutation = MutationCase.from_dict(pair["frozen_mutation"])
    expected = make_cycle_mutation(
        entry, mutation_id=mutation.mutation_id,
        source_bus=plan["source_bus"],
        original_payload=bytes.fromhex(plan["baseline_payload"]),
        random_seed=plan["random_seed"],
    )
    for field in ("trial_kind", "source_bus", "can_id", "original_payload",
                  "mutated_payload", "operator", "random_seed", "parameters"):
        if getattr(mutation, field) != getattr(expected, field):
            raise RuntimeError(f"{pair_id} mutation differs from the frozen catalogue")
    control_id = first if order[0] == "noop" else second
    control = MutationCase.from_dict(pair["frozen_noop"])
    expected_control = _expected_control(
        control_id, mutation, random_seed=plan["random_seed"]
    )
    for field in ("trial_kind", "mutation_id", "source_bus", "can_id",
                  "original_payload", "mutated_payload", "operator",
                  "random_seed", "parameters"):
        if getattr(control, field) != getattr(expected_control, field):
            raise RuntimeError(f"{pair_id} no-op differs from the frozen matched control")

    episodes: list[tuple[int, MutationCase, dict[str, Any]]] = []
    for position, trial_id in enumerate((first, second), start=1):
        trial_dir = store.path / f"trial_{trial_id:04d}"
        if not all((trial_dir / name).is_file() for name in _CAPTURE_FILES):
            raise RuntimeError(f"{pair_id} trial {trial_id} lacks required capture files")
        metadata = _read_object(trial_dir / "metadata.json")
        recorded = MutationCase.from_dict(_read_object(trial_dir / "mutation.json"))
        kind = order[position - 1]
        case = mutation if kind == "mutation" else control
        logs = metadata.get("logs")
        if (metadata.get("status") not in {"captured", "analyzed", "completed"}
                or metadata.get("experiment_id") != store.experiment_id
                or metadata.get("trial_id") != trial_id
                or metadata.get("pair_id") != pair_id
                or metadata.get("pair_position") != position
                or metadata.get("pair_order") != order
                or metadata.get("pair_role") != kind
                or metadata.get("cycle_entry") != link
                or metadata.get("trial_kind") != kind
                or str(metadata.get("source_bus", "")).lower() != plan["source_bus"]
                or metadata.get("target_id") != plan["target_id"]
                or metadata.get("dbc_path") != plan["dbc_path"]
                or metadata.get("collection_config") != pair.get("collection_config")
                or metadata.get("analysis_config") != pair.get("analysis_config")
                or not isinstance(logs, Mapping)
                or any(logs.get(bus) != f"{bus}.jsonl"
                       for bus in ("p_can", "b_can", "i_can"))
                or recorded != case):
            raise RuntimeError(f"{pair_id} trial {trial_id} differs from the frozen pair")
        _validate_capture_evidence(trial_dir, metadata, case)
        episodes.append((trial_id, case, metadata))
    return pair_path, pair, episodes


def _analyze_episode(
    store: ExperimentStore, trial_id: int,
    mutation: MutationCase, metadata: dict[str, Any],
    *, interesting_threshold: float,
) -> None:
    trial_dir = store.path / f"trial_{trial_id:04d}"
    anomaly_path = trial_dir / "anomalies.json"
    feedback_path = trial_dir / "feedback.json"
    has_anomaly, has_feedback = anomaly_path.is_file(), feedback_path.is_file()
    if has_feedback and not has_anomaly:
        raise RuntimeError(f"trial {trial_id} has feedback without anomaly analysis")
    if metadata["status"] in {"analyzed", "completed"} and not (has_anomaly and has_feedback):
        raise RuntimeError(f"trial {trial_id} is marked analyzed without both reports")
    if has_anomaly:
        anomalies_doc = _read_object(anomaly_path)
        if (anomalies_doc.get("trial_id") != trial_id
                or anomalies_doc.get("mutation_id") != mutation.mutation_id
                or not isinstance(anomalies_doc.get("anomalies"), list)
                or anomalies_doc.get("trial_kind") not in (None, mutation.trial_kind)):
            raise RuntimeError(f"trial {trial_id} anomaly report identity is invalid")
    else:
        logs = metadata["logs"]
        analysis = analyze_trial(
            rx_paths={bus: trial_dir / name for bus, name in logs.items()},
            phase_times_ns=metadata["phase_times_ns"],
            mutation=mutation,
            thresholds=metadata.get("analysis_config") or {},
            experiment_dir=store.path,
            current_trial_id=trial_id,
            dbc_path=Path(metadata["dbc_path"]) if metadata.get("dbc_path") else None,
            clock_offsets=metadata.get("clock_offsets"),
            trial_kind=mutation.trial_kind,
        )
        anomalies_doc = {
            "schema_version": 1,
            "trial_id": trial_id,
            "mutation_id": mutation.mutation_id,
            **analysis,
        }
        store.write_json(anomaly_path, anomalies_doc)
    if has_feedback:
        feedback = _read_object(feedback_path)
        if (feedback.get("trial_id") != trial_id
                or feedback.get("mutation_id") != mutation.mutation_id
                or feedback.get("trial_kind") != mutation.trial_kind):
            raise RuntimeError(f"trial {trial_id} feedback identity is invalid")
    else:
        feedback = create_trial_feedback(
            trial_id, mutation, anomalies_doc["anomalies"], interesting_threshold,
            prior_state=store.load_feedback_state(),
        )
        store.write_json(feedback_path, feedback)
    if metadata["status"] != "completed":
        metadata["status"] = "analyzed"
        store.write_json(trial_dir / "metadata.json", metadata)
    state = store.record_completed_trial(mutation, feedback)
    ledger = state["control_trial_ids"] if mutation.trial_kind == "noop" else state["completed_trial_ids"]
    if trial_id not in {int(value) for value in ledger}:
        raise RuntimeError(f"trial {trial_id} feedback ledger commit failed")
    if metadata["status"] != "completed":
        metadata["status"] = "completed"
        store.write_json(trial_dir / "metadata.json", metadata)


def _report_needs_review(report: Mapping[str, Any]) -> bool:
    gate = report.get("next_pair_gate")
    if isinstance(gate, Mapping):
        return gate.get("status") != "ready"
    second = (report.get("state_comparison") or {}).get("second_recovery")
    return not isinstance(second, Mapping) or second.get("status") != "stable"


@_with_offline_lock
def finalize_deferred_cycle(
    store: ExperimentStore, *, max_pairs: int | None = None,
) -> dict[str, Any]:
    """Analyze a saved capture prefix, committing each pair exactly once.

    Recovery findings remain visible in the returned summary.  They do not
    suppress analysis of later captures.  Repeating this call never transmits.
    """
    if max_pairs is not None and (type(max_pairs) is not int or max_pairs < 1):
        raise ValueError("max_pairs must be a positive integer")
    cycle_path = store.path / "pairs" / "cycle.json"
    plan = _read_object(cycle_path)
    validate_deferred_cycle(plan, dbc_path=plan["dbc_path"])
    captured = _captured_prefix(plan)
    experiment = _read_object(store.path / "experiment.json")
    if experiment.get("experiment_id") != store.experiment_id:
        raise RuntimeError("experiment identity differs from the store")
    config = experiment.get("config") or {}
    runner_config = config.get("runner_config", config)
    if not isinstance(runner_config, Mapping):
        raise RuntimeError("saved runner configuration is invalid")
    threshold = float((runner_config.get("feedback") or {}).get(
        "interesting_score_threshold", 0.6
    ))
    processed = 0
    review_pairs: list[str] = []
    while len(plan["completed_pairs"]) < len(captured):
        if max_pairs is not None and processed >= max_pairs:
            break
        item = captured[len(plan["completed_pairs"])]
        entry = plan["entries"][item["entry_index"]]
        pair_path, pair, episodes = _validate_pair(store, plan, item, entry)
        for trial_id, mutation, metadata in episodes:
            _analyze_episode(
                store, trial_id, mutation, metadata,
                interesting_threshold=threshold,
            )
        pair_id = item["pair_id"]
        report_name = f"{pair_id}_report.json"
        report_path = store.path / "pairs" / report_name
        if report_path.is_file():
            report = _read_object(report_path)
        else:
            mutation_id = next(trial_id for trial_id, case, _ in episodes
                               if case.trial_kind == "mutation")
            noop_id = next(trial_id for trial_id, case, _ in episodes
                           if case.trial_kind == "noop")
            report = analyze_trial_pair(
                store.path, mutation_id, noop_id, pair_id=pair_id,
            )
            store.write_json(report_path, report)
        comparability = (report.get("comparability") or {}).get("status")
        mutation_id = next(trial_id for trial_id, case, _ in episodes
                           if case.trial_kind == "mutation")
        noop_id = next(trial_id for trial_id, case, _ in episodes
                       if case.trial_kind == "noop")
        if (report.get("pair_id") != pair_id
                or report.get("mutation_trial_id") != mutation_id
                or report.get("noop_trial_id") != noop_id
                or report.get("verification_status") != "unverified"
                or report.get("feedback_eligible") is not False
                or comparability not in {"comparable", "inconclusive"}):
            raise RuntimeError(f"{pair_id} has an invalid comparison report")
        if pair.get("status") == "completed" and (
            pair.get("pair_report") != report_name
            or pair.get("comparability_status") != comparability
        ):
            raise RuntimeError(f"{pair_id} report and completed pair disagree")
        pair.update(status="completed", pair_report=report_name,
                    comparability_status=comparability, updated_at=utc_now())
        store.write_json(pair_path, pair)
        plan = advance_cycle(
            plan, entry["index"], pair_id,
            comparability_status=comparability,
        )
        plan["updated_at"] = utc_now()
        store.write_json(cycle_path, plan)
        processed += 1
        second_gate = pair["recovery_gates"]["second"]
        if (_report_needs_review(report)
                or second_gate["status"] != "stable"
                or second_gate["reasons"]):
            review_pairs.append(pair_id)
    for item in captured[:len(plan["completed_pairs"])]:
        pair_id = item["pair_id"]
        if pair_id in review_pairs:
            continue
        pair = _read_object(store.path / "pairs" / f"{pair_id}.json")
        report = _read_object(store.path / "pairs" / f"{pair_id}_report.json")
        if (pair.get("status") != "completed"
                or pair.get("pair_report") != f"{pair_id}_report.json"
                or report.get("pair_id") != pair_id
                or report.get("verification_status") != "unverified"
                or report.get("feedback_eligible") is not False
                or pair.get("comparability_status") != (report.get("comparability") or {}).get("status")):
            raise RuntimeError(f"{pair_id} saved comparison is inconsistent")
        gates = pair.get("recovery_gates") or {}
        second_gate = gates.get("second") or {}
        if (_report_needs_review(report)
                or second_gate.get("status") != "stable"
                or second_gate.get("reasons")):
            review_pairs.append(pair_id)
    all_captured = len(captured) == plan["scheduled_count"]
    all_analyzed = len(plan["completed_pairs"]) == plan["scheduled_count"]
    inconclusive_count = sum(
        item.get("comparability_status") == "inconclusive"
        for item in plan["completed_pairs"]
    )
    if all_captured and all_analyzed:
        if experiment.get("status") != "completed":
            store.complete()
        status = "completed"
    else:
        status = "analysis_pending" if len(plan["completed_pairs"]) < len(captured) else "capture_pending"
    return {
        "status": status,
        "scheduled_count": plan["scheduled_count"],
        "captured_count": len(captured),
        "analyzed_count": len(plan["completed_pairs"]),
        "comparable_count": len(plan["completed_pairs"]) - inconclusive_count,
        "inconclusive_count": inconclusive_count,
        "analyzed_this_invocation": processed,
        "review_required": bool(review_pairs),
        "review_pairs": review_pairs,
        "cycle_manifest": str(cycle_path),
    }
