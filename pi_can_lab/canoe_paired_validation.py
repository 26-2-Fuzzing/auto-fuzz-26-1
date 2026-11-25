"""Validate one finalized CANoe paired-trial episode before another exposure.

The CAPL schedule owns phase timing. This module checks that its markers and
the CAN frames were recorded in both CANoe logging formats. It does not infer
an ECU response or physical transmission from CAPL's output() acknowledgement.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import zip_longest
from pathlib import Path
import math
import re
from typing import Any, Sequence

import can

from canoe_baseline import select_baseline
from minimal_recovery_gate import STATE_MIN_FRAMES, WATCH_IDS, _compare_bus


class EpisodeValidationError(ValueError):
    """The episode does not meet the paired capture contract."""


_PHASES = ("baseline", "normal", "slot", "recovery")
_DURATIONS = (30.0, 10.0, 1.0, 20.0)
_PROBE_DURATION_S = 6.0
_TIME_TOLERANCE_S = 0.01
_DURATION_TOLERANCE_S = 0.1
_LATE_WINDOW_S = 5.0
_SOURCE_MIN_FRAMES = 3
_INTERVAL_S = 0.05
_MIN_TX_GAP_S = 0.5 * _INTERVAL_S
_MAX_TX_GAP_S = 1.5 * _INTERVAL_S
_SLOT_TIMING_TOLERANCE_S = 0.025
_LOCK_IDS = (0x184, 0x583, 0x3CE, 0x3CF, 0x3D0, 0x3D1)
_HEALTH = re.compile(rb"::FuzzIO::(BusOff|ErrorFrame)([123])\s*=\s*L?(-?\d+)\b")
_MARKER = re.compile(
    rb"^\s*(\d+(?:\.\d+)?)\s+SV:\s+\d+\s+\d+\s+\d+\s+"
    rb"::FuzzSchedule::(StartNs|EndNs)\s*=\s*L?(\d+)\s*$"
)


def _frame_signature(message: can.Message) -> tuple[Any, ...]:
    return (
        message.channel,
        message.arbitration_id,
        message.is_rx,
        message.is_extended_id,
        message.is_remote_frame,
        message.is_error_frame,
        message.is_fd,
        message.dlc,
        bytes(message.data),
    )


def _validated_phases(phases: list[dict], slot_count: int) -> list[dict]:
    if not isinstance(phases, list) or len(phases) != 4:
        raise EpisodeValidationError("exactly four schedule phases are required")
    validated = []
    previous_end = -1
    submissions = (0, 200, slot_count, 1)
    for phase, label, seconds, submitted in zip(phases, _PHASES, _DURATIONS, submissions):
        if not isinstance(phase, dict) or phase.get("label") != label:
            raise EpisodeValidationError(f"expected {label} phase in order")
        try:
            start_ns = int(phase["start_ns"])
            end_ns = int(phase["end_ns"])
            actual_submitted = int(phase["submitted_frames"])
            result = int(phase["result"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EpisodeValidationError(f"{label} has incomplete CAPL phase evidence") from exc
        if type(phase["start_ns"]) is not int or type(phase["end_ns"]) is not int:
            raise EpisodeValidationError(f"{label} phase markers must be integer nanoseconds")
        if start_ns < previous_end or end_ns <= start_ns:
            raise EpisodeValidationError(f"{label} phase markers overlap or are out of order")
        if abs((end_ns - start_ns) / 1e9 - seconds) > _DURATION_TOLERANCE_S:
            raise EpisodeValidationError(f"{label} phase duration differs from {seconds:g}s")
        if actual_submitted != submitted or result != 1:
            raise EpisodeValidationError(f"{label} CAPL result or submission count is incomplete")
        if "expected_submitted" in phase and phase["expected_submitted"] != submitted:
            raise EpisodeValidationError(f"{label} planned submission count differs")
        validated.append({
            "label": label,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "start_s": start_ns / 1e9,
            "end_s": end_ns / 1e9,
            "duration_s": (end_ns - start_ns) / 1e9,
            "submitted_frames": actual_submitted,
        })
        previous_end = end_ns
    return validated


def _validated_probe_phase(phase: dict) -> dict:
    if not isinstance(phase, dict) or phase.get("label") != "probe":
        raise EpisodeValidationError("probe CAPL completion record is required")
    try:
        start_ns = phase["start_ns"]
        end_ns = phase["end_ns"]
        submitted = phase["submitted_frames"]
        result = phase["result"]
    except KeyError as exc:
        raise EpisodeValidationError("probe has incomplete CAPL phase evidence") from exc
    if (type(start_ns) is not int or type(end_ns) is not int
            or type(submitted) is not int or type(result) is not int
            or start_ns <= 0 or end_ns <= start_ns
            or submitted != 0 or result != 1):
        raise EpisodeValidationError("probe CAPL completion fields are invalid")
    start_s, end_s = start_ns / 1e9, end_ns / 1e9
    if abs(end_s - start_s - _PROBE_DURATION_S) > _DURATION_TOLERANCE_S:
        raise EpisodeValidationError("probe CAPL duration differs from 6s")
    return {
        "start_s": start_s, "end_s": end_s, "end_ns": end_ns,
        "duration_s": end_s - start_s,
    }


def _check_asc_health(path: Path) -> None:
    expected = {(name, channel) for name in (b"BusOff", b"ErrorFrame") for channel in b"123"}
    observed: set[tuple[bytes, int]] = set()
    with path.open("rb") as source:
        for line in source:
            match = _HEALTH.search(line)
            if match is None:
                continue
            name, channel, value = match.groups()
            observed.add((name, channel[0]))
            if int(value) != 0:
                raise EpisodeValidationError(
                    f"CAN{chr(channel[0])} {name.decode()} became nonzero in ASC"
                )
    if observed != expected:
        raise EpisodeValidationError("ASC lacks complete CAN1/2/3 bus-off and error counters")


def _recorded_phase_windows(path: Path) -> list[tuple[float, float]]:
    """Read positive CAPL markers; zero resets are normal between requests.

    FuzzHarness resets EndNs to zero at each request, sometimes after its
    positive StartNs update appears in the ASC. The zero is not a phase end.
    """
    path = Path(path)
    windows = []
    pending = None
    last_positive = -1
    absolute = origin = False
    with path.open("rb") as source:
        for line in source:
            stripped = line.strip()
            if re.fullmatch(rb"base\s+(?:dec|hex)\s+timestamps\s+absolute", stripped.lower()):
                absolute = True
            if stripped.endswith(b"Start of measurement") and stripped.startswith(b"0.000000"):
                origin = True
            if b"::FuzzSchedule::StartNs" not in line and b"::FuzzSchedule::EndNs" not in line:
                continue
            match = _MARKER.fullmatch(line)
            if match is None:
                raise EpisodeValidationError("ASC has an unrecognized FuzzSchedule time marker")
            event_s = float(match[1])
            kind = match[2]
            value_ns = int(match[3])
            if value_ns == 0:
                continue
            if abs(event_s - value_ns / 1e9) > .001 or value_ns < last_positive:
                raise EpisodeValidationError("ASC FuzzSchedule marker time is inconsistent")
            last_positive = value_ns
            if kind == b"StartNs":
                if pending is not None:
                    raise EpisodeValidationError("ASC has overlapping FuzzSchedule phases")
                pending = value_ns
            else:
                if pending is None or value_ns <= pending:
                    raise EpisodeValidationError("ASC has an unmatched FuzzSchedule phase end")
                windows.append((pending / 1e9, value_ns / 1e9))
                pending = None
    if not absolute or not origin:
        raise EpisodeValidationError("ASC does not identify absolute measurement-relative time")
    if pending is not None:
        raise EpisodeValidationError("ASC has an unfinished FuzzSchedule phase")
    return windows


def _phase_for_time(seconds: float, phases: list[dict]) -> str:
    inside = [
        phase["label"] for phase in phases
        if phase["start_s"] <= seconds < phase["end_s"]
    ]
    if len(inside) == 1:
        return inside[0]
    if len(inside) > 1:
        raise EpisodeValidationError("target Tx lies inside overlapping CAPL phases")
    matches = [
        phase["label"] for phase in phases
        if phase["start_s"] - _TIME_TOLERANCE_S <= seconds
        <= phase["end_s"] + _TIME_TOLERANCE_S
    ]
    if len(matches) != 1:
        raise EpisodeValidationError("target Tx lies outside one unambiguous CAPL phase")
    return matches[0]


def _tx_timing(times: list[float], phase: dict, *, periodic: bool) -> dict:
    """Check normal-phase 50 ms spacing or the one-frame recovery."""
    if not times:
        raise EpisodeValidationError(f"{phase['label']} has no target Tx timestamps")
    first_delay = times[0] - phase["start_s"]
    if not 0 <= first_delay <= _MAX_TX_GAP_S:
        raise EpisodeValidationError(f"{phase['label']} target Tx starts late")
    gaps = [after - before for before, after in zip(times, times[1:])]
    if periodic:
        last_slack = phase["end_s"] - times[-1]
        if not 0 <= last_slack <= _MAX_TX_GAP_S:
            raise EpisodeValidationError(f"{phase['label']} target Tx ends early or late")
        if any(not _MIN_TX_GAP_S <= gap <= _MAX_TX_GAP_S for gap in gaps):
            raise EpisodeValidationError(f"{phase['label']} target Tx has a non-50ms interval")
    else:
        last_slack = phase["end_s"] - times[-1]
    return {
        "first_delay_ms": round(first_delay * 1000, 3),
        "last_slack_ms": round(last_slack * 1000, 3),
        "min_gap_ms": round(min(gaps) * 1000, 3) if gaps else None,
        "max_gap_ms": round(max(gaps) * 1000, 3) if gaps else None,
    }


def _validated_slot_frames(
    frames: Sequence[tuple[float, bytes]], baseline_payload: bytes, role: str,
) -> tuple[tuple[float, bytes], ...]:
    """Freeze the exact 1-second slot schedule accepted by this paired run."""
    if not isinstance(frames, Sequence) or isinstance(frames, (bytes, bytearray, str)):
        raise EpisodeValidationError("slot_frames must be a sequence of offsets and payloads")
    if len(frames) not in (10, 20):
        raise EpisodeValidationError("slot requires 10 frames at 100ms or 20 frames at 50ms")
    interval = 1.0 / len(frames)
    validated = []
    for index, item in enumerate(frames):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise EpisodeValidationError("slot frame must have an offset and payload")
        offset, payload = item
        if (type(offset) not in (int, float) or not math.isfinite(offset)
                or abs(offset - index * interval) > 1e-6):
            raise EpisodeValidationError("slot offsets must cover 1s at 50ms or 100ms")
        if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 8:
            raise EpisodeValidationError("slot payload must be eight bytes")
        validated.append((float(offset), bytes(payload)))
    if role == "noop" and any(payload != baseline_payload for _, payload in validated):
        raise EpisodeValidationError("no-op slot must use the original payload")
    if role == "mutation" and all(payload == baseline_payload for _, payload in validated):
        raise EpisodeValidationError("mutation slot must differ from the original payload")
    return tuple(validated)


def _slot_tx_timing(
    times: list[float], phase: dict, slot_frames: Sequence[tuple[float, bytes]],
) -> dict:
    """Compare recorded slot Tx times with every scheduled offset."""
    if len(times) != len(slot_frames):
        raise EpisodeValidationError("slot target Tx timestamp count differs from schedule")
    offsets = [offset for offset, _ in slot_frames]
    first_delay = times[0] - phase["start_s"]
    if not 0 <= first_delay <= _MAX_TX_GAP_S:
        raise EpisodeValidationError("slot target Tx starts late")
    last_slack = phase["end_s"] - times[-1]
    expected_slack = phase["duration_s"] - offsets[-1] - first_delay
    if last_slack < 0 or abs(last_slack - expected_slack) > _SLOT_TIMING_TOLERANCE_S:
        raise EpisodeValidationError("slot target Tx ends early or late")
    gaps = [after - before for before, after in zip(times, times[1:])]
    for gap, before, after in zip(gaps, offsets, offsets[1:]):
        if abs(gap - (after - before)) > _SLOT_TIMING_TOLERANCE_S:
            interval_ms = round((after - before) * 1000)
            raise EpisodeValidationError(
                f"slot target Tx has a non-{interval_ms}ms interval"
            )
    if any(
        abs((observed - times[0]) - offset) > _SLOT_TIMING_TOLERANCE_S
        for observed, offset in zip(times, offsets)
    ):
        raise EpisodeValidationError("slot target Tx drifts from scheduled offsets")
    return {
        "first_delay_ms": round(first_delay * 1000, 3),
        "last_slack_ms": round(last_slack * 1000, 3),
        "min_gap_ms": round(min(gaps) * 1000, 3),
        "max_gap_ms": round(max(gaps) * 1000, 3),
    }


def validate_episode(
    blf: Path,
    asc: Path,
    phases: list[dict],
    *,
    channel: int,
    frame_id: int,
    baseline_payload: bytes,
    slot_frames: Sequence[tuple[float, bytes]],
    role: str,
) -> dict:
    """Check one 30/10/1/20 second measurement using finalized CANoe logs.

    `phases` are the four FuzzSchedule completion records from the same
    measurement. Channel numbers are one-based CANoe labels; python-can's
    recorded channels are zero-based.
    """
    if role not in ("mutation", "noop"):
        raise EpisodeValidationError("role must be mutation or noop")
    if type(channel) is not int or channel not in (1, 2, 3):
        raise EpisodeValidationError("channel must be CAN1, CAN2 or CAN3")
    if type(frame_id) is not int or not 0 <= frame_id <= 0x7FF:
        raise EpisodeValidationError("frame ID must be Standard Classic CAN")
    baseline_payload = bytes(baseline_payload)
    if len(baseline_payload) != 8:
        raise EpisodeValidationError("paired episode requires an eight-byte baseline")
    slot_frames = _validated_slot_frames(slot_frames, baseline_payload, role)
    expected_submissions = (0, 200, len(slot_frames), 1)
    windows = _validated_phases(phases, len(slot_frames))

    blf = Path(blf).resolve(strict=True)
    asc = Path(asc).resolve(strict=True)
    if blf.suffix.lower() != ".blf" or asc.suffix.lower() != ".asc":
        raise EpisodeValidationError("finalized BLF and ASC paths are required")
    if blf.stem != asc.stem:
        raise EpisodeValidationError("BLF and ASC stems do not match")
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in (blf, asc)}
    with asc.open("rb") as source:
        source.seek(max(0, before[asc][0] - 256))
        if not source.read().rstrip().endswith(b"End TriggerBlock"):
            raise EpisodeValidationError("ASC has no final End TriggerBlock")
    _check_asc_health(asc)
    recorded_intervals = _recorded_phase_windows(asc)
    if len(recorded_intervals) != 4:
        raise EpisodeValidationError("ASC does not contain exactly four CAPL phase windows")
    for recorded, phase in zip(recorded_intervals, windows):
        if (abs(recorded[0] - phase["start_s"]) > .001
                or abs(recorded[1] - phase["end_s"]) > .001):
            raise EpisodeValidationError("ASC phase marker differs from COM completion record")

    rx_counts: Counter[int] = Counter()
    target_tx: Counter[str] = Counter()
    target_tx_times: dict[str, list[float]] = {label: [] for label in _PHASES}
    baseline_rx: Counter[bytes] = Counter()
    late_rx_counts: dict[str, Counter[int]] = {
        label: Counter() for label in ("baseline", "normal", "recovery")
    }
    selected_by_bus: dict[str, dict[tuple[str, int], list[bytes]]] = {
        f"can{number}": defaultdict(list) for number in (1, 2, 3)
    }
    total = 0
    with can.BLFReader(blf) as blf_reader, can.ASCReader(asc, relative_timestamp=True) as asc_reader:
        if blf_reader.file_size != before[blf][0]:
            raise EpisodeValidationError("BLF declared file size differs from its actual size")
        origin = blf_reader.start_timestamp
        for index, (blf_message, asc_message) in enumerate(
            zip_longest(blf_reader, asc_reader), start=1
        ):
            if blf_message is None or asc_message is None:
                raise EpisodeValidationError("BLF and ASC CAN frame counts differ")
            if (_frame_signature(blf_message) != _frame_signature(asc_message)
                    or abs(blf_message.timestamp - origin - asc_message.timestamp)
                    > _TIME_TOLERANCE_S):
                raise EpisodeValidationError(f"BLF and ASC CAN frame {index} differ")
            total += 1
            message = blf_message
            seconds = message.timestamp - origin
            if message.is_error_frame:
                raise EpisodeValidationError("CANoe recorded an error frame")
            if message.is_rx and message.channel in (0, 1, 2):
                rx_counts[message.channel + 1] += 1
                for phase in windows:
                    label = phase["label"]
                    if label == "slot":
                        continue
                    if phase["end_s"] - _LATE_WINDOW_S <= seconds < phase["end_s"]:
                        late_rx_counts[label][message.channel + 1] += 1
                        if (not message.is_extended_id and not message.is_remote_frame
                                and not message.is_fd
                                and (message.arbitration_id in WATCH_IDS
                                     or message.arbitration_id == frame_id)):
                            selected_by_bus[f"can{message.channel + 1}"][
                                (label, message.arbitration_id)
                            ].append(bytes(message.data))
                        break
            if (not message.is_rx
                    and windows[0]["start_s"] <= seconds < windows[0]["end_s"]):
                raise EpisodeValidationError("baseline phase contains CANoe Tx")
            if (message.channel == channel - 1
                    and message.arbitration_id == frame_id
                    and not message.is_rx
                    and (message.is_extended_id or message.is_remote_frame
                         or message.is_fd or message.dlc != 8 or len(message.data) != 8)):
                raise EpisodeValidationError("malformed target Tx in CANoe recording")
            target = (
                message.channel == channel - 1
                and message.arbitration_id == frame_id
                and not message.is_extended_id
                and not message.is_remote_frame
                and not message.is_fd
                and message.dlc == 8
                and len(message.data) == 8
            )
            if not target:
                continue
            payload = bytes(message.data)
            if message.is_rx:
                if windows[0]["start_s"] <= seconds < windows[0]["end_s"]:
                    baseline_rx[payload] += 1
            else:
                phase_label = _phase_for_time(seconds, windows)
                if phase_label == "slot":
                    slot_index = target_tx["slot"]
                    if slot_index >= len(slot_frames):
                        raise EpisodeValidationError("slot has more target Tx than scheduled")
                    expected_payload = slot_frames[slot_index][1]
                else:
                    expected_payload = baseline_payload
                if phase_label == "baseline" or payload != expected_payload:
                    raise EpisodeValidationError(
                        f"unexpected CAN{channel} target Tx in {phase_label}"
                        + (f" at index {target_tx['slot']}" if phase_label == "slot" else "")
                    )
                target_tx[phase_label] += 1
                target_tx_times[phase_label].append(seconds)

    if any(rx_counts[channel_number] == 0 for channel_number in (1, 2, 3)):
        raise EpisodeValidationError("one or more CAN1/2/3 Rx channels have no frames")
    if sum(baseline_rx.values()) < 3:
        raise EpisodeValidationError("baseline phase has fewer than three target Rx frames")
    payload, majority = baseline_rx.most_common(1)[0]
    if majority * 2 <= sum(baseline_rx.values()) or payload != baseline_payload:
        raise EpisodeValidationError("baseline phase target Rx has no matching strict majority")
    for label, expected in zip(_PHASES, expected_submissions):
        if target_tx[label] != expected:
            raise EpisodeValidationError(
                f"{label} has {target_tx[label]} target Tx frames; expected {expected}"
            )
    timing = {}
    for phase in windows[1:]:
        label = phase["label"]
        timing[label] = (
            _slot_tx_timing(target_tx_times[label], phase, slot_frames)
            if label == "slot" else
            _tx_timing(target_tx_times[label], phase, periodic=label == "normal")
        )

    for phase_label, counts in late_rx_counts.items():
        for number in (1, 2, 3):
            if counts[number] == 0:
                raise EpisodeValidationError(
                    f"{phase_label} late window lacks CAN{number} Rx coverage"
                )
    source = selected_by_bus[f"can{channel}"]
    source_late = {}
    for phase_label in ("baseline", "normal", "recovery"):
        payloads = source[(phase_label, frame_id)]
        source_late[phase_label] = len(payloads)
        if len(payloads) < _SOURCE_MIN_FRAMES:
            raise EpisodeValidationError(
                f"{phase_label} late window has fewer than three source target Rx frames"
            )
        if any(payload != baseline_payload for payload in payloads):
            raise EpisodeValidationError(
                f"{phase_label} late window source target Rx differs from baseline"
            )

    state_reasons: list[str] = []
    advisory: list[str] = []
    watched_checks: dict[str, Any] = {}
    for bus, selected in selected_by_bus.items():
        _compare_bus(bus, selected, state_reasons, advisory, watched_checks, frame_id)
    if not any(
        len(selected[("normal", 0x3D6)]) >= STATE_MIN_FRAMES
        and len(selected[("recovery", 0x3D6)]) >= STATE_MIN_FRAMES
        for selected in selected_by_bus.values()
    ):
        state_reasons.append("late light-state CAN 0x3D6 coverage is insufficient")
    if not any(
        len(selected[("normal", can_id)]) >= STATE_MIN_FRAMES
        and len(selected[("recovery", can_id)]) >= STATE_MIN_FRAMES
        for selected in selected_by_bus.values()
        for can_id in _LOCK_IDS
    ):
        state_reasons.append("late lock/door-state coverage is insufficient")
    if state_reasons:
        raise EpisodeValidationError("late state/rate recovery gate: " + "; ".join(sorted(set(state_reasons))))
    for path, original in before.items():
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != original:
            raise EpisodeValidationError(f"CANoe log changed while validating: {path}")

    return {
        "status": "validated",
        "role": role,
        "blf": str(blf),
        "asc": str(asc),
        "records": total,
        "rx_counts": {f"CAN{number}": rx_counts[number] for number in (1, 2, 3)},
        "target_tx_by_phase": {label: target_tx[label] for label in _PHASES},
        "baseline_target_rx": {
            "total": sum(baseline_rx.values()),
            "matching_original": baseline_rx[baseline_payload],
        },
        "source_late_target_rx": source_late,
        "late_window_rx_counts": {
            label: {f"CAN{number}": counts[number] for number in (1, 2, 3)}
            for label, counts in late_rx_counts.items()
        },
        "watched_state_checks": watched_checks,
        "advisory_observations": sorted(set(advisory)),
        "target_tx_timing_ms": timing,
        "slot_schedule": [
            {"offset_ms": round(offset * 1000), "payload_hex": payload.hex().upper()}
            for offset, payload in slot_frames
        ],
        "phase_durations_s": {phase["label"]: phase["duration_s"] for phase in windows},
        "phase_marker_source": "ASC FuzzSchedule StartNs/EndNs",
        "physical_transmit_confirmed": False,
    }


def validate_probe(
    blf: Path,
    asc: Path,
    *,
    channel: int,
    frame_id: int,
    expected_payload: bytes,
    phase: dict,
) -> dict:
    """Verify a finalized passive pre-probe before starting an episode."""
    if type(channel) is not int or channel not in (1, 2, 3):
        raise EpisodeValidationError("channel must be CAN1, CAN2 or CAN3")
    if type(frame_id) is not int or not 0 <= frame_id <= 0x7FF:
        raise EpisodeValidationError("frame ID must be Standard Classic CAN")
    expected_payload = bytes(expected_payload)
    if len(expected_payload) != 8:
        raise EpisodeValidationError("probe requires an eight-byte baseline")
    window = _validated_probe_phase(phase)
    blf = Path(blf).resolve(strict=True)
    asc = Path(asc).resolve(strict=True)
    if blf.suffix.lower() != ".blf" or asc.suffix.lower() != ".asc" or blf.stem != asc.stem:
        raise EpisodeValidationError("probe needs one matching BLF/ASC pair")
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in (blf, asc)}
    with asc.open("rb") as source:
        source.seek(max(0, before[asc][0] - 256))
        if not source.read().rstrip().endswith(b"End TriggerBlock"):
            raise EpisodeValidationError("probe ASC has no final End TriggerBlock")
    _check_asc_health(asc)
    intervals = _recorded_phase_windows(asc)
    if len(intervals) != 1:
        raise EpisodeValidationError("probe ASC must contain exactly one CAPL phase window")
    if (abs(intervals[0][0] - window["start_s"]) > .001
            or abs(intervals[0][1] - window["end_s"]) > .001):
        raise EpisodeValidationError("probe ASC marker differs from COM completion record")

    rx_counts: Counter[int] = Counter()
    total = 0
    with can.BLFReader(blf) as blf_reader, can.ASCReader(asc, relative_timestamp=True) as asc_reader:
        if blf_reader.file_size != before[blf][0]:
            raise EpisodeValidationError("probe BLF declared size differs from its actual size")
        origin = blf_reader.start_timestamp
        for index, (blf_message, asc_message) in enumerate(
            zip_longest(blf_reader, asc_reader), start=1
        ):
            if blf_message is None or asc_message is None:
                raise EpisodeValidationError("probe BLF/ASC frame counts differ")
            if (_frame_signature(blf_message) != _frame_signature(asc_message)
                    or abs(blf_message.timestamp - origin - asc_message.timestamp)
                    > _TIME_TOLERANCE_S):
                raise EpisodeValidationError(f"probe BLF/ASC frame {index} differs")
            if blf_message.is_error_frame:
                raise EpisodeValidationError("probe recorded an error frame")
            if blf_message.is_rx and blf_message.channel in (0, 1, 2):
                rx_counts[blf_message.channel + 1] += 1
            if not blf_message.is_rx:
                raise EpisodeValidationError("passive probe unexpectedly contains CANoe Tx")
            total += 1
    if any(rx_counts[number] == 0 for number in (1, 2, 3)):
        raise EpisodeValidationError("probe lacks CAN1/2/3 Rx frames")
    selection = select_baseline(blf, channel, frame_id)
    if selection.payload != expected_payload:
        raise EpisodeValidationError("probe target Rx majority differs from campaign baseline")
    for path, original in before.items():
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != original:
            raise EpisodeValidationError(f"probe log changed during validation: {path}")
    return {
        "status": "validated",
        "blf": str(blf),
        "asc": str(asc),
        "records": total,
        "rx_counts": {f"CAN{number}": rx_counts[number] for number in (1, 2, 3)},
        "baseline": selection.to_record(),
        "phase_duration_s": window["duration_s"],
        "phase_marker_source": "ASC FuzzSchedule StartNs/EndNs",
        "physical_transmit_confirmed": False,
    }


def _reference_normal_window(reference_phases: list[dict]) -> dict:
    if not isinstance(reference_phases, list):
        raise EpisodeValidationError("reference CAPL phase records are required")
    matches = [
        phase for phase in reference_phases
        if isinstance(phase, dict) and phase.get("label") == "normal"
    ]
    if len(matches) != 1:
        raise EpisodeValidationError("reference must have exactly one normal CAPL phase")
    phase = matches[0]
    try:
        start_ns = phase["start_ns"]
        end_ns = phase["end_ns"]
        submitted = phase["submitted_frames"]
        result = phase["result"]
    except KeyError as exc:
        raise EpisodeValidationError("reference normal CAPL record is incomplete") from exc
    if (type(start_ns) is not int or type(end_ns) is not int
            or type(submitted) is not int or type(result) is not int
            or start_ns <= 0 or end_ns <= start_ns
            or submitted != 200 or result != 1):
        raise EpisodeValidationError("reference normal CAPL completion is invalid")
    start_s, end_s = start_ns / 1e9, end_ns / 1e9
    if abs(end_s - start_s - 10.0) > _DURATION_TOLERANCE_S:
        raise EpisodeValidationError("reference normal CAPL duration differs from 10s")
    return {
        "start_s": start_s, "end_s": end_s, "end_ns": end_ns,
        "duration_s": end_s - start_s,
    }


def _late_snapshot(
    blf: Path, *, end_s: float, channel: int, frame_id: int,
    baseline_payload: bytes, label: str,
) -> dict:
    """Read one measurement's late five seconds in its own CAPL timebase."""
    blf = Path(blf).resolve(strict=True)
    if blf.suffix.lower() != ".blf":
        raise EpisodeValidationError(f"{label} requires a BLF log")
    before = (blf.stat().st_size, blf.stat().st_mtime_ns)
    rx_counts: Counter[int] = Counter()
    selected: dict[str, dict[int, list[bytes]]] = {
        f"can{number}": defaultdict(list) for number in (1, 2, 3)
    }
    with can.BLFReader(blf) as reader:
        if reader.file_size != before[0]:
            raise EpisodeValidationError(f"{label} BLF declared size differs from actual size")
        origin = reader.start_timestamp
        for message in reader:
            if message.is_error_frame:
                raise EpisodeValidationError(f"{label} BLF contains a CAN error frame")
            seconds = message.timestamp - origin
            if (not message.is_rx or message.channel not in (0, 1, 2)
                    or not (end_s - _LATE_WINDOW_S <= seconds < end_s)):
                continue
            bus = message.channel + 1
            rx_counts[bus] += 1
            if (message.is_extended_id or message.is_remote_frame or message.is_fd
                    or (message.arbitration_id not in WATCH_IDS
                        and message.arbitration_id != frame_id)):
                continue
            selected[f"can{bus}"][message.arbitration_id].append(bytes(message.data))
    if (blf.stat().st_size, blf.stat().st_mtime_ns) != before:
        raise EpisodeValidationError(f"{label} BLF changed during validation")
    for number in (1, 2, 3):
        if rx_counts[number] == 0:
            raise EpisodeValidationError(f"{label} late window lacks CAN{number} Rx coverage")
    source = selected[f"can{channel}"][frame_id]
    if len(source) < _SOURCE_MIN_FRAMES:
        raise EpisodeValidationError(
            f"{label} late window has fewer than three source target Rx frames"
        )
    if any(payload != baseline_payload for payload in source):
        raise EpisodeValidationError(f"{label} late window source target Rx differs from baseline")
    return {
        "rx_counts": {f"CAN{number}": rx_counts[number] for number in (1, 2, 3)},
        "source_target_rx": len(source),
        "selected": selected,
    }


