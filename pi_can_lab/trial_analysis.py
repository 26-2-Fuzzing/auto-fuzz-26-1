"""Offline baseline-vs-mutation statistics and anomaly detection per trial."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trial_models import MutationCase


FrameKey = tuple[str, int, bool]


def _historical_payloads(
    experiment_dir: Path | None, current_trial_id: int | None,
) -> tuple[dict[FrameKey, set[str]], dict[FrameKey, set[str]], int]:
    """Use only completed earlier trials; keep pre-injection and all-phase history distinct."""
    controls: dict[FrameKey, set[str]] = defaultdict(set)
    seen_anywhere: dict[FrameKey, set[str]] = defaultdict(set)
    trials_used = 0
    if experiment_dir is None or current_trial_id is None:
        return controls, seen_anywhere, trials_used
    for trial_dir in sorted(experiment_dir.glob("trial_*")):
        suffix = trial_dir.name.removeprefix("trial_")
        if not suffix.isdigit() or int(suffix) >= current_trial_id:
            continue
        metadata_path = trial_dir / "metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("status") != "completed":
            continue
        phases = metadata.get("phase_times_ns") or {}
        if not {"baseline_start", "baseline_end"} <= phases.keys():
            continue
        paths = {
            bus: trial_dir / name
            for bus, name in (metadata.get("logs") or {}).items()
            if isinstance(name, str)
        }
        if not paths or not all(path.is_file() for path in paths.values()):
            continue
        trials_used += 1
        spans = [(int(phases["baseline_start"]), int(phases["baseline_end"]))]
        if {"normal_start", "normal_end"} <= phases.keys():
            spans.append((int(phases["normal_start"]), int(phases["normal_end"])))
        for bus, path in paths.items():
            for frame in _load_frames(path, bus):
                key = (frame["bus"], frame["id"], frame["extended"])
                payload = frame["payload"].hex().upper()
                seen_anywhere[key].add(payload)
                if any(start <= frame["time_ns"] < end for start, end in spans):
                    controls[key].add(payload)
    return controls, seen_anywhere, trials_used


def _payload_control_ratios(
    baseline_frames: Sequence[dict[str, Any]],
    normal_frames: Sequence[dict[str, Any]],
    historical_controls: set[str],
    phase_times_ns: Mapping[str, int],
) -> list[float]:
    """Compare pre-mutation windows of the same length as the mutation window."""
    width = int(phase_times_ns["mutation_end"]) - int(phase_times_ns["mutation_start"])
    if width <= 0:
        return []
    baseline_payloads = {item["payload"].hex().upper() for item in baseline_frames}
    ratios: list[float] = []
    for phase, frames in (("baseline", baseline_frames), ("normal", normal_frames)):
        start_key, end_key = f"{phase}_start", f"{phase}_end"
        if start_key not in phase_times_ns or end_key not in phase_times_ns:
            continue
        start, end = int(phase_times_ns[start_key]), int(phase_times_ns[end_key])
        for window_start in range(start, end - width + 1, width):
            window_end = window_start + width
            window = [item for item in frames if window_start <= item["time_ns"] < window_end]
            if not window:
                continue
            if phase == "baseline":
                reference = historical_controls | {
                    item["payload"].hex().upper()
                    for item in baseline_frames
                    if not window_start <= item["time_ns"] < window_end
                }
            else:
                reference = historical_controls | baseline_payloads
            ratios.append(sum(item["payload"].hex().upper() not in reference for item in window) / len(window))
    return ratios


def validate_capture_log(path: Path, expected_experiment_id: int) -> dict[str, Any]:
    starts = ends = frames = 0
    mismatches = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL") from exc
            record_type = record.get("record_type")
            starts += int(record_type == "session_start")
            ends += int(record_type == "session_end")
            frames += int(record_type == "can_rx")
            recorded_id = record.get("experiment_id")
            if recorded_id is not None and str(recorded_id) != str(expected_experiment_id):
                mismatches.append(str(recorded_id))
    if starts != 1 or ends != 1 or frames < 1 or mismatches:
        raise ValueError(
            f"Incomplete capture {path.name}: starts={starts}, frames={frames}, "
            f"ends={ends}, experiment_mismatches={sorted(set(mismatches))}"
        )
    return {"session_start_count": starts, "frame_count": frames, "session_end_count": ends}


def _load_frames(path: Path, bus_name: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL") from exc
            if record.get("record_type") != "can_rx":
                continue
            if record.get("is_error_frame") or record.get("is_remote_frame"):
                continue
            timestamp = record.get("wall_time_ns", record.get("epoch_ns"))
            if timestamp is None:
                continue
            payload = record.get("data_hex", record.get("payload"))
            if not isinstance(payload, str):
                continue
            frames.append({
                "bus": str(record.get("bus") or bus_name).lower(),
                "id": int(record.get("arbitration_id", record.get("can_id")), 0)
                if isinstance(record.get("arbitration_id", record.get("can_id")), str)
                else int(record.get("arbitration_id", record.get("can_id"))),
                "extended": bool(record.get("is_extended_id", False)),
                "time_ns": int(timestamp),
                "payload": bytes.fromhex(payload),
            })
    return sorted(frames, key=lambda item: item["time_ns"])


def _phase(frames: Iterable[dict[str, Any]], start_ns: int, end_ns: int) -> list[dict[str, Any]]:
    return [item for item in frames if start_ns <= item["time_ns"] < end_ns]


def _metrics(frames: Sequence[dict[str, Any]], duration_seconds: float) -> dict[str, Any]:
    timestamps = [item["time_ns"] for item in frames]
    intervals = [
        (right - left) / 1_000_000.0
        for left, right in zip(timestamps, timestamps[1:])
    ]
    payloads = [item["payload"] for item in frames]
    changes = sum(before != after for before, after in zip(payloads, payloads[1:]))
    return {
        "message_count": len(frames),
        "frequency_hz": len(frames) / duration_seconds if duration_seconds > 0 else 0.0,
        "mean_cycle_time_ms": statistics.fmean(intervals) if intervals else None,
        "median_cycle_time_ms": statistics.median(intervals) if intervals else None,
        "cycle_time_stddev_ms": statistics.pstdev(intervals) if len(intervals) > 1 else (0.0 if intervals else None),
        "minimum_cycle_time_ms": min(intervals) if intervals else None,
        "maximum_cycle_time_ms": max(intervals) if intervals else None,
        "payload_unique_count": len(set(payloads)),
        "payload_change_count": changes,
        "payloads": sorted({payload.hex().upper() for payload in payloads}),
    }


def _relative(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before <= 0:
        return None
    return abs(after - before) / before


def _score(relative: float, threshold: float) -> float:
    return round(min(1.0, 0.5 + 0.5 * max(0.0, relative - threshold) / max(threshold, 1e-9)), 6)


def analyze_trial(
    *,
    rx_paths: Mapping[str, Path],
    phase_times_ns: Mapping[str, int],
    mutation: MutationCase,
    thresholds: Mapping[str, Any],
    experiment_dir: Path | None = None,
    current_trial_id: int | None = None,
) -> dict[str, Any]:
    baseline_start = int(phase_times_ns["baseline_start"])
    baseline_end = int(phase_times_ns["baseline_end"])
    mutation_start = int(phase_times_ns["mutation_start"])
    mutation_end = int(phase_times_ns["mutation_end"])
    baseline_duration = max((baseline_end - baseline_start) / 1e9, 1e-9)
    mutation_duration = max((mutation_end - mutation_start) / 1e9, 1e-9)

    timing_relative = float(thresholds.get("timing_relative_change", 0.25))
    timing_stddev_absolute = float(thresholds.get("timing_stddev_absolute_ms", 2.0))
    frequency_relative = float(thresholds.get("frequency_relative_change", 0.5))
    loss_ratio = float(thresholds.get("message_loss_ratio", 0.1))
    payload_ratio_threshold = float(thresholds.get("payload_novel_ratio", 0.2))
    min_baseline = int(thresholds.get("minimum_baseline_frames", 5))
    min_timing_intervals = int(thresholds.get("minimum_timing_intervals", 3))
    new_message_min = int(thresholds.get("new_message_minimum_frames", 3))
    min_payload_frames = int(thresholds.get("minimum_payload_mutation_frames", 5))
    min_control_windows = int(thresholds.get("minimum_payload_control_windows", 2))
    historical_controls, historical_anywhere, history_trial_count = _historical_payloads(
        experiment_dir, current_trial_id
    )

    grouped: dict[FrameKey, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"baseline": [], "normal": [], "mutation": []}
    )
    for configured_bus, path in rx_paths.items():
        frames = _load_frames(path, configured_bus)
        for item in _phase(frames, baseline_start, baseline_end):
            grouped[(item["bus"], item["id"], item["extended"])]["baseline"].append(item)
        if {"normal_start", "normal_end"} <= phase_times_ns.keys():
            for item in _phase(frames, int(phase_times_ns["normal_start"]), int(phase_times_ns["normal_end"])):
                grouped[(item["bus"], item["id"], item["extended"])]["normal"].append(item)
        for item in _phase(frames, mutation_start, mutation_end):
            grouped[(item["bus"], item["id"], item["extended"])]["mutation"].append(item)

    metrics: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    suppressed_payload_candidates: list[dict[str, Any]] = []

    def add_anomaly(bus: str, can_id: int, kind: str, score: float, evidence: Mapping[str, Any]) -> None:
        anomalies.append({
            "target_bus": bus.upper(),
            "target_id": f"0x{can_id:X}",
            "type": kind,
            "score": round(max(0.0, min(1.0, score)), 6),
            "evidence": dict(evidence),
        })

    for (bus, can_id, extended), windows in sorted(grouped.items()):
        frame_key = (bus, can_id, extended)
        baseline = _metrics(windows["baseline"], baseline_duration)
        observed = _metrics(windows["mutation"], mutation_duration)
        metrics.append({
            "bus": bus.upper(), "can_id": f"0x{can_id:X}", "is_extended_id": extended,
            "baseline": baseline, "mutation": observed,
        })

        # The target ID on any bus is transport/routing evidence, not by itself
        # a functional reaction. Interesting feedback is reserved for other IDs.
        if can_id == mutation.can_id:
            continue
        base_count = baseline["message_count"]
        mutation_count = observed["message_count"]
        previously_seen = bool(historical_anywhere.get(frame_key))
        if base_count == 0 and not windows["normal"] and not previously_seen and mutation_count >= new_message_min:
            add_anomaly(bus, can_id, "NEW_MESSAGE", min(1.0, 0.6 + mutation_count / 50.0), {
                "mutation_message_count": mutation_count,
                "historical_control_trials": history_trial_count,
                "feedback_eligible": False,
            })
        if base_count >= min_baseline:
            rate_ratio = observed["frequency_hz"] / baseline["frequency_hz"] if baseline["frequency_hz"] else 0.0
            if rate_ratio <= loss_ratio:
                add_anomaly(bus, can_id, "MESSAGE_LOSS", 1.0 - rate_ratio, {
                    "baseline_count": base_count, "mutation_count": mutation_count,
                    "frequency_ratio": rate_ratio,
                })
            frequency_change = _relative(baseline["frequency_hz"], observed["frequency_hz"])
            if frequency_change is not None and frequency_change >= frequency_relative:
                add_anomaly(bus, can_id, "FREQUENCY_CHANGE", _score(frequency_change, frequency_relative), {
                    "relative_change": frequency_change,
                    "baseline_hz": baseline["frequency_hz"], "mutation_hz": observed["frequency_hz"],
                })
        baseline_intervals = max(0, base_count - 1)
        mutation_intervals = max(0, mutation_count - 1)
        if baseline_intervals >= min_timing_intervals and mutation_intervals >= min_timing_intervals:
            timing_changes = {
                name: _relative(baseline[name], observed[name])
                for name in ("mean_cycle_time_ms", "median_cycle_time_ms")
            }
            maximum = max((value for value in timing_changes.values() if value is not None), default=0.0)
            baseline_stddev = baseline["cycle_time_stddev_ms"]
            mutation_stddev = observed["cycle_time_stddev_ms"]
            stddev_increase = max(0.0, mutation_stddev - baseline_stddev)
            relative_triggered = maximum >= timing_relative
            stddev_triggered = stddev_increase >= timing_stddev_absolute
            if relative_triggered or stddev_triggered:
                scores = []
                if relative_triggered:
                    scores.append(_score(maximum, timing_relative))
                if stddev_triggered:
                    scores.append(_score(stddev_increase, timing_stddev_absolute))
                add_anomaly(bus, can_id, "TIMING", max(scores), {
                    "relative_changes": timing_changes,
                    "baseline_mean_ms": baseline["mean_cycle_time_ms"],
                    "mutation_mean_ms": observed["mean_cycle_time_ms"],
                    "baseline_stddev_ms": baseline_stddev,
                    "mutation_stddev_ms": mutation_stddev,
                    "stddev_absolute_increase_ms": stddev_increase,
                })
        if base_count and mutation_count:
            baseline_payloads = set(baseline["payloads"])
            mutation_payloads = [item["payload"].hex().upper() for item in windows["mutation"]]
            raw_novel_ratio = sum(value not in baseline_payloads for value in mutation_payloads) / mutation_count
            normal_payloads = {item["payload"].hex().upper() for item in windows["normal"]}
            history_payloads = historical_controls.get(frame_key, set())
            reference = baseline_payloads | normal_payloads | history_payloads
            genuinely_novel = [value for value in mutation_payloads if value not in reference]
            novel_ratio = len(genuinely_novel) / mutation_count
            control_ratios = _payload_control_ratios(
                windows["baseline"], windows["normal"], history_payloads, phase_times_ns
            )
            control_max = max(control_ratios, default=0.0)
            excess_ratio = novel_ratio - control_max
            if excess_ratio >= payload_ratio_threshold:
                eligible = (
                    base_count >= min_baseline
                    and mutation_count >= min_payload_frames
                    and len(control_ratios) >= min_control_windows
                )
                add_anomaly(bus, can_id, "PAYLOAD_CHANGE", _score(excess_ratio, payload_ratio_threshold), {
                    "novel_frame_ratio": novel_ratio,
                    "raw_baseline_novel_ratio": raw_novel_ratio,
                    "control_max_novel_ratio": control_max,
                    "excess_novel_ratio": excess_ratio,
                    "matched_control_window_count": len(control_ratios),
                    "historical_control_trials": history_trial_count,
                    "previously_seen_anywhere_count": sum(
                        value in historical_anywhere.get(frame_key, set()) for value in set(genuinely_novel)
                    ),
                    "novel_payloads": sorted(set(genuinely_novel)),
                    "feedback_eligible": eligible,
                    "baseline_unique": baseline["payload_unique_count"],
                    "mutation_unique": observed["payload_unique_count"],
                })
            elif raw_novel_ratio >= payload_ratio_threshold:
                reason = "historical_or_normal_payload" if novel_ratio < payload_ratio_threshold else "pre_mutation_control_variability"
                suppressed_payload_candidates.append({
                    "target_bus": bus.upper(), "target_id": f"0x{can_id:X}",
                    "reason": reason, "raw_baseline_novel_ratio": raw_novel_ratio,
                    "remaining_novel_ratio": novel_ratio, "control_max_novel_ratio": control_max,
                    "excess_novel_ratio": excess_ratio,
                })

    propagated = []
    for bus, path in rx_paths.items():
        if bus.lower() == mutation.source_bus.lower():
            continue
        frames = _phase(_load_frames(path, bus), mutation_start, mutation_end)
        matches = sum(
            item["id"] == mutation.can_id and item["payload"] == mutation.mutated_payload
            for item in frames
        )
        if matches:
            propagated.append({"bus": bus.upper(), "matches": matches})

    existing_cross = {(item["target_bus"], item["target_id"]) for item in anomalies if item["type"] == "CROSS_BUS"}
    for item in list(anomalies):
        key = (item["target_bus"], item["target_id"])
        if item["target_bus"] != mutation.source_bus.upper() and key not in existing_cross:
            add_anomaly(item["target_bus"].lower(), int(item["target_id"], 0), "CROSS_BUS", item["score"], {
                "metric": "anomaly_on_other_bus", "related_type": item["type"],
                "source_bus": mutation.source_bus.upper(),
            })
            existing_cross.add(key)

    anomalies.sort(key=lambda item: (-item["score"], item["target_bus"], item["target_id"], item["type"]))
    return {
        "schema_version": 2,
        "thresholds": dict(thresholds),
        "phase_times_ns": dict(phase_times_ns),
        "metrics": metrics,
        "anomalies": anomalies,
        "suppressed_payload_candidates": suppressed_payload_candidates,
        "summary": {
            "anomaly_count": len(anomalies),
            "maximum_score": max((item["score"] for item in anomalies), default=0.0),
            "cross_bus": any(item["type"] == "CROSS_BUS" for item in anomalies),
            "propagated_payloads": propagated,
            "suppressed_payload_candidate_count": len(suppressed_payload_candidates),
            "historical_control_trials": history_trial_count,
        },
    }
