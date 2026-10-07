"""Frozen, bounded catalogue for repeated 0x366 mutation/no-op pairs.

The catalogue is exploratory: it never selects from anomaly feedback, retries
an uncertain transmission, or labels a response as verified.  The caller owns
the atomic manifest write and must reconcile a completed pair before advancing
the cursor after a crash.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Mapping, Optional

from a5_0x366_mutator import (
    A5BlinkmodiMutator,
    PROFILE_FAMILIES,
    TARGET_CAN_ID,
    TARGET_DLC,
    TargetedMutation,
)
from trial_models import MutationCase


CYCLE_SCHEMA_VERSION = 1
CYCLE_FAMILIES = PROFILE_FAMILIES["all-0x366"]
MAX_CATALOG_ENTRIES = 2000
MAX_MUTATION_FRAMES = 20
_UNSPECIFIED_FAMILY = object()


def _digest(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _planned_frames(duration_seconds: float, interval_ms: float) -> int:
    return int((
        Decimal(str(duration_seconds)) * Decimal(1000) / Decimal(str(interval_ms))
    ).to_integral_value(rounding=ROUND_CEILING))


def _stimulus(candidate: TargetedMutation, *, duration_seconds: float,
              normal_interval_ms: float) -> tuple[Optional[str], Optional[str]]:
    """Return the effective TX fingerprint and a reason to skip, if unsafe.

    A temporal pattern is useful only when the bounded mutation phase can
    deliver two complete cycles.  Fingerprinting the actual scheduled frames
    also detects a constant temporal sequence identical to a static payload.
    """
    sequence = candidate.sequence
    interval_ms = float(sequence.interval_ms) if sequence else normal_interval_ms
    if interval_ms < 50:
        return None, "temporal_interval_below_50ms_safety_limit"
    frame_count = _planned_frames(duration_seconds, interval_ms)
    if frame_count > MAX_MUTATION_FRAMES:
        return None, "mutation_frame_limit_exceeded"
    if sequence:
        if frame_count < 2 * len(sequence.frames):
            return None, "fewer_than_two_complete_temporal_cycles"
        payloads = sequence.frames
    else:
        payloads = (candidate.mutated_payload,)
    frames = [payloads[index % len(payloads)].hex().upper()
              for index in range(frame_count)]
    return _digest({"interval_ms": interval_ms, "frames": frames}), None


def _catalogue_fields(plan: Mapping[str, Any]) -> dict[str, Any]:
    fields = {key: plan[key] for key in (
        "schema_version", "source_bus", "target_id", "baseline_payload",
        "random_seed", "dbc_path", "dbc_sha256", "undefined_max_bits",
        "mutation_duration_s", "mutation_interval_ms", "families", "entries",
        "scheduled_count", "skipped_count", "family_counts",
    )}
    # Version 1 plans created before family selection have no such field.  Keep
    # their catalogue fingerprint byte-for-byte stable for existing resumes.
    if "selected_family" in plan:
        fields["selected_family"] = plan["selected_family"]
    return fields


def build_cycle_plan(
    dbc_path: str | Path,
    original_payload: bytes,
    *,
    source_bus: str,
    random_seed: int,
    undefined_max_bits: int = 2,
    mutation_duration_s: float = 1.0,
    mutation_interval_ms: float = 50.0,
    selected_family: str | None = None,
) -> dict[str, Any]:
    """Freeze every distinct, sendable case of each actual 0x366 family.

    This function only reads the DBC and constructs data.  No CAN or SSH
    operation occurs.  The first occurrence owns a duplicate stimulus; later
    cases remain in the catalogue with an explicit skip reason.
    """
    source_bus = source_bus.lower()
    if source_bus not in {"p_can", "b_can", "i_can"}:
        raise ValueError("source_bus must be p_can, b_can, or i_can")
    if selected_family is not None and selected_family not in CYCLE_FAMILIES:
        raise ValueError(f"unknown cycle family: {selected_family!r}")
    original_payload = bytes(original_payload)
    if len(original_payload) != TARGET_DLC:
        raise ValueError("0x366 original payload must be exactly 8 bytes")
    if isinstance(undefined_max_bits, bool) or not isinstance(undefined_max_bits, int) or undefined_max_bits < 2:
        raise ValueError("undefined_max_bits must be an integer >= 2")
    duration = float(mutation_duration_s)
    interval = float(mutation_interval_ms)
    if not math.isfinite(duration) or not 0 < duration <= 1:
        raise ValueError("mutation_duration_s must be finite and at most 1 second")
    if not math.isfinite(interval) or interval < 50:
        raise ValueError("mutation_interval_ms must be finite and at least 50")
    if _planned_frames(duration, interval) > MAX_MUTATION_FRAMES:
        raise ValueError("mutation schedule exceeds the 20-frame safety limit")

    dbc_path = Path(dbc_path).expanduser().resolve()
    dbc_sha256 = hashlib.sha256(dbc_path.read_bytes()).hexdigest()
    generator = A5BlinkmodiMutator(dbc_path, original_payload)
    raw_cases = generator.generate_profile("all-0x366", undefined_max_bits)
    if len(raw_cases) > MAX_CATALOG_ENTRIES:
        raise ValueError("0x366 candidate catalogue exceeds the explicit size guard")
    family_position = {family: index for index, family in enumerate(CYCLE_FAMILIES)}
    raw_cases.sort(key=lambda item: (
        family_position[item.mutation_family], item.case,
        item.mutated_payload.hex(),
        json.dumps(item.sequence.to_dict() if item.sequence else {}, sort_keys=True),
    ))

    seen: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    family_counts = {family: {"raw": 0, "scheduled": 0, "skipped": 0}
                     for family in CYCLE_FAMILIES}
    for index, candidate in enumerate(raw_cases):
        family = candidate.mutation_family
        family_counts[family]["raw"] += 1
        fingerprint, reason = _stimulus(
            candidate, duration_seconds=duration, normal_interval_ms=interval
        )
        duplicate_of = None
        if reason is None and candidate.mutated_payload == original_payload:
            # can_sender rejects an explicit mutation whose first payload is
            # the source payload, even if later temporal frames differ.
            reason = "first_payload_equals_original_sender_contract"
        elif reason is None and fingerprint in seen:
            duplicate_of = seen[fingerprint]
            reason = "duplicate_tx_stimulus"
        if reason is None:
            if fingerprint is None:
                raise AssertionError("sendable candidate has no TX fingerprint")
            seen[fingerprint] = index
        # Decide safety and global duplicates against the complete catalogue
        # before filtering.  A selected family must not reclaim a stimulus
        # already owned by an earlier family.
        if reason is None and selected_family is not None and family != selected_family:
            reason = "family_not_selected"
        if reason is None:
            family_counts[family]["scheduled"] += 1
        else:
            family_counts[family]["skipped"] += 1
        entry = {
            "index": index,
            "entry_id": f"cycle_case_{index:04d}",
            "family": family,
            "case": candidate.case,
            "disposition": "scheduled" if reason is None else "skipped",
            "reason": reason,
            "duplicate_of_index": duplicate_of,
            "tx_fingerprint": fingerprint,
            "mutated_payload": candidate.mutated_payload.hex().upper(),
            "targeted_parameters": candidate.parameters(),
        }
        entries.append(entry)

    scheduled_count = sum(counts["scheduled"] for counts in family_counts.values())
    if not scheduled_count:
        if selected_family is not None:
            raise ValueError(f"cycle family {selected_family!r} has no safe distinct candidates")
        raise ValueError("0x366 catalogue has no safe distinct candidates")
    plan: dict[str, Any] = {
        "schema_version": CYCLE_SCHEMA_VERSION,
        "source_bus": source_bus,
        "target_id": f"0x{TARGET_CAN_ID:X}",
        "baseline_payload": original_payload.hex().upper(),
        "random_seed": int(random_seed),
        "dbc_path": str(dbc_path),
        "dbc_sha256": dbc_sha256,
        "undefined_max_bits": undefined_max_bits,
        "mutation_duration_s": duration,
        "mutation_interval_ms": interval,
        "families": list(CYCLE_FAMILIES),
        "entries": entries,
        "scheduled_count": scheduled_count,
        "skipped_count": len(entries) - scheduled_count,
        "family_counts": family_counts,
        "cursor": 0,
        "completed_pairs": [],
        "status": "prepared",
    }
    if selected_family is not None:
        plan["selected_family"] = selected_family
    plan["catalog_sha256"] = _digest(_catalogue_fields(plan))
    validate_cycle_plan(plan)
    return plan


def validate_cycle_plan(
    plan: Mapping[str, Any], *, dbc_path: str | Path | None = None,
    selected_family: str | None | object = _UNSPECIFIED_FAMILY,
) -> None:
    """Reject an altered catalogue or cursor before a resumed exposure."""
    try:
        if int(plan["schema_version"]) != CYCLE_SCHEMA_VERSION:
            raise ValueError("unsupported cycle schema")
        if plan["target_id"] != f"0x{TARGET_CAN_ID:X}":
            raise ValueError("cycle target ID changed")
        if plan["families"] != list(CYCLE_FAMILIES):
            raise ValueError("cycle family order changed")
        plan_family = plan.get("selected_family")
        if ("selected_family" in plan
                and (not isinstance(plan_family, str) or plan_family not in CYCLE_FAMILIES)):
            raise ValueError("invalid frozen cycle family selection")
        if selected_family is not _UNSPECIFIED_FAMILY and selected_family != plan_family:
            raise ValueError("cycle family selection changed")
        entries = plan["entries"]
        if not isinstance(entries, list) or len(entries) > MAX_CATALOG_ENTRIES:
            raise ValueError("invalid cycle entries")
        if plan_family is not None and any(
            entry["disposition"] == "scheduled" and entry["family"] != plan_family
            for entry in entries
        ):
            raise ValueError("cycle schedule contains a nonselected family")
        if plan["catalog_sha256"] != _digest(_catalogue_fields(plan)):
            raise ValueError("frozen cycle catalogue fingerprint mismatch")
        if any(entry["index"] != index or entry["family"] not in CYCLE_FAMILIES
               for index, entry in enumerate(entries)):
            raise ValueError("cycle entry order changed")
        scheduled = [entry["index"] for entry in entries
                     if entry["disposition"] == "scheduled"]
        if len(scheduled) != plan["scheduled_count"] or len(entries) - len(scheduled) != plan["skipped_count"]:
            raise ValueError("cycle entry counts changed")
        ledger = plan["completed_pairs"]
        if not isinstance(ledger, list) or len(ledger) > len(scheduled):
            raise ValueError("invalid cycle completion ledger")
        if any(not isinstance(item, Mapping) or item.get(
            "comparability_status", "comparable"
        ) not in {"comparable", "inconclusive"} for item in ledger):
            raise ValueError("invalid cycle pair comparability status")
        if [item["entry_index"] for item in ledger] != scheduled[:len(ledger)]:
            raise ValueError("cycle ledger is not a scheduled prefix")
        if len({item["pair_id"] for item in ledger}) != len(ledger):
            raise ValueError("cycle pair IDs are not unique")
        expected_cursor = ledger[-1]["entry_index"] + 1 if ledger else 0
        if plan["cursor"] != expected_cursor:
            raise ValueError("cycle cursor and completion ledger disagree")
        done = len(ledger) == len(scheduled)
        if plan["status"] not in {"prepared", "active", "completed"}:
            raise ValueError("invalid cycle status")
        if (plan["status"] == "completed") != done:
            raise ValueError("cycle completion status disagrees with ledger")
        if dbc_path is not None:
            actual_path = Path(dbc_path).expanduser().resolve()
            if str(actual_path) != plan["dbc_path"] or hashlib.sha256(actual_path.read_bytes()).hexdigest() != plan["dbc_sha256"]:
                raise ValueError("DBC changed since cycle creation")
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("invalid frozen cycle manifest") from exc


def next_cycle_entry(plan: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return the next scheduled case, bypassing recorded skips."""
    validate_cycle_plan(plan)
    if plan["status"] == "completed":
        return None
    return next((entry for entry in plan["entries"][plan["cursor"]:]
                 if entry["disposition"] == "scheduled"), None)