def validate_resume_probe(
    reference_blf: Path,
    reference_phases: list[dict],
    probe_blf: Path,
    probe_asc: Path,
    probe_phase: dict,
    *,
    channel: int,
    frame_id: int,
    baseline_payload: bytes,
) -> dict:
    """Gate resumption against the last stable normal window before exposure.

    The reference is an interrupted episode's completed normal phase. The
    candidate is a new passive probe; both windows are read from disk only.
    """
    baseline_payload = bytes(baseline_payload)
    probe_validation = validate_probe(
        probe_blf, probe_asc, channel=channel, frame_id=frame_id,
        expected_payload=baseline_payload, phase=probe_phase,
    )
    reference_window = _reference_normal_window(reference_phases)
    probe_window = _validated_probe_phase(probe_phase)
    reference = _late_snapshot(
        reference_blf, end_s=reference_window["end_s"], channel=channel,
        frame_id=frame_id, baseline_payload=baseline_payload, label="reference normal",
    )
    candidate = _late_snapshot(
        probe_blf, end_s=probe_window["end_s"], channel=channel,
        frame_id=frame_id, baseline_payload=baseline_payload, label="resume probe",
    )

    reasons: list[str] = []
    advisory: list[str] = []
    watched_checks: dict[str, Any] = {}
    changed = False
    for bus in ("can1", "can2", "can3"):
        selected: dict[tuple[str, int], list[bytes]] = defaultdict(list)
        for can_id, payloads in reference["selected"][bus].items():
            selected[("normal", can_id)] = payloads
        for can_id, payloads in candidate["selected"][bus].items():
            selected[("recovery", can_id)] = payloads
        changed |= _compare_bus(
            bus, selected, reasons, advisory, watched_checks, frame_id,
        )
    if not any(
        len(reference["selected"][bus][0x3D6]) >= STATE_MIN_FRAMES
        and len(candidate["selected"][bus][0x3D6]) >= STATE_MIN_FRAMES
        for bus in ("can1", "can2", "can3")
    ):
        reasons.append("resume light-state coverage is insufficient")
    if not any(
        len(reference["selected"][bus][can_id]) >= STATE_MIN_FRAMES
        and len(candidate["selected"][bus][can_id]) >= STATE_MIN_FRAMES
        for bus in ("can1", "can2", "can3")
        for can_id in _LOCK_IDS
    ):
        reasons.append("resume lock/door-state coverage is insufficient")
    if changed or reasons or advisory:
        observations = sorted(set(reasons + advisory))
        raise EpisodeValidationError(
            "resume probe differs from pre-exposure normal: "
            + ("; ".join(observations) if observations else "watched state changed")
        )

    return {
        "status": "validated",
        "comparison": "reference normal last5s vs passive probe last5s",
        "reference_normal": {
            "blf": str(Path(reference_blf).resolve()),
            "phase_end_ns": reference_window["end_ns"],
            "late_rx_counts": reference["rx_counts"],
            "source_target_rx": reference["source_target_rx"],
        },
        "probe": {
            **probe_validation,
            "late_rx_counts": candidate["rx_counts"],
            "late_source_target_rx": candidate["source_target_rx"],
        },
        "watched_state_checks": watched_checks,
        "physical_transmit_confirmed": False,
    }


