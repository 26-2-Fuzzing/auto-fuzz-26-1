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
) -> dict[str, Any]:
    baseline_start = int(phase_times_ns["baseline_start"])
    baseline_end = int(phase_times_ns["baseline_end"])
    mutation_start = int(phase_times_ns["mutation_start"])
    mutation_end = int(phase_times_ns["mutation_end"])
    baseline_duration = max((baseline_end - baseline_start) / 1e9, 1e-9)
    mutation_duration = max((mutation_end - mutation_start) / 1e9, 1e-9)

    timing_relative = float(thresholds.get("timing_relative_change", 0.25))
    frequency_relative = float(thresholds.get("frequency_relative_change", 0.5))
    loss_ratio = float(thresholds.get("message_loss_ratio", 0.1))
    payload_ratio_threshold = float(thresholds.get("payload_novel_ratio", 0.2))
    min_baseline = int(thresholds.get("minimum_baseline_frames", 5))
    min_timing_intervals = int(thresholds.get("minimum_timing_intervals", 3))
    new_message_min = int(thresholds.get("new_message_minimum_frames", 3))

    grouped: dict[FrameKey, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"baseline": [], "mutation": []}
    )
    for configured_bus, path in rx_paths.items():
        frames = _load_frames(path, configured_bus)
        for item in _phase(frames, baseline_start, baseline_end):
            grouped[(item["bus"], item["id"], item["extended"])]["baseline"].append(item)
        for item in _phase(frames, mutation_start, mutation_end):
            grouped[(item["bus"], item["id"], item["extended"])]["mutation"].append(item)

    metrics: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    def add_anomaly(bus: str, can_id: int, kind: str, score: float, evidence: Mapping[str, Any]) -> None:
        anomalies.append({
            "target_bus": bus.upper(),
            "target_id": f"0x{can_id:X}",
            "type": kind,
            "score": round(max(0.0, min(1.0, score)), 6),
            "evidence": dict(evidence),
        })

    for (bus, can_id, extended), windows in sorted(grouped.items()):
        baseline = _metrics(windows["baseline"], baseline_duration)
        observed = _metrics(windows["mutation"], mutation_duration)
        metrics.append({
            "bus": bus.upper(), "can_id": f"0x{can_id:X}", "is_extended_id": extended,
            "baseline": baseline, "mutation": observed,
        })

        # The injected frame on its source bus is transport evidence, not a
        # functional anomaly. Other IDs and buses remain eligible.
        if bus == mutation.source_bus.lower() and can_id == mutation.can_id:
            continue
        base_count = baseline["message_count"]
        mutation_count = observed["message_count"]
        if base_count == 0 and mutation_count >= new_message_min:
            add_anomaly(bus, can_id, "NEW_MESSAGE", min(1.0, 0.6 + mutation_count / 50.0), {
                "mutation_message_count": mutation_count,
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
                for name in ("mean_cycle_time_ms", "median_cycle_time_ms", "cycle_time_stddev_ms")
            }
            maximum = max((value for value in timing_changes.values() if value is not None), default=0.0)
            if maximum >= timing_relative:
                add_anomaly(bus, can_id, "TIMING", _score(maximum, timing_relative), {
                    "relative_changes": timing_changes,
                    "baseline_mean_ms": baseline["mean_cycle_time_ms"],
                    "mutation_mean_ms": observed["mean_cycle_time_ms"],
                })
        if base_count and mutation_count:
            baseline_payloads = set(baseline["payloads"])
            novel_count = sum(
                item["payload"].hex().upper() not in baseline_payloads
                for item in windows["mutation"]
            )
            novel_ratio = novel_count / mutation_count
            if novel_ratio >= payload_ratio_threshold:
                add_anomaly(bus, can_id, "PAYLOAD_CHANGE", _score(novel_ratio, payload_ratio_threshold), {
                    "novel_frame_ratio": novel_ratio,
                    "baseline_unique": baseline["payload_unique_count"],
                    "mutation_unique": observed["payload_unique_count"],
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
            add_anomaly(bus.lower(), mutation.can_id, "CROSS_BUS", min(1.0, 0.7 + matches / 100.0), {
                "metric": "exact_mutated_payload_observed", "matches": matches,
                "source_bus": mutation.source_bus.upper(),
            })

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
        "schema_version": 1,
        "thresholds": dict(thresholds),
        "phase_times_ns": dict(phase_times_ns),
        "metrics": metrics,
        "anomalies": anomalies,
        "summary": {
            "anomaly_count": len(anomalies),
            "maximum_score": max((item["score"] for item in anomalies), default=0.0),
            "cross_bus": any(item["type"] == "CROSS_BUS" for item in anomalies),
            "propagated_payloads": propagated,
        },
    }
