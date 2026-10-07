#!/usr/bin/env python3
"""Bounded CAN transmitter supporting raw frames and DBC signal patching."""

from __future__ import annotations

import argparse
import json
import math
import random
import secrets
import signal
import sys
import time
import uuid
from collections import Counter
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from threading import current_thread, main_thread
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from can_common import (
    ConfigurationError,
    hostname,
    load_dbc,
    load_yaml_config,
    now_fields,
    open_can_bus,
    parse_assignment,
    parse_can_data,
    parse_int,
    protected_signal_names,
    reserve_output_path,
    require_module,
    resolve_message,
    resolve_path,
    shutdown_bus,
    signal_defaults,
    validate_frame_id,
    write_jsonl,
)
from mutation_engine import Mutator
from mutation_feedback import generate_guided_mutations, load_feedback_hints


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="지정 CAN 버스에 제한된 횟수 또는 시간 동안 raw/DBC 기반 프레임을 송신합니다."
    )
    parser.add_argument("--config", help="sender YAML 설정 파일")
    parser.add_argument("--channel", help="SocketCAN 채널 (기본: can0)")
    parser.add_argument("--bus-name", help="TX manifest에 기록할 논리 source bus")
    parser.add_argument("--interface", dest="interface_name", help="python-can interface (기본: socketcan)")
    parser.add_argument("--dbc", help="DBC 파일 경로")
    parser.add_argument("--id", dest="frame_id", help="CAN ID (예: 0x65A)")
    parser.add_argument("--message", help="DBC 메시지 이름 (예: BCM_01)")
    parser.add_argument("--data", help="raw 송신 payload (예: '00 01 02 03 04 05 06 07')")
    parser.add_argument("--set", dest="assignments", action="append", default=[], help="DBC 신호 설정 SIGNAL=VALUE")
    parser.add_argument("--base", choices=("live", "zero", "data"), help="DBC patch의 기준 payload")
    parser.add_argument("--base-data", help="--base data일 때 기준 payload")
    parser.add_argument("--base-timeout", type=float, help="live 기준 프레임 대기 시간(초)")
    parser.add_argument("--count", type=int, help="payload 수(시간 송신 시 순환할 corpus 크기)")
    parser.add_argument("--duration", type=float, help="반복 송신 시간(초); payload 목록을 순환 송신")
    parser.add_argument("--interval-ms", type=float, help="송신 간격(ms)")
    parser.add_argument("--output", help="송신 기록 JSONL 경로")
    parser.add_argument(
        "--output-policy", choices=("append", "numbered", "fail"),
        help="append, 기존 파일 거부(fail), 또는 NAME_1.jsonl 방식(numbered)",
    )
    parser.add_argument("--experiment-id", help="RX 로그와 공유할 실험 식별자")
    parser.add_argument("--extended", action="store_true", help="raw ID를 29-bit extended로 송신")
    parser.add_argument("--fd", action="store_true", help="raw payload를 CAN FD 프레임으로 송신")
    parser.add_argument("--no-restore", action="store_true", help="DBC patch 후 원본 payload 복원 송신 안 함")
    parser.add_argument("--allow-protected", action="store_true", help="CRC/counter 추정 신호가 있는 DBC 메시지 patch 허용")
    parser.add_argument("--mutate", action="store_true", help="pi_can_lab Mutator로 payload mutation 생성")
    parser.add_argument("--max-operations", type=int, help="mutation payload 하나당 최대 연산 수")
    parser.add_argument("--allow-dlc-change", action="store_true", help="mutation 중 DLC 변경 허용")
    parser.add_argument("--include-original", action="store_true", help="mutation 목록 첫 항목에 seed payload 포함")
    parser.add_argument("--random-seed", type=int, help="재현 가능한 mutation 난수 seed")
    parser.add_argument("--mutation-data", help="Trial runner가 선택한 단일 mutation payload")
    parser.add_argument(
        "--trial-contract-version", type=int, choices=(1,),
        help="runner와 sender의 보수적 안전 계약 버전 (현재 1만 지원)",
    )
    parser.add_argument(
        "--control-noop", action="store_true",
        help="campaign 비교 구간에 원본 payload만 송신하는 명시적 대조 trial",
    )
    parser.add_argument("--mutation-id", type=int, help="Trial mutation 고유 ID")
    parser.add_argument("--mutation-uid", help="사람이 추적할 MUT-000001 형식 ID")
    parser.add_argument("--parent-mutation-id", type=int, help="Feedback parent mutation ID")
    parser.add_argument("--mutation-operator", help="명시적 Trial mutation operator")
    parser.add_argument(
        "--mutation-metadata-json",
        help="Trial runner가 전달하는 signal/undefined/sequence provenance JSON",
    )
    parser.add_argument("--generation-reason", help="Feedback 기반 mutation 생성 이유")
    parser.add_argument("--feedback", help="이전 analyze_fuzz_response.py JSON 결과")
    parser.add_argument("--guided-ratio", type=float, help="guided mutation 비율(0~1)")
    parser.add_argument(
        "--bit-operation-ratio", type=float,
        help="random exploration의 bit 단위 연산 비율(0~1)",
    )
    parser.add_argument(
        "--mutation-seed-source", choices=("normal", "patched"),
        help="mutation seed로 live 원본(normal) 또는 DBC patch 결과(patched) 사용",
    )
    parser.add_argument("--baseline-duration", type=float, help="campaign 송신 전 passive baseline 관찰 시간(초)")
    parser.add_argument("--normal-duration", type=float, help="campaign 정상 payload 송신 시간(초)")
    parser.add_argument("--mutation-duration", type=float, help="campaign mutation payload 송신 시간(초)")
    parser.add_argument("--recovery-duration", type=float, help="campaign 송신 종료 후 passive recovery 관찰 시간(초)")
    parser.add_argument("--execute", action="store_true", help="실제로 CAN 버스에 송신 (없으면 preview만 수행)")
    return parser


def choose(cli_value: Any, config_value: Any, default: Any) -> Any:
    return cli_value if cli_value is not None else (config_value if config_value is not None else default)


