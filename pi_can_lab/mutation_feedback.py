"""Anomaly-to-mutation feedback helpers for distributed CAN campaigns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trial_models import MutationCase, mutation_region


CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class FeedbackHint:
    anomaly_type: str
    confidence: str
    score: int
    source_sequence: int
    source_payload: bytes
    changed_byte_indexes: tuple[int, ...]
    changed_bits: tuple[tuple[int, int], ...]
    target_bus: str
    target_id: str
    cross_bus: bool
    mapping_method: str
    latency_ms: float | None

    def summary(self) -> dict[str, Any]:
        return {
            "anomaly_type": self.anomaly_type,
            "confidence": self.confidence,
            "score": self.score,
            "source_sequence": self.source_sequence,
            "source_payload": self.source_payload.hex().upper(),
            "changed_byte_indexes": list(self.changed_byte_indexes),
            "changed_bits": [list(item) for item in self.changed_bits],
            "target_bus": self.target_bus,
            "target_id": self.target_id,
            "cross_bus": self.cross_bus,
            "mapping_method": self.mapping_method,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class GuidedMutation:
    payload: bytes
    strategy: str
    focus_bytes: tuple[int, ...]
    focus_bits: tuple[tuple[int, int], ...]
    hint: FeedbackHint

    def metadata(self) -> dict[str, Any]:
        return {
            "source": "guided",
            "strategy": self.strategy,
            "focus_bytes": list(self.focus_bytes),
            "focus_bits": [list(item) for item in self.focus_bits],
            "feedback": self.hint.summary(),
        }


def _parse_payload(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("feedback source payload must be a hexadecimal string")
    compact = value.replace(" ", "").replace("_", "")
    if len(compact) % 2:
        raise ValueError("feedback source payload must contain complete bytes")
    return bytes.fromhex(compact)


def _changed_bits_from_xor(xor_value: Any) -> tuple[tuple[int, int], ...]:
    if not isinstance(xor_value, str):
        return ()
    xor_payload = _parse_payload(xor_value)
    return tuple(
        (byte_index, bit_index)
        for byte_index, value in enumerate(xor_payload)
        for bit_index in range(8)
        if value & (1 << bit_index)
    )


def classify_anomaly(candidate: Mapping[str, Any]) -> str:
    explicit = candidate.get("anomaly_type")
    if isinstance(explicit, str) and explicit:
        return explicit

    reasons = {str(item) for item in candidate.get("reasons", [])}
    stimulus_frames = int(candidate.get("stimulus_frames", 0))
    rate_ratio = candidate.get("stimulus_to_baseline_rate_ratio")
    if "new ID during stimulus" in reasons:
        return "new_message"
    if stimulus_frames == 0 or (
        isinstance(rate_ratio, (int, float)) and rate_ratio <= 0.1
    ):
        return "message_disappearance"
    if "frame rate changed" in reasons:
        return "timing"
    if (
        "stable DBC signal changed" in reasons
        or "stable baseline payload changed" in reasons
        or "baseline-stable bits changed" in reasons
    ):
        return "payload_signal"
    return "generic"


def load_feedback_hints(path: Path) -> list[FeedbackHint]:
    document = json.loads(path.read_text(encoding="utf-8"))
    hints: list[FeedbackHint] = []

    for bus in document.get("buses", []):
        target_bus = str(bus.get("bus", "unknown"))
        for candidate in bus.get("reaction_candidates", []):
            confidence = str(candidate.get("confidence", "low"))
            if confidence not in {"high", "medium"}:
                continue
            source = candidate.get("source_mutation")
            if not isinstance(source, Mapping):
                continue
            try:
                source_payload = _parse_payload(source.get("payload"))
            except (TypeError, ValueError):
                continue
            mutation = source.get("mutation")
            mutation = mutation if isinstance(mutation, Mapping) else {}
            changed_indexes = tuple(
                sorted({
                    int(index)
                    for index in mutation.get("changed_byte_indexes", [])
                    if isinstance(index, int) or str(index).isdigit()
                })
            )
            try:
                changed_bits = _changed_bits_from_xor(mutation.get("xor_hex"))
            except (TypeError, ValueError):
                changed_bits = ()
            if not changed_indexes:
                changed_indexes = tuple(sorted({index for index, _ in changed_bits}))
            if not changed_indexes:
                continue
            hints.append(
                FeedbackHint(
                    anomaly_type=classify_anomaly(candidate),
                    confidence=confidence,
                    score=int(candidate.get("score", 0)),
                    source_sequence=int(source.get("sequence", 0)),
                    source_payload=source_payload,
                    changed_byte_indexes=changed_indexes,
                    changed_bits=changed_bits,
                    target_bus=target_bus,
                    target_id=str(candidate.get("can_id", "unknown")),
                    cross_bus=bool(source.get("source_bus", "") != target_bus),
                    mapping_method=str(
                        source.get("mapping_method", "nearest_preceding_tx")
                    ),
                    latency_ms=(
                        float(source["latency_ms"])
                        if source.get("latency_ms") is not None else None
                    ),
                )
            )

    hints.sort(
        key=lambda hint: (
            CONFIDENCE_ORDER.get(hint.confidence, 3),
            -hint.score,
            hint.source_sequence,
            hint.target_bus,
            hint.target_id,
        )
    )
    return hints


def _replace_byte(payload: bytes, index: int, value: int) -> bytes:
    changed = bytearray(payload)
    changed[index] = value & 0xFF
    return bytes(changed)


def _toggle_bit(payload: bytes, byte_index: int, bit_index: int) -> bytes:
    changed = bytearray(payload)
    changed[byte_index] ^= 1 << bit_index
    return bytes(changed)


def _boundary_values(original: int, interesting: int) -> list[int]:
    candidates = [
        interesting,
        interesting - 1,
        interesting + 1,
        interesting - 2,
        interesting + 2,
        interesting - 4,
        interesting + 4,
        interesting - 8,
        interesting + 8,
        (original + interesting) // 2,
        0x00,
        0x01,
        0x7F,
        0x80,
        0xFE,
        0xFF,
    ]
    result: list[int] = []
    for value in candidates:
        bounded = max(0, min(0xFF, value))
        if bounded != original and bounded not in result:
            result.append(bounded)
    return result


def _valid_focus_bytes(hint: FeedbackHint, length: int) -> tuple[int, ...]:
    return tuple(index for index in hint.changed_byte_indexes if 0 <= index < length)


def _valid_focus_bits(
    hint: FeedbackHint, length: int
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (byte_index, bit_index)
        for byte_index, bit_index in hint.changed_bits
        if 0 <= byte_index < length and 0 <= bit_index < 8
    )


def generate_guided_mutations(
    base_payload: bytes,
    hints: Sequence[FeedbackHint],
    limit: int,
) -> list[GuidedMutation]:
    """Generate deterministic, fine-grained candidates for prior anomalies."""
    if limit <= 0:
        return []

    output: list[GuidedMutation] = []
    seen = {base_payload}

    def add(
        payload: bytes,
        strategy: str,
        hint: FeedbackHint,
        focus_bytes: Iterable[int] = (),
        focus_bits: Iterable[tuple[int, int]] = (),
    ) -> None:
        if len(output) >= limit or len(payload) != len(base_payload) or payload in seen:
            return
        seen.add(payload)
        output.append(
            GuidedMutation(
                payload=payload,
                strategy=strategy,
                focus_bytes=tuple(focus_bytes),
                focus_bits=tuple(focus_bits),
                hint=hint,
            )
        )

    for hint in hints:
        if len(output) >= limit:
            break
        if len(hint.source_payload) != len(base_payload):
            continue
        focus_bytes = _valid_focus_bytes(hint, len(base_payload))
        focus_bits = _valid_focus_bits(hint, len(base_payload))
        if not focus_bytes:
            continue

        if hint.anomaly_type == "timing":
            for index in focus_bytes:
                for value in _boundary_values(
                    base_payload[index], hint.source_payload[index]
                ):
                    add(
                        _replace_byte(base_payload, index, value),
                        "boundary_search",
                        hint,
                        (index,),
                    )
        elif hint.anomaly_type == "new_message":
            for byte_index, bit_index in focus_bits:
                add(
                    _toggle_bit(base_payload, byte_index, bit_index),
                    "trigger_minimization",
                    hint,
                    (byte_index,),
                    ((byte_index, bit_index),),
                )
            for byte_index, bit_index in focus_bits:
                add(
                    _toggle_bit(hint.source_payload, byte_index, bit_index),
                    "trigger_bit_reversion",
                    hint,
                    (byte_index,),
                    ((byte_index, bit_index),),
                )
        elif hint.anomaly_type == "message_disappearance":
            add(
                hint.source_payload,
                "disappearance_reproduction",
                hint,
                focus_bytes,
                focus_bits,
            )
            for index in focus_bytes:
                add(
                    _replace_byte(
                        hint.source_payload, index, base_payload[index]
                    ),
                    "disappearance_byte_reversion",
                    hint,
                    (index,),
                )
            for byte_index, bit_index in focus_bits:
                add(
                    _toggle_bit(hint.source_payload, byte_index, bit_index),
                    "disappearance_bit_reversion",
                    hint,
                    (byte_index,),
                    ((byte_index, bit_index),),
                )
        else:
            for index in focus_bytes:
                add(
                    _replace_byte(
                        base_payload, index, hint.source_payload[index]
                    ),
                    "field_byte_localization",
                    hint,
                    (index,),
                )
            for byte_index, bit_index in focus_bits:
                add(
                    _toggle_bit(base_payload, byte_index, bit_index),
                    "field_bit_localization",
                    hint,
                    (byte_index,),
                    ((byte_index, bit_index),),
                )

        # Cross-bus propagation keeps the type-specific candidates above, then
        # adds the complete previously interesting payload as a reproduction case.
        if hint.cross_bus:
            add(
                hint.source_payload,
                "cross_bus_reproduction",
                hint,
                focus_bytes,
                focus_bits,
            )

    return output


def create_trial_feedback(
    trial_id: int,
    mutation: MutationCase,
    anomalies: Sequence[Mapping[str, Any]],
    interesting_threshold: float,
) -> dict[str, Any]:
    """Map every completed-trial anomaly to its single tracked mutation."""
    max_score = max((float(item.get("score", 0.0)) for item in anomalies), default=0.0)
    interesting = max_score >= interesting_threshold
    mappings = [
        {
            "trial_id": int(trial_id),
            "mutation_id": mutation.mutation_id,
            "mutation": {
                "source_bus": mutation.source_bus.upper(),
                "source_id": f"0x{mutation.can_id:X}",
                "operator": mutation.operator,
                "byte_indexes": list(mutation.changed_bytes),
                "bits": [list(item) for item in mutation.changed_bits],
            },
            "anomaly": dict(anomaly),
        }
        for anomaly in anomalies
    ]
    reasons = [
        f"{item.get('target_bus')} {item.get('target_id')} {item.get('type')}"
        for item in anomalies
        if float(item.get("score", 0.0)) >= interesting_threshold
    ]
    return {
        "schema_version": 1,
        "trial_id": int(trial_id),
        "mutation_id": mutation.mutation_id,
        "interesting": interesting,
        "anomaly_score": round(max_score, 6),
        "anomaly_types": sorted({str(item.get("type")) for item in anomalies}),
        "reasons": reasons,
        "mutation_region": mutation_region(mutation),
        "parent_mutation_id": mutation.parent_mutation_id,
        "mutation_anomaly_mappings": mappings,
    }
