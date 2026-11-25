"""Read-only CANoe BLF/ASC triage. Reports observations, not causation."""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from canoe_assessment import assess_window, summarize, summarize_control
from canoe_metrics import Activity
from canoe_phase_evidence import inspect_phase_metadata
from canoe_reader import Frame, dbc_decoder, read_log


def finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}: finite number required")
    return float(value)


def parse_config(config):
    origin = finite_number(config["timestamp_origin"], "timestamp_origin")
    channels = {int(k): v for k, v in config["channel_map"].items()}
    if (not channels or len(channels) != len(config["channel_map"])
            or any(not isinstance(v, str) or not v.strip() for v in channels.values())
            or len(set(channels.values())) != len(channels)):
        raise ValueError("channel_map must uniquely map reader channels to bus names")
    windows = {}
    for name, bounds in config["windows"].items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"{name}: expected [start_s, end_s]")
        start, end = [finite_number(x, name) for x in bounds]
        if start >= end:
            raise ValueError(f"{name}: end must be after start")
        windows[name] = (start, end)
    if not {"reference", "observation"} <= windows.keys():
        raise ValueError("windows must include reference and observation")
    ordered = sorted((a, b, n) for n, (a, b) in windows.items())
    if any(end > next_start for (_, end, _), (next_start, _, _) in zip(ordered, ordered[1:])):
        raise ValueError("windows must not overlap")
    if windows["reference"][1] > windows["observation"][0]:
        raise ValueError("reference must be before observation")
    if "previous_reference" in windows and windows["previous_reference"][1] > windows["reference"][0]:
        raise ValueError("previous_reference must be before reference")
    if "recovery" in windows and windows["recovery"][0] < windows["observation"][1]:
        raise ValueError("recovery must be after observation")
    edge = finite_number(config.get("edge_tolerance_s", 0.5), "edge_tolerance_s")
    signal_gap = finite_number(config.get("max_signal_gap_s", 2), "max_signal_gap_s")
    if min(edge, signal_gap) <= 0:
        raise ValueError("edge_tolerance_s and max_signal_gap_s must be positive")
    loss = config.get("capture_loss_reported")
    if loss is not None and not isinstance(loss, bool):
        raise ValueError("capture_loss_reported must be true, false or omitted")
    return origin, channels, windows, edge, signal_gap, loss


def reference_stationarity(reports, quality, durations, windows):
    """Measure drift between an optional earlier reference and the immediate one."""
    if "previous_reference" not in reports:
        return {"status": "not_requested"}
    if any(quality[w]["status"] != "usable" for w in ("reference", "previous_reference")):
        return {"status": "unmeasured", "reason": "previous_window_quality"}
    if not math.isclose(durations["reference"], durations["previous_reference"],
                        rel_tol=0, abs_tol=0.01):
        return {"status": "unmeasured", "reason": "previous_window_not_equal_duration"}
    if windows["reference"][0] - windows["previous_reference"][1] > 0.01:
        return {"status": "unmeasured", "reason": "previous_window_not_adjacent"}
    old, ref = reports["previous_reference"], reports["reference"]
    n_old, n_ref = old["count"], ref["count"]
    if max(n_old, n_ref) < 3:
        return {"status": "unmeasured", "reason": "insufficient_frames"}
    change = abs(n_ref - n_old)
    relative = change / max(n_old, n_ref)
    old_median, ref_median = old["median_cycle_time_ms"], ref["median_cycle_time_ms"]
    count_drift = change >= 2 and relative >= 0.25
    cycle_drift = (old_median is not None and ref_median is not None and old_median > 0
                   and abs(ref_median - old_median) >= max(2, old_median * 0.25))
    return dict(status="transitioning" if count_drift or cycle_drift else "stable",
                previous_count=n_old, reference_count=n_ref,
                count_relative_change=relative,
                previous_median_cycle_ms=old_median, reference_median_cycle_ms=ref_median)


