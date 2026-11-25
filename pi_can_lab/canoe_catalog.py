"""Deterministic 0x366 mutation catalogue for the paired CANoe capture runner.

This module prepares case definitions only. CANoe measurement and transmission
belong to the paired runner, not to catalogue generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from a5_0x366_mutator import BASELINE_PAYLOAD, TARGET_CAN_ID, TARGET_DLC
from can_common import ConfigurationError, load_yaml_config, parse_can_data, parse_int
from paired_cycle import CYCLE_FAMILIES, build_cycle_plan, make_cycle_mutation
from trial_models import MutationCase


DEFAULT_CONFIG = Path(__file__).with_name("canoe_paired.yaml")


@dataclass(frozen=True)
class CatalogSettings:
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


@dataclass(frozen=True)
class MutationCaseSelection:
    cases: tuple[MutationCase, ...]
    case_start: int
    case_end: int
    total_available_cases: int
    catalog_sha256: str
    dbc_sha256: str


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


def load_settings(args: argparse.Namespace) -> CatalogSettings:
    """Load only the values that determine paired-case payloads and timing."""
    config, config_path = load_yaml_config(args.config)
    if config_path is None:
        raise ConfigurationError("CANoe paired config is required")
    target = _mapping(config.get("target"), "target")
    mutation = _mapping(config.get("mutation"), "mutation")
    transmit = _mapping(config.get("transmit"), "transmit")

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

    return CatalogSettings(
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
    )


def select_mutation_cases(
    settings: CatalogSettings, *, first_case: int = 1
) -> MutationCaseSelection:
    """Select one numbered slice of the stable, full mutation catalogue."""
    if isinstance(first_case, bool) or not isinstance(first_case, int) or first_case < 1:
        raise ConfigurationError("case start must be a positive integer")
    try:
        plan = build_cycle_plan(
            settings.dbc_path,
            settings.baseline_payload,
            source_bus=settings.source_bus,
            random_seed=settings.random_seed,
            undefined_max_bits=settings.undefined_max_bits,
            mutation_duration_s=settings.mutation_duration_seconds,
            mutation_interval_ms=settings.interval_ms,
            selected_family=None,
        )
    except ConfigurationError:
        raise
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    # The shared catalogue deduplicates across every family in its fixed order.
    # A family selected on its own may legitimately repeat another family's
    # stimulus; keep that case while still skipping unsafe and same-family
    # duplicates. The old signal_single ordering and payloads stay identical.
    by_index = {int(entry["index"]): entry for entry in plan["entries"]}
    available = []
    seen_in_family: set[str] = set()
    for entry in plan["entries"]:
        if entry["family"] != settings.method:
            continue
        selected = None
        if entry["disposition"] == "scheduled":
            selected = entry
        elif entry["reason"] == "duplicate_tx_stimulus":
            owner = by_index.get(entry["duplicate_of_index"])
            if owner is not None and owner["family"] != settings.method:
                selected = {
                    **entry, "disposition": "scheduled", "reason": None,
                }
        if selected is not None:
            fingerprint = str(selected["tx_fingerprint"])
            if fingerprint not in seen_in_family:
                seen_in_family.add(fingerprint)
                available.append(selected)
    if not available:
        raise ConfigurationError(
            f"mutation method {settings.method!r} has no safe distinct cases"
        )
    if first_case > len(available):
        raise ConfigurationError(
            f"case start {first_case} exceeds {len(available)} available "
            f"{settings.method} cases"
        )
    entries = available[first_case - 1:first_case - 1 + settings.max_cases]
    cases = tuple(
        make_cycle_mutation(
            entry,
            mutation_id=index,
            source_bus=settings.source_bus,
            original_payload=settings.baseline_payload,
            random_seed=settings.random_seed,
        )
        for index, entry in enumerate(entries, start=first_case)
    )
    # paired_cycle's catalogue digest includes the absolute DBC path. That
    # makes identical checkouts disagree about the catalogue. Bind this digest
    # to DBC bytes, settings, and every scheduled case instead of a machine path.
    portable_catalogue = {
        "hash_scheme": "canoe_paired_catalog_v1",
        "dbc_sha256": str(plan["dbc_sha256"]),
        "source_bus": settings.source_bus,
        "frame_id": settings.frame_id,
        "baseline_payload_hex": settings.baseline_payload.hex().upper(),
        "method": settings.method,
        "random_seed": settings.random_seed,
        "undefined_max_bits": settings.undefined_max_bits,
        "mutation_duration_seconds": settings.mutation_duration_seconds,
        "interval_ms": settings.interval_ms,
        "restore_original": settings.restore_original,
        "restore_count": settings.restore_count,
        "cases": [
            {
                "number": number,
                "entry_index": int(entry["index"]),
                "entry_id": str(entry["entry_id"]),
                "family": str(entry["family"]),
                "name": str(entry["case"]),
                "payload_hex": str(entry["mutated_payload"]).upper(),
                **({"tx_fingerprint": str(entry["tx_fingerprint"])}
                   if settings.method == "temporal_sequence" else {}),
            }
            for number, entry in enumerate(available, start=1)
        ],
    }
    serialized = json.dumps(
        portable_catalogue, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    catalog_sha256 = hashlib.sha256(serialized).hexdigest()
    return MutationCaseSelection(
        cases=cases,
        case_start=first_case,
        case_end=first_case + len(cases) - 1,
        total_available_cases=len(available),
        catalog_sha256=catalog_sha256,
        dbc_sha256=str(plan["dbc_sha256"]),
    )


def build_slot_frames(
    case: MutationCase, settings: CatalogSettings,
) -> tuple[tuple[float, bytes], ...]:
    """Recreate the bounded mutation schedule frozen by ``build_cycle_plan``.

    Static families repeat one payload. Temporal cases cycle through their
    declared sequence at its own interval; the first payload alone does not
    represent the mutation.
    """
    if case.can_id != settings.frame_id or case.original_payload != settings.baseline_payload:
        raise ConfigurationError("mutation target or source payload differs from catalogue settings")
    if case.parameters.get("mutation_family") != settings.method:
        raise ConfigurationError("mutation family differs from catalogue settings")
    sequence = case.parameters.get("sequence")
    if sequence is None:
        payloads = (bytes(case.mutated_payload),)
        interval_ms = settings.interval_ms
    else:
        if not isinstance(sequence, Mapping):
            raise ConfigurationError("temporal mutation sequence must be a mapping")
        raw_frames = sequence.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise ConfigurationError("temporal mutation sequence has no frames")
        payloads = tuple(parse_can_data(frame) for frame in raw_frames)
        interval_ms = _finite(
            sequence.get("interval_ms", settings.interval_ms),
            "temporal sequence interval_ms", minimum=50.0,
        )
        if interval_ms not in (50.0, 100.0):
            raise ConfigurationError(
                "paired temporal slot supports only 50ms or 100ms intervals"
            )
    if any(len(payload) != TARGET_DLC for payload in payloads):
        raise ConfigurationError("0x366 mutation payloads must be exactly eight bytes")
    if payloads[0] != case.mutated_payload or payloads[0] == settings.baseline_payload:
        raise ConfigurationError("first mutation payload differs from frozen catalogue")
    frame_count = math.ceil(settings.mutation_duration_seconds * 1000.0 / interval_ms)
    if not 1 <= frame_count <= 20 or (sequence is not None and frame_count < 2 * len(payloads)):
        raise ConfigurationError("mutation slot is outside the bounded catalogue schedule")
    frames = tuple(
        (index * interval_ms / 1000.0, payloads[index % len(payloads)])
        for index in range(frame_count)
    )
    if frames[-1][0] >= settings.mutation_duration_seconds:
        raise ConfigurationError("mutation slot frame offset exceeds its duration")
    return frames
