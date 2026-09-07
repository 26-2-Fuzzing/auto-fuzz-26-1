#!/usr/bin/env python3
"""Raw CAN recorder with optional DBC decoding and change display."""

from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from can_common import (
    ConfigurationError,
    find_signal_messages,
    hostname,
    json_safe,
    load_dbc,
    load_yaml_config,
    now_fields,
    open_can_bus,
    parse_int,
    reserve_output_path,
    resolve_path,
    shutdown_bus,
    socketcan_error_details,
    write_jsonl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="모든 CAN 프레임을 JSONL로 저장하고 선택한 DBC 신호를 표시합니다."
    )
    parser.add_argument("--config", help="receiver YAML 설정 파일")
    parser.add_argument("--bus-name", help="로그에 기록할 논리 버스 이름 (b_can/i_can/c_can)")
    parser.add_argument("--channel", help="SocketCAN 채널 (기본: can0)")
    parser.add_argument("--interface", dest="interface_name", help="python-can interface (기본: socketcan)")
    parser.add_argument("--dbc", help="DBC 파일 경로; 생략하면 raw 기록만 수행")
    parser.add_argument("--output", help="출력 JSONL 경로")
    parser.add_argument(
        "--output-policy", choices=("append", "numbered", "fail"),
        help="append, 기존 파일 거부(fail), 또는 NAME_1.jsonl 방식(numbered)",
    )
    parser.add_argument("--experiment-id", help="여러 버스/TX 로그를 묶는 실험 식별자")
    parser.add_argument("--duration", type=float, help="수신 시간(초); 0이면 Ctrl+C까지 계속")
    parser.add_argument("--watch-id", action="append", default=[], help="표시/해석할 CAN ID; 반복 가능")
    parser.add_argument("--watch-message", action="append", default=[], help="표시/해석할 DBC 메시지 이름")
    parser.add_argument("--watch-signal", action="append", default=[], help="변화를 표시할 DBC 신호 이름")
    parser.add_argument(
        "--print-mode",
        choices=("all", "decoded", "changes", "errors", "none"),
        help="화면 출력 모드",
    )
    parser.add_argument("--watch-only", action="store_true", help="watch 대상 ID만 JSONL에 저장")
    parser.add_argument("--decode-all", action="store_true", help="DBC에 존재하는 모든 ID를 해석")
    parser.add_argument("--no-report", action="store_true", help="회차별 Markdown 요약 보고서 생성 안 함")
    return parser


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def choose(cli_value: Any, config_value: Any, default: Any) -> Any:
    return cli_value if cli_value is not None else (config_value if config_value is not None else default)


def validate_runtime_number(
    value: float,
    field_name: str,
    minimum: float = 0.0,
    allow_equal: bool = True,
) -> None:
    if not math.isfinite(value):
        raise ConfigurationError(f"{field_name}은(는) 유한한 숫자여야 합니다.")
    invalid = value < minimum if allow_equal else value <= minimum
    if invalid:
        comparator = "이상" if allow_equal else "초과"
        raise ConfigurationError(f"{field_name}은(는) {minimum:g} {comparator}이어야 합니다.")


def resolve_watch(
    database: Any,
    raw_ids: List[Any],
    message_names: List[str],
    signal_names: List[str],
) -> Tuple[Set[int], Set[str]]:
    ids = {parse_int(value, "watch ID") for value in raw_ids}
    signals = set(signal_names)

    if database is None and (message_names or signal_names):
        raise ConfigurationError("watch-message/watch-signal을 사용하려면 DBC가 필요합니다.")
    if database is None:
        return ids, signals

    for name in message_names:
        try:
            ids.add(database.get_message_by_name(name).frame_id)
        except KeyError as exc:
            raise ConfigurationError(f"DBC에 watch 메시지 '{name}'가 없습니다.") from exc

    found = find_signal_messages(database, signals)
    missing = sorted(name for name, messages in found.items() if not messages)
    if missing:
        raise ConfigurationError(f"DBC에 watch 신호가 없습니다: {', '.join(missing)}")
    for messages in found.values():
        ids.update(message.frame_id for message in messages)
    return ids, signals