def summarize_recovery(group, quality, counts):
    """Compare recorded samples only; do not infer states between samples."""
    ref = group["windows"].get("reference")
    recovery = group["windows"].get("recovery")
    if ref is None or recovery is None:
        return {"status": "unobserved"}
    if (ref["same_as_first"] != ref["samples"]
            or quality["reference"]["status"] != "usable"
            or quality["recovery"]["status"] != "usable"
            or counts["reference"].get("decode_errors", 0)
            or counts["recovery"].get("decode_errors", 0)
            or recovery.get("reference_compared_samples") != recovery["samples"]):
        return {"status": "inconclusive"}
    matched = recovery["matching_reference_samples"]
    total = recovery["samples"]
    status = "all_match_reference" if matched == total else "none_match_reference" if not matched else "mixed"
    return dict(status=status, reference_value=ref["first_value"], matching_samples=matched,
                recovery_samples=total)


def analyze(frames, config, decoder=None, *, phase_metadata=None):
    origin, channels, windows, edge, max_signal_gap, loss = parse_config(config)
    timing = assess_window(config, windows["observation"], origin, phase_metadata)
    role = config.get("capture_role", "unspecified")
    if role not in ("unspecified", "experiment", "no_op", "reference"):
        raise ValueError("capture_role must be unspecified, experiment, no_op or reference")
    counts = defaultdict(Counter)
    bus_activity = defaultdict(Activity)
    per_id = defaultdict(lambda: defaultdict(lambda: Activity(track_frame_details=True)))
    signal_groups = {}
    previous = {}
    unknown_channels = Counter()
    invalid_timestamps = 0
    outside_windows = 0

    for frame in frames:
        if not math.isfinite(frame.timestamp):
            invalid_timestamps += 1
            continue
        stamp = frame.timestamp - origin
        bus = channels.get(frame.channel)
        if bus is None:
            unknown_channels[str(frame.channel)] += 1
            continue
        window = next((name for name, (a, b) in windows.items() if a <= stamp < b), None)
        if window is None:
            outside_windows += 1
            continue
        key = (bus, window)
        if frame.is_error:
            counts[key]["error_frames"] += 1
        elif frame.is_rx is False:
            counts[key]["tx_frames"] += 1
        elif frame.is_rx is not True:
            counts[key]["unknown_direction"] += 1
        elif frame.is_remote:
            counts[key]["remote_frames"] += 1
        else:
            channel_stats = bus_activity[key]
            if channel_stats.last_s is not None and stamp < channel_stats.last_s:
                counts[key]["out_of_order"] += 1
                continue
            channel_stats.add(stamp)
            identity = (bus, frame.arbitration_id, frame.is_extended, frame.is_fd)
            per_id[identity][window].add(stamp, frame.data)
            if decoder is None:
                continue
            try:
                readings = decoder(bus, frame)
            except (ValueError, KeyError):
                counts[key]["decode_errors"] += 1
                # A subsequent transition may still be real, but its endpoint
                # interval is not trusted across a failed decode.
                for signal_key in list(previous):
                    if signal_key[:4] == identity:
                        previous.pop(signal_key, None)
                continue
            for name, value in readings.items():
                if isinstance(value, float) and not math.isfinite(value):
                    counts[key]["decode_errors"] += 1
                    previous.pop((*identity, name), None)
                    continue
                signal_key = (*identity, name)
                group = signal_groups.setdefault(signal_key, {
                    "windows": {}, "transitions": [], "transition_count": 0,
                    "first_within_observation_transition": None,
                })
                sample = group["windows"].setdefault(window, {
                    "samples": 0, "first_value": value, "first_s": stamp,
                    "same_as_first": 0, "comparable_pairs": 0,
                })
                sample.update(last_value=value, last_s=stamp)
                sample["samples"] += 1
                sample["same_as_first"] += value == sample["first_value"]
                if window == "recovery" and "reference" in group["windows"]:
                    sample["reference_compared_samples"] = sample.get("reference_compared_samples", 0) + 1
                    sample["matching_reference_samples"] = (
                        sample.get("matching_reference_samples", 0)
                        + (group["windows"]["reference"]["first_value"] == value)
                    )
                prior = previous.get(signal_key)
                if (prior is not None and prior["window"] == window and
                        0 < stamp - prior["time"] <= max_signal_gap):
                    sample["comparable_pairs"] += 1
                if prior is not None and prior["value"] != value:
                    gap = stamp - prior["time"]
                    if gap <= 0 or gap > max_signal_gap:
                        certainty = "sampling_gap_or_order_uncertain"
                    elif prior["window"] != window:
                        certainty = "crosses_window_boundary"
                    else:
                        certainty = "within_window"
                    event = dict(before=prior["value"], after=value, before_s=prior["time"],
                                 after_s=stamp, window=window, certainty=certainty)
                    group["transition_count"] += 1
                    if len(group["transitions"]) < 20:
                        group["transitions"].append(event)
                    if (window == "observation" and certainty == "within_window"
                            and group["first_within_observation_transition"] is None):
                        group["first_within_observation_transition"] = event
                previous[signal_key] = {"value": value, "time": stamp, "window": window}

    quality = {}
    for bus in channels.values():
        quality[bus] = {}
        for window, (start, end) in windows.items():
            duration = end - start
            stats = bus_activity[bus, window]
            events = counts[bus, window]
            reasons = []
            if loss:
                reasons.append("capture_loss_reported")
            if invalid_timestamps:
                reasons.append("invalid_timestamps")
            for name in ("out_of_order", "unknown_direction", "error_frames"):
                if events[name]:
                    reasons.append(name)
            if not stats.count:
                reasons.append("no_rx_data_frames")
            else:
                margin = min(edge, duration / 4)
                if stats.first_s - start > margin or end - stats.last_s > margin:
                    reasons.append("rx_does_not_span_window")
            quality[bus][window] = dict(status="inconclusive" if reasons else "usable",
                                        reasons=reasons, **events,
                                        rx=stats.report(duration))

    def labeled(identity):
        bus, frame_id, extended, fd = identity
        return dict(bus=bus, id=hex(frame_id), extended=extended, fd=fd)

    durations = {name: b - a for name, (a, b) in windows.items()}
    candidates = []
    observations = []
    summaries = []
    for identity, stats in sorted(per_id.items()):
        row = labeled(identity)
        reports = {
            w: (stats[w] if w in stats else Activity(track_frame_details=True)).report(durations[w])
            for w in windows
        }
        bus = row["bus"]
        stationarity = reference_stationarity(reports, quality[bus], durations, windows)
        summaries.append(dict(**row, windows=reports, reference_stationarity=stationarity))
        if any(quality[bus][w]["status"] != "usable" for w in ("reference", "observation")):
            continue
        ref, obs = reports["reference"], reports["observation"]
        expected = ref["hz"] * durations["observation"]
        n_ref, n_obs = ref["count"], obs["count"]
        detail = dict(reference_count=n_ref, observation_count=n_obs,
                      expected_at_reference_rate=expected)
        def emit_comparison(kind, **extra):
            item = dict(type=kind, **row, **detail, **extra)
            if (status := stationarity["status"]) in {"transitioning", "unmeasured"}:
                observations.append(dict(**item, classification="inconclusive",
                                         reason=f"reference_{status}"))
            else:
                candidates.append(item)

        if not n_ref and n_obs >= 3:
            earlier = [w for w in ("baseline", "previous_reference") if w in windows
                       and windows[w][1] <= windows["reference"][0] and reports[w]["count"] > 0]
            if earlier:
                observations.append(dict(type="new_id", **row, **detail, classification="background",
                                         reason="seen_in_earlier_reference_window",
                                         seen_in_windows=earlier))
            else:
                candidates.append(dict(type="new_id", **row, **detail))
        elif n_ref >= 5 and expected >= 5 and not n_obs:
            emit_comparison("missing_id")
        elif n_ref >= 5 and expected >= 5 and abs(n_obs - expected) >= max(3, expected * 0.5):
            emit_comparison("rate_change", direction="increase" if n_obs > expected else "decrease")
        if min(ref["interval_count"], obs["interval_count"]) >= 3:
            changed = []
            for field in ("mean_cycle_time_ms", "median_cycle_time_ms"):
                old, new = ref[field], obs[field]
                if old is not None and old > 0 and new is not None and abs(new - old) >= max(2, old * 0.25):
                    changed.append(dict(metric=field, reference_ms=old, observation_ms=new))
            if changed:
                directions = {"increase" if x["observation_ms"] > x["reference_ms"]
                              else "decrease" for x in changed}
                direction = next(iter(directions)) if len(directions) == 1 else "mixed"
                emit_comparison("timing_shift", direction=direction, cycle_changes=changed)
        if (min(ref["interval_count"], obs["interval_count"]) >= 5
                and obs["jitter_ms"] > max(ref["jitter_ms"] * 3, ref["jitter_ms"] + 2)):
            emit_comparison("timing_jitter", reference_jitter_ms=ref["jitter_ms"],
                            observation_jitter_ms=obs["jitter_ms"])

    signals = []
    for key, group in sorted(signal_groups.items()):
        event = group["first_within_observation_transition"]
        row = dict(**labeled(key[:4]), signal=key[4])
        bus = row["bus"]
        recovery = summarize_recovery(group, quality[bus], {
            "reference": counts[bus, "reference"], "recovery": counts[bus, "recovery"],
        })
        signals.append(dict(**row, **group, recovery_vs_reference=recovery))
        if (event and "reference" in group["windows"] and
                all(quality[bus][w]["status"] == "usable" and
                    not counts[bus, w]["decode_errors"] for w in ("reference", "observation"))):
            candidates.append(dict(type="signal_transition", **row, **event, recovery_vs_reference=recovery))

    report = dict(
        timestamp_origin=origin, channel_map=channels, windows=windows,
        capture_role=role,
        capture_loss_reported=loss, quality=quality, unmapped_channel_frames=unknown_channels,
        outside_windows=outside_windows, invalid_timestamps=invalid_timestamps,
        per_id=summaries, signals=signals, candidates=candidates, observations=observations,
        verdict="manual_review_only",
        notes=[
            "Candidates are differences within the supplied windows, not confirmed faults.",
            "Usable means observed capture coverage, not verified loss-free capture.",
            "RX/TX labels do not establish independent observation or ECU acceptance.",
        ],
    )
    return {"assessment": summarize(report, config, timing, decoder is not None), **report}


