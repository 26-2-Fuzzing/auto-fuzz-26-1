"""Interpret evidence in a completed offline recording, never infer causality."""

import math
from collections import Counter


def assess_window(config, observation, origin, metadata=None):
    """All operator times use the report time axis; file markers use the reader axis."""
    meta = metadata if metadata is not None else {
        "status": "not_inspected", "intervals_s": [], "reason": "frames_only",
    }
    def matches(pair):
        return all(math.isclose(a, b, rel_tol=0, abs_tol=0.001) for a, b in zip(pair, observation))

    result = dict(supported=False, observation_s=list(observation), file_metadata=meta)
    manual = config.get("window_evidence")
    if manual is not None:
        if not isinstance(manual, dict) or manual.get("source") != "operator_record":
            raise ValueError("window_evidence.source must be operator_record")
        desc, pair = manual.get("description"), manual.get("observation_s")
        if (not isinstance(desc, str) or not desc.strip() or not isinstance(pair, list)
                or len(pair) != 2 or not all(type(x) in (int, float) and math.isfinite(x) for x in pair)
                or pair[0] >= pair[1]):
            raise ValueError("window_evidence requires a description and finite ordered observation_s")
        result["operator_record"] = dict(description=desc.strip(), observation_s=pair)
        if not matches(pair):
            return dict(result, reason="operator_times_do_not_match")
    if meta["status"] == "ambiguous":
        return dict(result, reason="ambiguous_file_markers")
    if meta["status"] == "recorded_intervals":
        matched = [pair for pair in meta["intervals_s"] if matches([t-origin for t in pair])]
        if len(matched) != 1:
            return dict(result, reason="file_markers_do_not_uniquely_match")
        a, b = config["windows"]["reference"]
        if any(min(b, end-origin) - max(a, start-origin) > 0.001
               for start, end in meta["intervals_s"]):
            return dict(result, reason="reference_overlaps_a_recorded_interval")
        return dict(result, supported=True, reason="matching_recorded_markers",
                    matched_file_interval_s=matched[0])
    if manual is not None:
        return dict(result, supported=True, reason="operator_attested_times")
    return dict(result, reason="no_timing_evidence")


def summarize(report, config, timing, decoding_enabled):
    phases = ("reference", "observation")
    quality = report["quality"]
    usable = sorted(bus for bus, by_window in quality.items()
                    if all(by_window[p]["status"] == "usable" for p in phases))
    supported = timing["supported"] and bool(usable)
    types = Counter(c["type"] for c in report["candidates"])
    transitions = types.pop("signal_transition", 0)
    n_stat = sum(types.values())
    watched = {(w["bus"], w["message"] + "." + w["signal"]) for w in config.get("watch_signals", [])}
    sampled = {(s["bus"], s["signal"]) for s in report["signals"]}
    seen = {(s["bus"], s["signal"]) for s in report["signals"]
            if all(p in s["windows"] for p in phases)
            and s["windows"]["observation"]["comparable_pairs"] > 0}
    readable = {(bus, sig) for bus, sig in seen if bus in usable
                and all(not quality[bus][p].get("decode_errors") for p in phases)}
    if not decoding_enabled:
        dbc_status = "not_evaluated"
    elif not sampled:
        dbc_status = "no_samples"
    elif not readable:
        dbc_status = "insufficient_samples_or_quality"
    elif watched - readable:
        dbc_status = "partially_observed"
    else:
        dbc_status = "observed"
    reason = ("구간 근거 있음 · 인과관계 미확정" if supported else
              "비교 가능한 수신 구간 없음 · 퍼징 영향 판정 불가" if not usable else
              "구간 근거 미확인 · 퍼징 영향 판정 불가")
    dbc_text = (f"신호 전이 후보 {transitions}건" if readable else
                "DBC 신호 미평가" if not decoding_enabled else "DBC 신호 비교 근거 부족")
    return dict(
        status="window_review_only" if supported else "descriptive_only",
        headline_ko=f"통계 변화 후보 {n_stat}건 / {dbc_text} / {reason}",
        attribution="not_established", statistical_candidate_count=n_stat,
        statistical_candidates_by_type=dict(sorted(types.items())), window=timing,
        usable_buses=usable, unusable_buses=sorted(set(quality) - set(usable)),
        dbc=dict(status=dbc_status, configured_signal_count=len(watched), sampled_signal_count=len(sampled),
                 comparable_signal_count=len(readable),
                 transition_candidate_count=transitions if readable else None),
        control={"kind": "not_provided"},
    )


def summarize_control(report, control):
    primary, reference = report["assessment"], control["assessment"]
    comparison_counts = Counter(c["control"]["status"] for c in report["candidates"])
    primary["control"] = dict(
        kind="declared_no_op" if control.get("capture_role") == "no_op" else "reference_recording",
        # Even two timing-supported records do not establish equal vehicle conditions.
        both_windows_supported=primary["window"]["supported"] and reference["window"]["supported"],
        reference_window_reason=reference["window"]["reason"],
        candidate_comparisons=dict(sorted(comparison_counts.items())),
    )