def validate_passive_state_match(
    reference_blf: Path,
    reference_asc: Path,
    reference_phase: dict,
    candidate_blf: Path,
    candidate_asc: Path,
    candidate_phase: dict,
    *,
    channel: int,
    frame_id: int,
    baseline_payload: bytes,
) -> dict:
    """Compare two no-send probes after an operator-reported state change.

    This establishes stability of the newly observed state only. It does not
    claim that the interrupted episode returned to its pre-exposure state.
    """
    reference_blf = Path(reference_blf).resolve(strict=True)
    candidate_blf = Path(candidate_blf).resolve(strict=True)
    if reference_blf == candidate_blf:
        raise EpisodeValidationError("passive state comparison needs two distinct probes")
    baseline_payload = bytes(baseline_payload)
    reference_validation = validate_probe(
        reference_blf, reference_asc, channel=channel, frame_id=frame_id,
        expected_payload=baseline_payload, phase=reference_phase,
    )
    candidate_validation = validate_probe(
        candidate_blf, candidate_asc, channel=channel, frame_id=frame_id,
        expected_payload=baseline_payload, phase=candidate_phase,
    )
    reference_window = _validated_probe_phase(reference_phase)
    candidate_window = _validated_probe_phase(candidate_phase)
    reference = _late_snapshot(
        reference_blf, end_s=reference_window["end_s"], channel=channel,
        frame_id=frame_id, baseline_payload=baseline_payload, label="reference passive probe",
    )
    candidate = _late_snapshot(
        candidate_blf, end_s=candidate_window["end_s"], channel=channel,
        frame_id=frame_id, baseline_payload=baseline_payload, label="candidate passive probe",
    )

    reasons: list[str] = []
    advisory: list[str] = []
    watched_checks: dict[str, Any] = {}
    changed = False
    for bus in ("can1", "can2", "can3"):
        selected: dict[tuple[str, int], list[bytes]] = defaultdict(list)
        for can_id, payloads in reference["selected"][bus].items():
            selected[("normal", can_id)] = payloads
        for can_id, payloads in candidate["selected"][bus].items():
            selected[("recovery", can_id)] = payloads
        changed |= _compare_bus(
            bus, selected, reasons, advisory, watched_checks, frame_id,
        )
    if not any(
        len(reference["selected"][bus][0x3D6]) >= STATE_MIN_FRAMES
        and len(candidate["selected"][bus][0x3D6]) >= STATE_MIN_FRAMES
        for bus in ("can1", "can2", "can3")
    ):
        reasons.append("passive light-state coverage is insufficient")
    if not any(
        len(reference["selected"][bus][can_id]) >= STATE_MIN_FRAMES
        and len(candidate["selected"][bus][can_id]) >= STATE_MIN_FRAMES
        for bus in ("can1", "can2", "can3")
        for can_id in _LOCK_IDS
    ):
        reasons.append("passive lock/door-state coverage is insufficient")
    if changed or reasons or advisory:
        observations = sorted(set(reasons + advisory))
        raise EpisodeValidationError(
            "passive post-change state is not stable: "
            + ("; ".join(observations) if observations else "watched state changed")
        )

    return {
        "status": "validated",
        "comparison": "post-change passive probe last5s vs current passive probe last5s",
        "return_to_interrupted_prestate_confirmed": False,
        "reference": {
            "probe": reference_validation,
            "phase_end_ns": reference_window["end_ns"],
            "late_rx_counts": reference["rx_counts"],
            "late_source_target_rx": reference["source_target_rx"],
        },
        "candidate": {
            "probe": candidate_validation,
            "phase_end_ns": candidate_window["end_ns"],
            "late_rx_counts": candidate["rx_counts"],
            "late_source_target_rx": candidate["source_target_rx"],
        },
        "watched_state_checks": watched_checks,
        "physical_transmit_confirmed": False,
    }