def annotate_control(report, control):
    """Show what a separately supplied offline reference recording supports."""
    def key(item, *fields):
        return tuple(item.get(name) for name in ("bus", "id", "extended", "fd", *fields))

    control_ids = {key(item): item for item in control["per_id"]}
    control_signals = {key(item, "signal"): item for item in control["signals"]}
    observed_signals = {key(item, "signal"): item for item in report["signals"]}
    control_flags = {key(item, "type", "direction") for item in control["candidates"]}
    phases = ("reference", "observation")
    same_durations = all(
        math.isclose(report["windows"][w][1] - report["windows"][w][0],
                     control["windows"][w][1] - control["windows"][w][0],
                     abs_tol=0.01)
        for w in phases
    )
    for candidate in report["candidates"]:
        bus, kind = candidate["bus"], candidate["type"]
        support = (report["assessment"]["window"]["supported"]
                   and control["assessment"]["window"]["supported"])
        entry = {"status": "control_not_comparable",
                 "scope": "time_supported_windows" if support else "descriptive_windows_only"}
        candidate["control"] = entry
        if not same_durations or not all(
            control["quality"].get(bus, {}).get(w, {}).get("status") == "usable" for w in phases
        ):
            continue
        if kind == "signal_transition":
            signal_key = key(candidate, "signal")
            observed = observed_signals[signal_key]["windows"]
            paired = control_signals.get(signal_key, {})
            samples = paired.get("windows", {})
            if not all(w in samples for w in phases) or any(
                control["quality"][bus][w].get("decode_errors") for w in phases
            ):
                continue
            if not samples["observation"]["comparable_pairs"]:
                entry["reason"] = "insufficient_control_signal_samples"
                continue
            if (samples["reference"]["last_value"] != observed["reference"]["last_value"]
                    or samples["observation"]["first_value"] != observed["observation"]["first_value"]):
                entry["reason"] = "different_initial_signal_state"
                continue
            transitions = paired["transitions"]
            if any(t["window"] == "observation" and t["certainty"] == "within_window"
                   and (t["before"], t["after"]) == (candidate["before"], candidate["after"])
                   for t in transitions):
                entry["status"] = "also_flagged_in_control"
            elif paired["transition_count"] > len(transitions):
                entry["reason"] = "control_transition_list_truncated"
            else:
                entry["status"] = "not_flagged_in_control"
            continue

        control_row = control_ids.get(key(candidate), {})
        matched = control_row.get("windows", {})
        reference, observation = [matched.get(w, {}).get("count", 0) for w in phases]
        entry.update(reference_count=reference, observation_count=observation)
        if kind == "new_id" and (reference or observation):
            entry["status"] = "id_seen_in_control"
        elif (kind != "new_id" and control_row.get("reference_stationarity", {}).get("status")
              in {"transitioning", "unmeasured"}):
            entry["reason"] = "unstable_or_unmeasured_control_reference"
        elif kind in {"rate_change", "missing_id"} and (
            reference < 5 or matched.get("reference", {}).get("hz", 0) *
            (control["windows"]["observation"][1] - control["windows"]["observation"][0]) < 5
        ):
            entry["reason"] = "insufficient_control_reference_frames"
        elif kind in {"timing_shift", "timing_jitter"} and min(
            matched.get(w, {}).get("interval_count", 0) for w in phases
        ) < (5 if kind == "timing_jitter" else 3):
            entry["reason"] = "insufficient_control_intervals"
        else:
            entry["status"] = (
                "also_flagged_in_control" if key(candidate, "type", "direction") in control_flags
                else "not_flagged_in_control"
            )
    summarize_control(report, control)


