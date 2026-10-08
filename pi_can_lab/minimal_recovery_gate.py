"""Small capture-only recovery check used while a paired cycle is running.

This module reads local trial artifacts. It does not derive anomaly candidates,
pair comparability, feedback, or a causal verdict. Those belong to the offline
finalizer. Capture integrity controls whether another transmission can proceed;
state observations are retained for later analysis.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from pair_analysis import (
    MAX_CLOCK_UNCERTAINTY_NS, _phase_times, _tx_schedule,
    bcm02_activity_changed,
)
from replay_trial_analysis import _normalized_clocks
from trial_analysis import _clock_alignment, validate_capture_log
from trial_models import MutationCase


WINDOW_NS = 5_000_000_000
SOURCE_MIN_FRAMES = 3
STATE_MIN_FRAMES = 5
WATCH_IDS = {0x2A0, 0x3D6, 0x184, 0x583, 0x3CE, 0x3CF, 0x3D0, 0x3D1}

# The bit positions below come from A5.dbc. Only directly relevant light and
# door/lock fields are checked; counters, checksums, and unrelated body fields
# are deliberately outside this brief runtime gate.
LIGHT_BITS = {
    "LH_Aussenlicht_def": 7,  # Exterior-light fault indication, not lamp-on state.
    "LH_Standlicht_H_aktiv": 8,
    "LH_Parklicht_HL_aktiv": 9,
    "LH_Parklicht_HR_aktiv": 10,
    "LH_Bremslicht_H_aktiv": 11,
    "LH_Nebelschluss_aktiv": 12,
    "LH_Rueckfahrlicht_aktiv": 13,
    "LH_Blinker_HL_akt": 14,
    "LH_Blinker_HR_akt": 15,
    "LH_Schlusslicht_li_def": 18,
    "LH_Schlusslicht_re_def": 34,
}
LOCK_COMMAND_BITS = {
    "ZV_FT_verriegeln": 12,
    "ZV_FT_entriegeln": 13,
    "ZV_BT_verriegeln": 14,
    "ZV_BT_entriegeln": 15,
    "ZV_HFS_verriegeln": 16,
    "ZV_HFS_entriegeln": 17,
    "ZV_HBFS_verriegeln": 18,
    "ZV_HBFS_entriegeln": 19,
    "ZV_zentral_safen": 20,
    "ZV_zentral_entsafen": 21,
    "ZV_auf_FT": 26,
    "ZV_zu_FT": 27,
    "ZV_auf_BT": 28,
    "ZV_zu_BT": 29,
    "ZV_auf_Kessy": 30,
    "ZV_zu_Kessy": 31,
    "ZV_auf_Funk": 32,
    "ZV_zu_Funk": 33,
    "ZV_zu_Zeitl_Nachverr": 36,
    "ZV_HSK_entriegeln": 37,
    "ZV_HSK_verriegeln": 38,
    "ZV_entriegeln_Anf": 62,
    "ZV_auto_Ansteuerung": 63,
}
LOCK_STATE_BITS = {
    "ZV_verriegelt_intern_ist": 16,
    "ZV_verriegelt_extern_ist": 17,
    "ZV_verriegelt_intern_soll": 18,
    "ZV_verriegelt_extern_soll": 19,
    "ZV_gesafet_extern_ist": 20,
    "ZV_gesafet_extern_soll": 21,
    "ZV_FT_offen": 24,
    "ZV_BT_offen": 25,
    "ZV_HFS_offen": 26,
    "ZV_HBFS_offen": 27,
    "ZV_HD_offen": 28,
    "ZV_HS_offen": 29,
}
DOOR_BITS = {
    "Tuer_geoeffnet": 0,
    "verriegelt": 1,
    "gesafet": 2,
    "Unlock_Taster": 5,
    "Lock_Taster": 6,
}
SIGNAL_BITS = {
    0x3D6: LIGHT_BITS,
    0x184: LOCK_COMMAND_BITS,
    0x583: LOCK_STATE_BITS,
    **{can_id: DOOR_BITS for can_id in (0x3CE, 0x3CF, 0x3D0, 0x3D1)},
}
# A5.dbc describes this field as exterior lighting not being controllable
# because its coding cannot be read. It is a diagnostic fault indication, not
# a command or a measured lamp-on state. Keep it as an observation, including
# its timing and value, without treating this signal alone as a review gate.
OBSERVATION_ONLY_SIGNAL_KEYS = {(0x3D6, "LH_Aussenlicht_def")}


def _result(
    reasons: list[str], observed_change: bool, checks: dict[str, Any],
    integrity_reasons: list[str], advisory_observations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "review_required" if reasons else "stable",
        "observed_change": observed_change,
        "reasons": sorted(set(reasons)),
        "capture_integrity_status": "review_required" if integrity_reasons else "stable",
        "capture_integrity_reasons": sorted(set(integrity_reasons)),
        "advisory_observations": sorted(set(advisory_observations or [])),
        "checks": checks,
    }


def fault_only_advisory(gate: Mapping[str, Any]) -> bool:
    """Validate the sole observation that may leave a stable gate marked changed.

    The saved advisory must agree with the structured CAN evidence. This keeps
    a malformed or unrelated observation from authorizing the next exposure.
    """
    advisory = gate.get("advisory_observations")
    checks = gate.get("checks")
    watched = checks.get("watched_ids") if isinstance(checks, Mapping) else None
    if not isinstance(advisory, list) or not advisory or not isinstance(watched, Mapping):
        return False
    expected: list[str] = []
    for bus, ids in watched.items():
        if not isinstance(bus, str) or not isinstance(ids, Mapping):
            return False
        for can_id, details in ids.items():
            if not isinstance(details, Mapping) or details.get("rate_changed"):
                return False
            changes = details.get("signal_changes", [])
            if not isinstance(changes, list):
                return False
            for change in changes:
                if (not isinstance(change, Mapping)
                        or can_id != "0x3D6"
                        or change.get("signal") != "LH_Aussenlicht_def"
                        or change.get("review_required") is not False):
                    return False
                values = (change.get("normal_active"), details.get("normal_count"),
                          change.get("recovery_active"), details.get("recovery_count"))
                if any(type(value) is not int or value < 0 for value in values):
                    return False
                before_active, before_count, after_active, after_count = values
                expected.append(
                    f"{bus.upper()} 0x3D6 LH_Aussenlicht_def late recovery changed "
                    f"({before_active}/{before_count} vs {after_active}/{after_count} active)"
                )
    return bool(expected) and len(expected) == len(advisory) and sorted(expected) == sorted(advisory)


def _late_window(phases: Mapping[str, int], phase: str) -> tuple[int, int]:
    end = phases[f"{phase}_end"]
    return end - WINDOW_NS, end


def _read_selected_frames(
    path: Path, bus: str, correction_ns: int, phases: Mapping[str, int], target_id: int,
) -> dict[tuple[str, int], list[bytes]]:
    """Keep only preselected IDs from three short windows, without full analysis."""
    windows = {phase: _late_window(phases, phase)
               for phase in ("baseline", "normal", "recovery")}
    selected: dict[tuple[str, int], list[bytes]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != "can_rx":
                continue
            if record.get("is_error_frame") or record.get("is_remote_frame"):
                continue
            if str(record.get("bus", bus)).lower() != bus:
                raise ValueError(f"{path.name}:{line_number}: CAN bus mismatch")
            if record.get("is_extended_id"):
                continue
            raw_id = record.get("arbitration_id", record.get("can_id"))
            can_id = int(raw_id, 0) if isinstance(raw_id, str) else int(raw_id)
            if can_id not in WATCH_IDS and can_id != target_id:
                continue
            raw_time = record.get("wall_time_ns", record.get("epoch_ns"))
            time_ns = int(raw_time) + correction_ns
            payload_hex = record.get("data_hex", record.get("payload"))
            if not isinstance(payload_hex, str):
                raise ValueError(f"{path.name}:{line_number}: watched frame has no payload")
            payload = bytes.fromhex(payload_hex)
            for phase, (start, end) in windows.items():
                if start <= time_ns < end:
                    selected[(phase, can_id)].append(payload)
                    break
    return selected


def _material_rate_change(can_id: int, before: int, after: int) -> bool:
    larger = max(before, after)
    if can_id == 0x2A0:
        return bcm02_activity_changed(before, after)
    return larger >= STATE_MIN_FRAMES and abs(before - after) >= max(3, .4 * larger)


def _compare_bus(
    bus: str, selected: Mapping[tuple[str, int], list[bytes]],
    reasons: list[str], advisory_observations: list[str],
    checks: dict[str, Any], target_id: int,
) -> bool:
    changed = False
    watched: dict[str, Any] = {}
    for can_id in sorted(WATCH_IDS):
        if can_id == target_id:
            continue
        before = selected.get(("normal", can_id), [])
        after = selected.get(("recovery", can_id), [])
        if not before and not after:
            continue
        id_label = f"0x{can_id:X}"
        details: dict[str, Any] = {"normal_count": len(before), "recovery_count": len(after)}
        watched[id_label] = details
        if _material_rate_change(can_id, len(before), len(after)):
            changed = True
            details["rate_changed"] = True
            reasons.append(f"{bus.upper()} {id_label} late recovery rate changed "
                           f"({len(before)} vs {len(after)} frames)")
        # A sporadic ID can be present in only one window. The rate check above
        # handles a material appearance/loss; smaller bursts are deferred.
        if len(before) < STATE_MIN_FRAMES or len(after) < STATE_MIN_FRAMES:
            continue
        for name, bit in SIGNAL_BITS.get(can_id, {}).items():
            minimum_bytes = bit // 8 + 1
            if any(len(payload) < minimum_bytes for payload in before + after):
                reasons.append(f"{bus.upper()} {id_label} {name} payload is too short")
                continue
            bvalues = [(payload[bit // 8] >> (bit % 8)) & 1 for payload in before]
            avalues = [(payload[bit // 8] >> (bit % 8)) & 1 for payload in after]
            bactive, aactive = sum(bvalues), sum(avalues)
            bmode, bcount = Counter(bvalues).most_common(1)[0]
            amode, acount = Counter(avalues).most_common(1)[0]
            stable_shift = (bmode != amode and bcount / len(bvalues) >= .8
                            and acount / len(avalues) >= .8)
            # Detect repeated lamp/lock commands even when the majority stays
            # zero. Two active 1 Hz lock messages or three 10 Hz lamp messages
            # in a five-second tail window are material observations.
            pulse_shift = (aactive >= (2 if can_id == 0x184 else 3)
                           and aactive >= bactive + 2
                           and aactive >= 2 * max(1, bactive))
            if stable_shift or pulse_shift:
                changed = True
                observation_only = (can_id, name) in OBSERVATION_ONLY_SIGNAL_KEYS
                details.setdefault("signal_changes", []).append({
                    "signal": name,
                    "normal_active": bactive,
                    "recovery_active": aactive,
                    "kind": "stable_state" if stable_shift else "repeated_activity",
                    "review_required": not observation_only,
                })
                message = (f"{bus.upper()} {id_label} {name} late recovery changed "
                           f"({bactive}/{len(before)} vs {aactive}/{len(after)} active)")
                if observation_only:
                    advisory_observations.append(message)
                else:
                    reasons.append(message)
    checks[bus] = watched
    return changed


def check_minimal_recovery(trial_dir: Path) -> dict[str, Any]:
    """Check capture integrity and a short recovery snapshot.

    `capture_integrity_status` controls progression. The overall `status` marks
    review-worthy state changes; diagnostic fault indications that do not
    describe lamp actuation remain in `advisory_observations` and `checks`.
    This is not a pair verdict.
    """
    trial_dir = Path(trial_dir)
    reasons: list[str] = []
    integrity_reasons: list[str] = []
    advisory_observations: list[str] = []
    checks: dict[str, Any] = {"capture_quality": {}, "watched_ids": {}}
    try:
        metadata = json.loads((trial_dir / "metadata.json").read_text(encoding="utf-8"))
        mutation = MutationCase.from_dict(json.loads(
            (trial_dir / "mutation.json").read_text(encoding="utf-8")
        ))
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata is not an object")
        phases = _phase_times(metadata.get("phase_times_ns"))
        trial_id = int(trial_dir.name.removeprefix("trial_"))
        target = metadata.get("target_id")
        target_id = int(target, 0) if isinstance(target, str) else int(target)
        if (metadata.get("status") not in {"captured", "completed"}
                or metadata.get("trial_id") != trial_id
                or target_id != mutation.can_id
                or str(metadata.get("source_bus", "")).lower() != mutation.source_bus.lower()
                or str(metadata.get("trial_kind", mutation.trial_kind)).replace("no_op", "noop")
                != mutation.trial_kind):
            raise ValueError("captured trial metadata differs from prepared mutation")
        for phase in ("baseline", "normal", "recovery"):
            if phases[f"{phase}_end"] - phases[f"{phase}_start"] < WINDOW_NS:
                raise ValueError(f"{phase} phase is shorter than the five-second state window")
        tx, tx_errors = _tx_schedule(trial_dir, phases, mutation, metadata.get("experiment_id"))
        checks["tx"] = tx
        reasons.extend(tx_errors)
        integrity_reasons.extend(tx_errors)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        message = f"missing or invalid captured trial evidence ({exc})"
        return _result([message], False, checks, [message])

    logs = metadata.get("logs")
    if not isinstance(logs, Mapping):
        message = "receiver capture manifest is missing"
        return _result([message], False, checks, [message])
    buses = {str(bus).lower(): name for bus, name in logs.items()
             if str(bus).lower() != "tx"}
    distributed = "distributed" in str(metadata.get("execution_mode", "")).lower()
    required = {"p_can", "i_can"} if distributed else {"p_can", "b_can", "i_can"}
    required.add(mutation.source_bus.lower())
    missing = sorted(required - set(buses))
    missing_reasons = [f"{bus}: required receiver capture is not listed" for bus in missing]
    reasons.extend(missing_reasons)
    integrity_reasons.extend(missing_reasons)
    clocks = _normalized_clocks(metadata)
    selected_by_bus: dict[str, dict[tuple[str, int], list[bytes]]] = {}
    for bus, name in sorted(buses.items()):
        if not isinstance(name, str) or Path(name).name != name:
            message = f"{bus}: unsafe receiver filename"
            reasons.append(message)
            integrity_reasons.append(message)
            continue
        path = trial_dir / name
        try:
            capture = validate_capture_log(path, int(metadata["experiment_id"]))
            correction, uncertainty, status = _clock_alignment(
                bus, mutation.source_bus, clocks,
            )
            checks["capture_quality"][bus] = {
                "status": status,
                "uncertainty_ms": uncertainty / 1e6 if uncertainty is not None else None,
                "frame_count": capture["frame_count"],
            }
            if status not in {"source_clock", "aligned"}:
                message = f"{bus}: receiver clock is not aligned ({status})"
                reasons.append(message)
                integrity_reasons.append(message)
                continue
            if uncertainty is None or uncertainty > MAX_CLOCK_UNCERTAINTY_NS:
                message = f"{bus}: receiver clock uncertainty exceeds 100 ms"
                reasons.append(message)
                integrity_reasons.append(message)
                continue
            start, end = capture["session_start_ns"], capture["session_end_ns"]
            if start is None or end is None:
                message = f"{bus}: receiver session has no wall-clock bounds"
                reasons.append(message)
                integrity_reasons.append(message)
                continue
            if start + correction + uncertainty > phases["baseline_start"]:
                message = f"{bus}: receiver capture missed baseline start"
                reasons.append(message)
                integrity_reasons.append(message)
                continue
            if end + correction - uncertainty < phases["recovery_end"]:
                message = f"{bus}: receiver capture missed recovery end"
                reasons.append(message)
                integrity_reasons.append(message)
                continue
            selected_by_bus[bus] = _read_selected_frames(
                path, bus, correction, phases, mutation.can_id,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            message = f"{bus}: invalid receiver capture ({exc})"
            reasons.append(message)
            integrity_reasons.append(message)

    observed_change = False
    source = mutation.source_bus.lower()
    selected_source = selected_by_bus.get(source)
    if selected_source is None:
        message = f"{source}: source target payload could not be checked"
        reasons.append(message)
        integrity_reasons.append(message)
    else:
        source_checks: dict[str, Any] = {}
        for phase in ("baseline", "normal", "recovery"):
            frames = selected_source.get((phase, mutation.can_id), [])
            originals = sum(payload == mutation.original_payload for payload in frames)
            source_checks[phase] = {"target_frames": len(frames), "original_frames": originals}
            if len(frames) < SOURCE_MIN_FRAMES:
                message = f"{source}: fewer than three source target frames in late {phase}"
                reasons.append(message)
                integrity_reasons.append(message)
            elif originals != len(frames):
                message = f"{source}: source target payload drifted in late {phase}"
                reasons.append(message)
                integrity_reasons.append(message)
                observed_change |= phase == "recovery"
        checks["source_payload"] = source_checks

    observed_light = observed_lock = False
    for bus, selected in sorted(selected_by_bus.items()):
        observed_change |= _compare_bus(
            bus, selected, reasons, advisory_observations,
            checks["watched_ids"], mutation.can_id,
        )
        observed_light |= bool(
            len(selected.get(("normal", 0x3D6), [])) >= STATE_MIN_FRAMES
            and len(selected.get(("recovery", 0x3D6), [])) >= STATE_MIN_FRAMES
        )
        observed_lock |= any(
            len(selected.get(("normal", can_id), [])) >= STATE_MIN_FRAMES
            and len(selected.get(("recovery", can_id), [])) >= STATE_MIN_FRAMES
            for can_id in (0x184, 0x583, 0x3CE, 0x3CF, 0x3D0, 0x3D1)
        )
    if not observed_light:
        reasons.append("rear-light state 0x3D6 is not observed in both late windows")
    if not observed_lock:
        reasons.append("door/lock state is not observed in both late windows")
    return _result(reasons, observed_change, checks, integrity_reasons,
                   advisory_observations)