def capture_live_payload(
    bus: Any,
    frame_id: int,
    is_extended: bool,
    timeout: float,
    sample_count: int = 1,
    min_mode_ratio: float = 1.0,
) -> bytes:
    if sample_count < 1:
        raise ConfigurationError("base sample_count는 1 이상이어야 합니다.")
    if not math.isfinite(min_mode_ratio) or not 0.0 < min_mode_ratio <= 1.0:
        raise ConfigurationError("base min_mode_ratio는 0 초과 1 이하여야 합니다.")
    print(
        f"[BASE] 0x{frame_id:X} 원본 프레임 {sample_count}개를 "
        f"최대 {timeout:.1f}초 기다립니다..."
    )
    deadline = time.monotonic() + timeout
    samples: List[bytes] = []
    while time.monotonic() < deadline and len(samples) < sample_count:
        message = bus.recv(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
        if message is None:
            continue
        if int(message.arbitration_id) == frame_id and bool(message.is_extended_id) == is_extended:
            samples.append(bytes(message.data))
    if len(samples) < sample_count:
        raise RuntimeError(
            f"{timeout:.1f}초 동안 CAN ID 0x{frame_id:X}를 "
            f"{sample_count}개 중 {len(samples)}개만 수신했습니다. "
            "버스/bitrate/DBC가 맞는지 확인하세요."
        )
    payload, occurrences = Counter(samples).most_common(1)[0]
    mode_ratio = occurrences / len(samples)
    if mode_ratio < min_mode_ratio:
        raise RuntimeError(
            f"CAN ID 0x{frame_id:X} baseline이 불안정합니다: "
            f"mode_ratio={mode_ratio:.3f} < {min_mode_ratio:.3f}. "
            "다른 송신/fuzzer가 없는 상태에서 다시 수집하세요."
        )
    print(
        f"[BASE] 수신 완료: {payload.hex().upper()} "
        f"(mode={occurrences}/{len(samples)})"
    )
    return payload


def validate_length(payload: bytes, is_fd: bool, expected: Optional[int] = None) -> None:
    maximum = 64 if is_fd else 8
    if len(payload) > maximum:
        raise ConfigurationError(
            f"payload 길이 {len(payload)}는 {'CAN FD' if is_fd else 'Classic CAN'} 최대 {maximum}바이트를 초과합니다."
        )
    if expected is not None and len(payload) != expected:
        raise ConfigurationError(f"DBC 메시지는 {expected}바이트지만 payload는 {len(payload)}바이트입니다.")


def validate_finite(
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


def optional_positive_limit(config: Dict[str, Any], name: str, *, integer: bool = False) -> Optional[float | int]:
    """Absent campaign limits retain compatibility; configured limits fail closed."""
    value = config.get(name)
    if value is None:
        return None
    if integer:
        if isinstance(value, bool) or not str(value).isdigit():
            raise ConfigurationError(f"{name}은(는) 양의 정수여야 합니다.")
        result = int(value)
        if result < 1:
            raise ConfigurationError(f"{name}은(는) 양의 정수여야 합니다.")
        return result
    result = float(value)
    validate_finite(result, name, allow_equal=False)
    return result


def planned_frame_count(duration_seconds: float, interval_ms: float) -> int:
    """Upper bound for periodic sends at t=0, interval, ... before the deadline."""
    if interval_ms <= 0:
        raise ConfigurationError("반복 송신 interval_ms는 0보다 커야 합니다.")
    return int((
        Decimal(str(duration_seconds)) * Decimal(1000) / Decimal(str(interval_ms))
    ).to_integral_value(rounding=ROUND_CEILING))


class SenderInterrupted(RuntimeError):
    """Raised by the SIGTERM handler so the normal abort/restore path runs."""


def mutation_summary(base_payload: bytes, payload: bytes) -> Dict[str, Any]:
    overlap = min(len(base_payload), len(payload))
    xor_bytes = bytes(
        base_payload[index] ^ payload[index]
        for index in range(overlap)
    )
    changed = [index for index, value in enumerate(xor_bytes) if value]
    changed.extend(range(overlap, max(len(base_payload), len(payload))))
    return {
        "length_delta": len(payload) - len(base_payload),
        "changed_byte_indexes": changed,
        "xor_hex": xor_bytes.hex().upper(),
        "changed_bit_count": sum(value.bit_count() for value in xor_bytes),
    }


def resolve_random_seed(configured_seed: Optional[int]) -> Tuple[int, bool]:
    """Return a reproducible seed, generating a fresh one for each run if omitted."""
    if configured_seed is not None:
        return configured_seed, False
    return secrets.randbits(64), True


def transmission_schedule(
    payloads: Sequence[bytes],
    interval_seconds: float,
    duration_seconds: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    deadline_monotonic: Optional[float] = None,
    send_start_clock: Optional[Callable[[], Optional[float]]] = None,
) -> Iterator[Tuple[int, bytes]]:
    """Yield payloads at a minimum interval measured from each send start."""
    if not payloads:
        return

    deadline = (
        deadline_monotonic if deadline_monotonic is not None
        else clock() + duration_seconds if duration_seconds is not None else None
    )
    sequence = 0
    previous_yield = None
    # Floating-point clock increments can otherwise add an extra frame exactly
    # at the nominal deadline (e.g. 0.1 + 4 * 0.05 versus 0.1 + 0.2).
    while True:
        if deadline is None and sequence >= len(payloads):
            break

        if sequence and interval_seconds > 0:
            send_start = send_start_clock() if send_start_clock is not None else None
            anchor = send_start if send_start is not None else previous_yield
            assert anchor is not None
            remaining_wait = max(0.0, anchor + interval_seconds - clock())
            if deadline is not None:
                remaining_wait = min(remaining_wait, max(0.0, deadline - clock()))
            if remaining_wait:
                sleeper(remaining_wait)

        if deadline is not None and sequence and clock() + 1e-12 >= deadline:
            break

        previous_yield = clock()

        yield sequence + 1, payloads[sequence % len(payloads)]
        sequence += 1

        if deadline is not None and previous_yield + interval_seconds >= deadline:
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(remaining)
            break


def generate_mutations(
    base_payload: bytes,
    count: int,
    max_operations: int,
    allow_dlc_change: bool,
    include_original: bool,
    random_seed: Optional[int],
    bit_operation_ratio: Optional[float] = None,
) -> List[bytes]:
    return [
        payload
        for payload, _ in generate_mutation_entries(
            base_payload,
            count,
            max_operations,
            allow_dlc_change,
            include_original,
            random_seed,
            bit_operation_ratio,
        )
    ]


def generate_mutation_entries(
    base_payload: bytes,
    count: int,
    max_operations: int,
    allow_dlc_change: bool,
    include_original: bool,
    random_seed: Optional[int],
    bit_operation_ratio: Optional[float] = None,
) -> List[Tuple[bytes, Dict[str, Any]]]:
    """Generate payloads together with mutation operator provenance."""
    if max_operations < 1:
        raise ConfigurationError("max_operations는 1 이상이어야 합니다.")

    # The upstream Mutator may emit the unchanged base after a no-op. Request one
    # spare result and filter it explicitly when include_original is false.
    requested_budget = count if include_original else count + 1
    weights = {
        "manager.budget": requested_budget,
        "manager.max_ops": max_operations,
        "manager.structural": allow_dlc_change,
        "manager.include_original": include_original,
    }
    if bit_operation_ratio is not None:
        weights["manager.bit_operation_ratio"] = bit_operation_ratio
    random_state = random.getstate()
    try:
        if random_seed is not None:
            random.seed(random_seed)
        mutator = Mutator(
            data=base_payload,
            weights=weights,
            min_length=1,
        )
        generated = mutator.mutate_manager()
        traces = mutator.generated_operators
    finally:
        random.setstate(random_state)

    entries: List[Tuple[bytes, Dict[str, Any]]] = []
    seen: set[bytes] = set()
    for payload, operators in zip(generated, traces):
        item = bytes(payload)
        if not allow_dlc_change and len(item) != len(base_payload):
            continue
        if not include_original and item == base_payload:
            continue
        if item not in seen:
            seen.add(item)
            entries.append((
                item,
                {
                    "source": "exploration",
                    "strategy": "random",
                    "operators": list(operators),
                },
            ))
        if len(entries) == count:
            break

    if include_original and base_payload not in seen:
        entries.insert(0, (
            base_payload,
            {
                "source": "exploration",
                "strategy": "original",
                "operators": ["original"],
            },
        ))
        entries = entries[:count]
    if len(entries) != count:
        raise RuntimeError(
            f"요청한 mutation {count}개 중 {len(entries)}개만 생성됐습니다. "
            "count 또는 max_operations를 조정하세요."
        )
    return entries


def load_assignments(configured: Any, command_line: List[str]) -> Dict[str, Any]:
    if configured is None:
        result: Dict[str, Any] = {}
    elif isinstance(configured, dict):
        result = dict(configured)
    else:
        raise ConfigurationError("sender.set은 SIGNAL: VALUE mapping이어야 합니다.")
    for raw in command_line:
        name, value = parse_assignment(raw)
        result[name] = value
    return result


def create_message(
    frame_id: int,
    payload: bytes,
    is_extended: bool,
    is_fd: bool,
    bitrate_switch: bool,
) -> Any:
    can = require_module("can", "python-can")
    return can.Message(
        arbitration_id=frame_id,
        data=payload,
        is_extended_id=is_extended,
        is_fd=is_fd,
        bitrate_switch=bitrate_switch if is_fd else False,
    )


def tx_record(
    bus_name: str,
    channel: str,
    frame_id: int,
    payload: bytes,
    is_extended: bool,
    is_fd: bool,
    status: str,
    sequence: int,
    message_name: Optional[str] = None,
    signals: Optional[Dict[str, Any]] = None,
    kind: str = "inject",
    mutation: Optional[Dict[str, Any]] = None,
    tx_session_id: Optional[str] = None,
    experiment_id: Optional[str] = None,
    phase: Optional[str] = None,
    trial_kind: Optional[str] = None,
) -> Dict[str, Any]:
    record = {
        "record_type": "can_tx",
        "schema_version": 3,
        **now_fields(),
        "host": hostname(),
        "bus": bus_name,
        "channel": channel,
        "tx_session_id": tx_session_id,
        "experiment_id": experiment_id,
        "phase": phase,
        "trial_kind": trial_kind,
        "kind": kind,
        "status": status,
        "sequence": sequence,
        "arbitration_id": frame_id,
        "arbitration_id_hex": f"0x{frame_id:X}",
        "dlc": len(payload),
        "data_hex": payload.hex().upper(),
        "is_extended_id": is_extended,
        "is_fd": is_fd,
        "message_name": message_name,
        "signals": signals,
        "mutation": mutation,
    }
    if mutation is not None:
        # Flat manifest fields make cross-bus joins possible without coupling
        # analyzers to the complete nested mutation schema.
        record.update({
            "mutation_id": mutation.get("mutation_uid") or mutation.get("mutation_id"),
            "mutation_numeric_id": mutation.get("mutation_id"),
            "timestamp": record["wall_time"],
            "timestamp_ns": record["wall_time_ns"],
            "interface": channel,
            "can_id": f"0x{frame_id:X}",
            "payload": payload.hex().upper(),
            "mutation_family": mutation.get("mutation_family"),
            "case": mutation.get("case"),
            "signals_changed": mutation.get("signals_changed", []),
            "undefined_bits_changed": mutation.get("undefined_bits_changed", []),
            "undefined_enum": mutation.get("undefined_enum"),
        })
    return record


def run(args: argparse.Namespace) -> int:
    config, config_path = load_yaml_config(args.config)
    bus_cfg = config.get("bus", {})
    tx_cfg = config.get("sender", {})
    base_cfg = tx_cfg.get("base", {})
    transmit_cfg = tx_cfg.get("transmit", {})
    safety_cfg = tx_cfg.get("safety", {})
    mutation_cfg = tx_cfg.get("mutation", {})
    feedback_cfg = mutation_cfg.get("feedback", {})
    if not isinstance(feedback_cfg, dict):
        raise ConfigurationError("mutation.feedback은 mapping이어야 합니다.")
    campaign_cfg = tx_cfg.get("campaign", {})
    analysis_cfg = tx_cfg.get("analysis", {})

    interface_name = choose(args.interface_name, bus_cfg.get("interface"), "socketcan")
    channel = choose(args.channel, bus_cfg.get("channel"), "can0")
    bus_name = str(choose(args.bus_name, tx_cfg.get("bus_name"), "b_can")).lower()
    count = int(choose(args.count, transmit_cfg.get("count"), 1))
    duration_value = choose(args.duration, transmit_cfg.get("duration_seconds"), None)
    duration_seconds = float(duration_value) if duration_value is not None else None
    interval_ms = float(choose(args.interval_ms, transmit_cfg.get("interval_ms"), 100.0))
    send_timeout = float(transmit_cfg.get("send_timeout_seconds", 1.0))
    max_count = int(safety_cfg.get("max_count", 100))
    max_duration_seconds = float(safety_cfg.get("max_duration_seconds", 30.0))
    min_interval_ms = float(safety_cfg.get("min_interval_ms", 10.0))
    contract_version = args.trial_contract_version
    max_mutation_duration = optional_positive_limit(safety_cfg, "max_mutation_duration_seconds")
    max_mutation_frames = optional_positive_limit(safety_cfg, "max_mutation_frames", integer=True)
    max_normal_frames = optional_positive_limit(safety_cfg, "max_normal_frames", integer=True)
    if contract_version == 1:
        max_mutation_duration = min(max_mutation_duration, 1.0) if max_mutation_duration is not None else 1.0
        max_mutation_frames = min(max_mutation_frames, 20) if max_mutation_frames is not None else 20
        max_normal_frames = min(max_normal_frames, 200) if max_normal_frames is not None else 200
        min_interval_ms = max(min_interval_ms, 50.0)
    restore = bool(transmit_cfg.get("restore_original", True)) and not args.no_restore
    configured_restore_count = int(transmit_cfg.get("restore_count", 1))
    restore_count = max(1, configured_restore_count)
    raw_restore_delay_ms = float(transmit_cfg.get("restore_delay_ms", interval_ms))
    validate_finite(raw_restore_delay_ms, "restore_delay_ms")
    restore_delay_ms = raw_restore_delay_ms
    if contract_version == 1 and (not restore or configured_restore_count != 1):
        raise ConfigurationError("trial contract 1 requires exactly one original restore frame")
    allow_protected = bool(args.allow_protected or safety_cfg.get("allow_protected_dbc_patch", False))
    control_noop = bool(args.control_noop)
    mutation_enabled = bool(args.mutate or mutation_cfg.get("enabled", False) or control_noop)
    trial_kind = "noop" if control_noop else ("mutation" if mutation_enabled else "standard")
    explicit_mutation = parse_can_data(args.mutation_data) if args.mutation_data else None
    max_operations = int(choose(args.max_operations, mutation_cfg.get("max_operations"), 3))
    allow_dlc_change = bool(args.allow_dlc_change or mutation_cfg.get("allow_dlc_change", False))
    include_original = bool(args.include_original or mutation_cfg.get("include_original", False))
    random_seed_value = choose(args.random_seed, mutation_cfg.get("random_seed"), None)
    configured_random_seed = int(random_seed_value) if random_seed_value is not None else None
    random_seed, random_seed_generated = resolve_random_seed(configured_random_seed)
    explicit_metadata: Dict[str, Any] = {}
    if args.mutation_metadata_json:
        try:
            loaded_metadata = json.loads(args.mutation_metadata_json)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("mutation metadata JSON이 올바르지 않습니다.") from exc
        if not isinstance(loaded_metadata, dict):
            raise ConfigurationError("mutation metadata JSON은 object여야 합니다.")
        explicit_metadata = loaded_metadata
    bit_operation_ratio = float(choose(
        args.bit_operation_ratio,
        mutation_cfg.get("bit_operation_ratio"),
        0.75,
    ))
    guided_ratio = float(choose(
        args.guided_ratio,
        feedback_cfg.get("guided_ratio"),
        0.5,
    ))
    feedback_value = (
        args.feedback
        if args.feedback is not None
        else feedback_cfg.get("path")
    )
    feedback_path = (
        resolve_path(feedback_value, None if args.feedback is not None else config_path)
        if feedback_value else None
    )
    if feedback_path is not None and (contract_version == 1 or control_noop):
        raise ConfigurationError(
            "안전 계약/no-op 대조 trial에서는 sender-side feedback mutation을 사용할 수 없습니다."
        )
    campaign_enabled = bool(campaign_cfg.get("enabled", False))
    baseline_duration = float(choose(
        args.baseline_duration, campaign_cfg.get("baseline_duration_seconds"), 10.0
    ))
    normal_duration = float(choose(
        args.normal_duration, campaign_cfg.get("normal_duration_seconds"), 60.0
    ))
    campaign_mutation_value = choose(
        args.mutation_duration,
        campaign_cfg.get("mutation_duration_seconds"),
        duration_seconds if duration_seconds is not None else 60.0,
    )
    mutation_duration = float(campaign_mutation_value)
    recovery_duration = float(choose(
        args.recovery_duration, campaign_cfg.get("recovery_duration_seconds"), 60.0
    ))
    output_policy = choose(args.output_policy, tx_cfg.get("output_policy"), "append")
    experiment_id_value = choose(
        args.experiment_id, tx_cfg.get("experiment_id"), None
    )
    experiment_id = str(experiment_id_value) if experiment_id_value else None
    tx_session_id = uuid.uuid4().hex

    if count < 1 or count > max_count:
        raise ConfigurationError(f"count는 1~{max_count} 범위여야 합니다.")
    if explicit_mutation is not None and count != 1:
        raise ConfigurationError("--mutation-data Trial 모드에서는 --count 1이어야 합니다.")
    if explicit_mutation is not None and not mutation_enabled:
        raise ConfigurationError("--mutation-data를 사용하려면 mutation이 활성화되어야 합니다.")
    if control_noop and not campaign_enabled:
        raise ConfigurationError("--control-noop은 campaign에서만 사용할 수 있습니다.")
    validate_finite(interval_ms, "interval_ms")
    validate_finite(send_timeout, "send_timeout_seconds")
    validate_finite(max_duration_seconds, "max_duration_seconds", allow_equal=False)
    validate_finite(min_interval_ms, "min_interval_ms")
    if not 0.0 <= bit_operation_ratio <= 1.0:
        raise ConfigurationError("bit_operation_ratio는 0~1 범위여야 합니다.")
    if not 0.0 <= guided_ratio <= 1.0:
        raise ConfigurationError("guided_ratio는 0~1 범위여야 합니다.")
    if duration_seconds is not None:
        validate_finite(duration_seconds, "duration_seconds", allow_equal=False)
        if duration_seconds > max_duration_seconds:
            raise ConfigurationError(
                f"duration_seconds는 안전 제한 {max_duration_seconds:g}초 이하여야 합니다."
            )
    if campaign_enabled:
        if not mutation_enabled:
            raise ConfigurationError("campaign을 사용하려면 mutation.enabled=true여야 합니다.")
        for field_name, value in (
            ("baseline_duration_seconds", baseline_duration),
            ("normal_duration_seconds", normal_duration),
            ("mutation_duration_seconds", mutation_duration),
            ("recovery_duration_seconds", recovery_duration),
        ):
            validate_finite(value, field_name, allow_equal=False)
        max_campaign_duration = float(
            safety_cfg.get("max_campaign_duration_seconds", 240.0)
        )
        validate_finite(
            max_campaign_duration, "max_campaign_duration_seconds", allow_equal=False
        )
        total_campaign_duration = (
            baseline_duration + normal_duration + mutation_duration + recovery_duration
        )
        if total_campaign_duration > max_campaign_duration:
            raise ConfigurationError(
                f"campaign 총 시간 {total_campaign_duration:g}초는 안전 제한 "
                f"{max_campaign_duration:g}초를 초과합니다."
            )
    if (campaign_enabled or count > 1 or duration_seconds is not None) and interval_ms < min_interval_ms:
        raise ConfigurationError(
            f"반복 송신 interval_ms는 안전 제한 {min_interval_ms:g}ms 이상이어야 합니다."
        )
    if campaign_enabled:
        if max_mutation_duration is not None and mutation_duration > max_mutation_duration:
            raise ConfigurationError(
                f"mutation_duration_seconds는 안전 제한 {max_mutation_duration:g}초 이하여야 합니다."
            )
        sequence_spec_for_limit = explicit_metadata.get("sequence")
        mutation_interval_ms = interval_ms
        if sequence_spec_for_limit is not None:
            if not isinstance(sequence_spec_for_limit, dict):
                raise ConfigurationError("temporal sequence metadata는 object여야 합니다.")
            mutation_interval_ms = float(sequence_spec_for_limit.get("interval_ms", interval_ms))
            validate_finite(mutation_interval_ms, "temporal sequence interval_ms", allow_equal=False)
            if mutation_interval_ms < min_interval_ms:
                raise ConfigurationError(
                    f"temporal interval은 안전 제한 {min_interval_ms:g}ms 이상이어야 합니다."
                )
        if max_normal_frames is not None and planned_frame_count(normal_duration, interval_ms) > max_normal_frames:
            raise ConfigurationError(
                f"normal 송신 계획은 안전 제한 {max_normal_frames}프레임을 초과합니다."
            )
        if max_mutation_frames is not None and planned_frame_count(mutation_duration, mutation_interval_ms) > max_mutation_frames:
            raise ConfigurationError(
                f"mutation 송신 계획은 안전 제한 {max_mutation_frames}프레임을 초과합니다."
            )

    configured_id = tx_cfg.get("id")
    frame_id = parse_int(args.frame_id if args.frame_id is not None else configured_id, "CAN ID") \
        if (args.frame_id is not None or configured_id is not None) else None
    message_name_arg = args.message if args.message is not None else tx_cfg.get("message")
    assignments = load_assignments(tx_cfg.get("set"), args.assignments)
    raw_data_value = args.data if args.data is not None else tx_cfg.get("data")
    dbc_path = (
        resolve_path(args.dbc, None)
        if args.dbc is not None
        else resolve_path(tx_cfg.get("dbc"), config_path)
    )

    raw_mode = raw_data_value is not None
    if raw_mode and assignments:
        raise ConfigurationError("raw data 송신과 DBC signal set은 동시에 사용할 수 없습니다.")
    if not raw_mode and not assignments:
        raise ConfigurationError("--data 또는 하나 이상의 --set SIGNAL=VALUE가 필요합니다.")

    bus = None
    definition = None
    base_payload: Optional[bytes] = None
    patched_values: Optional[Dict[str, Any]] = None
    protected: List[str] = []

    if raw_mode:
        if frame_id is None:
            raise ConfigurationError("raw 송신에는 CAN ID가 필요합니다.")
        is_extended = bool(args.extended or tx_cfg.get("extended", frame_id > 0x7FF))
        is_fd = bool(args.fd or tx_cfg.get("fd", False))
        bitrate_switch = bool(tx_cfg.get("bitrate_switch", False))
        payload = parse_can_data(raw_data_value)
        validate_frame_id(frame_id, is_extended)
        validate_length(payload, is_fd)
    else:
        if dbc_path is None:
            raise ConfigurationError("DBC 신호 송신에는 dbc 경로가 필요합니다.")
        database = load_dbc(dbc_path)
        definition = resolve_message(database, frame_id, message_name_arg)
        frame_id = int(definition.frame_id)
        message_name_arg = definition.name
        is_extended = bool(definition.is_extended_frame)
        is_fd = bool(getattr(definition, "is_fd", False))
        bitrate_switch = bool(tx_cfg.get("bitrate_switch", False))
        validate_frame_id(frame_id, is_extended)

        signal_names = {signal.name for signal in definition.signals}
        unknown = sorted(set(assignments) - signal_names)
        if unknown:
            raise ConfigurationError(
                f"{definition.name}에 없는 신호입니다: {', '.join(unknown)}"
            )
        protected = protected_signal_names(definition)
        if protected and not allow_protected:
            raise ConfigurationError(
                "이 메시지에는 CRC/counter로 추정되는 신호가 있어 단순 patch 시 거부될 수 있습니다: "
                + ", ".join(protected)
                + ". 의도한 실험이면 --allow-protected를 명시하세요."
            )

        base_mode = choose(args.base, base_cfg.get("mode"), "live")
        base_timeout = float(choose(args.base_timeout, base_cfg.get("timeout_seconds"), 5.0))
        validate_finite(base_timeout, "base timeout", allow_equal=False)
        base_sample_count = int(base_cfg.get("sample_count", 1))
        base_min_mode_ratio = float(base_cfg.get("min_mode_ratio", 1.0))
        base_data_value = args.base_data if args.base_data is not None else base_cfg.get("data")

        if base_mode == "live":
            bus = open_can_bus(interface_name, channel, receive_own_messages=False)
            base_payload = capture_live_payload(
                bus,
                frame_id,
                is_extended,
                base_timeout,
                base_sample_count,
                base_min_mode_ratio,
            )
        elif base_mode == "data":
            if base_data_value is None:
                raise ConfigurationError("base mode가 data이면 base-data가 필요합니다.")
            base_payload = parse_can_data(base_data_value)
        elif base_mode == "zero":
            defaults = signal_defaults(definition)
            try:
                base_payload = bytes(definition.encode(defaults, scaling=True, padding=False, strict=True))
            except Exception as exc:
                raise RuntimeError(f"DBC 기본 payload 생성 실패: {type(exc).__name__}: {exc}") from exc
        else:
            raise ConfigurationError(f"지원하지 않는 base mode입니다: {base_mode}")

        validate_length(base_payload, is_fd, int(definition.length))
        try:
            base_values = definition.decode(base_payload, decode_choices=False, scaling=True)
            patched_values = dict(base_values)
            patched_values.update(assignments)
            encoded_base = bytes(
                definition.encode(base_values, scaling=True, padding=False, strict=True)
            )
            encoded_patch = bytes(
                definition.encode(patched_values, scaling=True, padding=False, strict=True)
            )
            # Apply only the DBC-computed signal delta to the original bytes. This
            # preserves reserved/unmodelled bits that decode -> encode cannot retain.
            payload = bytes(
                original ^ before ^ after
                for original, before, after in zip(
                    base_payload, encoded_base, encoded_patch
                )
            )
            verified = definition.decode(payload, decode_choices=False, scaling=True)
            mismatched = [
                name for name, expected in assignments.items()
                if verified.get(name) != expected
            ]
            if mismatched:
                raise ValueError(
                    "patch 검증 실패 신호: " + ", ".join(sorted(mismatched))
                )
        except Exception as exc:
            raise RuntimeError(f"DBC payload patch 실패: {type(exc).__name__}: {exc}") from exc

    assert frame_id is not None
    if args.output is not None:
        output_path = resolve_path(args.output, None)
    elif tx_cfg.get("output"):
        output_path = resolve_path(tx_cfg.get("output"), config_path)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_dir = config_path.parent if config_path else Path.cwd()
        output_path = (base_dir / "logs" / f"{bus_name}_tx_{stamp}.jsonl").resolve()
    assert output_path is not None
    output_path = reserve_output_path(output_path, output_policy)

    execute = bool(args.execute)

    normal_payload = base_payload if base_payload is not None else payload
    mutation_seed_source = str(choose(
        args.mutation_seed_source, mutation_cfg.get("seed_source"), "patched"
    ))
    if mutation_seed_source not in {"patched", "normal"}:
        raise ConfigurationError("mutation.seed_source는 patched 또는 normal이어야 합니다.")
    mutation_base = normal_payload if control_noop or mutation_seed_source == "normal" else payload
    feedback_hints = []
    guided_entries = []
    mutation_metadata: Dict[bytes, Dict[str, Any]] = {}
    if control_noop:
        if count != 1:
            raise ConfigurationError("--control-noop은 --count 1이어야 합니다.")
        if explicit_mutation is not None and explicit_mutation != normal_payload:
            raise ConfigurationError("--control-noop의 비교 payload는 원본과 같아야 합니다.")
        sequence_spec = explicit_metadata.get("sequence")
        sequence_payloads = None
        sequence_interval_ms = None
        if sequence_spec is not None:
            if not isinstance(sequence_spec, dict):
                raise ConfigurationError("no-op temporal sequence metadata는 object여야 합니다.")
            raw_frames = sequence_spec.get("frames")
            if not isinstance(raw_frames, list) or not raw_frames:
                raise ConfigurationError("no-op temporal sequence frames가 비어 있습니다.")
            sequence_payloads = [parse_can_data(str(value)) for value in raw_frames]
            if any(frame != normal_payload for frame in sequence_payloads):
                raise ConfigurationError(
                    "no-op temporal sequence는 모든 프레임이 원본 payload여야 합니다."
                )
            sequence_interval_ms = float(sequence_spec.get("interval_ms", interval_ms))
            validate_finite(sequence_interval_ms, "no-op temporal sequence interval_ms", allow_equal=False)
            if sequence_interval_ms < min_interval_ms:
                raise ConfigurationError(
                    f"no-op temporal interval은 안전 제한 {min_interval_ms:g}ms 이상이어야 합니다."
                )
        payloads = [normal_payload]
    elif mutation_enabled:
        if explicit_mutation is not None:
            if len(explicit_mutation) != len(mutation_base):
                raise ConfigurationError(
                    "Trial mutation payload 길이는 baseline payload 길이와 같아야 합니다."
                )
            if explicit_mutation == mutation_base:
                raise ConfigurationError("Trial mutation payload는 baseline과 달라야 합니다.")
            combined_entries = [(
                explicit_mutation,
                {
                    "source": "trial_runner",
                    "strategy": "previous_trial_feedback",
                    "operators": [args.mutation_operator or "EXPLICIT"],
                    "mutation_id": args.mutation_id,
                    "mutation_uid": args.mutation_uid,
                    "parent_mutation_id": args.parent_mutation_id,
                    "generation_reason": args.generation_reason,
                    **explicit_metadata,
                },
            )]
        else:
            random_entries = generate_mutation_entries(
                mutation_base,
                count,
                max_operations,
                allow_dlc_change,
                include_original,
                random_seed,
                bit_operation_ratio,
            )
            if feedback_path is not None:
                if not feedback_path.is_file():
                    raise ConfigurationError(
                        f"feedback 분석 JSON을 찾을 수 없습니다: {feedback_path}"
                    )
                feedback_hints = load_feedback_hints(feedback_path)
                guided_limit = min(count, int(round(count * guided_ratio)))
                guided_entries = generate_guided_mutations(
                    mutation_base, feedback_hints, guided_limit
                )

            combined_entries = []
            seen_payloads: set[bytes] = set()
            for item in guided_entries:
                if item.payload not in seen_payloads:
                    seen_payloads.add(item.payload)
                    combined_entries.append((item.payload, item.metadata()))
            for random_payload, metadata in random_entries:
                if random_payload not in seen_payloads:
                    seen_payloads.add(random_payload)
                    combined_entries.append((random_payload, metadata))
                if len(combined_entries) == count:
                    break
        if len(combined_entries) != count:
            raise RuntimeError(
                f"guided/random 결합 후 mutation {count}개 중 "
                f"{len(combined_entries)}개만 생성됐습니다."
            )
        payloads = [item for item, _ in combined_entries]
        mutation_metadata = {
            item: metadata for item, metadata in combined_entries
        }
        sequence_spec = explicit_metadata.get("sequence")
        sequence_payloads: Optional[List[bytes]] = None
        sequence_interval_ms: Optional[float] = None
        if sequence_spec is not None:
            if not isinstance(sequence_spec, dict):
                raise ConfigurationError("temporal sequence metadata는 object여야 합니다.")
            raw_frames = sequence_spec.get("frames")
            if not isinstance(raw_frames, list) or not raw_frames:
                raise ConfigurationError("temporal sequence frames가 비어 있습니다.")
            sequence_payloads = [parse_can_data(str(value)) for value in raw_frames]
            frame_changes = sequence_spec.get("frame_signals_changed", [])
            if frame_changes and (
                not isinstance(frame_changes, list)
                or len(frame_changes) != len(sequence_payloads)
            ):
                raise ConfigurationError(
                    "temporal frame_signals_changed 길이가 frames와 다릅니다."
                )
            for index, sequence_payload in enumerate(sequence_payloads):
                validate_length(sequence_payload, is_fd, len(mutation_base))
                frame_metadata = dict(combined_entries[0][1])
                if frame_changes:
                    frame_metadata["signals_changed"] = frame_changes[index]
                    frame_metadata["sequence_frame_index"] = index
                mutation_metadata[sequence_payload] = frame_metadata
            sequence_interval_ms = float(sequence_spec.get("interval_ms", interval_ms))
            validate_finite(sequence_interval_ms, "temporal sequence interval_ms", allow_equal=False)
            if sequence_interval_ms < min_interval_ms:
                raise ConfigurationError(
                    f"temporal interval은 안전 제한 {min_interval_ms:g}ms 이상이어야 합니다."
                )
        for item in payloads:
            validate_length(item, is_fd)
    else:
        payloads = [payload] * count
        sequence_payloads = None
        sequence_interval_ms = None

    mode_name = "RAW" if raw_mode else "DBC PATCH"
    if control_noop:
        mode_name += " + NOOP CONTROL"
    elif mutation_enabled:
        mode_name += " + LOCAL MUTATOR"
    print(f"[MODE]  {mode_name} / {'EXECUTE' if execute else 'PREVIEW'}")
    print(f"[BUS]   {interface_name}:{channel} ({bus_name})")
    print(f"[FRAME] ID=0x{frame_id:X}, DLC={len(payload)}, DATA={payload.hex().upper()}")
    if definition is not None:
        print(f"[DBC]   {definition.name} / set={assignments}")
        print(f"[BASE]  {base_payload.hex().upper() if base_payload is not None else '-'}")
    duration_text = f", duration={duration_seconds:g}s (cycle)" if duration_seconds is not None else ""
    restore_payload = normal_payload
    should_restore = restore and (base_payload is not None or campaign_enabled)
    print(
        f"[TX]    corpus={len(payloads)}{duration_text}, interval={interval_ms:g}ms, "
        f"restore={should_restore}"
    )
    if campaign_enabled:
        print(
            f"[CAMPAIGN] baseline={baseline_duration:g}s(passive) -> "
            f"normal={normal_duration:g}s -> mutation={mutation_duration:g}s -> "
            f"recovery={recovery_duration:g}s(passive)"
        )
        print(f"[NORMAL] 0x{frame_id:X}#{normal_payload.hex().upper()}")
    if mutation_enabled and not control_noop:
        print(
            f"[MUT]   max_ops={max_operations}, structural={allow_dlc_change}, "
            f"include_seed={include_original}, random_seed={random_seed}"
            f" ({'auto' if random_seed_generated else 'fixed'})"
        )
        print(
            f"[MUT]   bit_operation_ratio={bit_operation_ratio:g}, "
            f"guided={len(guided_entries)}/{len(payloads)}"
        )
        if feedback_path is not None:
            print(
                f"[FEEDBACK] {feedback_path} / usable_hints={len(feedback_hints)}"
            )
    print(f"[LOG]   {output_path}")
    print(f"[LOG]   policy={output_policy}, experiment_id={experiment_id or '-'}")
    if not execute:
        print("[SAFE]  PREVIEW이므로 송신하지 않습니다. 확인 후 --execute를 추가하세요.")
        if campaign_enabled:
            print("[SAFE]  preview는 phase를 대기하지 않고 normal 1개와 mutation corpus 1회를 표시합니다.")
        elif duration_seconds is not None:
            print(
                f"[SAFE]  실제 실행은 {duration_seconds:g}초 동안 payload corpus를 순환하며, "
                "preview는 corpus를 한 번만 표시합니다."
            )

    try:
        if execute and bus is None:
            bus = open_can_bus(interface_name, channel, receive_own_messages=False)

        with output_path.open("a", encoding="utf-8") as handle:
            write_jsonl(
                handle,
                {
                    "record_type": "tx_session_start",
                    "schema_version": 3,
                    **now_fields(),
                    "host": hostname(),
                    "bus": bus_name,
                    "interface": interface_name,
                    "channel": channel,
                    "tx_session_id": tx_session_id,
                    "experiment_id": experiment_id,
                    "trial_kind": trial_kind,
                    "trial_contract_version": contract_version,
                    "effective_safety": {
                        "min_interval_ms": min_interval_ms,
                        "max_campaign_duration_seconds": (
                            float(safety_cfg.get("max_campaign_duration_seconds", 240.0))
                            if campaign_enabled else None
                        ),
                        "max_mutation_duration_seconds": max_mutation_duration,
                        "max_mutation_frames": max_mutation_frames,
                        "max_normal_frames": max_normal_frames,
                    },
                    "restore_policy": {
                        "enabled": should_restore,
                        "count": restore_count if should_restore else 0,
                        "delay_ms": restore_delay_ms if should_restore else 0.0,
                    },
                    "execute": execute,
                    "dbc": str(dbc_path) if dbc_path else None,
                    "protected_signals": protected,
                    "analysis": analysis_cfg,
                    "transmission": {
                        "payload_corpus_size": len(payloads),
                        "duration_seconds": duration_seconds,
                        "interval_ms": interval_ms,
                    },
                    "campaign": {
                        "enabled": campaign_enabled,
                        "baseline_duration_seconds": baseline_duration if campaign_enabled else None,
                        "normal_duration_seconds": normal_duration if campaign_enabled else None,
                        "mutation_duration_seconds": mutation_duration if campaign_enabled else None,
                        "recovery_duration_seconds": recovery_duration if campaign_enabled else None,
                        "normal_data_hex": normal_payload.hex().upper() if campaign_enabled else None,
                    },
                    "mutation": {
                        "enabled": mutation_enabled and not control_noop,
                        "control_noop": control_noop,
                        "base_data_hex": mutation_base.hex().upper(),
                        "seed_source": mutation_seed_source,
                        "max_operations": max_operations,
                        "allow_dlc_change": allow_dlc_change,
                        "include_original": include_original,
                        "random_seed": random_seed,
                        "random_seed_generated": random_seed_generated,
                        "bit_operation_ratio": bit_operation_ratio,
                        "feedback_path": str(feedback_path) if feedback_path else None,
                        "guided_ratio": guided_ratio,
                        "feedback_hint_count": len(feedback_hints),
                        "guided_payload_count": len(guided_entries),
                        "trial_mutation_id": args.mutation_id,
                        "trial_mutation_uid": args.mutation_uid,
                        "parent_mutation_id": args.parent_mutation_id,
                        "generation_reason": args.generation_reason,
                        "targeted_metadata": explicit_metadata or None,
                        "explicit_trial_payload": explicit_mutation is not None,
                    },
                },
            )
            attempted_count = 0
            phase_attempted: Dict[str, int] = {}
            phase_sent: Dict[str, int] = {}
            mutation_started = False
            restore_result: Dict[str, Any] = {
                "enabled": should_restore,
                "attempted": 0,
                "sent": 0,
                "status": "pending" if should_restore else "disabled",
                "error": None,
            }

            def phase_marker(phase: str, event: str, duration: float) -> None:
                write_jsonl(handle, {
                    "record_type": "tx_phase",
                    **now_fields(),
                    "host": hostname(),
                    "bus": bus_name,
                    "tx_session_id": tx_session_id,
                    "experiment_id": experiment_id,
                    "trial_kind": trial_kind,
                    "phase": phase,
                    "event": event,
                    "duration_seconds": duration,
                    "execute": execute,
                })
                handle.flush()
                print(f"[PHASE] {phase} {event} ({duration:g}s)")

            def passive_phase(phase: str, duration: float) -> None:
                phase_marker(phase, "start", duration)
                if execute:
                    time.sleep(duration)
                phase_marker(phase, "end", duration)

            def transmit_phase(
                phase: str,
                phase_payloads: Sequence[bytes],
                phase_duration: Optional[float],
                mutated: bool,
                phase_interval_ms: Optional[float] = None,
            ) -> None:
                nonlocal attempted_count, mutation_started
                marker_duration = phase_duration or 0.0
                phase_marker(phase, "start", marker_duration)
                if phase == "mutation":
                    mutation_started = True
                active_duration = phase_duration if execute else None
                selected_interval_ms = (
                    phase_interval_ms if phase_interval_ms is not None else interval_ms
                )
                phase_deadline = (
                    time.monotonic() + active_duration
                    if active_duration is not None else None
                )
                phase_limit = (
                    max_normal_frames if phase == "normal" else
                    max_mutation_frames if phase == "mutation" and campaign_enabled else None
                )
                phase_attempted.setdefault(phase, 0)
                phase_sent.setdefault(phase, 0)
                last_send_start: Optional[float] = None
                # Reserve at most 10ms (20% of the interval) at the boundary.
                # This skips a late send; a send crossing the deadline still aborts.
                send_guard_seconds = min(0.01, selected_interval_ms / 5000.0)
                for phase_sequence, current_payload in transmission_schedule(
                    phase_payloads, selected_interval_ms / 1000.0,
                    clock=time.monotonic, sleeper=time.sleep,
                    deadline_monotonic=phase_deadline,
                    send_start_clock=lambda: last_send_start,
                ):
                    if phase_deadline is not None and time.monotonic() >= phase_deadline:
                        raise RuntimeError(f"{phase} phase monotonic deadline exceeded before send")
                    if phase_limit is not None and phase_attempted[phase] >= phase_limit:
                        raise RuntimeError(f"{phase} phase frame limit {phase_limit} exceeded")
                    kind = "normal" if phase == "normal" else "inject"
                    if phase == "mutation" and control_noop:
                        kind = "control"
                    elif mutated:
                        kind = "seed" if current_payload == mutation_base else "mutation"
                    details = (
                        mutation_summary(mutation_base, current_payload)
                        if mutated and not control_noop else None
                    )
                    if details is not None:
                        details.update(mutation_metadata.get(current_payload, {}))
                    record_signals = (
                        assignments if assignments and not campaign_enabled else None
                    )
                    if execute:
                        assert bus is not None
                        message = create_message(
                            frame_id, current_payload, is_extended, is_fd, bitrate_switch
                        )
                        remaining = (
                            phase_deadline - time.monotonic()
                            if phase_deadline is not None else None
                        )
                        if remaining is not None and remaining <= 0:
                            raise RuntimeError(f"{phase} phase monotonic deadline exceeded before send")
                        if remaining is not None and remaining <= send_guard_seconds:
                            write_jsonl(handle, {
                                "record_type": "tx_schedule_skip",
                                "schema_version": 3,
                                **now_fields(),
                                "host": hostname(),
                                "bus": bus_name,
                                "frame_id": frame_id,
                                "tx_session_id": tx_session_id,
                                "experiment_id": experiment_id,
                                "trial_kind": trial_kind,
                                "phase": phase,
                                "phase_sequence": phase_sequence,
                                "interval_ms": selected_interval_ms,
                                "execute": execute,
                                "reason": "too_close_to_phase_deadline",
                                "remaining_seconds": remaining,
                                "guard_seconds": send_guard_seconds,
                            })
                            handle.flush()
                            break
                    attempted_count += 1
                    phase_attempted[phase] += 1
                    attempt_ns = time.time_ns()
                    if execute:
                        last_send_start = time.monotonic()
                        try:
                            bus.send(message, timeout=send_timeout)
                            status = "sent"
                            phase_sent[phase] += 1
                            send_over_deadline = (
                                phase_deadline is not None and time.monotonic() > phase_deadline
                            )
                        except BaseException as exc:
                            failed = tx_record(
                                bus_name, channel, frame_id, current_payload,
                                is_extended, is_fd, "send_error", attempted_count,
                                message_name_arg,
                                record_signals,
                                kind=kind, mutation=details,
                                tx_session_id=tx_session_id,
                                experiment_id=experiment_id, phase=phase,
                                trial_kind=trial_kind,
                            )
                            failed["phase_sequence"] = phase_sequence
                            failed["send_attempt_wall_time_ns"] = attempt_ns
                            failed["error"] = f"{type(exc).__name__}: {exc}"
                            write_jsonl(handle, failed)
                            handle.flush()
                            raise
                    else:
                        status = "preview"
                        send_over_deadline = False
                    record = tx_record(
                        bus_name, channel, frame_id, current_payload,
                        is_extended, is_fd, status, attempted_count,
                        message_name_arg,
                        record_signals,
                        kind=kind, mutation=details,
                        tx_session_id=tx_session_id,
                        experiment_id=experiment_id, phase=phase,
                        trial_kind=trial_kind,
                    )
                    record["phase_sequence"] = phase_sequence
                    record["send_attempt_wall_time_ns"] = attempt_ns
                    write_jsonl(handle, record)
                    handle.flush()
                    print(
                        f"[TX {phase}:{phase_sequence:06}] {status.upper()} "
                        f"0x{frame_id:X}#{current_payload.hex().upper()}"
                    )
                    if send_over_deadline:
                        raise RuntimeError(f"{phase} phase monotonic deadline exceeded during send")
                if phase_deadline is not None:
                    remaining = phase_deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                phase_marker(phase, "end", marker_duration)

            def restore_original(*, aborting: bool = False) -> None:
                if not should_restore:
                    return
                restore_result["status"] = "attempting"
                if execute and restore_delay_ms and not aborting:
                    time.sleep(restore_delay_ms / 1000.0)
                count_to_send = 1 if aborting else restore_count
                for sequence in range(1, count_to_send + 1):
                    restore_attempt_ns = time.time_ns()
                    restore_result["attempted"] += 1
                    try:
                        if execute:
                            assert bus is not None
                            message = create_message(
                                frame_id, restore_payload, is_extended, is_fd, bitrate_switch
                            )
                            bus.send(
                                message,
                                timeout=min(send_timeout, 1.0) if aborting else send_timeout,
                            )
                            status = "sent"
                            restore_result["sent"] += 1
                        else:
                            status = "preview"
                    except BaseException as exc:
                        status = "send_error"
                        restore_result["status"] = "failed"
                        restore_result["error"] = f"{type(exc).__name__}: {exc}"
                        failed_restore = tx_record(
                            bus_name, channel, frame_id, restore_payload,
                            is_extended, is_fd, status, sequence, message_name_arg,
                            kind="restore", tx_session_id=tx_session_id,
                            experiment_id=experiment_id,
                            phase="recovery" if campaign_enabled else "restore",
                            trial_kind=trial_kind,
                        )
                        failed_restore["send_attempt_wall_time_ns"] = restore_attempt_ns
                        failed_restore["error"] = restore_result["error"]
                        write_jsonl(handle, failed_restore)
                        handle.flush()
                        raise
                    restore_record = tx_record(
                        bus_name, channel, frame_id, restore_payload,
                        is_extended, is_fd, status, sequence, message_name_arg,
                        kind="restore", tx_session_id=tx_session_id,
                        experiment_id=experiment_id,
                        phase="recovery" if campaign_enabled else "restore",
                        trial_kind=trial_kind,
                    )
                    restore_record["send_attempt_wall_time_ns"] = restore_attempt_ns
                    write_jsonl(handle, restore_record)
                    handle.flush()
                    print(
                        f"[RESTORE {sequence:03}/{count_to_send:03}] "
                        f"{status.upper()} 0x{frame_id:X}#{restore_payload.hex().upper()}"
                    )
                    if execute and sequence < count_to_send:
                        time.sleep(interval_ms / 1000.0)
                restore_result["status"] = "sent" if execute else "preview"
                restore_result["error"] = None

            def write_session_end(status: str, error: Optional[str] = None) -> None:
                record = {
                    "record_type": "tx_session_end",
                    "schema_version": 3,
                    **now_fields(),
                    "host": hostname(),
                    "bus": bus_name,
                    "tx_session_id": tx_session_id,
                    "experiment_id": experiment_id,
                    "trial_kind": trial_kind,
                    "trial_contract_version": contract_version,
                    "status": status,
                    "execute": execute,
                    "planned": None if execute and (campaign_enabled or duration_seconds is not None) else attempted_count,
                    "payload_corpus_size": len(payloads),
                    "duration_seconds": duration_seconds,
                    "attempted": attempted_count,
                    "phase_attempted": dict(phase_attempted),
                    "phase_sent": dict(phase_sent),
                    "restore": dict(restore_result),
                }
                if error is not None:
                    record["error"] = error
                write_jsonl(handle, record)
                handle.flush()

            previous_sigterm = None
            if current_thread() is main_thread():
                previous_sigterm = signal.getsignal(signal.SIGTERM)

                def on_sigterm(signum: int, frame: Any) -> None:
                    del signum, frame
                    raise SenderInterrupted("sender received SIGTERM")

                signal.signal(signal.SIGTERM, on_sigterm)
            try:
                if campaign_enabled:
                    passive_phase("baseline", baseline_duration)
                    transmit_phase("normal", [normal_payload], normal_duration, False)
                    transmit_phase(
                        "mutation",
                        sequence_payloads or payloads,
                        mutation_duration,
                        True,
                        sequence_interval_ms,
                    )
                else:
                    transmit_phase(
                        "mutation" if mutation_enabled else "inject",
                        payloads,
                        duration_seconds,
                        mutation_enabled,
                    )

                restore_original()
                if campaign_enabled:
                    passive_phase("recovery", recovery_duration)
                write_session_end("completed")
            except BaseException as exc:
                # A second SIGTERM must not interrupt the bounded, best-effort
                # restore or replace the original failure in the manifest.
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                if mutation_started and should_restore and restore_result["sent"] == 0:
                    try:
                        restore_original(aborting=True)
                    except BaseException as restore_exc:
                        restore_result["status"] = "failed"
                        restore_result["error"] = f"{type(restore_exc).__name__}: {restore_exc}"
                elif not mutation_started and restore_result["status"] == "pending":
                    restore_result["status"] = "not_needed"
                try:
                    write_session_end("aborted", f"{type(exc).__name__}: {exc}")
                except Exception as log_exc:
                    print(f"[WARN] failed to write abort manifest: {log_exc}", file=sys.stderr)
                raise
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
    finally:
        if bus is not None:
            active_exception = sys.exc_info()[1]
            try:
                shutdown_bus(bus)
            except Exception as shutdown_exc:
                if active_exception is None:
                    raise
                print(f"[WARN] failed to close CAN bus: {shutdown_exc}", file=sys.stderr)

    print("[DONE] 송신 작업 완료")
    return 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
