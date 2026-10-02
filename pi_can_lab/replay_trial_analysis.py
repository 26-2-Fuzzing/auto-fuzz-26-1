"""Read-only replay and negative-control diagnostics for captured CAN trials.

This module never updates an experiment. An explicit output path is created
exclusively, outside the experiment directory, after all analysis completes.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trial_analysis import _clock_alignment, analyze_trial
from trial_models import MutationCase


PHASES = ("baseline", "normal", "mutation", "recovery")
STATE_ID = 0x2A0


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def resolve_experiment_dir(path: Path) -> Path:
    root = path.expanduser().resolve()
    if (root / "experiment.json").is_file():
        return root
    nested = [item.parent for item in root.glob("experiment_*/experiment.json")]
    if len(nested) == 1:
        return nested[0]
    raise ValueError(f"Expected experiment.json in {root} or one experiment_* child")


def parse_trial_specs(specs: Sequence[str] | None) -> set[int] | None:
    if not specs:
        return None
    selected: set[int] = set()
    for spec in specs:
        for piece in spec.split(","):
            piece = piece.strip()
            if not piece:
                raise ValueError("Empty trial selector")
            limits = piece.split("-")
            if len(limits) == 1 and limits[0].isdigit():
                first = last = int(limits[0])
            elif len(limits) == 2 and all(value.isdigit() for value in limits):
                first, last = map(int, limits)
            else:
                raise ValueError(f"Invalid trial selector: {piece!r}")
            if first < 1 or last < first:
                raise ValueError(f"Invalid trial range: {piece!r}")
            selected.update(range(first, last + 1))
    return selected


def _trial_id(path: Path) -> int | None:
    suffix = path.name.removeprefix("trial_")
    return int(suffix) if path.name.startswith("trial_") and suffix.isdigit() else None


def _trial_kind(metadata: Mapping[str, Any], mutation: MutationCase) -> str:
    recorded = metadata.get("trial_kind", metadata.get("kind"))
    if recorded is None:
        return "no_op" if mutation.original_payload == mutation.mutated_payload else "mutation"
    value = str(recorded).lower().replace("-", "_")
    if value in {"noop", "sham"}:
        value = "no_op"
    if value not in {"mutation", "no_op"}:
        raise ValueError(f"Unsupported trial_kind {recorded!r}")
    mutation_kind = "no_op" if mutation.trial_kind == "noop" else mutation.trial_kind
    if value != mutation_kind:
        raise ValueError(
            f"Explicit metadata trial_kind {recorded!r} conflicts with mutation.json "
            f"trial_kind {mutation.trial_kind!r}"
        )
    if value == "no_op" and mutation.original_payload != mutation.mutated_payload:
        raise ValueError("A no_op trial cannot have a changed mutation payload")
    if value == "mutation" and mutation.original_payload == mutation.mutated_payload:
        raise ValueError("An explicitly marked mutation trial has unchanged payload")
    return value


def _resolve_dbc(
    experiment_dir: Path, config: Mapping[str, Any], explicit: Path | None,
) -> tuple[Path | None, str | None]:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"DBC does not exist: {path}")
        return path, None
    configured = config.get("dbc")
    if not isinstance(configured, str) or not configured:
        return None, "No DBC configured; signal-level interpretation may be limited"
    configured_path = Path(configured).expanduser()
    candidates = [configured_path] if configured_path.is_absolute() else [
        experiment_dir / configured_path,
        Path(__file__).resolve().parent / configured_path,
        Path.cwd() / configured_path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(), None
    return None, f"Configured DBC was not found: {configured}"


def _phase_durations(phases: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for phase in PHASES:
        start, end = f"{phase}_start", f"{phase}_end"
        if start in phases and end in phases:
            result[phase] = round((int(phases[end]) - int(phases[start])) / 1e9, 6)
    return result


def _normalized_clocks(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Never infer a shared timebase from old, unreferenced distributed offsets."""
    raw = metadata.get("clock_offsets") or {}
    if not isinstance(raw, Mapping):
        return {}
    clocks = {str(bus).lower(): dict(value) for bus, value in raw.items()
              if isinstance(value, Mapping)}
    mode = str(metadata.get("execution_mode", "")).lower()
    if "distributed" in mode:
        references = [value.get("reference_id") for value in clocks.values()]
        if (not references or not all(isinstance(ref, str) and ref for ref in references)
                or len(set(references)) != 1):
            for value in clocks.values():
                value["alignment_valid"] = False
    return clocks


