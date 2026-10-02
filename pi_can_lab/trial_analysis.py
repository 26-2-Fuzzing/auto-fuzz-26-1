"""Offline, state-aware CAN observations for one trial.

This module reports single-trial evidence. It never verifies a causal reaction or
authorizes mutation feedback; that requires matched control trials.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trial_models import MutationCase


FrameKey = tuple[str, int, bool]


@dataclass(frozen=True)
class SignalDefinition:
    name: str
    start: int
    length: int
    byte_order: int
    signed: bool
    muxed: bool = False

    def decode(self, payload: bytes) -> int | None:
        if self.length < 1 or self.length > 64:
            return None
        if self.byte_order == 1:
            if self.start + self.length > len(payload) * 8:
                return None
            value = (int.from_bytes(payload, "little") >> self.start) & ((1 << self.length) - 1)
        else:
            byte, bit = divmod(self.start, 8)
            value = 0
            for _ in range(self.length):
                if byte >= len(payload):
                    return None
                value = (value << 1) | ((payload[byte] >> bit) & 1)
                bit -= 1
                if bit < 0:
                    byte += 1
                    bit = 7
        if self.signed and value & (1 << (self.length - 1)):
            value -= 1 << self.length
        return value

    def bit_indexes(self) -> set[int]:
        if self.byte_order == 1:
            return set(range(self.start, self.start + self.length))
        byte, bit = divmod(self.start, 8)
        result: set[int] = set()
        for _ in range(self.length):
            result.add(byte * 8 + bit)
            bit -= 1
            if bit < 0:
                byte += 1
                bit = 7
        return result


def _dbc_signals(path: Path | None) -> dict[int, tuple[SignalDefinition, ...]]:
    """Read the signal layout without making offline analysis require cantools."""
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError(f"DBC does not exist: {path}")
    messages: dict[int, list[SignalDefinition]] = defaultdict(list)
    message_id: int | None = None
    message_pattern = re.compile(r"^\s*BO_\s+(\d+)\s+")
    signal_pattern = re.compile(
        r"^\s*SG_\s+([A-Za-z_][\w]*)\s+([mM]\d*)?\s*:\s*"
        r"(\d+)\|(\d+)@([01])([+-])"
    )
    with path.open("r", encoding="cp1252", errors="replace") as handle:
        for line in handle:
            message = message_pattern.match(line)
            if message:
                message_id = int(message.group(1))
                continue
            if line.lstrip().startswith("BO_"):
                message_id = None
            if message_id is None:
                continue
            signal = signal_pattern.match(line)
            if signal:
                name, multiplex, start, length, order, sign = signal.groups()
                messages[message_id].append(SignalDefinition(
                    name, int(start), int(length), int(order), sign == "-",
                    bool(multiplex and multiplex.startswith("m")),
                ))
    return {key: tuple(value) for key, value in messages.items()}


def _clock_value(sample: Any, key: str) -> float | None:
    if isinstance(sample, Mapping):
        value = sample.get(key)
    else:
        value = sample if key == "offset_ms" else None
    try:
        parsed = float(value) if value is not None else None
        return parsed if parsed is not None and math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _clock_alignment(
    bus: str, source_bus: str, clock_offsets: Mapping[str, Any] | None,
) -> tuple[int, int | None, str]:
    """Convert an RX host wall clock to the TX host clock, if comparable."""
    if bus.lower() == source_bus.lower():
        return 0, 0, "source_clock"
    if not clock_offsets:
        return 0, None, "unknown"
    source = clock_offsets.get(source_bus.lower(), clock_offsets.get(source_bus.upper()))
    target = clock_offsets.get(bus.lower(), clock_offsets.get(bus.upper()))
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return 0, None, "unknown"
    if source.get("alignment_valid") is False or target.get("alignment_valid") is False:
        return 0, None, "invalid_reference"
    source_ref, target_ref = source.get("reference_id"), target.get("reference_id")
    if source_ref != target_ref and (source_ref is not None or target_ref is not None):
        return 0, None, "invalid_reference"
    source_offset = _clock_value(source, "offset_ms")
    target_offset = _clock_value(target, "offset_ms")
    source_rtt = _clock_value(source, "round_trip_ms")
    target_rtt = _clock_value(target, "round_trip_ms")
    if source_offset is None or target_offset is None:
        return 0, None, "unknown"
    correction = round((source_offset - target_offset) * 1_000_000)
    source_uncertainty = _clock_value(source, "uncertainty_ms")
    target_uncertainty = _clock_value(target, "uncertainty_ms")
    if source_uncertainty is None and source_rtt is not None and source_rtt >= 0:
        source_uncertainty = source_rtt / 2
    if target_uncertainty is None and target_rtt is not None and target_rtt >= 0:
        target_uncertainty = target_rtt / 2
    if (source_uncertainty is None or target_uncertainty is None
            or source_uncertainty < 0 or target_uncertainty < 0):
        return correction, None, "uncertain"
    uncertainty = round((source_uncertainty + target_uncertainty) * 1_000_000)
    return correction, uncertainty, "aligned"


def _historical_payloads(
    experiment_dir: Path | None, current_trial_id: int | None,
) -> tuple[dict[FrameKey, set[str]], dict[FrameKey, set[str]], int]:
    """Use completed earlier trials; keep untreated and all-phase history distinct."""
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
        if "mutation_start" not in phases:
            continue
        paths = {
            bus: trial_dir / name
            for bus, name in (metadata.get("logs") or {}).items()
            if isinstance(name, str)
        }
        if not paths or not all(path.is_file() for path in paths.values()):
            continue
        source_bus = str(metadata.get("source_bus", "b_can")).lower()
        clocks = metadata.get("clock_offsets") or metadata.get("clock_samples")
        distributed = str(metadata.get("execution_mode", "")).startswith("distributed")
        used_trial = False
        for bus, path in paths.items():
            if distributed and bus.lower() != source_bus:
                source_sample = clocks.get(source_bus) if isinstance(clocks, Mapping) else None
                target_sample = clocks.get(bus.lower()) if isinstance(clocks, Mapping) else None
                if (not isinstance(source_sample, Mapping) or not isinstance(target_sample, Mapping)
                        or source_sample.get("alignment_valid") is not True
                        or target_sample.get("alignment_valid") is not True
                        or not source_sample.get("reference_id")
                        or source_sample.get("reference_id") != target_sample.get("reference_id")):
                    continue
            correction, _, status = _clock_alignment(bus, source_bus, clocks)
            if status == "invalid_reference":
                continue
            for frame in _load_frames(path, bus, correction_ns=correction):
                key = (frame["bus"], frame["id"], frame["extended"])
                payload = frame["payload"].hex().upper()
                seen_anywhere[key].add(payload)
                if (status in {"source_clock", "aligned"}
                        and frame["time_ns"] < int(phases["mutation_start"])):
                    used_trial = True
                    controls[key].add(payload)
        trials_used += int(used_trial)
    return controls, seen_anywhere, trials_used


def validate_capture_log(path: Path, expected_experiment_id: int) -> dict[str, Any]:
    starts = ends = frames = 0
    mismatches = []
    start_line = end_line = None
    first_frame_line = last_frame_line = None
    start_ns = end_ns = None
    start_session_id = end_session_id = None
    earliest_frame_ns = latest_frame_ns = None
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
            if record_type == "session_start":
                starts += 1
                start_line = line_number
                start_session_id = record.get("session_id")
                timestamp = record.get("wall_time_ns")
                if timestamp is not None:
                    try:
                        start_ns = int(timestamp)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{path}:{line_number}: invalid session_start wall_time_ns") from exc
            elif record_type == "session_end":
                ends += 1
                end_line = line_number
                end_session_id = record.get("session_id")
                timestamp = record.get("wall_time_ns")
                if timestamp is not None:
                    try:
                        end_ns = int(timestamp)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{path}:{line_number}: invalid session_end wall_time_ns") from exc
            elif record_type == "can_rx":
                frames += 1
                if first_frame_line is None:
                    first_frame_line = line_number
                last_frame_line = line_number
                timestamp = record.get("wall_time_ns")
                if timestamp is not None:
                    try:
                        frame_ns = int(timestamp)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{path}:{line_number}: invalid can_rx wall_time_ns") from exc
                    earliest_frame_ns = frame_ns if earliest_frame_ns is None else min(earliest_frame_ns, frame_ns)
                    latest_frame_ns = frame_ns if latest_frame_ns is None else max(latest_frame_ns, frame_ns)
            recorded_id = record.get("experiment_id")
            if recorded_id is not None and str(recorded_id) != str(expected_experiment_id):
                mismatches.append(str(recorded_id))
    if starts != 1 or ends != 1 or frames < 1 or mismatches:
        raise ValueError(
            f"Incomplete capture {path.name}: starts={starts}, frames={frames}, "
            f"ends={ends}, experiment_mismatches={sorted(set(mismatches))}"
        )
    if not (start_line < first_frame_line <= last_frame_line < end_line):
        raise ValueError(f"Incomplete capture {path.name}: session markers or frames are out of order")
    if (start_session_id is not None and end_session_id is not None
            and start_session_id != end_session_id):
        raise ValueError(f"Incomplete capture {path.name}: session identifiers do not match")
    if start_ns is not None and end_ns is not None and start_ns >= end_ns:
        raise ValueError(f"Incomplete capture {path.name}: session marker timestamps are out of order")
    if (start_ns is not None and earliest_frame_ns is not None and earliest_frame_ns < start_ns
            or end_ns is not None and latest_frame_ns is not None and latest_frame_ns > end_ns):
        raise ValueError(f"Incomplete capture {path.name}: CAN frame timestamps fall outside session markers")
    return {
        "session_start_count": starts, "frame_count": frames, "session_end_count": ends,
        "session_start_ns": start_ns, "session_end_ns": end_ns,
    }


def _load_frames(path: Path, bus_name: str, *, correction_ns: int = 0) -> list[dict[str, Any]]:
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
            recorded_bus = str(record.get("bus") or bus_name).lower()
            if recorded_bus != bus_name.lower():
                raise ValueError(f"{path}:{line_number}: CAN bus does not match capture source {bus_name}")
            frames.append({
                "bus": recorded_bus,
                "id": int(record.get("arbitration_id", record.get("can_id")), 0)
                if isinstance(record.get("arbitration_id", record.get("can_id")), str)
                else int(record.get("arbitration_id", record.get("can_id"))),
                "extended": bool(record.get("is_extended_id", False)),
                "time_ns": int(timestamp) + correction_ns,
                "local_time_ns": int(timestamp),
                "monotonic_ns": record.get("monotonic_ns"),
                "payload": bytes.fromhex(payload),
            })
    return sorted(frames, key=lambda item: item["time_ns"])


def _phase(frames: Iterable[dict[str, Any]], start_ns: int, end_ns: int) -> list[dict[str, Any]]:
    return [item for item in frames if start_ns <= item["time_ns"] < end_ns]


def _metrics(frames: Sequence[dict[str, Any]], duration_seconds: float) -> dict[str, Any]:
    monotonic = [item.get("monotonic_ns") for item in frames]
    if monotonic and all(isinstance(value, int) for value in monotonic) and all(
        left < right for left, right in zip(monotonic, monotonic[1:])
    ):
        timestamps = monotonic
    else:
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


def _mode(values: Sequence[Any]) -> Any:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _first_run(
    frames: Sequence[dict[str, Any]], values: Sequence[Any], reference: Any,
) -> tuple[int | None, Any, int]:
    """Find the first changed state and its initial consecutive persistence."""
    for index, value in enumerate(values):
        if value == reference or value is None:
            continue
        count = 1
        while index + count < len(values) and values[index + count] == value:
            count += 1
        return frames[index]["time_ns"], value, count
    return None, None, 0


def _recovery_state(values: Sequence[Any], reference: Any) -> str:
    if not values:
        return "unobserved"
    reference_count = sum(value == reference for value in values)
    if reference_count == len(values):
        return "restored"
    if reference_count == 0:
        return "persistent"
    return "mixed"


def _control_stationarity(
    previous: Sequence[dict[str, Any]], control: Sequence[dict[str, Any]],
    width_seconds: float, *, previous_available: bool,
) -> dict[str, Any]:
    previous_metrics = _metrics(previous, width_seconds)
    control_metrics = _metrics(control, width_seconds)
    before_count = previous_metrics["message_count"]
    after_count = control_metrics["message_count"]
    count_change = abs(before_count - after_count)
    rate_change = count_change / max(before_count, after_count, 1)
    previous_cycle = previous_metrics["median_cycle_time_ms"]
    control_cycle = control_metrics["median_cycle_time_ms"]
    cycle_change = _relative(previous_cycle, control_cycle)
    cycle_delta = (
        abs(control_cycle - previous_cycle)
        if previous_cycle is not None and control_cycle is not None else 0.0
    )
    transitioning = previous_available and (
        (max(before_count, after_count) >= 3 and count_change >= 2 and rate_change >= 0.25)
        or (cycle_change is not None and cycle_change >= 0.25 and cycle_delta >= 2.0)
    )
    return {
        "status": "transitioning" if transitioning else ("stable" if previous_available else "unmeasured"),
        "previous_count": before_count,
        "control_count": after_count,
        "count_relative_change": round(rate_change, 6),
        "previous_median_cycle_ms": previous_cycle,
        "control_median_cycle_ms": control_cycle,
    }


_AUTOMATIC_FIELD = re.compile(
    r"CRC|CHECKSUM|CHKSUM|COUNTER|(^|_)CNT($|_)|ALIVE|(^|_)BZ($|_)", re.IGNORECASE
)
_NATURALLY_VARIABLE_FIELD = re.compile(
    r"LAT|LONG|SAT|SPEED|HEADING|TEMP|PRESS|ACCEL|RPM|UTC|TIME|POSITION",
    re.IGNORECASE,
)


def _signal_is_contextual(name: str) -> bool:
    return bool(_NATURALLY_VARIABLE_FIELD.search(name))


def _novel_signal_strength(values: Sequence[int], changed: int, contextual: bool) -> str:
    """Decide whether a value is outside observed, plausible natural motion."""
    if not values:
        return "insufficient"
    if changed in values:
        return "background"
    steps = [abs(right - left) for left, right in zip(values, values[1:])]
    max_step = max(steps, default=0)
    spread = max(values) - min(values)
    distance = min(abs(changed - min(values)), abs(changed - max(values)))
    if min(values) <= changed <= max(values):
        return "background"
    if not contextual and spread == 0:
        return "candidate"
    if contextual and distance < max(3, 3 * max_step, spread):
        return "inconclusive"
    if spread and distance < max(2, 3 * max_step):
        return "inconclusive"
    return "candidate"


def _raw_bit_candidate(
    pre: Sequence[dict[str, Any]], control: Sequence[dict[str, Any]],
    post: Sequence[dict[str, Any]], covered_bits: set[int],
) -> tuple[int | None, int, int, str, list[int]]:
    """Find a persistent change in bits stable before injection but absent from DBC signals."""
    if not pre or not control or not post:
        return None, 0, 0, "unobserved", []
    size = len(control[0]["payload"])
    if any(len(item["payload"]) != size for item in (*pre, *post)):
        return None, 0, 0, "variable_dlc", []
    full_mask = (1 << (size * 8)) - 1
    first = int.from_bytes(pre[0]["payload"], "little")
    stable_mask = full_mask
    for item in pre[1:]:
        stable_mask &= ~(int.from_bytes(item["payload"], "little") ^ first)
    for bit in covered_bits:
        stable_mask &= ~(1 << bit)
    if not stable_mask:
        return None, 0, 0, "no_stable_uncovered_bits", []
    reference = first & stable_mask
    values = [int.from_bytes(item["payload"], "little") & stable_mask for item in post]
    onset, changed, run = _first_run(post, values, reference)
    if onset is None:
        return None, 0, 0, "no_stable_bit_change", []
    xor = changed ^ reference
    changed_bits = [bit for bit in range(size * 8) if xor & (1 << bit)]
    return onset, xor, run, "persistent" if run >= 3 else "short", changed_bits


def analyze_trial(
    *,
    rx_paths: Mapping[str, Path],
    phase_times_ns: Mapping[str, int],
    mutation: MutationCase,
    thresholds: Mapping[str, Any],
    experiment_dir: Path | None = None,
    current_trial_id: int | None = None,
    dbc_path: Path | None = None,
    clock_offsets: Mapping[str, Any] | None = None,
    trial_kind: str = "mutation",
) -> dict[str, Any]:
    """Describe changes observed during injection, without causal verification."""
    if trial_kind == "no_op":
        trial_kind = "noop"
    if trial_kind not in {"mutation", "noop", "calibration"}:
        raise ValueError(f"unsupported trial_kind: {trial_kind}")
    required = {"baseline_start", "baseline_end", "mutation_start", "mutation_end"}
    if not required <= phase_times_ns.keys():
        raise ValueError(f"missing phase timestamps: {sorted(required - phase_times_ns.keys())}")
    baseline_start = int(phase_times_ns["baseline_start"])
    baseline_end = int(phase_times_ns["baseline_end"])
    mutation_start = int(phase_times_ns["mutation_start"])
    mutation_end = int(phase_times_ns["mutation_end"])
    if baseline_end <= baseline_start or mutation_end <= mutation_start:
        raise ValueError("baseline and mutation phases must have positive duration")

    has_normal = {"normal_start", "normal_end"} <= phase_times_ns.keys()
    normal_start = int(phase_times_ns["normal_start"]) if has_normal else None
    normal_end = int(phase_times_ns["normal_end"]) if has_normal else None
    if has_normal and (normal_end <= normal_start or normal_end > mutation_start):
        raise ValueError("normal phase must end before mutation begins")
    control_phase = "normal" if has_normal else "baseline_tail"
    control_end = normal_end if normal_end is not None else baseline_end
    available_control = control_end - (normal_start if normal_start is not None else baseline_start)
    requested_width = float(thresholds.get("comparison_window_seconds", 5.0))
    if not math.isfinite(requested_width) or requested_width <= 0:
        raise ValueError("comparison_window_seconds must be positive and finite")
    width_ns = min(round(requested_width * 1_000_000_000), mutation_end - mutation_start, available_control)
    if width_ns <= 0:
        raise ValueError("no comparable control and mutation window")
    control_start = control_end - width_ns
    previous_start = control_start - width_ns
    comparison_seconds = width_ns / 1_000_000_000
    recovery_start = int(phase_times_ns.get("recovery_start", mutation_end))
    recovery_end = int(phase_times_ns.get("recovery_end", recovery_start))
    if recovery_end < recovery_start:
        raise ValueError("recovery phase ends before it starts")

    source_bus = mutation.source_bus.lower()
    historical_controls, historical_anywhere, history_trial_count = _historical_payloads(
        experiment_dir, current_trial_id
    )
    signal_layout = _dbc_signals(dbc_path)
    grouped: dict[FrameKey, list[dict[str, Any]]] = defaultdict(list)
    by_bus: dict[str, list[dict[str, Any]]] = {}
    clock_quality: dict[str, dict[str, Any]] = {}
    for configured_bus, path in rx_paths.items():
        bus = configured_bus.lower()
        correction_ns, uncertainty_ns, clock_status = _clock_alignment(
            bus, source_bus, clock_offsets
        )
        frames = _load_frames(path, bus, correction_ns=correction_ns)
        by_bus[bus] = frames
        clock_quality[bus] = {
            "status": clock_status,
            "correction_ms": correction_ns / 1_000_000,
            "uncertainty_ms": uncertainty_ns / 1_000_000 if uncertainty_ns is not None else None,
        }
        for frame in frames:
            grouped[(frame["bus"], frame["id"], frame["extended"])].append(frame)

    min_frames = max(1, int(thresholds.get(
        "minimum_comparison_frames", thresholds.get("minimum_baseline_frames", 5)
    )))
    min_intervals = max(2, int(thresholds.get("minimum_timing_intervals", 3)))
    min_new_frames = max(2, int(thresholds.get("new_message_minimum_frames", 3)))
    min_persistence = max(2, int(thresholds.get("minimum_persistence_frames", 3)))
    frequency_threshold = float(thresholds.get("frequency_relative_change", 0.5))
    timing_threshold = float(thresholds.get("timing_relative_change", 0.25))
    timing_absolute = float(thresholds.get("timing_stddev_absolute_ms", 2.0))
    loss_ratio = float(thresholds.get("message_loss_ratio", 0.1))

    metrics: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    suppressed_payload_candidates: list[dict[str, Any]] = []

    def emit(
        key: FrameKey, kind: str, score: float, classification: str,
        evidence: Mapping[str, Any], *, onset_ns: int | None = None,
    ) -> None:
        bus, can_id, extended = key
        quality = clock_quality.get(bus, {"status": "unknown", "uncertainty_ms": None})
        detail = dict(evidence)
        detail.update({
            "trial_kind": trial_kind,
            "clock_status": quality["status"],
            "clock_uncertainty_ms": quality["uncertainty_ms"],
            "feedback_eligible": False,
        })
        if onset_ns is not None:
            detail["onset_ms_from_mutation"] = round((onset_ns - mutation_start) / 1_000_000, 3)
            uncertainty_ns = round(quality["uncertainty_ms"] * 1_000_000) if quality["uncertainty_ms"] is not None else None
            if (classification == "candidate" and uncertainty_ns is not None and uncertainty_ns > 0
                    and min(abs(onset_ns - mutation_start), abs(onset_ns - mutation_end)) <= uncertainty_ns):
                classification = "inconclusive"
                detail["reason"] = "onset_overlaps_mutation_boundary_uncertainty"
        if bus != source_bus and quality["status"] == "invalid_reference":
            classification = "inconclusive"
            detail["reason"] = "cross_host_clock_reference_invalid"
        elif classification == "candidate" and bus != source_bus and quality["status"] not in {"aligned", "source_clock"}:
            classification = "inconclusive"
            detail["reason"] = "cross_host_clock_alignment_unavailable"
        detail["verification_candidate"] = classification == "candidate" and trial_kind == "mutation"
        item = {
            "target_bus": bus.upper(),
            "target_id": f"0x{can_id:X}",
            "is_extended_id": extended,
            "type": kind,
            "classification": classification,
            "cross_bus": bus != source_bus,
            "score": round(max(0.0, min(1.0, score)), 6),
            "evidence": detail,
        }
        (anomalies if classification == "candidate" else observations).append(item)
        if kind == "PAYLOAD_CHANGE" and classification != "candidate":
            suppressed_payload_candidates.append({
                "target_bus": bus.upper(),
                "target_id": f"0x{can_id:X}",
                "reason": detail.get("reason", classification),
            })

    for key, frames in sorted(grouped.items()):
        bus, can_id, extended = key
        baseline = _phase(frames, baseline_start, baseline_end)
        normal = _phase(frames, normal_start, normal_end) if has_normal else []
        pre = [item for item in frames if item["time_ns"] < mutation_start]
        control = _phase(frames, control_start, control_end)
        previous = (
            _phase(frames, previous_start, control_start)
            if previous_start >= baseline_start else []
        )
        mutation_window = _phase(frames, mutation_start, mutation_start + width_ns)
        mutation_all = _phase(frames, mutation_start, mutation_end)
        recovery = _phase(frames, recovery_start, recovery_end)
        post = _phase(frames, mutation_start, max(mutation_end, recovery_end))
        control_stats = _metrics(control, comparison_seconds)
        mutation_stats = _metrics(mutation_window, comparison_seconds)
        stationarity = _control_stationarity(
            previous, control, comparison_seconds,
            previous_available=previous_start >= baseline_start,
        )
        count_before = control_stats["message_count"]
        count_after = mutation_stats["message_count"]
        metrics.append({
            "bus": bus.upper(),
            "can_id": f"0x{can_id:X}",
            "is_extended_id": extended,
            "baseline": _metrics(baseline, (baseline_end - baseline_start) / 1e9),
            "normal": _metrics(normal, (normal_end - normal_start) / 1e9) if has_normal else None,
            "control": control_stats,
            "mutation": mutation_stats,
            "recovery": _metrics(recovery, (recovery_end - recovery_start) / 1e9) if recovery_end > recovery_start else None,
            "comparison_window_seconds": comparison_seconds,
            "control_phase": control_phase,
            "stationarity": stationarity,
            "comparison_status": (
                "insufficient" if count_before < min_frames or count_after < min_frames
                else "transitioning" if stationarity["status"] == "transitioning"
                else "assessable"
            ),
        })
        if can_id == mutation.can_id:
            continue

        common = {
            "control_count": count_before,
            "mutation_count": count_after,
            "comparison_window_seconds": comparison_seconds,
            "control_phase": control_phase,
            "stationarity": stationarity,
            "historical_control_trials": history_trial_count,
        }
        if stationarity["status"] == "transitioning":
            emit(key, "CONTROL_TRANSITION", 0.0, "preexisting", common | {
                "reason": "rate_or_cycle_mode_changed_before_mutation",
            })

        if count_before == 0 and count_after >= min_new_frames:
            # A frame emitted only during an earlier mutation is not an
            # untreated reference and must not suppress a repeat observation.
            natural_occurrence = bool(pre or historical_controls.get(key))
            sustained = len(recovery) >= min_new_frames
            emit(
                key, "NEW_MESSAGE", min(1.0, 0.6 + count_after / 50),
                "background" if natural_occurrence else ("candidate" if sustained else "inconclusive"),
                common | {
                    "reason": (
                        "seen_before_mutation_or_in_untreated_history" if natural_occurrence
                        else "unconfirmed_short_burst" if not sustained else "new_after_mutation"
                    ),
                    "historically_seen": bool(historical_anywhere.get(key)),
                    "recovery_count": len(recovery),
                },
                onset_ns=mutation_window[0]["time_ns"],
            )
        elif count_before < min_frames:
            if count_before != count_after or (count_after and control_stats["payloads"] != mutation_stats["payloads"]):
                emit(key, "INSUFFICIENT_SAMPLE", 0.0, "inconclusive", common | {
                    "reason": "too_few_frames_in_immediate_control",
                })
        else:
            ratio = count_after / count_before
            count_delta = abs(count_before - count_after)
            rate_change = count_delta / count_before
            timing_class = "inconclusive" if stationarity["status"] == "transitioning" else "candidate"
            rate_class = timing_class
            boundary_count = 0
            quality = clock_quality.get(bus, {})
            uncertainty_ms = quality.get("uncertainty_ms")
            if uncertainty_ms is not None and uncertainty_ms > 0:
                tolerance_ns = round(uncertainty_ms * 1_000_000)
                boundary_count = sum(
                    min(abs(item["time_ns"] - boundary) for boundary in (
                        control_start, control_end, mutation_start, mutation_start + width_ns
                    )) <= tolerance_ns
                    for item in (*control, *mutation_window)
                )
                if count_delta <= boundary_count:
                    rate_class = "inconclusive"
            if ratio <= loss_ratio:
                emit(key, "MESSAGE_LOSS", 1.0 - ratio, rate_class, common | {
                    "frequency_ratio": ratio,
                    "boundary_uncertain_frames": boundary_count,
                    "reason": "control_mode_unstable" if stationarity["status"] == "transitioning" else "message_absent_or_rare",
                })
            elif rate_change >= frequency_threshold and count_delta >= 2:
                emit(key, "FREQUENCY_CHANGE", _score(rate_change, frequency_threshold), rate_class, common | {
                    "relative_change": rate_change,
                    "control_hz": control_stats["frequency_hz"],
                    "mutation_hz": mutation_stats["frequency_hz"],
                    "boundary_uncertain_frames": boundary_count,
                    "reason": "control_mode_unstable" if stationarity["status"] == "transitioning" else "rate_changed",
                })
            control_intervals = max(0, count_before - 1)
            mutation_intervals = max(0, count_after - 1)
            if control_intervals >= min_intervals and mutation_intervals >= min_intervals:
                cycle_change = _relative(
                    control_stats["median_cycle_time_ms"], mutation_stats["median_cycle_time_ms"]
                )
                control_cycle = control_stats["median_cycle_time_ms"]
                mutation_cycle = mutation_stats["median_cycle_time_ms"]
                absolute_change = abs(mutation_cycle - control_cycle)
                control_stddev = control_stats["cycle_time_stddev_ms"] or 0.0
                mutation_stddev = mutation_stats["cycle_time_stddev_ms"] or 0.0
                jitter_increase = max(0.0, mutation_stddev - control_stddev)
                bursty_control = bool(control_cycle and control_stddev > 0.5 * control_cycle)
                if (
                    (cycle_change is not None and cycle_change >= timing_threshold and absolute_change >= 2.0)
                    or jitter_increase >= timing_absolute
                ):
                    emit(key, "TIMING", max(
                        _score(cycle_change or 0.0, timing_threshold) if cycle_change is not None else 0.0,
                        _score(jitter_increase, timing_absolute) if jitter_increase >= timing_absolute else 0.0,
                    ), "inconclusive" if bursty_control else timing_class, common | {
                        "control_median_ms": control_cycle,
                        "mutation_median_ms": mutation_cycle,
                        "control_stddev_ms": control_stddev,
                        "mutation_stddev_ms": mutation_stddev,
                        "relative_change": cycle_change,
                        "reason": (
                            "bursty_control_not_periodic" if bursty_control else
                            "control_mode_unstable" if stationarity["status"] == "transitioning"
                            else "cycle_changed"
                        ),
                    })
            elif count_after and count_before != count_after:
                emit(key, "TIMING_SUPPORT", 0.0, "inconclusive", common | {
                    "reason": "too_few_intervals_for_timing",
                })

        if not control or not mutation_all or not pre:
            continue
        first_post_payloads = {item["payload"].hex().upper() for item in mutation_all}
        pre_payloads = {item["payload"].hex().upper() for item in pre}
        historical_payloads = historical_anywhere.get(key, set())
        definitions = signal_layout.get(can_id, ())
        covered_bits: set[int] = set()
        signal_observed = False
        for signal in definitions:
            covered_bits.update(signal.bit_indexes())
            if signal.muxed or _AUTOMATIC_FIELD.search(signal.name):
                continue
            pre_values = [value for item in pre if (value := signal.decode(item["payload"])) is not None]
            control_values = [value for item in control if (value := signal.decode(item["payload"])) is not None]
            post_values = [signal.decode(item["payload"]) for item in post]
            recovery_values = [signal.decode(item["payload"]) for item in recovery]
            if not control_values or not post_values:
                continue
            reference = _mode(control_values)
            onset, changed, run = _first_run(post, post_values, reference)
            if onset is None:
                continue
            signal_observed = True
            mutation_support = sum(
                signal.decode(item["payload"]) == changed for item in mutation_all
            )
            classification = _novel_signal_strength(
                pre_values, changed, _signal_is_contextual(signal.name)
            )
            if classification == "candidate" and (
                run < min_persistence or onset >= mutation_end
                or len(recovery) < min_persistence or mutation_support < 2
            ):
                classification = "inconclusive"
            if (len(pre_values) < min_frames or len(control_values) < min_frames) and classification == "candidate":
                classification = "inconclusive"
            changed_payloads = sorted({
                item["payload"].hex().upper()
                for item, value in zip(post, post_values)
                if value == changed and item["time_ns"] < mutation_end
            })
            emit(key, "PAYLOAD_CHANGE", 0.8 if classification == "candidate" else 0.0,
                 classification, common | {
                    "signal_name": signal.name,
                    "reference_value": reference,
                    "observed_value": changed,
                    "pre_injection_values": sorted(set(pre_values)),
                    "contextual_signal": _signal_is_contextual(signal.name),
                    "persistence_frames": run,
                    "mutation_support_frames": mutation_support,
                    "recovery_state": _recovery_state(recovery_values, reference),
                    "historically_seen_payload": any(value in historical_payloads for value in changed_payloads),
                    "pre_injection_exact_payload": any(value in pre_payloads for value in changed_payloads),
                    "novel_payloads": changed_payloads,
                    "reason": (
                        "known_pre_injection_signal_state" if classification == "background"
                        else "contextual_signal_needs_control" if classification == "inconclusive"
                        else "persistent_signal_transition"
                    ),
                }, onset_ns=onset)

        onset, xor, run, raw_status, changed_bits = _raw_bit_candidate(
            pre, control, post, covered_bits
        )
        if onset is not None:
            changed_frame = next(item for item in post if item["time_ns"] == onset)
            changed_payload = changed_frame["payload"].hex().upper()
            reference_raw = int.from_bytes(control[-1]["payload"], "little") & xor
            mutation_support = sum(
                (int.from_bytes(item["payload"], "little") & xor) != reference_raw
                for item in mutation_all
            )
            classification = (
                "candidate" if (
                    run >= min_persistence and len(pre) >= min_frames
                    and len(control) >= min_frames and mutation_support >= 2
                    and onset < mutation_end and len(recovery) >= min_persistence
                )
                else "inconclusive"
            )
            recovery_raw = [int.from_bytes(item["payload"], "little") & xor for item in recovery]
            emit(key, "PAYLOAD_CHANGE", 0.8 if classification == "candidate" else 0.0,
                 classification, common | {
                    "signal_name": None,
                    "changed_bits": changed_bits,
                    "changed_xor": f"0x{xor:X}",
                    "persistence_frames": run,
                    "mutation_support_frames": mutation_support,
                    "recovery_state": _recovery_state(recovery_raw, reference_raw),
                    "historically_seen_payload": changed_payload in historical_payloads,
                    "pre_injection_exact_payload": changed_payload in pre_payloads,
                    "novel_payloads": [changed_payload],
                    "reason": "persistent_stable_bit_transition" if classification == "candidate" else raw_status,
                }, onset_ns=onset)
        elif not signal_observed and first_post_payloads - pre_payloads:
            emit(key, "PAYLOAD_CHANGE", 0.0, "background", common | {
                "reason": "only_variable_or_automatic_fields_changed",
                "pre_injection_unique_payloads": len(pre_payloads),
                "mutation_unique_payloads": len(first_post_payloads),
            })

    propagated = []
    for bus, frames in by_bus.items():
        if bus == source_bus:
            continue
        matches = sum(
            item["id"] == mutation.can_id and item["payload"] == mutation.mutated_payload
            and mutation_start <= item["time_ns"] < mutation_end
            for item in frames
        )
        if matches:
            propagated.append({"bus": bus.upper(), "matches": matches})

    anomalies.sort(key=lambda item: (
        -item["score"], item["target_bus"], item["target_id"], item["type"]
    ))
    observations.sort(key=lambda item: (
        item["target_bus"], item["target_id"], item["type"], item["classification"]
    ))
    cross_bus = any(item["cross_bus"] for item in (*anomalies, *observations))
    return {
        "schema_version": 3,
        "thresholds": dict(thresholds),
        "phase_times_ns": dict(phase_times_ns),
        "trial_kind": trial_kind,
        "comparison_window_seconds": comparison_seconds,
        "clock_quality": clock_quality,
        "metrics": metrics,
        "anomalies": anomalies,
        "observations": observations,
        "suppressed_payload_candidates": suppressed_payload_candidates,
        "summary": {
            "anomaly_count": len(anomalies),
            "candidate_count": len(anomalies),
            "false_alert_count": len(anomalies) if trial_kind != "mutation" else 0,
            "inconclusive_count": sum(
                item["classification"] == "inconclusive" for item in observations
            ),
            "incomparable_count": sum(
                item["comparison_status"] != "assessable"
                for item in metrics if item["can_id"] != f"0x{mutation.can_id:X}"
            ),
            "preexisting_count": sum(
                item["classification"] == "preexisting" for item in observations
            ),
            "maximum_score": max((item["score"] for item in anomalies), default=0.0),
            "cross_bus": cross_bus,
            "propagated_payloads": propagated,
            "suppressed_payload_candidate_count": len(suppressed_payload_candidates),
            "historical_control_trials": history_trial_count,
        },
    }
