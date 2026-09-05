#!/usr/bin/env python3
"""Correlate a TX manifest with passive I/P-CAN receiver JSONL logs."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from can_common import (
    ConfigurationError,
    json_safe,
    load_dbc,
    protected_signal_names,
    reserve_output_path,
    socketcan_error_details,
)


FrameKey = Tuple[int, bool]


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"숫자가 아닙니다: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 유한한 숫자여야 합니다.")
    return parsed


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise ConfigurationError(f"JSONL 파일을 찾을 수 없습니다: {path}")
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{path}:{line_number} JSON 해석 실패: {exc}"
                ) from exc
            if isinstance(item, dict):
                records.append(item)
    return records


def parse_can_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ConfigurationError(f"CAN ID를 해석할 수 없습니다: {value!r}")


def parse_data(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ConfigurationError(f"payload를 해석할 수 없습니다: {value!r}")
    return bytes.fromhex(value)


def last_tx_session(records: Sequence[Dict[str, Any]]) -> Sequence[Dict[str, Any]]:
    starts = [
        index
        for index, record in enumerate(records)
        if record.get("record_type") == "tx_session_start"
        or record.get("event") == "run_start"
    ]
    return records[starts[-1]:] if starts else records


def load_tx_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for record in last_tx_session(load_jsonl(path)):
        is_pi_sender = record.get("record_type") == "can_tx"
        is_tx_only = record.get("event") == "tx"
        if not (is_pi_sender or is_tx_only) or record.get("status") != "sent":
            continue
        timestamp = (
            record.get("send_attempt_wall_time_ns")
            or record.get("epoch_ns")
            or record.get("wall_time_ns")
        )
        if timestamp is None:
            continue
        events.append({
            "time_ns": int(timestamp),
            "sequence": int(record.get("sequence", len(events) + 1)),
            "arbitration_id": parse_can_id(
                record.get("arbitration_id", record.get("can_id"))
            ),
            "is_extended_id": bool(record.get("is_extended_id", False)),
            "payload": parse_data(record.get("data_hex", record.get("payload"))),
            "kind": record.get("kind", "mutation"),
            "phase": record.get("phase"),
            "mutation": record.get("mutation"),
            "tx_session_id": record.get("tx_session_id"),
            "experiment_id": record.get("experiment_id"),
        })
    if not events:
        raise ConfigurationError(f"마지막 TX session에 status=sent 기록이 없습니다: {path}")
    return sorted(events, key=lambda event: event["time_ns"])


def load_rx_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for record in load_jsonl(path):
        is_pi_receiver = record.get("record_type") == "can_rx"
        is_monitor = record.get("event") == "rx"
        if not (is_pi_receiver or is_monitor):
            continue
        if record.get("is_error_frame") or record.get("is_remote_frame"):
            continue
        timestamp = record.get("wall_time_ns", record.get("epoch_ns"))
        if timestamp is None:
            continue
        try:
            event = {
                "time_ns": int(timestamp),
                "arbitration_id": parse_can_id(
                    record.get("arbitration_id", record.get("can_id"))
                ),
                "is_extended_id": bool(record.get("is_extended_id", False)),
                "payload": parse_data(record.get("data_hex", record.get("payload"))),
                "session_id": record.get("session_id"),
                "experiment_id": record.get("experiment_id"),
            }
        except (TypeError, ValueError, ConfigurationError):
            continue
        events.append(event)
    return sorted(events, key=lambda event: event["time_ns"])


def load_rx_error_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for record in load_jsonl(path):
        if record.get("record_type") != "can_rx" or not record.get("is_error_frame"):
            continue
        timestamp = record.get("wall_time_ns", record.get("epoch_ns"))
        if timestamp is None:
            continue
        error_mask = parse_can_id(record.get("arbitration_id", 0))
        payload = parse_data(record.get("data_hex", ""))
        events.append({
            "time_ns": int(timestamp),
            "error_mask": error_mask,
            "payload": payload,
            "details": record.get("can_error")
            or socketcan_error_details(error_mask, payload),
            "session_id": record.get("session_id"),
            "experiment_id": record.get("experiment_id"),
        })
    return sorted(events, key=lambda event: event["time_ns"])


def parse_rx_spec(value: str) -> Tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--rx는 BUS=JSONL 형식이어야 합니다.")
    bus, raw_path = value.split("=", 1)
    bus = bus.strip()
    if not bus or not raw_path.strip():
        raise argparse.ArgumentTypeError("--rx는 BUS=JSONL 형식이어야 합니다.")
    return bus, Path(raw_path).expanduser().resolve()


def phase_events(
    events: Iterable[Dict[str, Any]],
    start_ns: int,
    end_ns: int,
) -> List[Dict[str, Any]]:
    return [event for event in events if start_ns <= event["time_ns"] < end_ns]


def profiles(events: Iterable[Dict[str, Any]]) -> Dict[FrameKey, Counter[bytes]]:
    result: Dict[FrameKey, Counter[bytes]] = defaultdict(Counter)
    for event in events:
        key = (event["arbitration_id"], event["is_extended_id"])
        result[key][event["payload"]] += 1
    return dict(result)


def format_can_id(key: FrameKey) -> str:
    arbitration_id, is_extended = key
    width = 8 if is_extended else 3
    return f"0x{arbitration_id:0{width}X}"


def changed_bytes(reference: bytes, payload: bytes) -> List[Dict[str, Any]]:
    changes = [
        {
            "index": index,
            "baseline": f"{before:02X}",
            "observed": f"{after:02X}",
            "xor": f"{before ^ after:02X}",
        }
        for index, (before, after) in enumerate(zip(reference, payload))
        if before != after
    ]
    if len(reference) != len(payload):
        changes.append({
            "index": "length",
            "baseline": len(reference),
            "observed": len(payload),
            "xor": None,
        })
    return changes


def decoded_changes(
    database: Any,
    key: FrameKey,
    reference: bytes,
    payload: bytes,
) -> List[Dict[str, Any]]:
    if database is None:
        return []
    try:
        message = database.get_message_by_frame_id(key[0])
        before = message.decode(reference, decode_choices=False, scaling=True)
        after = message.decode(payload, decode_choices=False, scaling=True)
    except Exception:
        return []
    return [
        {"signal": name, "baseline": json_safe(value), "observed": json_safe(after[name])}
        for name, value in before.items()
        if name in after and after[name] != value
    ]


def stable_bit_profile(
    counts: Mapping[bytes, int],
) -> Tuple[Optional[bytes], bytes]:
    """Return baseline mode and a mask of bits that never changed in baseline."""
    if not counts:
        return None, b""
    reference = max(counts, key=counts.get)  # type: ignore[arg-type]
    mask = bytearray([0xFF] * len(reference))
    for payload in counts:
        if len(payload) != len(reference):
            return reference, bytes(len(reference))
        for index, (before, observed) in enumerate(zip(reference, payload)):
            mask[index] &= (~(before ^ observed)) & 0xFF
    return reference, bytes(mask)


def stable_bit_evidence(
    counts: Mapping[bytes, int], reference: Optional[bytes], mask: bytes,
) -> Dict[str, Any]:
    changed_bits: Counter[int] = Counter()
    changed_frames = 0
    length_change_frames = 0
    signatures: Counter[str] = Counter()
    if reference is None:
        return {
            "changed_bits": [], "changed_frames": 0,
            "length_change_frames": 0, "signatures": [],
        }
    for payload, count in counts.items():
        if len(payload) != len(reference):
            length_change_frames += count
            continue
        delta = bytes(
            (before ^ observed) & stable
            for before, observed, stable in zip(reference, payload, mask)
        )
        if not any(delta):
            continue
        changed_frames += count
        signatures[delta.hex().upper()] += count
        for byte_index, value in enumerate(delta):
            for bit in range(8):
                if value & (1 << bit):
                    changed_bits[byte_index * 8 + bit] += count
    return {
        "changed_bits": [
            {"bit": bit, "frames": count}
            for bit, count in changed_bits.most_common()
        ],
        "changed_frames": changed_frames,
        "length_change_frames": length_change_frames,
        "signatures": [
            {"xor_on_stable_bits": signature, "frames": count}
            for signature, count in signatures.most_common(5)
        ],
    }


def stable_recovery_ratio(
    counts: Mapping[bytes, int], reference: Optional[bytes], mask: bytes,
) -> Optional[float]:
    total = sum(counts.values())
    if not total or reference is None:
        return None
    recovered = 0
    for payload, count in counts.items():
        if len(payload) != len(reference):
            continue
        delta = bytes(
            (before ^ observed) & stable
            for before, observed, stable in zip(reference, payload, mask)
        )
        if not any(delta):
            recovered += count
    return recovered / total


def stable_signal_evidence(
    database: Any,
    key: FrameKey,
    baseline: Mapping[bytes, int],
    stimulus: Mapping[bytes, int],
    recovery: Mapping[bytes, int],
) -> List[Dict[str, Any]]:
    if database is None or not baseline or not stimulus:
        return []
    try:
        message = database.get_message_by_frame_id(key[0])
    except KeyError:
        return []
    protected = set(protected_signal_names(message))

    def decode_counts(counts: Mapping[bytes, int]) -> Dict[str, Counter[str]]:
        result: Dict[str, Counter[str]] = defaultdict(Counter)
        for payload, count in counts.items():
            try:
                values = message.decode(payload, decode_choices=False, scaling=True)
            except Exception:
                continue
            for name, value in values.items():
                if name not in protected:
                    result[name][json.dumps(json_safe(value), sort_keys=True)] += count
        return result

    base_values = decode_counts(baseline)
    stimulus_values = decode_counts(stimulus)
    recovery_values = decode_counts(recovery)
    evidence: List[Dict[str, Any]] = []
    for name, baseline_counter in base_values.items():
        if len(baseline_counter) != 1:
            continue
        changed = {
            value: count for value, count in stimulus_values.get(name, Counter()).items()
            if value not in baseline_counter
        }
        if not changed:
            continue
        recovery_counter = recovery_values.get(name, Counter())
        recovery_total = sum(recovery_counter.values())
        returned = sum(
            count for value, count in recovery_counter.items()
            if value in baseline_counter
        )
        evidence.append({
            "signal": name,
            "baseline": json.loads(next(iter(baseline_counter))),
            "observed": [json.loads(value) for value in changed],
            "stimulus_frames": sum(changed.values()),
            "recovery_baseline_ratio": (
                returned / recovery_total if recovery_total else None
            ),
        })
    evidence.sort(key=lambda item: (-item["stimulus_frames"], item["signal"]))
    return evidence[:20]


def first_stable_change_latency_ms(
    events: Sequence[Dict[str, Any]],
    key: FrameKey,
    reference: Optional[bytes],
    mask: bytes,
    tx_events: Sequence[Dict[str, Any]],
) -> Optional[float]:
    changed_times = []
    for event in events:
        if (event["arbitration_id"], event["is_extended_id"]) != key:
            continue
        payload = event["payload"]
        if reference is None:
            changed_times.append(event["time_ns"])
            continue
        if len(payload) != len(reference):
            changed_times.append(event["time_ns"])
            continue
        if any(
            (before ^ observed) & stable
            for before, observed, stable in zip(reference, payload, mask)
        ):
            changed_times.append(event["time_ns"])
    latencies = [
        changed - tx["time_ns"]
        for changed in changed_times
        for tx in tx_events
        if tx["time_ns"] <= changed
    ]
    return min(latencies) / 1_000_000.0 if latencies else None


def direct_correlations(
    tx_events: Sequence[Dict[str, Any]],
    rx_events: Sequence[Dict[str, Any]],
    baseline: Mapping[FrameKey, Counter[bytes]],
    window_ns: int,
) -> Dict[str, Any]:
    by_frame: Dict[Tuple[int, bool, bytes], List[int]] = defaultdict(list)
    for event in rx_events:
        frame = (
            event["arbitration_id"],
            event["is_extended_id"],
            event["payload"],
        )
        by_frame[frame].append(event["time_ns"])

    matches: List[Dict[str, Any]] = []
    for tx in tx_events:
        frame = (tx["arbitration_id"], tx["is_extended_id"], tx["payload"])
        timestamps = by_frame.get(frame, [])
        left = bisect.bisect_left(timestamps, tx["time_ns"] - window_ns)
        right = bisect.bisect_right(timestamps, tx["time_ns"] + window_ns)
        if left == right:
            continue
        rx_time = min(timestamps[left:right], key=lambda item: abs(item - tx["time_ns"]))
        key = (tx["arbitration_id"], tx["is_extended_id"])
        baseline_seen = tx["payload"] in baseline.get(key, Counter())
        matches.append({
            "sequence": tx["sequence"],
            "can_id": format_can_id(key),
            "payload": tx["payload"].hex().upper(),
            "latency_ms": (rx_time - tx["time_ns"]) / 1_000_000.0,
            "baseline_seen_payload": baseline_seen,
        })
    return {
        "matched_tx_count": len(matches),
        "novel_matched_tx_count": sum(not item["baseline_seen_payload"] for item in matches),
        "matches": matches,
    }


def analyse_bus(
    bus_name: str,
    rx_events: Sequence[Dict[str, Any]],
    tx_events: Sequence[Dict[str, Any]],
    database: Any,
    baseline_start_ns: int,
    tx_start_ns: int,
    stimulus_end_ns: int,
    recovery_end_ns: int,
    correlation_window_ns: int,
    rx_errors: Sequence[Dict[str, Any]] = (),
    reaction_window_ns: int = 500_000_000,
    rate_ratio_low: float = 0.5,
    rate_ratio_high: float = 2.0,
) -> Dict[str, Any]:
    baseline_events = phase_events(rx_events, baseline_start_ns, tx_start_ns)
    stimulus_events = phase_events(rx_events, tx_start_ns, stimulus_end_ns)
    recovery_events = phase_events(rx_events, stimulus_end_ns, recovery_end_ns)
    baseline = profiles(baseline_events)
    stimulus = profiles(stimulus_events)
    recovery = profiles(recovery_events)
    direct = direct_correlations(
        tx_events,
        phase_events(rx_events, tx_start_ns - correlation_window_ns, stimulus_end_ns),
        baseline,
        correlation_window_ns,
    )

    baseline_seconds = (tx_start_ns - baseline_start_ns) / 1_000_000_000.0
    stimulus_seconds = (stimulus_end_ns - tx_start_ns) / 1_000_000_000.0
    recovery_seconds = (recovery_end_ns - stimulus_end_ns) / 1_000_000_000.0
    keys = set(baseline) | set(stimulus) | set(recovery)
    candidates: List[Dict[str, Any]] = []
    tx_keys = {
        (event["arbitration_id"], event["is_extended_id"])
        for event in tx_events
    }

    direct_by_id = Counter(item["can_id"] for item in direct["matches"])
    novel_direct_by_id = Counter(
        item["can_id"] for item in direct["matches"]
        if not item["baseline_seen_payload"]
    )
    for key in keys:
        base_counts = baseline.get(key, Counter())
        stimulus_counts = stimulus.get(key, Counter())
        recovery_counts = recovery.get(key, Counter())
        novel = [payload for payload in stimulus_counts if payload not in base_counts]
        baseline_rate = sum(base_counts.values()) / baseline_seconds
        stimulus_rate = sum(stimulus_counts.values()) / stimulus_seconds
        recovery_rate = sum(recovery_counts.values()) / recovery_seconds
        rate_ratio = stimulus_rate / baseline_rate if baseline_rate > 0 else None
        can_id = format_can_id(key)
        reference, stable_mask = stable_bit_profile(base_counts)
        stable_bits = stable_bit_evidence(stimulus_counts, reference, stable_mask)
        stable_recovery = stable_recovery_ratio(
            recovery_counts, reference, stable_mask
        )
        signal_evidence = stable_signal_evidence(
            database, key, base_counts, stimulus_counts, recovery_counts
        )
        latency_ms = first_stable_change_latency_ms(
            stimulus_events, key, reference, stable_mask, tx_events
        )
        expected_stimulus_frames = baseline_rate * stimulus_seconds
        rate_changed = (
            len(base_counts) >= 1
            and sum(base_counts.values()) >= 10
            and expected_stimulus_frames >= 3
            and rate_ratio is not None
            and (rate_ratio >= rate_ratio_high or rate_ratio <= rate_ratio_low)
        )
        is_transport = key in tx_keys

        reasons: List[str] = []
        score = 0
        if is_transport and novel_direct_by_id[can_id]:
            reasons.append("novel direct TX payload observed")
            score += 100 + min(novel_direct_by_id[can_id], 20)
        elif is_transport and direct_by_id[can_id]:
            reasons.append("TX payload observed")
            score += 70
        if not is_transport:
            if not base_counts and sum(stimulus_counts.values()) >= 2:
                reasons.append("new ID during stimulus")
                score += 40
            elif stable_bits["changed_frames"] or stable_bits["length_change_frames"]:
                if len(base_counts) == 1:
                    reasons.append("stable baseline payload changed")
                else:
                    reasons.append("baseline-stable bits changed")
                score += 30
            if signal_evidence:
                reasons.append("stable DBC signal changed")
                score += 20
            if rate_changed:
                reasons.append("frame rate changed")
                score += 15
            if latency_ms is not None and latency_ms <= reaction_window_ns / 1_000_000:
                reasons.append("prompt change after TX")
                score += 10
            if stable_recovery is not None and stable_recovery >= 0.9 and score:
                reasons.append("returned during recovery")
                score += 10
        if not reasons:
            continue

        samples = [payload for payload, _ in stimulus_counts.most_common(5) if payload in novel]
        sample_details: List[Dict[str, Any]] = []
        for payload in samples:
            detail: Dict[str, Any] = {
                "payload": payload.hex().upper(),
                "count": stimulus_counts[payload],
            }
            if reference is not None:
                detail["changed_bytes"] = changed_bytes(reference, payload)
                signal_changes = decoded_changes(database, key, reference, payload)
                if signal_changes:
                    detail["changed_signals"] = signal_changes
            sample_details.append(detail)

        recovery_baseline_frames = sum(
            count for payload, count in recovery_counts.items() if payload in base_counts
        )
        recovery_total = sum(recovery_counts.values())
        candidate_type = "transport" if is_transport else "reaction"
        if candidate_type == "transport":
            confidence = "high" if novel_direct_by_id[can_id] else "medium"
        else:
            prompt = (
                latency_ms is not None
                and latency_ms <= reaction_window_ns / 1_000_000
            )
            signal_prompt = bool(signal_evidence) and prompt
            stable_prompt_recovered = (
                bool(stable_bits["changed_frames"])
                and prompt
                and stable_recovery is not None
                and stable_recovery >= 0.9
            )
            new_id_prompt = (
                not base_counts
                and sum(stimulus_counts.values()) >= 3
                and prompt
            )
            if signal_prompt and stable_recovery is not None and stable_recovery >= 0.9:
                confidence = "high"
            elif signal_prompt or stable_prompt_recovered or new_id_prompt:
                confidence = "medium"
            else:
                confidence = "low"
        candidates.append({
            "can_id": can_id,
            "score": score,
            "candidate_type": candidate_type,
            "confidence": confidence,
            "reasons": reasons,
            "baseline_frames": sum(base_counts.values()),
            "baseline_unique_payloads": len(base_counts),
            "stimulus_frames": sum(stimulus_counts.values()),
            "stimulus_unique_payloads": len(stimulus_counts),
            "novel_payload_count": len(novel),
            "baseline_rate_hz": baseline_rate,
            "stimulus_rate_hz": stimulus_rate,
            "stimulus_to_baseline_rate_ratio": rate_ratio,
            "recovery_rate_hz": recovery_rate,
            "recovery_baseline_payload_ratio": (
                recovery_baseline_frames / recovery_total if recovery_total else None
            ),
            "recovery_stable_bit_ratio": stable_recovery,
            "first_change_latency_ms": latency_ms,
            "stable_bit_evidence": stable_bits,
            "stable_signal_evidence": signal_evidence,
            "direct_tx_matches": direct_by_id[can_id],
            "novel_direct_tx_matches": novel_direct_by_id[can_id],
            "baseline_mode_payload": reference.hex().upper() if reference else None,
            "novel_payload_samples": sample_details,
        })

    candidates.sort(key=lambda item: (-item["score"], item["can_id"]))
    reactions = [
        candidate for candidate in candidates
        if candidate["candidate_type"] == "reaction"
    ]
    analysis_events = phase_events(rx_events, baseline_start_ns, recovery_end_ns)
    window_errors = phase_events(rx_errors, baseline_start_ns, recovery_end_ns)
    first_rx_ns = analysis_events[0]["time_ns"] if analysis_events else None
    last_rx_ns = analysis_events[-1]["time_ns"] if analysis_events else None
    coverage_tolerance_ns = 1_000_000_000
    baseline_covered = (
        first_rx_ns is not None
        and first_rx_ns <= baseline_start_ns + coverage_tolerance_ns
        and bool(baseline_events)
    )
    recovery_covered = (
        last_rx_ns is not None
        and last_rx_ns >= recovery_end_ns - coverage_tolerance_ns
        and bool(recovery_events)
    )
    coverage_complete = baseline_covered and recovery_covered
    if not coverage_complete:
        quality_status = "incomplete"
    elif window_errors:
        quality_status = "degraded"
    else:
        quality_status = "good"
    transport_confirmed = direct["novel_matched_tx_count"] > 0
    meaningful_reactions = [
        item for item in reactions if item["confidence"] in {"high", "medium"}
    ]
    if not coverage_complete:
        label = "데이터 불충분"
    elif window_errors:
        label = "버스 이상 관측"
    elif meaningful_reactions:
        label = "반응 후보 관측"
    elif transport_confirmed:
        label = "주입 관측 / 기능 반응 미확인"
    else:
        label = "특이사항 없음"
    novel_ids = {
        key for key in keys
        if key not in tx_keys
        and any(payload not in baseline.get(key, Counter()) for payload in stimulus.get(key, Counter()))
    }
    ranked_reaction_keys = {
        next(key for key in keys if format_can_id(key) == item["can_id"])
        for item in reactions
    }
    return {
        "bus": bus_name,
        "rx_frame_count": len(rx_events),
        "phase_counts": {
            "baseline": len(baseline_events),
            "stimulus": len(stimulus_events),
            "recovery": len(recovery_events),
        },
        "capture_quality": {
            "status": quality_status,
            "baseline_covered": baseline_covered,
            "recovery_covered": recovery_covered,
            "session_ids": sorted({
                event["session_id"] for event in analysis_events
                if event.get("session_id")
            }),
            "can_error_frame_count": len(window_errors),
            "can_errors": [
                {
                    "time_ns": event["time_ns"],
                    "details": event["details"],
                }
                for event in window_errors[:100]
            ],
        },
        "direct_correlation": direct,
        "candidates": candidates,
        "reaction_candidates": reactions,
        "suppressed_dynamic_novel_id_count": len(novel_ids - ranked_reaction_keys),
        "verdict": {
            "label": label,
            "transport_confirmed": transport_confirmed,
            "reaction_candidate_count": len(reactions),
            "meaningful_reaction_candidate_count": len(meaningful_reactions),
            "bus_health": "error_observed" if window_errors else "no_error_observed",
        },
    }


def markdown_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# CAN 퍼징 반응 분석 보고서",
        "",
        "## 한눈에 보는 결론",
        "",
        "| Bus | 최종 판정 | 데이터 품질 | 주입 관측 | 반응 후보 | CAN 오류 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for bus in result["buses"]:
        verdict = bus["verdict"]
        quality = bus["capture_quality"]
        lines.append(
            f"| {bus['bus']} | **{verdict['label']}** | {quality['status']} | "
            f"{'예' if verdict['transport_confirmed'] else '아니오'} | "
            f"{verdict['meaningful_reaction_candidate_count']} | "
            f"{quality['can_error_frame_count']} |"
        )
    lines.extend([
        "",
        f"- TX stimulus frames: {result['tx']['frame_count']}",
        f"- Restore frames: {result['tx']['restore_frame_count']}",
        f"- TX ID: `{result['tx']['can_id']}`",
        f"- TX duration: {result['tx']['duration_ms']:.3f} ms",
        f"- Clock correlation window: ±{result['settings']['correlation_window_ms']:.1f} ms",
        "",
    ])
    for bus in result["buses"]:
        direct = bus["direct_correlation"]
        quality = bus["capture_quality"]
        lines.extend([
            f"## {bus['bus']}",
            "",
            (
                f"판정: **{bus['verdict']['label']}**. "
                f"Frames: baseline {bus['phase_counts']['baseline']:,}, "
                f"stimulus {bus['phase_counts']['stimulus']:,}, "
                f"recovery {bus['phase_counts']['recovery']:,}. "
                f"주입 일치 {direct['matched_tx_count']}건 "
                f"(baseline에 없던 일치 {direct['novel_matched_tx_count']}건)."
            ),
            "",
            "### 기능 반응 후보",
            "",
            "| 신뢰도 | Score | CAN ID | 근거 | 최초 지연 | 안정 비트 원복 | DBC 신호 |",
            "|---|---:|---|---|---:|---:|---|",
        ])
        for candidate in bus["reaction_candidates"][:30]:
            latency = candidate["first_change_latency_ms"]
            recovery = candidate["recovery_stable_bit_ratio"]
            latency_text = f"{latency:.3f} ms" if latency is not None else "-"
            recovery_text = f"{recovery:.1%}" if recovery is not None else "-"
            signals = ", ".join(
                item["signal"] for item in candidate["stable_signal_evidence"][:5]
            ) or "-"
            lines.append(
                f"| {candidate['confidence']} | {candidate['score']} | `{candidate['can_id']}` | "
                f"{'; '.join(candidate['reasons'])} | {latency_text} | "
                f"{recovery_text} | {signals} |"
            )
        if not bus["reaction_candidates"]:
            lines.append("| - | - | - | 기능 반응 후보 없음 | - | - | - |")
        lines.extend([
            "",
            f"정상 rolling counter/CRC 등으로 보이는 단순 신규 payload ID "
            f"{bus['suppressed_dynamic_novel_id_count']}개는 후보에서 제외했습니다.",
            "",
            "### 캡처/버스 건전성",
            "",
            f"- 데이터 품질: **{quality['status']}**",
            f"- Baseline 구간 확보: {'예' if quality['baseline_covered'] else '아니오'}",
            f"- Recovery 구간 확보: {'예' if quality['recovery_covered'] else '아니오'}",
            f"- 분석 구간 CAN error frame: {quality['can_error_frame_count']}건",
            "",
        ])
    lines.extend([
        "## 해석 기준",
        "",
        "- 주입 payload 직접 일치는 프레임이 해당 버스에서 관찰됐다는 근거이며, 기능 동작 자체의 증거는 아닙니다.",
        "- 반응 후보는 baseline에서 안정적이던 비트/DBC 신호, 신규 ID, 주기 변화를 기준으로 추렸습니다.",
        "- 후보는 인과관계의 증명이 아닙니다. 같은 입력을 3회 이상 반복하고 no-op 대조군에서도 재현되는지 비교하십시오.",
        "- CAN 로그만으로 램프·모터 등 물리 동작을 증명할 수 없습니다. 영상, 전류, GPIO 같은 별도 oracle을 함께 기록하십시오.",
        "- 음수 지연은 장비 간 시계 오차일 수 있으므로 모든 호스트의 NTP/chrony 동기화가 필요합니다.",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TX manifest와 I/P-CAN raw JSONL을 baseline/stimulus/recovery로 비교합니다."
    )
    parser.add_argument("--tx", type=Path, required=True, help="can_sender 또는 tx_only TX JSONL")
    parser.add_argument(
        "--rx", action="append", type=parse_rx_spec, required=True,
        metavar="BUS=JSONL", help="예: i_can=logs/i_can.jsonl; 반복 가능",
    )
    parser.add_argument("--dbc", type=Path, default=None, help="변경 신호 해석용 DBC")
    parser.add_argument("--baseline-seconds", type=positive_float, default=10.0)
    parser.add_argument("--response-seconds", type=positive_float, default=2.0)
    parser.add_argument("--recovery-seconds", type=positive_float, default=10.0)
    parser.add_argument("--correlation-window-ms", type=positive_float, default=250.0)
    parser.add_argument("--reaction-window-ms", type=positive_float, default=500.0)
    parser.add_argument("--rate-ratio-low", type=positive_float, default=0.5)
    parser.add_argument("--rate-ratio-high", type=positive_float, default=2.0)
    parser.add_argument("--output", type=Path, default=None, help="JSON 결과 경로")
    return parser


def run(args: argparse.Namespace) -> int:
    tx_path = args.tx.expanduser().resolve()
    all_tx_events = load_tx_events(tx_path)
    phased_mutations = [
        event for event in all_tx_events if event.get("phase") == "mutation"
    ]
    tx_events = phased_mutations or [
        event for event in all_tx_events if event.get("kind") != "restore"
    ]
    restore_events = [event for event in all_tx_events if event.get("kind") == "restore"]
    if not tx_events:
        raise ConfigurationError("restore가 아닌 sent TX stimulus가 없습니다.")
    if not 0 < args.rate_ratio_low < 1 < args.rate_ratio_high:
        raise ConfigurationError("rate ratio는 low < 1 < high여야 합니다.")
    database = load_dbc(args.dbc.expanduser().resolve()) if args.dbc else None
    first_tx_ns = tx_events[0]["time_ns"]
    last_tx_ns = tx_events[-1]["time_ns"]
    baseline_start_ns = first_tx_ns - int(args.baseline_seconds * 1_000_000_000)
    stimulus_end_ns = last_tx_ns + int(args.response_seconds * 1_000_000_000)
    recovery_end_ns = stimulus_end_ns + int(args.recovery_seconds * 1_000_000_000)
    correlation_window_ns = int(args.correlation_window_ms * 1_000_000)

    buses = []
    for bus_name, rx_path in args.rx:
        bus_result = analyse_bus(
            bus_name,
            load_rx_events(rx_path),
            tx_events,
            database,
            baseline_start_ns,
            first_tx_ns,
            stimulus_end_ns,
            recovery_end_ns,
            correlation_window_ns,
            load_rx_error_events(rx_path),
            int(args.reaction_window_ms * 1_000_000),
            args.rate_ratio_low,
            args.rate_ratio_high,
        )
        bus_result["path"] = str(rx_path)
        buses.append(bus_result)

    key = (tx_events[0]["arbitration_id"], tx_events[0]["is_extended_id"])
    result = {
        "record_type": "fuzz_response_analysis",
        "generated_wall_time_ns": time.time_ns(),
        "tx": {
            "path": str(tx_path),
            "frame_count": len(tx_events),
            "restore_frame_count": len(restore_events),
            "can_id": format_can_id(key),
            "first_tx_ns": first_tx_ns,
            "last_tx_ns": last_tx_ns,
            "duration_ms": (last_tx_ns - first_tx_ns) / 1_000_000.0,
        },
        "settings": {
            "baseline_seconds": args.baseline_seconds,
            "response_seconds": args.response_seconds,
            "recovery_seconds": args.recovery_seconds,
            "correlation_window_ms": args.correlation_window_ms,
            "reaction_window_ms": args.reaction_window_ms,
            "rate_ratio_low": args.rate_ratio_low,
            "rate_ratio_high": args.rate_ratio_high,
            "dbc": str(args.dbc.expanduser().resolve()) if args.dbc else None,
        },
        "buses": buses,
    }

    reserved_output = False
    if args.output:
        output = args.output.expanduser().resolve()
    else:
        output = reserve_output_path(
            (tx_path.parent / "fuzz_response.json").resolve(), "numbered"
        )
        reserved_output = True
    report_path = output.with_suffix(".md")
    for path in (output, report_path):
        if path.exists() and not (reserved_output and path == output):
            raise FileExistsError(f"출력 파일이 이미 존재합니다: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w" if reserved_output else "x", encoding="utf-8") as handle:
        json.dump(json_safe(result), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(markdown_report(result))

    print(f"[DONE] TX={len(tx_events)} frames, buses={len(buses)}")
    for bus in buses:
        direct = bus["direct_correlation"]
        print(
            f"[{bus['bus']}] candidates={len(bus['candidates'])}, "
            f"direct={direct['matched_tx_count']}, novel_direct={direct['novel_matched_tx_count']}"
        )
    print(f"[JSON] {output}")
    print(f"[REPORT] {report_path}")
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