def _clock_alignment_report(clocks: Mapping[str, Any], source_bus: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for bus in sorted(set(clocks) | {source_bus.lower()}):
        correction, uncertainty, status = _clock_alignment(bus, source_bus, clocks)
        result[bus] = {
            "status": status,
            "correction_to_source_ms": round(correction / 1e6, 3)
            if status in {"source_clock", "aligned"} else None,
            "uncertainty_ms": round(uncertainty / 1e6, 3)
            if status in {"source_clock", "aligned"} and uncertainty is not None else None,
            "reference_id": clocks.get(bus, {}).get("reference_id"),
        }
    return result


def _state_evidence(
    source_path: Path | None, phases: Mapping[str, Any], *, can_id: int = STATE_ID,
) -> dict[str, Any]:
    """Report a preselected state marker, without imposing a tuned state label."""
    result: dict[str, Any] = {"bus_id": f"0x{can_id:X}", "phase_counts": {}, "pre_phase_1s_counts": {}}
    if source_path is None:
        result["warning"] = "Source-bus capture unavailable"
        return result
    timestamps: list[int] = []
    with source_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("record_type") != "can_rx" or record.get("arbitration_id") != can_id:
                continue
            if record.get("is_error_frame") or record.get("is_remote_frame"):
                continue
            stamp = record.get("wall_time_ns", record.get("epoch_ns"))
            if stamp is not None:
                timestamps.append(int(stamp))
    for phase in PHASES:
        start_key, end_key = f"{phase}_start", f"{phase}_end"
        if start_key not in phases or end_key not in phases:
            continue
        start, end = int(phases[start_key]), int(phases[end_key])
        result["phase_counts"][phase] = sum(start <= stamp < end for stamp in timestamps)
        if phase in {"baseline", "normal"}:
            width = 1_000_000_000
            result["pre_phase_1s_counts"][phase] = [
                sum(left <= stamp < left + width for stamp in timestamps)
                for left in range(start, end - width + 1, width)
            ]
    return result


def _tx_evidence(
    path: Path, phases: Mapping[str, Any], mutation: MutationCase, experiment_id: Any,
) -> dict[str, Any]:
    """Gate no-op comparability on an executed contract and time-resolved TX."""
    result: dict[str, Any] = {"control_status": "unassessed", "reason": None}
    if not path.is_file():
        result["reason"] = "tx.jsonl is missing"
        return result
    payload_counts: dict[str, Counter[str]] = {"normal": Counter(), "mutation": Counter()}
    sent: dict[str, list[dict[str, Any]]] = {"normal": [], "mutation": [], "recovery": []}
    starts: list[dict[str, Any]] = []
    ends: list[dict[str, Any]] = []
    markers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unsent_count = 0
    unexpected_tx_count = 0
    other_id_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            record_type = record.get("record_type")
            if record_type == "tx_session_start":
                starts.append(record)
            elif record_type == "tx_session_end":
                ends.append(record)
            elif record_type == "tx_phase":
                markers.setdefault((record.get("phase"), record.get("event")), []).append(record)
            elif record_type != "can_tx":
                continue
            if record_type != "can_tx":
                continue
            if record.get("status") != "sent":
                unsent_count += 1
                continue
            phase = record.get("phase")
            if phase not in sent:
                unexpected_tx_count += 1
                continue
            sent[phase].append(record)
            if phase not in payload_counts:
                continue
            recorded_id = record.get("arbitration_id")
            if isinstance(recorded_id, str):
                recorded_id = int(recorded_id, 0)
            if recorded_id != mutation.can_id:
                other_id_count += 1
                continue
            payload = str(record.get("data_hex", "")).upper()
            payload_counts[phase][payload] += 1
    normal_count = sum(payload_counts["normal"].values())
    mutation_count = sum(payload_counts["mutation"].values())
    durations = _phase_durations(phases)
    normal_duration = durations.get("normal", 0.0)
    mutation_duration = durations.get("mutation", 0.0)
    normal_hz = normal_count / normal_duration if normal_duration > 0 else None
    mutation_hz = mutation_count / mutation_duration if mutation_duration > 0 else None
    result.update({
        "normal_sent": normal_count,
        "mutation_sent": mutation_count,
        "normal_payload_counts": dict(payload_counts["normal"]),
        "mutation_payload_counts": dict(payload_counts["mutation"]),
        "normal_hz": round(normal_hz, 6) if normal_hz is not None else None,
        "mutation_hz": round(mutation_hz, 6) if mutation_hz is not None else None,
        "other_id_sent_in_compared_phases": other_id_count,
        "unsent_tx_count": unsent_count,
        "unexpected_phase_tx_count": unexpected_tx_count,
    })
    if mutation.original_payload != mutation.mutated_payload:
        result["control_status"] = "not_no_op"
        result["reason"] = "mutation payload differs from original"
        return result
    if len(starts) != 1 or len(ends) != 1:
        result["reason"] = "no-op TX needs exactly one session start and completed end"
        return result
    start, end = starts[0], ends[0]
    session_id = start.get("tx_session_id")
    if (not session_id or end.get("tx_session_id") != session_id
            or any(item.get("trial_contract_version") != 1
                   or item.get("trial_kind") != "noop"
                   or item.get("execute") is not True
                   or str(item.get("experiment_id")) != str(experiment_id)
                   for item in (start, end))
            or end.get("status") != "completed"
            or (start.get("campaign") or {}).get("enabled") is not True
            or (start.get("mutation") or {}).get("control_noop") is not True
            or (start.get("mutation") or {}).get("trial_mutation_id") != mutation.mutation_id
            or (start.get("campaign") or {}).get("normal_data_hex")
            != mutation.original_payload.hex().upper()):
        result.update(control_status="invalid", reason="TX session contract is incomplete or inconsistent")
        return result
    if unsent_count or unexpected_tx_count or any(
        record.get("tx_session_id") != session_id or record.get("trial_kind") != "noop"
        for records in sent.values() for record in records
    ):
        result.update(control_status="invalid", reason="TX contains failed, unexpected, or foreign-session frames")
        return result
    for phase in PHASES:
        for event in ("start", "end"):
            group = markers.get((phase, event), [])
            expected = phases.get(f"{phase}_{event}")
            if (len(group) != 1 or expected is None
                    or group[0].get("tx_session_id") != session_id
                    or group[0].get("trial_kind") != "noop"
                    or group[0].get("execute") is not True
                    or group[0].get("wall_time_ns") != int(expected)):
                result.update(control_status="invalid", reason=f"TX {phase} {event} marker does not match phase metadata")
                return result
    if any(int((end.get("phase_sent") or {}).get(phase, -1)) != len(sent[phase])
           for phase in ("normal", "mutation")):
        result.update(control_status="invalid", reason="TX session phase counts disagree with sent records")
        return result
    restored = [record for record in sent["recovery"] if record.get("kind") == "restore"]
    restore = end.get("restore") or {}
    if (restore.get("status") != "sent" or restore.get("sent") != 1
            or len(restored) != 1 or len(sent["recovery"]) != 1
            or restored[0].get("arbitration_id") != mutation.can_id
            or str(restored[0].get("data_hex", "")).upper()
            != mutation.original_payload.hex().upper()):
        result.update(control_status="invalid", reason="TX original-payload restore is missing or inconsistent")
        return result
    original = mutation.original_payload.hex().upper()
    if not normal_count or not mutation_count or normal_hz is None or mutation_hz is None:
        result["control_status"] = "invalid"
        result["reason"] = "no sent target frames in normal or mutation slot"
    elif other_id_count or set(payload_counts["normal"]) != {original} or set(payload_counts["mutation"]) != {original}:
        result["control_status"] = "invalid"
        result["reason"] = "target ID or sent payload differs between compared slots"
    elif abs(normal_hz - mutation_hz) > max(1 / normal_duration, 1 / mutation_duration):
        result["control_status"] = "invalid"
        result["reason"] = "sent target rates differ by more than one-frame boundary resolution"
    else:
        interval = (start.get("transmission") or {}).get("interval_ms")
        try:
            interval_ns = float(interval) * 1e6
        except (TypeError, ValueError):
            interval_ns = 0
        if not math.isfinite(interval_ns) or interval_ns <= 0:
            result["reason"] = "TX contract has no valid target interval"
            return result
        timing: dict[str, Any] = {}
        for phase in ("normal", "mutation"):
            stamps = [record.get("wall_time_ns") for record in sent[phase]]
            if any(not isinstance(stamp, int) for stamp in stamps):
                result["reason"] = f"TX {phase} has no per-send wall timestamps"
                return result
            phase_start, phase_end = int(phases[f"{phase}_start"]), int(phases[f"{phase}_end"])
            gaps = [right - left for left, right in zip(stamps, stamps[1:])]
            first_delay = stamps[0] - phase_start
            tail_gap = phase_end - stamps[-1]
            expected_count = (phase_end - phase_start) / interval_ns
            timing[phase] = {
                "target_interval_ms": round(interval_ns / 1e6, 3),
                "first_delay_ms": round(first_delay / 1e6, 3),
                "tail_gap_ms": round(tail_gap / 1e6, 3),
                "min_gap_ms": round(min(gaps) / 1e6, 3) if gaps else None,
                "median_gap_ms": round(statistics.median(gaps) / 1e6, 3) if gaps else None,
                "max_gap_ms": round(max(gaps) / 1e6, 3) if gaps else None,
                "expected_frame_count": round(expected_count, 3),
            }
            if (first_delay < 0 or first_delay > 1.5 * interval_ns
                    or tail_gap < 0 or tail_gap > 1.5 * interval_ns
                    or abs(len(stamps) - expected_count) > 1
                    or any(gap < 0.5 * interval_ns or gap > 1.5 * interval_ns
                           for gap in gaps)):
                result.update(control_status="invalid", reason=f"TX {phase} schedule is bursty or incomplete")
                result["timing"] = timing
                return result
        result["timing"] = timing
        result["control_status"] = "comparable"
    return result


def _brief_anomaly(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("target_bus", "target_id", "type", "classification", "score", "evidence")
        if key in item
    }


def _analysis_brief(analysis: Mapping[str, Any], trial_kind: str) -> dict[str, Any]:
    summary = dict(analysis.get("summary") or {})
    anomalies = list(analysis.get("anomalies") or [])
    observations = list(analysis.get("observations") or [])
    classifications = Counter(str(item.get("classification", "unclassified")) for item in observations)
    candidate_count = int(summary.get("candidate_count", sum(
        item.get("classification") == "candidate" for item in anomalies
    )))
    inconclusive_count = int(summary.get("inconclusive_count", classifications.get("inconclusive", 0)))
    result = {
        "summary": summary,
        "candidate_count": candidate_count,
        "inconclusive_count": inconclusive_count,
        "incomparable_count": int(summary.get("incomparable_count", 0)),
        "observation_classifications": dict(sorted(classifications.items())),
        "candidates": [_brief_anomaly(item) for item in anomalies if item.get("classification") == "candidate"],
        "no_op_false_alert_count": candidate_count if trial_kind in {"no_op", "calibration"} else 0,
    }
    if "state_evidence" in analysis:
        result["analyzer_state_evidence"] = analysis["state_evidence"]
    if "phase_evidence" in analysis:
        result["analyzer_phase_evidence"] = analysis["phase_evidence"]
    return result


def passive_phase_quartets(phases: Mapping[str, Any], window_seconds: float) -> list[dict[str, int]]:
    """Make disjoint reference/control/test/recovery windows inside passive baseline."""
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("Passive window duration must be positive and finite")
    width = round(window_seconds * 1e9)
    if width < 1:
        raise ValueError("Passive window duration is below one nanosecond")
    start, end = int(phases["baseline_start"]), int(phases["baseline_end"])
    quartet_count = (end - start) // (4 * width)
    if quartet_count > 64:
        raise ValueError("Passive window size would create more than 64 replay groups per trial")
    quartets: list[dict[str, int]] = []
    for first in range(start, end - 4 * width + 1, 4 * width):
        quartets.append({
            "baseline_start": first,
            "baseline_end": first + width,
            "normal_start": first + width,
            "normal_end": first + 2 * width,
            "mutation_start": first + 2 * width,
            "mutation_end": first + 3 * width,
            "recovery_start": first + 3 * width,
            "recovery_end": first + 4 * width,
        })
    return quartets


def _passive_controls(
    *, rx_paths: Mapping[str, Path], phases: Mapping[str, Any], mutation: MutationCase,
    thresholds: Mapping[str, Any], dbc_path: Path | None, clocks: Mapping[str, Any],
    window_seconds: Sequence[float], experiment_dir: Path | None,
    current_trial_id: int | None,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for duration in window_seconds:
        quartets = passive_phase_quartets(phases, duration)
        pseudo_groups = []
        for index, pseudo_phases in enumerate(quartets, 1):
            analysis = analyze_trial(
                rx_paths=rx_paths, phase_times_ns=pseudo_phases, mutation=mutation,
                thresholds=thresholds, experiment_dir=experiment_dir,
                current_trial_id=current_trial_id,
                dbc_path=dbc_path, clock_offsets=clocks, trial_kind="calibration",
            )
            brief = _analysis_brief(analysis, "calibration")
            pseudo_groups.append({
                "index": index,
                "phase_times_ns": pseudo_phases,
                "candidate_count": brief["candidate_count"],
                "false_alert_count": brief["no_op_false_alert_count"],
                "inconclusive_count": brief["inconclusive_count"],
                "incomparable_count": brief["incomparable_count"],
                "candidates": brief["candidates"],
            })
        groups.append({
            "window_seconds": duration,
            "group_count": len(pseudo_groups),
            "any_false_alert": any(group["false_alert_count"] for group in pseudo_groups),
            "false_alert_group_count": sum(bool(group["false_alert_count"]) for group in pseudo_groups),
            "inconclusive_group_count": sum(bool(group["inconclusive_count"]) for group in pseudo_groups),
            "insufficient_baseline_length": not bool(pseudo_groups),
            "within_trial_groups_are_dependent": True,
            "history_from_earlier_completed_trials": experiment_dir is not None,
            "groups": pseudo_groups,
        })
    return groups


def replay_experiment(
    experiment_dir: Path, *, trial_ids: set[int] | None = None,
    kinds: set[str] | None = None, passive_windows: Sequence[float] = (),
    dbc_path: Path | None = None, include_history: bool = True,
) -> dict[str, Any]:
    root = resolve_experiment_dir(experiment_dir)
    experiment = _read_json(root / "experiment.json")
    config = (experiment.get("config") or {}).get("runner_config") or {}
    thresholds = config.get("anomaly_thresholds") or {}
    if not isinstance(thresholds, Mapping):
        raise ValueError("runner_config.anomaly_thresholds must be a mapping")
    dbc, dbc_warning = _resolve_dbc(root, config, dbc_path)
    available = {_trial_id(path): path for path in root.glob("trial_*") if path.is_dir() and _trial_id(path)}
    if trial_ids is not None:
        missing = trial_ids - set(available)
        if missing:
            raise ValueError(f"Trial directories not found: {sorted(missing)}")
    trial_results = []
    skipped = []
    for trial_id, path in sorted(available.items()):
        if trial_ids is not None and trial_id not in trial_ids:
            continue
        metadata_path, mutation_path = path / "metadata.json", path / "mutation.json"
        if not metadata_path.is_file() or not mutation_path.is_file():
            skipped.append({"trial_id": trial_id, "reason": "missing metadata or mutation file"})
            continue
        metadata = _read_json(metadata_path)
        if metadata.get("status") != "completed":
            skipped.append({"trial_id": trial_id, "reason": f"status={metadata.get('status')!r}"})
            continue
        mutation = MutationCase.from_dict(_read_json(mutation_path))
        kind = _trial_kind(metadata, mutation)
        if kinds is not None and kind not in kinds:
            continue
        trial_thresholds = metadata.get("analysis_config", thresholds)
        if not isinstance(trial_thresholds, Mapping):
            raise ValueError(f"Trial {trial_id} analysis_config must be a mapping")
        phases = metadata.get("phase_times_ns") or {}
        if not isinstance(phases, Mapping) or not {
            "baseline_start", "baseline_end", "mutation_start", "mutation_end"
        } <= set(phases):
            raise ValueError(f"Trial {trial_id} has incomplete phase_times_ns")
        log_names = metadata.get("logs") or {}
        if not isinstance(log_names, Mapping) or not log_names:
            raise ValueError(f"Trial {trial_id} has no receiver logs")
        rx_paths: dict[str, Path] = {}
        for bus, name in log_names.items():
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError(f"Trial {trial_id} has unsafe log name: {name!r}")
            log_path = path / name
            if not log_path.is_file():
                raise ValueError(f"Trial {trial_id} is missing capture {log_path}")
            rx_paths[str(bus).lower()] = log_path
        clocks = _normalized_clocks(metadata)
        analysis = analyze_trial(
            rx_paths=rx_paths, phase_times_ns=phases, mutation=mutation,
            thresholds=trial_thresholds, experiment_dir=root if include_history else None,
            current_trial_id=trial_id if include_history else None,
            dbc_path=dbc, clock_offsets=clocks, trial_kind=kind,
        )
        brief = _analysis_brief(analysis, kind)
        tx_evidence = _tx_evidence(path / "tx.jsonl", phases, mutation,
                                   experiment.get("experiment_id"))
        trial_results.append({
            "trial_id": trial_id,
            "trial_kind": kind,
            "source_bus": mutation.source_bus.upper(),
            "source_id": f"0x{mutation.can_id:X}",
            "mutation_id": mutation.mutation_id,
            "analysis_config": dict(trial_thresholds),
            "phase_durations_seconds": _phase_durations(phases),
            "clock_alignment": _clock_alignment_report(clocks, mutation.source_bus),
            "state_evidence": _state_evidence(rx_paths.get(mutation.source_bus), phases),
            "tx_evidence": tx_evidence,
            "analysis": brief,
            "passive_negative_controls": _passive_controls(
                rx_paths=rx_paths, phases=phases, mutation=mutation, thresholds=trial_thresholds,
                dbc_path=dbc, clocks=clocks, window_seconds=passive_windows,
                experiment_dir=root if include_history else None,
                current_trial_id=trial_id if include_history else None,
            ),
        })
    pseudo_by_duration = []
    for duration in passive_windows:
        groups = [
            group for trial in trial_results for group in trial["passive_negative_controls"]
            if group["window_seconds"] == duration
        ]
        pseudo_by_duration.append({
            "window_seconds": duration,
            "trials_with_usable_groups": sum(bool(group["group_count"]) for group in groups),
            "trials_with_any_false_alert": sum(bool(group["any_false_alert"]) for group in groups),
            "trials_without_usable_groups": sum(not group["group_count"] for group in groups),
            "dependent_group_count_for_diagnostics_only": sum(group["group_count"] for group in groups),
        })
    no_op = [trial for trial in trial_results if trial["trial_kind"] == "no_op"]
    comparable_no_op = [
        trial for trial in no_op if trial["tx_evidence"]["control_status"] == "comparable"
    ]
    result = {
        "schema_version": 1,
        "mode": "offline_read_only_replay",
        "experiment_dir": str(root),
        "dbc_path": str(dbc) if dbc else None,
        "warnings": [dbc_warning] if dbc_warning else [],
        "selected_trial_ids": sorted(trial_ids) if trial_ids is not None else None,
        "trial_kind_filter": sorted(kinds) if kinds is not None else None,
        "history_from_earlier_completed_trials": include_history,
        "passive_window_seconds": list(passive_windows),
        "trials": trial_results,
        "skipped_trials": skipped,
        "aggregate": {
            "completed_trial_count": len(trial_results),
            "mutation_trial_count": sum(trial["trial_kind"] == "mutation" for trial in trial_results),
            "no_op_trial_count": len(no_op),
            "no_op_trials_with_false_alert": sum(bool(trial["analysis"]["no_op_false_alert_count"]) for trial in no_op),
            "no_op_false_alert_events": sum(trial["analysis"]["no_op_false_alert_count"] for trial in no_op),
            "comparable_no_op_trial_count": len(comparable_no_op),
            "comparable_no_op_trials_with_false_alert": sum(
                bool(trial["analysis"]["no_op_false_alert_count"]) for trial in comparable_no_op
            ),
            "incomparable_no_op_trial_count": len(no_op) - len(comparable_no_op),
            "candidate_event_count": sum(trial["analysis"]["candidate_count"] for trial in trial_results),
            "inconclusive_event_count": sum(trial["analysis"]["inconclusive_count"] for trial in trial_results),
            "incomparable_comparison_count": sum(
                trial["analysis"]["incomparable_count"] for trial in trial_results
            ),
            "passive_negative_controls": pseudo_by_duration,
            "independent_unit_warning": (
                "Pseudo windows from one trial are dependent; do not treat group_count as an independent "
                "sample size or an estimated false-positive rate. Validate on held-out sessions."
            ),
        },
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_dir", type=Path, help="Experiment directory or its one-child wrapper")
    parser.add_argument("--trial", action="append", metavar="N[-M]", help="Select trial IDs; repeat or use comma/range")
    parser.add_argument("--kind", action="append", choices=("mutation", "no_op"), help="Filter trial kind")
    parser.add_argument(
        "--passive-window-seconds", action="append", type=float, default=[], metavar="SECONDS",
        help="Replay non-overlapping four-window passive-baseline pseudo controls (repeatable)",
    )
    parser.add_argument("--dbc", type=Path, help="DBC override; otherwise resolve the experiment snapshot")
    parser.add_argument("--no-history", action="store_true", help="Do not use earlier trial logs as history")
    parser.add_argument("--output", type=Path, help="Create a new JSON report outside the experiment; never overwrite")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        root = resolve_experiment_dir(args.experiment_dir)
        selected = parse_trial_specs(args.trial)
        durations = tuple(args.passive_window_seconds)
        for duration in durations:
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Passive window durations must be positive and finite")
        output = args.output.expanduser().resolve() if args.output is not None else None
        if output is not None and (output == root or root in output.parents):
            raise ValueError("Output must be outside the experiment directory")
        result = replay_experiment(
            root, trial_ids=selected, kinds=set(args.kind) if args.kind else None,
            passive_windows=durations, dbc_path=args.dbc, include_history=not args.no_history,
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if output is None:
            sys.stdout.write(payload)
        else:
            with output.open("x", encoding="utf-8") as handle:
                handle.write(payload)
            sys.stdout.write(f"Created {output}\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
