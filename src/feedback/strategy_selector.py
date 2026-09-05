from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..models.experiment import AnomalyObservation, AnomalyType, AssociationSummary, MutationRecord


@dataclass(frozen=True)
class MutationPlan:
    strategy: str
    focus_bytes: tuple[int, ...] = ()
    focus_bits: tuple[tuple[int, int], ...] = ()
    parameters: Dict[str, float] = field(default_factory=dict)
    reason: str = "exploration"


class StrategySelector:
    """Selects how to mutate; scheduling/seed priority remains a separate concern."""

    def __init__(self, exploration_rate: float = 0.2):
        if not 0.0 <= exploration_rate <= 1.0:
            raise ValueError("exploration_rate must be between 0 and 1")
        self.exploration_rate = exploration_rate

    def select(
        self,
        mutation: MutationRecord,
        observation: Optional[AnomalyObservation] = None,
        association: Optional[AssociationSummary] = None,
    ) -> MutationPlan:
        # Exploration is always possible, even around a highly reproducible anomaly.
        if observation is None or random.random() < self.exploration_rate:
            return MutationPlan(strategy="general_mutation")

        if association is not None and not association.eligible_for_feedback:
            return MutationPlan(
                strategy="reproduction_test",
                reason="association has not passed the reproduction/control gate",
            )

        focus_bytes = mutation.changed_bytes
        focus_bits = mutation.changed_bits
        mapping = {
            AnomalyType.TIMING: "value_boundary_search",
            AnomalyType.NEW_MESSAGE: "trigger_minimization",
            AnomalyType.MESSAGE_DISAPPEARANCE: "trigger_minimization",
            AnomalyType.PAYLOAD: "field_localization",
            AnomalyType.SIGNAL_RANGE: "field_localization",
            AnomalyType.UDS_RESPONSE: "diagnostic_sequence_search",
            AnomalyType.CROSS_BUS: "cross_bus_propagation_search",
        }
        strategy = mapping.get(observation.anomaly_type, "general_mutation")
        return MutationPlan(
            strategy=strategy,
            focus_bytes=focus_bytes,
            focus_bits=focus_bits if strategy == "trigger_minimization" else (),
            reason=f"{observation.anomaly_type.value} anomaly",
        )

    def weights_for(self, plan: MutationPlan, data_length: int) -> Dict[str, float]:
        """Translate a plan into Mutator weights without starving other locations."""
        weights = {f"byte:{i}": 1.0 for i in range(data_length)}
        for index in plan.focus_bytes:
            if 0 <= index < data_length:
                weights[f"byte:{index}"] = 5.0
        for _, bit in plan.focus_bits:
            weights[f"bit:{bit}"] = 5.0
        return weights

    def generate_followups(
        self, mutation: MutationRecord, plan: MutationPlan
    ) -> list[MutationRecord]:
        """Materialize anomaly-specific candidates, not merely priority changes."""
        candidates: list[tuple[bytes, Dict[str, object]]] = []
        original = mutation.original_data
        mutated = mutation.mutated_data

        if len(original) != len(mutated):
            return []  # structural changes need a sequence-aware minimizer

        if plan.strategy == "value_boundary_search":
            for index in plan.focus_bytes:
                for value in (0x00, 0x01, 0x7F, 0x80, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF):
                    data = bytearray(mutated)
                    data[index] = value
                    candidates.append((bytes(data), {"byte": index, "value": value}))

        elif plan.strategy == "trigger_minimization":
            # One-step delta debugging: revert one changed bit at a time. Repeated
            # feedback can continue minimizing only candidates that reproduce.
            for byte_index, bit_index in mutation.changed_bits:
                data = bytearray(mutated)
                mask = 1 << bit_index
                data[byte_index] = (
                    (data[byte_index] & ~mask) | (original[byte_index] & mask)
                )
                candidates.append(
                    (bytes(data), {"reverted_byte": byte_index, "reverted_bit": bit_index})
                )

        elif plan.strategy in {"field_localization", "cross_bus_propagation_search"}:
            for index in plan.focus_bytes:
                data = bytearray(original)
                data[index] = mutated[index]
                candidates.append((bytes(data), {"isolated_byte": index}))

        records = []
        seen = {mutated}
        for payload, parameters in candidates:
            if payload in seen or payload == original:
                continue
            seen.add(payload)
            records.append(
                MutationRecord(
                    source_bus=mutation.source_bus,
                    message_id=mutation.message_id,
                    original_data=original,
                    mutated_data=payload,
                    operator=plan.strategy,
                    seed_id=mutation.seed_id,
                    parameters={"parent_mutation_id": mutation.mutation_id, **parameters},
                )
            )
        return records
