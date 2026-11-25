"""Extract recorded window evidence only; never open a bus or interpret data frames."""

import re
from pathlib import Path


# ASC stores elapsed event times with less precision than the ns system variables.
# Delayed/preloaded assignments outside this bound are not accepted as timings.
_EVENT_TOLERANCE_NS = 1_000_000
_VARIABLE = re.compile(rb"::FuzzSchedule::(StartNs|EndNs)\b")
_ASSIGNMENT = re.compile(
    rb"\s*(\d{1,12})(?:\.(\d{1,9}))?\s+SV:\s+\d+\s+\d+\s+\d+\s+"
    rb"::FuzzSchedule::(StartNs|EndNs)\s*=\s*L?(\d{1,19})\s*"
)
_MODE = re.compile(rb"base\s+(?:dec|hex)\s+timestamps\s+(absolute|relative)", re.I)
_ORIGIN = re.compile(rb"0+(?:\.0+)?\s+Start of measurement", re.I)


def inspect_phase_metadata(path):
    """Describe supported ASC schedule markers, not TX, delivery or ground truth.

    intervals_s uses the same measurement-relative seconds as the single-block
    ASC reader before applying a configured timestamp_origin. BLF metadata and
    unknown marker dialects must not be guessed from CAN traffic.
    """
    path = Path(path)
    counts = dict(start_positive=0, end_positive=0, start_zero=0, end_zero=0,
                  ready_messages=0, no_active_messages=0)
    def result(status, reason, intervals=()):
        return dict(status=status, reason=reason, intervals_s=list(intervals), marker_counts=counts)

    if path.suffix.lower() != ".asc":
        return result("unsupported", "no_metadata_reader_for_format")

    begins = ends = origins = modes = 0
    inside = absolute = False
    pending = last_end = last_event = None
    invalid = None
    intervals = []
    with path.open("rb") as lines:
        for line in lines:
            # Most ASC entries are CAN or unrelated system events. Avoid decoding
            # or retaining those rows in this separate, metadata-only pass.
            if line[:1].isalpha():
                stripped = line.strip()
                if stripped.startswith(b"Begin TriggerBlock"):
                    begins += 1
                    inside = True
                    if begins != 1 or modes != 1:
                        invalid = invalid or "unknown_or_multiple_trigger_blocks"
                elif stripped == b"End TriggerBlock":
                    ends += 1
                    if not inside:
                        invalid = invalid or "unmatched_end_trigger"
                    inside = False
                elif (mode := _MODE.fullmatch(stripped)):
                    modes += 1
                    absolute = mode[1].lower() == b"absolute"
                continue
            if b"Start of measurement" in line:
                if not _ORIGIN.fullmatch(line.strip()) or not inside:
                    invalid = invalid or "unknown_measurement_origin"
                origins += 1
            if b"::FuzzDashboard::PhaseText" in line and line.rstrip().endswith(b'= "READY / awaiting experiment"'):
                counts["ready_messages"] += 1
            if b"::FuzzDashboard::SourceText" in line and line.rstrip().endswith(b'= "No active injection"'):
                counts["no_active_messages"] += 1
            if b"::FuzzSchedule::" not in line or not _VARIABLE.search(line):
                continue
            match = _ASSIGNMENT.fullmatch(line)
            if not match or not inside or origins != 1:
                invalid = invalid or "invalid_marker_syntax_or_location"
                continue
            seconds, fraction, key, literal = match.groups()
            event_ns = int(seconds) * 10**9 + int((fraction or b"").ljust(9, b"0"))
            value = int(literal)
            start = key == b"StartNs"
            counts[("start" if start else "end") + ("_positive" if value else "_zero")] += 1
            if last_event is not None and event_ns < last_event:
                invalid = invalid or "marker_events_out_of_order"
            last_event = event_ns
            if value == 0:
                if pending is not None:
                    invalid = invalid or "reset_inside_marker_pair"
                continue
            if abs(value - event_ns) > _EVENT_TOLERANCE_NS:
                invalid = invalid or "marker_value_not_at_recorded_event_time"
            if start:
                if pending is not None or (last_end is not None and value < last_end):
                    invalid = invalid or "duplicate_or_overlapping_start"
                pending = value
            elif pending is None or value <= pending:
                invalid = invalid or "end_without_matching_start"
            else:
                if len(intervals) >= 4096:
                    invalid = invalid or "too_many_marker_pairs"
                else:
                    intervals.append([pending / 10**9, value / 10**9])
                pending, last_end = None, value

    if begins != 1 or ends != 1 or inside:
        return result("ambiguous", "requires_one_complete_trigger_block")
    if modes != 1 or not absolute:
        return result("unsupported", "requires_single_absolute_timestamp_header")
    if origins != 1:
        return result("ambiguous", "unknown_measurement_origin")
    if invalid or pending is not None:
        return result("ambiguous", invalid or "incomplete_start_end_pair")
    if not intervals:
        return result("no_recorded_interval", "no_positive_start_end_pair")
    return result("recorded_intervals", "schedule_times_match_logged_event_times", intervals)