def frame_record(
    message: Any,
    bus_name: str,
    channel: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    record = {
        "record_type": "can_rx",
        **now_fields(),
        "host": hostname(),
        "bus": bus_name,
        "channel": channel,
        "session_id": session_id,
        "can_timestamp": getattr(message, "timestamp", None),
        "arbitration_id": int(message.arbitration_id),
        "arbitration_id_hex": f"0x{message.arbitration_id:X}",
        "dlc": int(message.dlc),
        "data_hex": bytes(message.data).hex().upper(),
        "is_extended_id": bool(message.is_extended_id),
        "is_remote_frame": bool(message.is_remote_frame),
        "is_error_frame": bool(message.is_error_frame),
        "is_fd": bool(getattr(message, "is_fd", False)),
        "bitrate_switch": bool(getattr(message, "bitrate_switch", False)),
        "error_state_indicator": bool(getattr(message, "error_state_indicator", False)),
    }
    is_rx = getattr(message, "is_rx", None)
    if is_rx is not None:
        record["direction"] = "rx" if is_rx else "tx"
    return record


def display_frame(record: Dict[str, Any], values: Optional[Dict[str, Any]] = None) -> None:
    prefix = f"[{record['wall_time']}] [{record['bus']}] {record['arbitration_id_hex']}"
    raw = f"DLC={record['dlc']} DATA={record['data_hex']}"
    if values is None:
        print(f"{prefix} {raw}")
    else:
        rendered = ", ".join(f"{name}={value}" for name, value in values.items())
        print(f"{prefix} {raw} | {record.get('message_name', '?')} | {rendered}")


def capture_markdown_report(
    *,
    bus_name: str,
    channel: str,
    session_id: str,
    experiment_id: Optional[str],
    jsonl_path: Path,
    duration_seconds: float,
    total: int,
    logged: int,
    decoded_count: int,
    decode_errors: int,
    id_counts: Counter[Tuple[int, bool]],
    id_payloads: Dict[Tuple[int, bool], Set[str]],
    id_first_ns: Dict[Tuple[int, bool], int],
    id_last_ns: Dict[Tuple[int, bool], int],
    can_errors: List[Dict[str, Any]],
    signal_transitions: List[Dict[str, Any]],
    max_capture_lag_ns: Optional[int],
) -> str:
    if can_errors:
        verdict = "CAN 오류 관측"
    elif decode_errors:
        verdict = "DBC 해석 오류 관측"
    elif signal_transitions:
        verdict = "Watch 신호 변화 관측"
    elif total == 0:
        verdict = "데이터 불충분(수신 프레임 없음)"
    else:
        verdict = "특이사항 없음(이 캡처의 watch/오류 기준)"

    rate = total / duration_seconds if duration_seconds > 0 else 0.0
    lines = [
        f"# {bus_name} 모니터링 보고서",
        "",
        "## 한눈에 보는 결론",
        "",
        f"**{verdict}**",
        "",
        f"- JSONL: `{jsonl_path.name}`",
        f"- Session: `{session_id}`",
        f"- Experiment: `{experiment_id or '-'}`",
        f"- Channel: `{channel}`",
        f"- Duration: {duration_seconds:.3f}초",
        f"- Frames: received {total:,}, logged {logged:,} ({rate:.1f} frame/s)",
        f"- IDs: {len(id_counts)}, decoded {decoded_count:,}, decode errors {decode_errors:,}",
        f"- CAN error frames: {len(can_errors):,}",
        (
            f"- Max logger lag: {max_capture_lag_ns / 1_000_000:.3f} ms"
            if max_capture_lag_ns is not None else "- Max logger lag: -"
        ),
        "",
        "## Watch 신호 변화",
        "",
    ]
    if signal_transitions:
        lines.extend([
            "| Time | CAN ID | Message | Signal | Before | After | Payload |",
            "|---|---|---|---|---:|---:|---|",
        ])
        for item in signal_transitions[:100]:
            lines.append(
                f"| {item['wall_time']} | `{item['can_id']}` | {item['message']} | "
                f"`{item['signal']}` | {item['before']} | {item['after']} | "
                f"`{item['data_hex']}` |"
            )
        if len(signal_transitions) > 100:
            lines.append(f"\n상위 100건만 표시했습니다(전체 {len(signal_transitions)}건).")
    else:
        lines.append("관찰된 watch 신호 변화가 없습니다.")

    lines.extend(["", "## CAN 오류", ""])
    if can_errors:
        lines.extend([
            "| Time | Error classes | Controller status | Severity |",
            "|---|---|---|---|",
        ])
        for item in can_errors[:100]:
            details = item["details"]
            lines.append(
                f"| {item['wall_time']} | {', '.join(details.get('classes', [])) or '-'} | "
                f"{', '.join(details.get('controller_status', [])) or '-'} | "
                f"{details.get('severity', '-')} |"
            )
    else:
        lines.append("CAN error frame이 관찰되지 않았습니다.")

    lines.extend([
        "",
        "## 트래픽 상위 ID",
        "",
        "| CAN ID | Frames | Rate | Unique payloads |",
        "|---|---:|---:|---:|",
    ])
    for key, count in id_counts.most_common(30):
        frame_id, extended = key
        can_id = f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"
        span_ns = id_last_ns.get(key, 0) - id_first_ns.get(key, 0)
        id_rate = (count - 1) * 1_000_000_000 / span_ns if count > 1 and span_ns > 0 else 0.0
        lines.append(
            f"| `{can_id}` | {count:,} | {id_rate:.2f} Hz | "
            f"{len(id_payloads.get(key, set())):,} |"
        )

    lines.extend([
        "",
        "## 판정 범위",
        "",
        "이 보고서는 한 회차의 캡처 건전성, CAN 오류, watch 신호 변화와 트래픽을 자동 요약합니다. "
        "주입과의 인과관계 및 다른 ID의 반응 여부는 같은 회차 TX manifest를 "
        "`analyze_fuzz_response.py`로 상관분석한 보고서에서 최종 판단해야 합니다. "
        "CAN 신호 변화만으로 실제 램프·액추에이터 동작을 증명할 수 없으므로 물리 관찰도 함께 남기십시오.",
        "",
    ])
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    config, config_path = load_yaml_config(args.config)
    bus_cfg = config.get("bus", {})
    rx_cfg = config.get("receiver", {})
    watch_cfg = rx_cfg.get("watch", {})

    interface_name = choose(args.interface_name, bus_cfg.get("interface"), "socketcan")
    channel = choose(args.channel, bus_cfg.get("channel"), "can0")
    bus_name = choose(args.bus_name, rx_cfg.get("bus_name"), channel)
    duration = float(choose(args.duration, rx_cfg.get("duration_seconds"), 0.0))
    recv_timeout = float(rx_cfg.get("recv_timeout_seconds", 1.0))
    stats_interval = float(rx_cfg.get("stats_interval_seconds", 5.0))
    flush_every = max(1, int(rx_cfg.get("flush_every", 50)))
    print_mode = choose(args.print_mode, rx_cfg.get("print_mode"), "changes")
    decode_all = bool(args.decode_all or rx_cfg.get("decode_all", False))
    log_all = not args.watch_only and bool(rx_cfg.get("log_all", True))
    write_report = not args.no_report and bool(rx_cfg.get("write_report", True))
    output_policy = choose(args.output_policy, rx_cfg.get("output_policy"), "append")
    experiment_id_value = choose(
        args.experiment_id, rx_cfg.get("experiment_id"), None
    )
    experiment_id = str(experiment_id_value) if experiment_id_value else None

    validate_runtime_number(duration, "duration_seconds")
    validate_runtime_number(recv_timeout, "recv_timeout_seconds", allow_equal=False)
    validate_runtime_number(stats_interval, "stats_interval_seconds")
    if print_mode not in {"all", "decoded", "changes", "errors", "none"}:
        raise ConfigurationError(f"지원하지 않는 print_mode입니다: {print_mode}")

    dbc_path = (
        resolve_path(args.dbc, None)
        if args.dbc is not None
        else resolve_path(rx_cfg.get("dbc"), config_path)
    )
    database = load_dbc(dbc_path) if dbc_path else None

    configured_ids = as_list(watch_cfg.get("ids"))
    configured_messages = as_list(watch_cfg.get("messages"))
    configured_signals = as_list(watch_cfg.get("signals"))
    watch_ids, watch_signals = resolve_watch(
        database,
        configured_ids + args.watch_id,
        configured_messages + args.watch_message,
        configured_signals + args.watch_signal,
    )

    if args.output is not None:
        output_path = resolve_path(args.output, None)
    elif rx_cfg.get("output"):
        output_path = resolve_path(rx_cfg.get("output"), config_path)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = config_path.parent if config_path else Path.cwd()
        output_path = (base / "logs" / f"{bus_name}_{stamp}.jsonl").resolve()
    assert output_path is not None
    output_path = reserve_output_path(output_path, output_policy)
    report_path = output_path.with_suffix(".md")
    if report_path.exists():
        report_path = output_path.with_name(
            f"{output_path.stem}_{uuid.uuid4().hex[:8]}.md"
        )

    print(f"[START] bus={bus_name}, interface={interface_name}, channel={channel}")
    print(f"[LOG]   {output_path}")
    print(f"[LOG]   policy={output_policy}, experiment_id={experiment_id or '-'}")
    print(f"[REPORT] {report_path if write_report else '생성 안 함'}")
    print(f"[DBC]   {dbc_path if dbc_path else '사용 안 함(raw only)'}")
    if watch_ids:
        print("[WATCH] " + ", ".join(f"0x{value:X}" for value in sorted(watch_ids)))
    else:
        print("[WATCH] ID 제한 없음")
    print("[STOP]  Ctrl+C")

    bus = None
    session_id = uuid.uuid4().hex
    started_mono = time.monotonic()
    last_stats = started_mono
    total = logged = decoded_count = decode_errors = 0
    error_frames = 0
    id_counts: Counter[Tuple[int, bool]] = Counter()
    id_payloads: Dict[Tuple[int, bool], Set[str]] = defaultdict(set)
    id_first_ns: Dict[Tuple[int, bool], int] = {}
    id_last_ns: Dict[Tuple[int, bool], int] = {}
    frame_type_counts: Counter[str] = Counter()
    can_errors: List[Dict[str, Any]] = []
    signal_transitions: List[Dict[str, Any]] = []
    report_last_values: Dict[Tuple[int, str], Any] = {}
    max_capture_lag_ns: Optional[int] = None
    last_values: Dict[Tuple[int, str], Any] = {}
    stop_requested = False
    previous_sigterm = None

    def request_stop(signum: int, frame: Any) -> None:
        del signum, frame
        nonlocal stop_requested
        stop_requested = True

    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_stop)

    def session_end_record(reason: str) -> Dict[str, Any]:
        return {
            "record_type": "session_end",
            "schema_version": 2,
            **now_fields(),
            "host": hostname(),
            "bus": bus_name,
            "session_id": session_id,
            "experiment_id": experiment_id,
            "received": total,
            "logged": logged,
            "decoded": decoded_count,
            "decode_errors": decode_errors,
            "can_error_frames": error_frames,
            "frame_types": dict(frame_type_counts),
            "unique_ids": len(id_counts),
            "observed_id_counts": {
                (f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"): count
                for (frame_id, extended), count in sorted(
                    id_counts.items(), key=lambda item: (item[0][1], item[0][0])
                )
            },
            "max_capture_lag_ns": max_capture_lag_ns,
            "report": str(report_path) if write_report else None,
            "duration_seconds": time.monotonic() - started_mono,
            "reason": reason,
        }

    try:
        bus = open_can_bus(interface_name, channel, receive_own_messages=False)
        with output_path.open("a", encoding="utf-8") as handle:
            write_jsonl(
                handle,
                {
                    "record_type": "session_start",
                    "schema_version": 2,
                    **now_fields(),
                    "host": hostname(),
                    "bus": bus_name,
                    "channel": channel,
                    "interface": interface_name,
                    "session_id": session_id,
                    "experiment_id": experiment_id,
                    "dbc": str(dbc_path) if dbc_path else None,
                    "output": str(output_path),
                    "report": str(report_path) if write_report else None,
                    "output_policy": output_policy,
                    "capture_options": {
                        "log_all": log_all,
                        "decode_all": decode_all,
                        "flush_every": flush_every,
                        "recv_timeout_seconds": recv_timeout,
                    },
                    "watch_ids": [f"0x{value:X}" for value in sorted(watch_ids)],
                    "watch_signals": sorted(watch_signals),
                },
            )
            handle.flush()

            while (
                not stop_requested
                and (duration <= 0 or (time.monotonic() - started_mono) < duration)
            ):
                message = bus.recv(timeout=recv_timeout)
                now_mono = time.monotonic()
                if message is None:
                    if stats_interval > 0 and now_mono - last_stats >= stats_interval:
                        print(f"[STATS] received={total}, logged={logged}, decoded={decoded_count}, errors={decode_errors}")
                        last_stats = now_mono
                    continue

                total += 1
                frame_id = int(message.arbitration_id)
                watched = not watch_ids or frame_id in watch_ids
                record = frame_record(message, bus_name, channel, session_id)
                record["experiment_id"] = experiment_id
                record["rx_sequence"] = total
                if message.is_error_frame:
                    error_frames += 1
                    frame_type_counts["error"] += 1
                    record["can_error"] = socketcan_error_details(
                        frame_id, bytes(message.data)
                    )
                    can_errors.append({
                        "wall_time": record["wall_time"],
                        "details": record["can_error"],
                    })
                else:
                    frame_key = (frame_id, bool(message.is_extended_id))
                    id_counts[frame_key] += 1
                    id_payloads[frame_key].add(record["data_hex"])
                    id_first_ns.setdefault(frame_key, record["wall_time_ns"])
                    id_last_ns[frame_key] = record["wall_time_ns"]
                    frame_type_counts[
                        "extended" if message.is_extended_id else "standard"
                    ] += 1
                if message.is_remote_frame:
                    frame_type_counts["remote"] += 1
                if getattr(message, "is_fd", False):
                    frame_type_counts["fd"] += 1
                can_timestamp = record.get("can_timestamp")
                if isinstance(can_timestamp, (int, float)) and math.isfinite(can_timestamp):
                    capture_lag_ns = record["wall_time_ns"] - int(can_timestamp * 1e9)
                    record["capture_lag_ns"] = capture_lag_ns
                    max_capture_lag_ns = (
                        capture_lag_ns if max_capture_lag_ns is None
                        else max(max_capture_lag_ns, capture_lag_ns)
                    )
                decoded_values: Optional[Dict[str, Any]] = None
                display_values: Optional[Dict[str, Any]] = None

                if (
                    database is not None
                    and not message.is_error_frame
                    and not message.is_remote_frame
                    and (decode_all or frame_id in watch_ids)
                ):
                    try:
                        definition = database.get_message_by_frame_id(frame_id)
                        raw_decoded = definition.decode(
                            bytes(message.data), decode_choices=False, scaling=True
                        )
                        decoded_values = {
                            key: json_safe(value) for key, value in raw_decoded.items()
                        }
                        record["message_name"] = definition.name
                        record["signals"] = decoded_values
                        decoded_count += 1
                        if watch_signals:
                            display_values = {
                                key: value for key, value in decoded_values.items() if key in watch_signals
                            }
                        else:
                            display_values = decoded_values
                        for name, value in (display_values or {}).items():
                            transition_key = (frame_id, name)
                            if (
                                transition_key in report_last_values
                                and report_last_values[transition_key] != value
                            ):
                                signal_transitions.append({
                                    "wall_time": record["wall_time"],
                                    "can_id": record["arbitration_id_hex"],
                                    "message": definition.name,
                                    "signal": name,
                                    "before": report_last_values[transition_key],
                                    "after": value,
                                    "data_hex": record["data_hex"],
                                })
                            report_last_values[transition_key] = value
                    except KeyError:
                        record["dbc_status"] = "unknown_id"
                    except Exception as exc:
                        decode_errors += 1
                        record["dbc_status"] = "decode_error"
                        record["decode_error"] = f"{type(exc).__name__}: {exc}"

                if log_all or watched:
                    write_jsonl(handle, record)
                    logged += 1
                    if logged % flush_every == 0:
                        handle.flush()

                if print_mode == "all":
                    display_frame(record, display_values)
                elif print_mode == "decoded" and decoded_values is not None and watched:
                    display_frame(record, display_values)
                elif print_mode == "errors" and record.get("dbc_status") == "decode_error":
                    display_frame(record)
                    print(f"         DECODE ERROR: {record['decode_error']}")
                elif print_mode == "changes" and display_values is not None and watched:
                    changed: Dict[str, Any] = {}
                    for name, value in display_values.items():
                        key = (frame_id, name)
                        if key not in last_values or last_values[key] != value:
                            changed[name] = value
                        last_values[key] = value
                    if changed:
                        display_frame(record, changed)

                if stats_interval > 0 and now_mono - last_stats >= stats_interval:
                    print(f"[STATS] received={total}, logged={logged}, decoded={decoded_count}, errors={decode_errors}")
                    last_stats = now_mono

            write_jsonl(
                handle,
                session_end_record("remote_stop" if stop_requested else "duration_complete"),
            )
            handle.flush()
    except KeyboardInterrupt:
        print("\n[STOP] 사용자 중지")
        with output_path.open("a", encoding="utf-8") as handle:
            write_jsonl(handle, session_end_record("user_interrupt"))
            handle.flush()
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if bus is not None:
            shutdown_bus(bus)

    if write_report:
        report_text = capture_markdown_report(
            bus_name=bus_name,
            channel=channel,
            session_id=session_id,
            experiment_id=experiment_id,
            jsonl_path=output_path,
            duration_seconds=time.monotonic() - started_mono,
            total=total,
            logged=logged,
            decoded_count=decoded_count,
            decode_errors=decode_errors,
            id_counts=id_counts,
            id_payloads=id_payloads,
            id_first_ns=id_first_ns,
            id_last_ns=id_last_ns,
            can_errors=can_errors,
            signal_transitions=signal_transitions,
            max_capture_lag_ns=max_capture_lag_ns,
        )
        with report_path.open("x", encoding="utf-8") as handle:
            handle.write(report_text)

    print(
        f"[DONE] received={total}, logged={logged}, decoded={decoded_count}, "
        f"decode_errors={decode_errors}, can_errors={error_frames}"
    )
    print(f"[DONE] log={output_path}")
    if write_report:
        print(f"[DONE] report={report_path}")
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