def make_cycle_mutation(
    entry: Mapping[str, Any], *, mutation_id: int,
    source_bus: str, original_payload: bytes, random_seed: int,
) -> MutationCase:
    """Turn a frozen catalogue case into an exploratory trial, never feedback."""
    if entry.get("disposition") != "scheduled":
        raise ValueError("only a scheduled cycle case may be transmitted")
    family = str(entry["family"])
    if family not in CYCLE_FAMILIES:
        raise ValueError("unknown cycle mutation family")
    parameters = copy.deepcopy(entry["targeted_parameters"])
    payload = bytes.fromhex(str(entry["mutated_payload"]))
    original_payload = bytes(original_payload)
    if (len(original_payload) != TARGET_DLC or len(payload) != TARGET_DLC
            or payload == original_payload
            or parameters.get("mutation_family") != family
            or parameters.get("case") != entry["case"]
            or parameters.get("base_payload") != original_payload.hex().upper()
            or parameters.get("mutated_payload") != payload.hex().upper()):
        raise ValueError("cycle entry conflicts with frozen source or payload")
    parameters["mutation_profile"] = "all-0x366"
    parameters["cycle_entry_index"] = int(entry["index"])
    parameters["cycle_entry_id"] = str(entry["entry_id"])
    return MutationCase(
        mutation_id=int(mutation_id), source_bus=source_bus.lower(),
        can_id=TARGET_CAN_ID, operator=family.upper(),
        original_payload=original_payload, mutated_payload=payload,
        random_seed=int(random_seed), parent_mutation_id=None,
        generation_reason="Frozen ordered 0x366 exploration; no feedback or exploit",
        strategy_mode="EXPLORE", parameters=parameters,
    )


def advance_cycle(
    plan: Mapping[str, Any], entry_index: int, pair_id: str,
    *, comparability_status: str = "comparable",
) -> dict[str, Any]:
    """Advance after the caller has reconciled a completed pair and its evidence."""
    entry = next_cycle_entry(plan)
    if entry is None or entry["index"] != entry_index:
        raise ValueError("cycle can advance only its next scheduled entry")
    if not isinstance(pair_id, str) or not pair_id.startswith("pair_") or not pair_id[5:].isdigit():
        raise ValueError("pair_id must identify a persisted pair")
    if comparability_status not in {"comparable", "inconclusive"}:
        raise ValueError("invalid cycle pair comparability status")
    updated = copy.deepcopy(dict(plan))
    recorded = {"entry_index": entry_index, "pair_id": pair_id}
    if comparability_status == "inconclusive":
        recorded["comparability_status"] = comparability_status
    updated["completed_pairs"].append(recorded)
    updated["cursor"] = entry_index + 1
    updated["status"] = "completed" if len(updated["completed_pairs"]) == updated["scheduled_count"] else "active"
    validate_cycle_plan(updated)
    return updated
