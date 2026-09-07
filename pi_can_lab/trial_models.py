"""Serializable models for iteration-based CAN fuzzing trials."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def changed_locations(original: bytes, mutated: bytes) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...]]:
    changed_bytes = tuple(
        index for index in range(max(len(original), len(mutated)))
        if (original[index] if index < len(original) else None)
        != (mutated[index] if index < len(mutated) else None)
    )
    changed_bits: list[tuple[int, int]] = []
    if len(original) == len(mutated):
        for byte_index in changed_bytes:
            delta = original[byte_index] ^ mutated[byte_index]
            changed_bits.extend(
                (byte_index, bit_index)
                for bit_index in range(8)
                if delta & (1 << bit_index)
            )
    return changed_bytes, tuple(changed_bits)


@dataclass(frozen=True)
class MutationCase:
    mutation_id: int
    source_bus: str
    can_id: int
    operator: str
    original_payload: bytes
    mutated_payload: bytes
    random_seed: int
    parent_mutation_id: Optional[int] = None
    generation_reason: str = "Initial exploration"
    strategy_mode: str = "EXPLORE"
    parameters: dict[str, Any] = field(default_factory=dict)
    signal: Optional[dict[str, Any]] = None
    reproduction_of_mutation_id: Optional[int] = None
    created_at: str = field(default_factory=utc_now)

    @property
    def changed_bytes(self) -> tuple[int, ...]:
        return changed_locations(self.original_payload, self.mutated_payload)[0]

    @property
    def changed_bits(self) -> tuple[tuple[int, int], ...]:
        return changed_locations(self.original_payload, self.mutated_payload)[1]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({
            "source_bus": self.source_bus.upper(),
            "can_id": f"0x{self.can_id:X}",
            "original_payload": " ".join(f"{value:02X}" for value in self.original_payload),
            "mutated_payload": " ".join(f"{value:02X}" for value in self.mutated_payload),
            "changed_byte_indexes": list(self.changed_bytes),
            "changed_bits": [list(item) for item in self.changed_bits],
        })
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MutationCase":
        def payload(name: str) -> bytes:
            return bytes.fromhex(str(value[name]).replace("_", " "))

        can_id = value["can_id"]
        return cls(
            mutation_id=int(value["mutation_id"]),
            source_bus=str(value["source_bus"]).lower(),
            can_id=int(can_id, 0) if isinstance(can_id, str) else int(can_id),
            operator=str(value["operator"]),
            original_payload=payload("original_payload"),
            mutated_payload=payload("mutated_payload"),
            random_seed=int(value.get("random_seed", 0)),
            parent_mutation_id=value.get("parent_mutation_id"),
            generation_reason=str(value.get("generation_reason", "feedback follow-up")),
            strategy_mode=str(value.get("strategy_mode", "EXPLOIT")),
            parameters=dict(value.get("parameters", {})),
            signal=value.get("signal"),
            reproduction_of_mutation_id=value.get("reproduction_of_mutation_id"),
            created_at=str(value.get("created_at", utc_now())),
        )


def mutation_region(mutation: MutationCase) -> dict[str, Any]:
    first_byte = mutation.changed_bytes[0] if mutation.changed_bytes else None
    first_bit = next(
        (bit for byte, bit in mutation.changed_bits if byte == first_byte), None
    )
    return {
        "can_id": f"0x{mutation.can_id:X}",
        "byte_index": first_byte,
        "bit_index": first_bit,
        "signal_name": mutation.signal.get("signal_name") if mutation.signal else None,
    }