def analyze_file(log_path, config_path, dbc_path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    watches = config.get("watch_signals", [])
    if watches and dbc_path is None:
        raise ValueError("watch_signals requires --dbc")
    decoder = dbc_decoder(dbc_path, watches, config["channel_map"].values()) if watches else None
    report = analyze(read_log(log_path), config, decoder, phase_metadata=inspect_phase_metadata(log_path))
    report["source_file"] = str(log_path)
    report["capture_label"] = config.get("capture_label", "unlabeled")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("--dbc", type=Path)
    parser.add_argument("--control-log", type=Path, help="A prior control recording (read only)")
    parser.add_argument("--control-config", type=Path)
    parser.add_argument("--out", type=Path, help="Write JSON to a new file (default: stdout)")
    args = parser.parse_args(argv)
    try:
        if bool(args.control_log) != bool(args.control_config):
            raise ValueError("--control-log and --control-config must be provided together")
        if args.control_log and args.log.samefile(args.control_log):
            raise ValueError("the control must be a separately recorded file")
        report = analyze_file(args.log, args.config, args.dbc)
        if args.control_log:
            control = analyze_file(args.control_log, args.control_config, args.dbc)
            annotate_control(report, control)
            report["control_file"] = str(args.control_log)
            report["notes"].append("not_flagged_in_control does not confirm a fault or causation.")
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.out:
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
            print(args.out)
            print(report["assessment"]["headline_ko"], file=sys.stderr)
        else:
            print(encoded)
    except ImportError as exc:
        parser.exit(2, f"Cannot open a log or DBC without its reader dependency: {exc}\n")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid log, config or output: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
