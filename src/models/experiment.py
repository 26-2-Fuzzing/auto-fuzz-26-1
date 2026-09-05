from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class AnomalyType(str, Enum):
    TIMING = "timing"
    NEW_MESSAGE = "new_message"
    MESSAGE_DISAPPEARANCE = "message_disappearance"
    PAYLOAD = "payload"
    SIGNAL_RANGE = "signal_range"
    UDS_RESPONSE = "uds_response"
    CROSS_BUS = "cross_bus"
    UNKNOWN = "unknown"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class MutationRecord:
    """One injectable payload plus complete mutation provenance."""

    source_bus: str
    message_id: int
    original_data: bytes
    mutated_data: bytes
    operator: str
    seed_id: Optional[int] = None
    changed_bytes: Tuple[int, ...] = ()
    changed_bits: Tuple[Tuple[int, int], ...] = ()
    parameters: Dict[str, Any] = field(default_factory=dict)
    mutation_id: str = field(default_factory=lambda: _new_id("mut"))
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.changed_bytes:
            limit = max(len(self.original_data), len(self.mutated_data))
            changed = tuple(
                i for i in range(limit)
                if (self.original_data[i] if i < len(self.original_data) else None)
                != (self.mutated_data[i] if i < len(self.mutated_data) else None)
            )
            object.__setattr__(self, "changed_bytes", changed)

        if not self.changed_bits and len(self.original_data) == len(self.mutated_data):
            bits: List[Tuple[int, int]] = []
            for byte_index in self.changed_bytes:
                delta = self.original_data[byte_index] ^ self.mutated_data[byte_index]
                bits.extend((byte_index, bit) for bit in range(8) if delta & (1 << bit))
            object.__setattr__(self, "changed_bits", tuple(bits))

    @property
    def fingerprint(self) -> str:
        stable = {
            "bus": self.source_bus,
            "id": self.message_id,
            "original": self.original_data.hex(),
            "mutated": self.mutated_data.hex(),
            "operator": self.operator,
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["original_data"] = self.original_data.hex()
        result["mutated_data"] = self.mutated_data.hex()
        return result


@dataclass(frozen=True)
class AnomalyObservation:
    target_bus: str
    target_id: Optional[int]
    anomaly_type: AnomalyType
    magnitude: float = 0.0
    confidence: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    observation_id: str = field(default_factory=lambda: _new_id("obs"))
    observed_at: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        stable = {
            "bus": self.target_bus,
            "id": self.target_id,
            "type": self.anomaly_type.value,
            "metric": self.evidence.get("metric"),
            "signal": self.evidence.get("signal"),
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["anomaly_type"] = self.anomaly_type.value
        return result


@dataclass
class TrialRecord:
    mutation_id: str
    trial_index: int
    is_control: bool = False
    state_signature: Optional[str] = None
    observations: List[AnomalyObservation] = field(default_factory=list)
    trial_id: str = field(default_factory=lambda: _new_id("trial"))
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass(frozen=True)
class AssociationSummary:
    mutation_id: str
    anomaly_fingerprint: str
    mutation_trials: int
    mutation_hits: int
    control_trials: int = 0
    control_hits: int = 0

    @property
    def reproduction_rate(self) -> float:
        return self.mutation_hits / self.mutation_trials if self.mutation_trials else 0.0

    @property
    def baseline_rate(self) -> float:
        return self.control_hits / self.control_trials if self.control_trials else 0.0

    @property
    def effect_size(self) -> float:
        """Risk difference; association evidence, not proof of causality."""
        return self.reproduction_rate - self.baseline_rate

    @property
    def eligible_for_feedback(self) -> bool:
        # Deliberately conservative defaults; campaigns may impose stricter gates.
        return (
            self.mutation_trials >= 3
            and self.control_trials >= 3
            and self.reproduction_rate >= 0.6
            and self.effect_size >= 0.3
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "reproduction_rate": self.reproduction_rate,
            "baseline_rate": self.baseline_rate,
            "effect_size": self.effect_size,
            "eligible_for_feedback": self.eligible_for_feedback,
        }
