#!/usr/bin/env python3
"""Config-first 0x366 mutation sender for CANoe.

This is intentionally narrower than ``experiment_runner.py``: it reuses the
same deterministic mutation catalogue, but performs no SSH capture, paired
control, log analysis, or feedback selection.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from a5_0x366_mutator import BASELINE_PAYLOAD, TARGET_CAN_ID, TARGET_DLC
from can_common import ConfigurationError, load_yaml_config, parse_can_data, parse_int
from canoe_interface import CANoeComInterface, CANoeFrame
from paired_cycle import CYCLE_FAMILIES, build_cycle_plan, make_cycle_mutation
from trial_models import MutationCase


DEFAULT_CONFIG = Path(__file__).with_name("canoe_fuzzer.yaml")


@dataclass(frozen=True)
class FuzzerSettings:
    dbc_path: Path
    source_bus: str
    frame_id: int
    baseline_payload: bytes
    method: str
    max_cases: int
    random_seed: int
    undefined_max_bits: int
    mutation_duration_seconds: float
    interval_ms: float
    restore_original: bool
    restore_count: int
    case_gap_ms: float
    canoe_options: dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="기존 0x366 mutation catalogue를 CANoe로 송신합니다."
    )
    parser.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG), help="CANoe fuzzer YAML"
    )
    parser.add_argument(
        "-m", "--method", choices=tuple(CYCLE_FAMILIES),
        help="mutation family; 생략하면 YAML의 mutation.method 사용",
    )
    parser.add_argument(
        "-n", "--cases", type=int,
        help="이번 실행에서 송신할 mutation case 수",
    )
    parser.add_argument(
        "-x", "--execute", action="store_true",
        help="CANoe에 실제 송신; 생략하면 preview",
    )
    parser.add_argument(
        "--list-methods",
        action="store_true",
        help="지원하는 mutation family 이름 출력(case 생성 가능 여부는 preview로 확인)",
    )
    return parser


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a YAML mapping")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be true or false")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    return parse_int(value, name)


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed < minimum or (
        maximum is not None and parsed > maximum
    ):
        limit = f"{minimum:g}..{maximum:g}" if maximum is not None else f">= {minimum:g}"
        raise ConfigurationError(f"{name} must be finite and within {limit}")
    return parsed


def load_settings(args: argparse.Namespace) -> FuzzerSettings:
    config, config_path = load_yaml_config(args.config)
    if config_path is None:
        raise ConfigurationError("CANoe fuzzer config is required")
    target = _mapping(config.get("target"), "target")
    mutation = _mapping(config.get("mutation"), "mutation")
    transmit = _mapping(config.get("transmit"), "transmit")
    canoe = dict(_mapping(config.get("canoe"), "canoe"))

    dbc_value = config.get("dbc", "../A5.dbc")
    dbc_path = Path(str(dbc_value)).expanduser()
    if not dbc_path.is_absolute():
        dbc_path = config_path.parent / dbc_path
    dbc_path = dbc_path.resolve()
    if not dbc_path.is_file():
        raise ConfigurationError(f"DBC file does not exist: {dbc_path}")

    frame_id = parse_int(target.get("id", TARGET_CAN_ID), "target.id")
    if frame_id != TARGET_CAN_ID:
        raise ConfigurationError("this mutation catalogue is dedicated to CAN ID 0x366")
    baseline_payload = parse_can_data(
        target.get("baseline_payload", BASELINE_PAYLOAD.hex())
    )
    if len(baseline_payload) != TARGET_DLC:
        raise ConfigurationError("0x366 baseline payload must be exactly 8 bytes")
    source_bus = str(target.get("source_bus", "b_can")).strip().lower()
    if source_bus not in {"p_can", "b_can", "i_can"}:
        raise ConfigurationError("target.source_bus must be P_CAN, B_CAN, or I_CAN")

    method = args.method or str(mutation.get("method", "signal_single"))
    if method not in CYCLE_FAMILIES:
        raise ConfigurationError(
            "unknown mutation method; choose one of: " + ", ".join(CYCLE_FAMILIES)
        )
    max_cases = _integer(
        args.cases if args.cases is not None else mutation.get("max_cases", 10),
        "mutation.max_cases",
    )
    if not 1 <= max_cases <= 200:
        raise ConfigurationError("mutation case count must be between 1 and 200")
    random_seed = _integer(mutation.get("random_seed", 366), "mutation.random_seed")
    undefined_max_bits = _integer(
        mutation.get("undefined_max_bits", 2), "mutation.undefined_max_bits"
    )
    if undefined_max_bits < 2:
        raise ConfigurationError("mutation.undefined_max_bits must be at least 2")
    duration = _finite(
        mutation.get("duration_seconds", 1.0),
        "mutation.duration_seconds", minimum=0.001, maximum=1.0,
    )
    interval_ms = _finite(
        mutation.get("interval_ms", 50.0),
        "mutation.interval_ms", minimum=50.0,
    )

    restore_original = _boolean(
        transmit.get("restore_original", True), "transmit.restore_original"
    )
    restore_count = _integer(
        transmit.get("restore_count", 1), "transmit.restore_count"
    )
    if not 1 <= restore_count <= 10:
        raise ConfigurationError("transmit.restore_count must be between 1 and 10")
    case_gap_ms = _finite(
        transmit.get("case_gap_ms", 100.0), "transmit.case_gap_ms", minimum=0.0
    )

    channel = parse_int(canoe.get("channel", 1), "canoe.channel")
    if isinstance(canoe.get("channel", 1), bool) or channel < 1:
        raise ConfigurationError("canoe.channel must be an integer greater than zero")
    configuration_value = canoe.get("configuration_path")
    if configuration_value:
        configuration_path = Path(str(configuration_value)).expanduser()
        if not configuration_path.is_absolute():
            configuration_path = config_path.parent / configuration_path
        canoe["configuration_path"] = str(configuration_path.resolve())
    else:
        canoe["configuration_path"] = None
    canoe_options = {
        "channel": channel,
        "prog_id": str(canoe.get("prog_id", "CANoe.Application")),
        "attach_existing": _boolean(
            canoe.get("attach_existing", True), "canoe.attach_existing"
        ),
        "configuration_path": canoe["configuration_path"],
        "open_configuration": _boolean(
            canoe.get("open_configuration", False), "canoe.open_configuration"
        ),
        "start_measurement": _boolean(
            canoe.get("start_measurement", True), "canoe.start_measurement"
        ),
        "restart_running_measurement": _boolean(
            canoe.get("restart_running_measurement", False),
            "canoe.restart_running_measurement",
        ),
        "stop_measurement_on_close": _boolean(
            canoe.get("stop_measurement_on_close", True),
            "canoe.stop_measurement_on_close",
        ),
        "compile_capl": _boolean(
            canoe.get("compile_capl", True), "canoe.compile_capl"
        ),
        "measurement_timeout_seconds": _finite(
            canoe.get("measurement_timeout_seconds", 10.0),
            "canoe.measurement_timeout_seconds", minimum=0.1,
        ),
        "capl_function": str(canoe.get("capl_function", "AutoFuzzSend")),
        "visible": _boolean(canoe.get("visible", True), "canoe.visible"),
    }
    if not canoe_options["prog_id"].strip():
        raise ConfigurationError("canoe.prog_id cannot be empty")
    if not canoe_options["capl_function"].strip():
        raise ConfigurationError("canoe.capl_function cannot be empty")
    if (
        canoe_options["restart_running_measurement"]
        and not canoe_options["start_measurement"]
    ):
        raise ConfigurationError(
            "canoe.restart_running_measurement requires start_measurement: true"
        )
    return FuzzerSettings(
        dbc_path=dbc_path,
        source_bus=source_bus,
        frame_id=frame_id,
        baseline_payload=baseline_payload,
        method=method,
        max_cases=max_cases,
        random_seed=random_seed,
        undefined_max_bits=undefined_max_bits,
        mutation_duration_seconds=duration,
        interval_ms=interval_ms,
        restore_original=restore_original,
        restore_count=restore_count,
        case_gap_ms=case_gap_ms,
        canoe_options=canoe_options,
    )


def build_mutation_cases(settings: FuzzerSettings) -> tuple[MutationCase, ...]:
    try:
        plan = build_cycle_plan(
            settings.dbc_path,
            settings.baseline_payload,
            source_bus=settings.source_bus,
            random_seed=settings.random_seed,
            undefined_max_bits=settings.undefined_max_bits,
            mutation_duration_s=settings.mutation_duration_seconds,
            mutation_interval_ms=settings.interval_ms,
            selected_family=settings.method,
        )
    except ConfigurationError:
        raise
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    entries = [
        entry for entry in plan["entries"] if entry["disposition"] == "scheduled"
    ][: settings.max_cases]
    if not entries:
        raise ConfigurationError(
            f"mutation method {settings.method!r} has no safe distinct cases"
        )
    return tuple(
        make_cycle_mutation(
            entry,
            mutation_id=index,
            source_bus=settings.source_bus,
            original_payload=settings.baseline_payload,
            random_seed=settings.random_seed,
        )
        for index, entry in enumerate(entries, start=1)
    )


def case_schedule(
    mutation: MutationCase, settings: FuzzerSettings
) -> tuple[tuple[bytes, ...], float]:
    if mutation.can_id != settings.frame_id:
        raise ConfigurationError(
            f"mutation CAN ID 0x{mutation.can_id:X} does not match target "
            f"0x{settings.frame_id:X}"
        )
    if mutation.original_payload != settings.baseline_payload:
        raise ConfigurationError("mutation original payload does not match baseline")
    sequence = mutation.parameters.get("sequence")
    if sequence is None:
        source_payloads = (mutation.mutated_payload,)
        interval_ms = settings.interval_ms
    else:
        if not isinstance(sequence, Mapping):
            raise ConfigurationError("temporal mutation sequence must be a mapping")
        raw_frames = sequence.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise ConfigurationError("temporal mutation sequence has no frames")
        source_payloads = tuple(parse_can_data(frame) for frame in raw_frames)
        interval_ms = _finite(
            sequence.get("interval_ms", settings.interval_ms),
            "temporal sequence interval_ms",
            minimum=50.0,
        )
    for payload in source_payloads:
        if len(payload) != TARGET_DLC:
            raise ConfigurationError(
                f"0x366 mutation payload must be exactly {TARGET_DLC} bytes"
            )
    frame_count = int(math.ceil(
        settings.mutation_duration_seconds * 1000.0 / interval_ms
    ))
    payloads = tuple(
        source_payloads[index % len(source_payloads)] for index in range(frame_count)
    )
    return payloads, interval_ms


def transmit_cases(
    interface: Any,
    settings: FuzzerSettings,
    cases: Sequence[MutationCase],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> int:
    sent = 0
    total = len(cases)
    for position, mutation in enumerate(cases, start=1):
        payloads, interval_ms = case_schedule(mutation, settings)
        started = clock()
        emit(
            f"[CASE {position:03}/{total:03}] {settings.method}/{mutation.parameters['case']} "
            f"frames={len(payloads)} interval={interval_ms:g}ms"
        )
        try:
            for sequence, payload in enumerate(payloads):
                target_time = started + sequence * interval_ms / 1000.0
                remaining = target_time - clock()
                if remaining > 0:
                    sleeper(remaining)
                interface.send_frame(CANoeFrame(
                    channel=int(settings.canoe_options["channel"]),
                    arbitration_id=settings.frame_id,
                    data=payload,
                ))
                sent += 1
            phase_remaining = (
                started + settings.mutation_duration_seconds - clock()
            )
            if phase_remaining > 0:
                sleeper(phase_remaining)
        except BaseException as transmission_error:
            if settings.restore_original:
                try:
                    interface.send_frame(CANoeFrame(
                        channel=int(settings.canoe_options["channel"]),
                        arbitration_id=settings.frame_id,
                        data=settings.baseline_payload,
                    ))
                except Exception as restore_error:
                    if hasattr(transmission_error, "add_note"):
                        transmission_error.add_note(
                            "original-payload restore also failed: "
                            f"{type(restore_error).__name__}: {restore_error}"
                        )
            raise

        if settings.restore_original:
            for _ in range(settings.restore_count):
                interface.send_frame(CANoeFrame(
                    channel=int(settings.canoe_options["channel"]),
                    arbitration_id=settings.frame_id,
                    data=settings.baseline_payload,
                ))
                sent += 1
        if position != total and settings.case_gap_ms:
            sleeper(settings.case_gap_ms / 1000.0)
    return sent


def run(
    args: argparse.Namespace,
    *,
    interface_factory: Callable[..., Any] = CANoeComInterface,
) -> int:
    if args.list_methods:
        print("\n".join(CYCLE_FAMILIES))
        return 0
    settings = load_settings(args)
    cases = build_mutation_cases(settings)
    print(
        f"[PLAN] method={settings.method}, cases={len(cases)}, "
        f"ID=0x{settings.frame_id:X}, channel={settings.canoe_options['channel']}"
    )
    for position, mutation in enumerate(cases, start=1):
        payloads, interval_ms = case_schedule(mutation, settings)
        print(
            f"[PLAN {position:03}] {mutation.parameters['case']} "
            f"{mutation.mutated_payload.hex().upper()} "
            f"({len(payloads)} frame(s) @ {interval_ms:g}ms)"
        )
    if not args.execute:
        print("[SAFE] Preview only; add --execute (-x) to transmit through CANoe.")
        return 0

    interface = interface_factory(**settings.canoe_options)
    try:
        interface.connect()
        sent = transmit_cases(interface, settings, cases)
    except BaseException as run_error:
        try:
            interface.close()
        except Exception as close_error:
            if hasattr(run_error, "add_note"):
                run_error.add_note(
                    f"CANoe close also failed: {type(close_error).__name__}: "
                    f"{close_error}"
                )
        raise
    else:
        interface.close()
    print(f"[DONE] cases={len(cases)}, frames={sent}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except (ConfigurationError, OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
