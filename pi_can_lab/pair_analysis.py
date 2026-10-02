"""Read-only quality gates and diagnostics for a mutation/no-op trial pair.

An event present in only one episode is an observation, never verification or
authorization for feedback.  The functions in this module do not write files.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from replay_trial_analysis import _normalized_clocks
from trial_analysis import (
    _AUTOMATIC_FIELD, _clock_alignment, _dbc_signals, _load_frames,
    _signal_is_contextual, validate_capture_log,
)
from trial_models import MutationCase


PHASES = ("baseline", "normal", "mutation", "recovery")
STATE_WINDOW_NS = 5_000_000_000
MAX_CLOCK_UNCERTAINTY_NS = 100_000_000
MIN_STATE_FRAMES = 10


def _json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _trial_dir(experiment_dir: Path, trial_id: int) -> Path:
    if not isinstance(trial_id, int) or trial_id < 1:
        raise ValueError("trial_id must be a positive integer")
    return Path(experiment_dir) / f"trial_{trial_id:04d}"


def _phase_times(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise ValueError("phase_times_ns is missing")
    result = {key: int(raw[key]) for phase in PHASES
              for key in (f"{phase}_start", f"{phase}_end")}
    previous = None
    for phase in PHASES:
        start, end = result[f"{phase}_start"], result[f"{phase}_end"]
        if start >= end or (previous is not None and start < previous):
            raise ValueError("phase timestamps are incomplete or out of order")
        previous = end
    return result


def _clocked_captures(
    trial_dir: Path, metadata: Mapping[str, Any], phases: Mapping[str, int],
    mutation: MutationCase,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[str]]:
    errors: list[str] = []
    frames: dict[str, list[dict[str, Any]]] = {}
    quality: dict[str, Any] = {}
    logs = metadata.get("logs") or {}
    if not isinstance(logs, Mapping):
        return frames, quality, ["receiver log manifest is missing"]
    clocks = _normalized_clocks(metadata)
    buses = {str(bus).lower(): name for bus, name in logs.items()
             if str(bus).lower() != "tx"}
    if not buses:
        return frames, quality, ["no receiver capture is listed"]
    distributed = "distributed" in str(metadata.get("execution_mode", "")).lower()
    required = {"p_can", "i_can"} if distributed else {"p_can", "b_can", "i_can"}
    for bus in sorted(required - set(buses)):
        errors.append(f"{bus}: required receiver capture is not listed")
    for bus, name in sorted(buses.items()):
        if not isinstance(name, str) or Path(name).name != name:
            errors.append(f"{bus}: unsafe or missing receiver filename")
            continue
        path = trial_dir / name
        try:
            capture = validate_capture_log(path, int(metadata["experiment_id"]))
            correction, uncertainty, status = _clock_alignment(
                bus, mutation.source_bus, clocks
            )
            quality[bus] = {
                "status": status,
                "uncertainty_ms": (uncertainty / 1e6 if uncertainty is not None else None),
                "frame_count": capture["frame_count"],
            }
            if status not in {"source_clock", "aligned"}:
                errors.append(f"{bus}: receiver clock is not aligned ({status})")
            elif uncertainty is None or uncertainty > MAX_CLOCK_UNCERTAINTY_NS:
                errors.append(f"{bus}: receiver clock uncertainty exceeds 100 ms")
            start, end = capture["session_start_ns"], capture["session_end_ns"]
            if start is None or end is None:
                errors.append(f"{bus}: receiver session has no wall-clock bounds")
            elif status in {"source_clock", "aligned"}:
                margin = uncertainty or 0
                if start + correction + margin > phases["baseline_start"]:
                    errors.append(f"{bus}: capture did not cover baseline start")
                if end + correction - margin < phases["recovery_end"]:
                    errors.append(f"{bus}: capture did not cover recovery end")
            frames[bus] = _load_frames(path, bus, correction_ns=correction)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"{bus}: invalid receiver capture ({exc})")
    return frames, quality, errors


def _tx_schedule(
    trial_dir: Path, phases: Mapping[str, int], mutation: MutationCase,
    experiment_id: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Check the executed send schedule, original restore, and on-wire intent."""
    result: dict[str, Any] = {"status": "inconclusive", "phase_counts": {}, "phase_rates_hz": {}}
    errors: list[str] = []
    def recorded_count(phase: str) -> int | None:
        try:
            return int((end.get("phase_sent") or {}).get(phase))
        except (TypeError, ValueError):
            return None
    try:
        with (trial_dir / "tx.jsonl").open("r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return result, [f"TX log missing or invalid ({exc})"]
    if not all(isinstance(record, Mapping) for record in records):
        return result, ["TX log contains a non-object record"]
    starts = [r for r in records if r.get("record_type") == "tx_session_start"]
    ends = [r for r in records if r.get("record_type") == "tx_session_end"]
    if len(starts) != 1 or len(ends) != 1:
        return result, ["TX session must have one start and one completed end"]
    start, end = starts[0], ends[0]
    session = start.get("tx_session_id")
    result["session_id"] = session
    interval = (start.get("transmission") or {}).get("interval_ms")
    try:
        interval_ms = float(interval)
    except (TypeError, ValueError):
        interval_ms = math.nan
    result["interval_ms"] = interval_ms if math.isfinite(interval_ms) else None
    if not session or end.get("tx_session_id") != session:
        errors.append("TX session identifiers differ")
    if any(r.get("trial_contract_version") != 1 or r.get("trial_kind") != mutation.trial_kind
           or r.get("execute") is not True
           or str(r.get("experiment_id")) != str(experiment_id) for r in (start, end)):
        errors.append("TX execution contract or trial identity is invalid")
    if end.get("status") != "completed":
        errors.append("TX session did not complete")
    if (start.get("campaign") or {}).get("enabled") is not True:
        errors.append("TX campaign contract is absent")
    if (start.get("campaign") or {}).get("normal_data_hex") != mutation.original_payload.hex().upper():
        errors.append("TX normal payload differs from probed original")
    stimulus = start.get("mutation") or {}
    if bool(stimulus.get("control_noop")) != (mutation.trial_kind == "noop"):
        errors.append("TX no-op intent differs from trial kind")
    if stimulus.get("trial_mutation_id") != mutation.mutation_id:
        errors.append("TX mutation ID differs from prepared trial")
    if not math.isfinite(interval_ms) or interval_ms <= 0:
        errors.append("TX interval is missing or invalid")
    markers = defaultdict(list)
    sent = defaultdict(list)
    for record in records:
        kind = record.get("record_type")
        if kind == "tx_phase":
            markers[(record.get("phase"), record.get("event"))].append(record)
        elif kind == "can_tx":
            if record.get("status") != "sent":
                errors.append("TX contains an unsent frame")
            elif (record.get("tx_session_id") != session
                  or record.get("trial_kind") != mutation.trial_kind
                  or str(record.get("experiment_id")) != str(experiment_id)):
                errors.append("TX contains a foreign-session frame")
            else:
                sent[record.get("phase")].append(record)
    for phase in PHASES:
        for event in ("start", "end"):
            group = markers[(phase, event)]
            if (len(group) != 1 or group[0].get("tx_session_id") != session
                    or group[0].get("trial_kind") != mutation.trial_kind
                    or group[0].get("execute") is not True
                    or str(group[0].get("experiment_id")) != str(experiment_id)
                    or group[0].get("wall_time_ns") != phases[f"{phase}_{event}"]):
                errors.append(f"TX {phase} {event} marker differs from phase metadata")
    if any(phase not in {"normal", "mutation", "recovery"} for phase in sent):
        errors.append("TX contains frames outside expected phases")
    sequence = mutation.parameters.get("sequence")
    allowed_mutation = (
        {str(value).replace(" ", "").upper() for value in sequence.get("frames", [])}
        if isinstance(sequence, Mapping) else {mutation.mutated_payload.hex().upper()}
    )
    if mutation.trial_kind == "noop":
        allowed_mutation = {mutation.original_payload.hex().upper()}
    if not allowed_mutation:
        errors.append("mutation sequence has no prepared payloads")
    phase_interval = {"normal": interval_ms, "mutation": interval_ms}
    if isinstance(sequence, Mapping):
        try:
            phase_interval["mutation"] = float(sequence.get("interval_ms", interval_ms))
        except (TypeError, ValueError):
            phase_interval["mutation"] = math.nan
    for phase, allowed_payloads in (
        ("normal", {mutation.original_payload.hex().upper()}),
        ("mutation", allowed_mutation),
    ):
        group = sent[phase]
        duration = (phases[f"{phase}_end"] - phases[f"{phase}_start"]) / 1e9
        result["phase_counts"][phase] = len(group)
        result["phase_rates_hz"][phase] = round(len(group) / duration, 6)
        result.setdefault("phase_interval_ms", {})[phase] = (
            phase_interval[phase] if math.isfinite(phase_interval[phase]) else None
        )
        if recorded_count(phase) != len(group):
            errors.append(f"TX {phase} frame count disagrees with session end")
        if len(group) > (200 if phase == "normal" else 20):
            errors.append(f"TX {phase} exceeds the trial frame limit")
        if not group:
            errors.append(f"TX {phase} sent no frames")
            continue
        if any(r.get("arbitration_id") != mutation.can_id
               or str(r.get("data_hex", "")).upper() not in allowed_payloads
               for r in group):
            errors.append(f"TX {phase} sent an unexpected ID or payload")
        if phase == "mutation" and isinstance(sequence, Mapping):
            ordered = [str(value).replace(" ", "").upper()
                       for value in sequence.get("frames", [])]
            if ordered and any(str(r.get("data_hex", "")).upper() != ordered[index % len(ordered)]
                               for index, r in enumerate(group)):
                errors.append("TX temporal mutation sequence order differs from prepared trial")
        stamps = [r.get("wall_time_ns") for r in group]
        selected_interval = phase_interval[phase]
        if any(not isinstance(t, int) for t in stamps) or not math.isfinite(selected_interval) or selected_interval <= 0:
            errors.append(f"TX {phase} lacks timing evidence")
            continue
        interval_ns = selected_interval * 1e6
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        expected_count = duration * 1000 / selected_interval
        if (abs(len(group) - expected_count) > 1.5
                or stamps[0] < phases[f"{phase}_start"]
                or stamps[0] - phases[f"{phase}_start"] > 1.5 * interval_ns
                or stamps[-1] > phases[f"{phase}_end"]
                or phases[f"{phase}_end"] - stamps[-1] > 1.5 * interval_ns
                or any(gap < .5 * interval_ns or gap > 1.5 * interval_ns for gap in gaps)):
            errors.append(f"TX {phase} schedule is incomplete or bursty")
        result.setdefault("timing", {})[phase] = {
            "first_delay_ms": round((stamps[0] - phases[f"{phase}_start"]) / 1e6, 3),
            "median_gap_ms": round(statistics.median(gaps) / 1e6, 3) if gaps else None,
            "frame_count": len(group),
        }
    restore = sent["recovery"]
    ended_restore = end.get("restore") or {}
    if (len(restore) != 1 or restore[0].get("kind") != "restore"
            or restore[0].get("arbitration_id") != mutation.can_id
            or str(restore[0].get("data_hex", "")).upper() != mutation.original_payload.hex().upper()
            or ended_restore.get("status") != "sent" or ended_restore.get("sent") != 1):
        errors.append("TX original-payload restore is not confirmed")
    result["status"] = "valid" if not errors else "inconclusive"
    return result, errors


def _load_trial(experiment_dir: Path, trial_id: int) -> dict[str, Any]:
    trial_dir = _trial_dir(experiment_dir, trial_id)
    result: dict[str, Any] = {"trial_id": trial_id, "dir": trial_dir, "errors": []}
    try:
        metadata = _json(trial_dir / "metadata.json")
        mutation = MutationCase.from_dict(_json(trial_dir / "mutation.json"))
        phases = _phase_times(metadata.get("phase_times_ns"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        result["errors"].append(f"trial {trial_id}: missing or invalid trial record ({exc})")
        return result
    result.update(metadata=metadata, mutation=mutation, phases=phases)
    if metadata.get("status") != "completed":
        result["errors"].append(f"trial {trial_id}: status is not completed")
    if metadata.get("trial_id") != trial_id:
        result["errors"].append(f"trial {trial_id}: metadata trial ID mismatch")
    if str(metadata.get("trial_kind", mutation.trial_kind)).replace("no_op", "noop") != mutation.trial_kind:
        result["errors"].append(f"trial {trial_id}: metadata trial kind mismatch")
    if str(metadata.get("source_bus", "")).lower() != mutation.source_bus.lower():
        result["errors"].append(f"trial {trial_id}: metadata source bus mismatch")
    try:
        target = metadata.get("target_id")
        target_id = int(target, 0) if isinstance(target, str) else int(target)
    except (TypeError, ValueError):
        target_id = None
    if target_id != mutation.can_id:
        result["errors"].append(f"trial {trial_id}: metadata target ID mismatch")
    dbc = metadata.get("dbc_path")
    if dbc and not Path(str(dbc)).is_file():
        result["errors"].append(f"trial {trial_id}: configured DBC is unavailable")
    frames, clock_quality, capture_errors = _clocked_captures(trial_dir, metadata, phases, mutation)
    result.update(frames=frames, clock_quality=clock_quality)
    result["errors"].extend(f"trial {trial_id}: {error}" for error in capture_errors)
    tx, tx_errors = _tx_schedule(trial_dir, phases, mutation, metadata.get("experiment_id"))
    result["tx"] = tx
    result["errors"].extend(f"trial {trial_id}: {error}" for error in tx_errors)
    try:
        result["analysis"] = _json(trial_dir / "anomalies.json")
        if result["analysis"].get("trial_id") != trial_id or not isinstance(
            result["analysis"].get("anomalies"), list
        ):
            result["errors"].append(f"trial {trial_id}: anomaly analysis identity or events invalid")
            result["analysis"]["anomalies"] = []
        elif not all(isinstance(item, Mapping) for item in result["analysis"]["anomalies"]):
            result["errors"].append(f"trial {trial_id}: anomaly event is not an object")
            result["analysis"]["anomalies"] = []
        if (result["analysis"].get("mutation_id") not in (None, mutation.mutation_id)
                or result["analysis"].get("trial_kind") not in (None, mutation.trial_kind)
                or result["analysis"].get("phase_times_ns") not in (None, phases)):
            result["errors"].append(f"trial {trial_id}: anomaly analysis does not match completed trial")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["errors"].append(f"trial {trial_id}: anomaly analysis unavailable ({exc})")
    return result


def _window(trial: Mapping[str, Any], phase: str) -> dict[str, list[dict[str, Any]]]:
    phases = trial["phases"]
    end = phases[f"{phase}_end"]
    start = max(phases[f"{phase}_start"], end - STATE_WINDOW_NS)
    return {bus: [frame for frame in frames if start <= frame["time_ns"] < end]
            for bus, frames in trial["frames"].items()}


def _periodic(frames: list[dict[str, Any]]) -> bool:
    """Avoid using intermittent IDs as vehicle-state markers."""
    if len(frames) < MIN_STATE_FRAMES:
        return False
    stamps = sorted(frame["time_ns"] for frame in frames)
    if stamps[-1] - stamps[0] < 3_500_000_000:
        return False
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    median = statistics.median(gaps)
    if median <= 0 or max(gaps) > 3 * median:
        return False
    regular = sum(.5 * median <= gap <= 1.5 * median for gap in gaps)
    return regular >= .8 * len(gaps)


def _signal_layout(trial: Mapping[str, Any]) -> dict[int, Any]:
    dbc = trial["metadata"].get("dbc_path")
    if not dbc:
        return {}
    path = Path(str(dbc))
    return _dbc_signals(path) if path.is_file() else {}


def _source_original_evidence(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Confirm the probed original stayed present on the source RX bus."""
    mutation = trial["mutation"]
    source = mutation.source_bus.lower()
    if source not in trial["frames"]:
        return {"status": "source_target_prestate_unobserved", "phase_counts": {},
                "reason": "source-bus RX was not captured; a later live probe is only a point sample"}
    details: dict[str, Any] = {}
    reasons: list[str] = []
    for phase in ("baseline", "normal", "recovery"):
        targets = [frame for frame in _window(trial, phase)[source]
                   if frame["id"] == mutation.can_id]
        originals = sum(frame["payload"] == mutation.original_payload for frame in targets)
        details[phase] = {"target_frames": len(targets), "original_frames": originals}
        if len(targets) < 3:
            reasons.append(f"source target has fewer than 3 RX frames in late {phase}")
        elif originals != len(targets):
            reasons.append(f"source target payload drifted from probed original in late {phase}")
    return {"status": "observed_stable" if not reasons else "inconclusive",
            "phase_counts": details, "reasons": reasons}


def _compare_windows(
    before: Mapping[str, list[dict[str, Any]]],
    after: Mapping[str, list[dict[str, Any]]],
    target_id: int, layouts: Mapping[int, Any], label: str,
) -> dict[str, Any]:
    """Use robust rate markers and stable non-contextual DBC signals.

    Exact payload equality is deliberately not a global state rule: GPS and
    similar contextual data can legitimately move while the system is stable.
    """
    errors: list[str] = []
    markers: list[dict[str, Any]] = []
    for bus in sorted(set(before) | set(after)):
        left = defaultdict(list)
        right = defaultdict(list)
        for frame in before.get(bus, []):
            if frame["id"] != target_id:
                left[(frame["id"], frame["extended"])].append(frame)
        for frame in after.get(bus, []):
            if frame["id"] != target_id:
                right[(frame["id"], frame["extended"])].append(frame)
        bus_markers = 0
        for key in sorted(set(left) | set(right)):
            lhs, rhs = left[key], right[key]
            can_id = key[0]
            left_periodic, right_periodic = _periodic(lhs), _periodic(rhs)
            if not left_periodic and not right_periodic:
                continue
            # A single sporadic burst should not make an entire pair fail.
            # A high-rate periodic stream that disappears is material state.
            if not left_periodic or not right_periodic:
                if can_id != 0x2A0 and max(len(lhs), len(rhs)) < 50:
                    continue
            count_change = abs(len(lhs) - len(rhs))
            tolerance = max(8, 4 * math.sqrt(len(lhs) + len(rhs)), .5 * max(len(lhs), len(rhs)))
            changed = (not left_periodic or not right_periodic
                       or count_change > tolerance)
            if left_periodic and right_periodic:
                bus_markers += 1
            row: dict[str, Any] = {
                "bus": bus.upper(), "id": f"0x{can_id:X}",
                "before_count": len(lhs), "after_count": len(rhs),
                "rate_mode": ("0x2A0" if can_id == 0x2A0 else "generic"),
                "rate_changed": changed,
            }
            if can_id == 0x2A0:
                row["before_hz"] = round(len(lhs) / 5, 3)
                row["after_hz"] = round(len(rhs) / 5, 3)
            if changed:
                errors.append(f"{label}: {bus.upper()} 0x{can_id:X} rate/mode changed ({len(lhs)} vs {len(rhs)} frames)")
            for signal in layouts.get(can_id, ()):
                if signal.muxed or _AUTOMATIC_FIELD.search(signal.name) or _signal_is_contextual(signal.name):
                    continue
                lvals = [value for frame in lhs if (value := signal.decode(frame["payload"])) is not None]
                rvals = [value for frame in rhs if (value := signal.decode(frame["payload"])) is not None]
                if len(lvals) < MIN_STATE_FRAMES or len(rvals) < MIN_STATE_FRAMES:
                    continue
                lmode, lcount = Counter(lvals).most_common(1)[0]
                rmode, rcount = Counter(rvals).most_common(1)[0]
                if lcount / len(lvals) >= .9 and rcount / len(rvals) >= .9 and lmode != rmode:
                    row.setdefault("signal_changes", []).append({
                        "signal": signal.name, "before": lmode, "after": rmode,
                    })
                    errors.append(f"{label}: {bus.upper()} 0x{can_id:X} {signal.name} state changed")
            markers.append(row)
        if bus_markers == 0:
            errors.append(f"{label}: {bus.upper()} has no sufficiently sampled common state marker")
    if set(before) != set(after):
        errors.append(f"{label}: receiver bus sets differ")
    return {"status": "stable" if not errors else "inconclusive",
            "reasons": errors, "markers": markers}


def _candidate_recovery_checks(trial: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recheck candidate targets against actual late normal/recovery frames."""
    normal = _window(trial, "normal")
    recovery = _window(trial, "recovery")
    layouts = _signal_layout(trial)
    checks: list[dict[str, Any]] = []
    for event in (trial.get("analysis") or {}).get("anomalies", []):
        if event.get("classification") != "candidate":
            continue
        kind = event.get("type")
        if kind not in {"PAYLOAD_CHANGE", "NEW_MESSAGE", "MESSAGE_LOSS",
                        "FREQUENCY_CHANGE", "TIMING"}:
            continue
        try:
            bus = str(event["target_bus"]).lower()
            raw_id = event["target_id"]
            can_id = int(raw_id, 0) if isinstance(raw_id, str) else int(raw_id)
        except (KeyError, TypeError, ValueError):
            checks.append({"event_type": kind, "status": "insufficient",
                           "reason": "candidate target identity is invalid"})
            continue
        before = [f for f in normal.get(bus, []) if f["id"] == can_id]
        after = [f for f in recovery.get(bus, []) if f["id"] == can_id]
        row: dict[str, Any] = {
            "bus": bus.upper(), "id": f"0x{can_id:X}", "event_type": kind,
            "normal_count": len(before), "late_recovery_count": len(after),
            "status": "insufficient", "reason": "not enough comparable target frames",
        }
        if bus not in normal or bus not in recovery:
            checks.append(row)
            continue
        if kind in {"NEW_MESSAGE", "MESSAGE_LOSS", "FREQUENCY_CHANGE"}:
            if max(len(before), len(after)) >= 5:
                delta = abs(len(before) - len(after))
                material = delta >= max(3, .4 * max(len(before), len(after)))
                row.update(status="changed" if material else "restored",
                           reason="late recovery rate differs" if material else "late recovery rate matches")
        elif kind == "TIMING" and len(before) >= 5 and len(after) >= 5:
            bgaps = [b["time_ns"] - a["time_ns"] for a, b in zip(before, before[1:])]
            agaps = [b["time_ns"] - a["time_ns"] for a, b in zip(after, after[1:])]
            bmed, amed = statistics.median(bgaps), statistics.median(agaps)
            bstd, astd = statistics.pstdev(bgaps), statistics.pstdev(agaps)
            material = ((abs(amed - bmed) >= 2_000_000 and
                         abs(amed - bmed) / max(bmed, 1) >= .25)
                        or astd - bstd >= 2_000_000)
            row.update(status="changed" if material else "restored",
                       reason="late recovery timing differs" if material else "late recovery timing matches",
                       normal_median_ms=round(bmed / 1e6, 3),
                       recovery_median_ms=round(amed / 1e6, 3))
        elif kind == "PAYLOAD_CHANGE" and len(before) >= 3 and len(after) >= 3:
            evidence = event.get("evidence") or {}
            signal = next((item for item in layouts.get(can_id, ())
                           if item.name == evidence.get("signal_name")), None)
            changed_bits = evidence.get("changed_bits") or []
            mask = 0
            for bit in changed_bits:
                if (isinstance(bit, int) and bit >= 0):
                    mask |= 1 << bit
            if signal is not None and not signal.muxed and not _signal_is_contextual(signal.name):
                bvalues = [value for frame in before if (value := signal.decode(frame["payload"])) is not None]
                avalues = [value for frame in after if (value := signal.decode(frame["payload"])) is not None]
            elif mask:
                bvalues = [int.from_bytes(frame["payload"], "little") & mask for frame in before]
                avalues = [int.from_bytes(frame["payload"], "little") & mask for frame in after]
            else:
                bvalues = [frame["payload"] for frame in before]
                avalues = [frame["payload"] for frame in after]
            if bvalues and avalues:
                bmode, bcount = Counter(bvalues).most_common(1)[0]
                amode, acount = Counter(avalues).most_common(1)[0]
                if bcount / len(bvalues) >= .8 and acount / len(avalues) >= .8:
                    changed = bmode != amode
                    row.update(status="changed" if changed else "restored",
                               reason="late recovery payload state differs" if changed
                               else "late recovery payload state matches")
        checks.append(row)
    return checks


def recovery_returned_to_prestate(
    experiment_dir: Path, trial_id: int | None = None,
) -> dict[str, Any]:
    """Gate the next episode on observed recovery, not elapsed time alone."""
    path = Path(experiment_dir)
    if trial_id is None:
        suffix = path.name.removeprefix("trial_")
        if not path.name.startswith("trial_") or not suffix.isdigit():
            raise ValueError("Pass an experiment directory and trial_id, or a trial directory")
        trial_id = int(suffix)
        path = path.parent
    trial = _load_trial(path, trial_id)
    errors = list(trial["errors"])
    state_comparison: dict[str, Any] = {}
    if {"frames", "phases", "mutation"} <= trial.keys():
        mutation = trial["mutation"]
        layouts = _signal_layout(trial)
        baseline = _window(trial, "baseline")
        normal = _window(trial, "normal")
        recovery = _window(trial, "recovery")
        pre = _compare_windows(baseline, normal, mutation.can_id, layouts,
                               "baseline-to-normal")
        restored = _compare_windows(normal, recovery, mutation.can_id, layouts,
                                    "normal-to-recovery")
        state_comparison = {"pre_exposure_stability": pre, "recovery": restored}
        errors.extend(pre["reasons"])
        errors.extend(restored["reasons"])
        source_evidence = _source_original_evidence(trial)
        state_comparison["source_original"] = source_evidence
        if source_evidence["status"] == "inconclusive":
            errors.extend(source_evidence["reasons"])
        # Distributed sender hosts may have TX only. The next live probe must
        # independently confirm the original before a second send; the final
        # pair report will still mark the unobserved source state inconclusive.
        candidate_checks = _candidate_recovery_checks(trial)
        state_comparison["candidate_recovery_checks"] = candidate_checks
        for check in candidate_checks:
            if check["status"] != "restored":
                errors.append(
                    f"{check.get('bus', '?')} {check.get('id', '?')} {check['event_type']} "
                    f"late recovery {check['status']}: {check['reason']}"
                )
    return {"status": "stable" if not errors else "inconclusive",
            "reasons": sorted(set(errors)), "state_comparison": state_comparison}


def _event_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    evidence = item.get("evidence") or {}
    kind = item.get("type")
    direction = None
    if kind == "FREQUENCY_CHANGE":
        direction = "up" if evidence.get("mutation_hz", 0) > evidence.get("control_hz", 0) else "down"
    elif kind == "TIMING":
        a, b = evidence.get("control_median_ms"), evidence.get("mutation_median_ms")
        direction = "up" if a is not None and b is not None and b > a else "down"
    elif kind == "PAYLOAD_CHANGE":
        direction = json.dumps({
            "signal_name": evidence.get("signal_name"),
            "observed_value": evidence.get("observed_value"),
            "changed_bits": evidence.get("changed_bits"),
        }, sort_keys=True, default=str)
    return (item.get("target_bus"), item.get("target_id"),
            item.get("is_extended_id", False), kind, direction)


def _unverified(item: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(item.get("evidence") or {})
    evidence["feedback_eligible"] = False
    evidence["verification_candidate"] = False
    return {"target_bus": item.get("target_bus"), "target_id": item.get("target_id"),
            "type": item.get("type"), "score": item.get("score"),
            "evidence": evidence,
            "verification_status": "unverified", "feedback_eligible": False}


def analyze_trial_pair(
    experiment_dir: Path, mutation_trial_id: int, noop_trial_id: int,
    *, pair_id: str | None = None,
) -> dict[str, Any]:
    """Compare two completed episodes without promoting any event to feedback."""
    if mutation_trial_id == noop_trial_id:
        raise ValueError("A pair needs two distinct trial IDs")
    root = Path(experiment_dir)
    mutation = _load_trial(root, mutation_trial_id)
    noop = _load_trial(root, noop_trial_id)
    reasons = list(mutation["errors"] + noop["errors"])
    state_comparison: dict[str, Any] = {}
    tx_comparison: dict[str, Any] = {}
    if {"mutation", "metadata", "phases"} <= mutation.keys() and {"mutation", "metadata", "phases"} <= noop.keys():
        mcase, ncase = mutation["mutation"], noop["mutation"]
        if mcase.trial_kind != "mutation" or ncase.trial_kind != "noop":
            reasons.append("pair roles do not match mutation.json trial kinds")
        if (mcase.source_bus, mcase.can_id, mcase.original_payload) != (
            ncase.source_bus, ncase.can_id, ncase.original_payload
        ):
            reasons.append("source bus, target ID, or probed original payload differs")
        if mutation["metadata"].get("experiment_id") != noop["metadata"].get("experiment_id"):
            reasons.append("episodes belong to different experiments")
        recorded_ids = {mutation["metadata"].get("pair_id"), noop["metadata"].get("pair_id")}
        expected_id = pair_id or mutation["metadata"].get("pair_id")
        if not isinstance(expected_id, str) or not expected_id or recorded_ids != {expected_id}:
            reasons.append("episodes lack a matching recorded pair ID")
        if mutation["metadata"].get("pair_role") != "mutation" or noop["metadata"].get("pair_role") != "noop":
            reasons.append("recorded pair roles are missing or inconsistent")
        order = mutation["metadata"].get("pair_order")
        if (order not in (["mutation", "noop"], ["noop", "mutation"])
                or noop["metadata"].get("pair_order") != order
                or mutation["metadata"].get("pair_position") != order.index("mutation") + 1
                or noop["metadata"].get("pair_position") != order.index("noop") + 1):
            reasons.append("recorded pair order or positions are inconsistent")
        elif (noop_trial_id if order[0] == "noop" else mutation_trial_id) + 1 != (
            mutation_trial_id if order[1] == "mutation" else noop_trial_id
        ):
            reasons.append("pair episodes are not consecutive trial IDs in recorded order")
        if mutation["metadata"].get("analysis_config") != noop["metadata"].get("analysis_config"):
            reasons.append("anomaly analysis thresholds differ between episodes")
        if mutation["metadata"].get("dbc_path") != noop["metadata"].get("dbc_path"):
            reasons.append("DBC reference differs between episodes")
        config_m = mutation["metadata"].get("collection_config") or {}
        config_n = noop["metadata"].get("collection_config") or {}
        duration_keys = ("baseline_seconds", "normal_seconds", "mutation_seconds", "post_seconds", "interval_ms")
        if any(key not in config_m or key not in config_n or config_m[key] != config_n[key]
               for key in duration_keys):
            reasons.append("configured phase durations or TX interval differ")
        actual_durations: dict[str, Any] = {}
        for phase in PHASES:
            msec = (mutation["phases"][f"{phase}_end"] - mutation["phases"][f"{phase}_start"]) / 1e9
            nsec = (noop["phases"][f"{phase}_end"] - noop["phases"][f"{phase}_start"]) / 1e9
            actual_durations[phase] = {"mutation_seconds": round(msec, 6), "noop_seconds": round(nsec, 6)}
            if abs(msec - nsec) > max(.1, .05 * max(msec, nsec)):
                reasons.append(f"actual {phase} phase durations differ")
            config_key = {"baseline": "baseline_seconds", "normal": "normal_seconds",
                          "mutation": "mutation_seconds", "recovery": "post_seconds"}[phase]
            for role, actual, config in (("mutation", msec, config_m), ("noop", nsec, config_n)):
                try:
                    configured = float(config[config_key])
                except (KeyError, TypeError, ValueError):
                    continue
                if abs(actual - configured) > max(.1, .05 * configured):
                    reasons.append(f"{role} {phase} duration differs from configured duration")
        tx_comparison = {"mutation": mutation.get("tx"), "noop": noop.get("tx"),
                         "actual_phase_durations": actual_durations}
        mtx, ntx = mutation.get("tx") or {}, noop.get("tx") or {}
        if mtx.get("interval_ms") != ntx.get("interval_ms"):
            reasons.append("executed TX intervals differ")
        for phase in ("normal", "mutation"):
            if (mtx.get("phase_interval_ms") or {}).get(phase) != (
                (ntx.get("phase_interval_ms") or {}).get(phase)
            ):
                reasons.append(f"executed TX {phase} intervals differ")
            mr = (mtx.get("phase_rates_hz") or {}).get(phase)
            nr = (ntx.get("phase_rates_hz") or {}).get(phase)
            if mr is None or nr is None or abs(mr - nr) > max(1., .1 * max(mr, nr)):
                reasons.append(f"executed TX {phase} rates differ")
        first, second = (mutation, noop) if mutation_trial_id < noop_trial_id else (noop, mutation)
        first_gate = recovery_returned_to_prestate(root, first["trial_id"])
        second_gate = recovery_returned_to_prestate(root, second["trial_id"])
        state_comparison["first_recovery"] = first_gate
        state_comparison["second_recovery"] = second_gate
        for role, trial in (("mutation", mutation), ("noop", noop)):
            if "frames" in trial:
                source_evidence = _source_original_evidence(trial)
                state_comparison[f"{role}_source_original"] = source_evidence
                if source_evidence["status"] == "source_target_prestate_unobserved":
                    reasons.append(f"{role}: source_target_prestate_unobserved")
        if first_gate["status"] != "stable":
            reasons.append("first episode did not demonstrably return to pre-exposure state")
        if second_gate["status"] != "stable":
            reasons.append("second episode did not demonstrably return to pre-exposure state")
        if "frames" in mutation and "frames" in noop:
            layouts = _signal_layout(mutation)
            for phase in ("baseline", "normal"):
                comparison = _compare_windows(
                    _window(mutation, phase), _window(noop, phase),
                    mcase.can_id, layouts, f"paired {phase}",
                )
                state_comparison[f"paired_{phase}"] = comparison
                reasons.extend(comparison["reasons"])
    m_candidates = [item for item in (mutation.get("analysis") or {}).get("anomalies", [])
                    if item.get("classification") == "candidate"]
    n_candidates = [item for item in (noop.get("analysis") or {}).get("anomalies", [])
                    if item.get("classification") == "candidate"]
    mkeys, nkeys = {_event_key(item) for item in m_candidates}, {_event_key(item) for item in n_candidates}
    comparable = not reasons
    event_comparison = {
        "status": "descriptive_only" if comparable else "inconclusive",
        "mutation_candidates": [_unverified(item) for item in m_candidates],
        "noop_candidates": [_unverified(item) for item in n_candidates],
        "mutation_only": [_unverified(item) for item in m_candidates if _event_key(item) not in nkeys] if comparable else [],
        "noop_shared": [_unverified(item) for item in m_candidates if _event_key(item) in nkeys] if comparable else [],
        "noop_only": [_unverified(item) for item in n_candidates if _event_key(item) not in mkeys] if comparable else [],
        "verification_status": "unverified",
        "feedback_eligible": False,
    }
    return {
        "schema_version": 1,
        "pair_id": pair_id or (mutation.get("metadata") or {}).get("pair_id"),
        "mutation_trial_id": mutation_trial_id,
        "noop_trial_id": noop_trial_id,
        "comparability": {"status": "comparable" if comparable else "inconclusive",
                           "reasons": sorted(set(reasons))},
        "state_comparison": state_comparison,
        "tx_comparison": tx_comparison,
        "event_comparison": event_comparison,
        "verification_status": "unverified",
        "feedback_eligible": False,
    }
