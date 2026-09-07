"""Deterministic previous-trial feedback strategy selection.

Adapted from ``src/feedback/strategy_selector.py`` for the standalone lab.  It
selects exactly one mutation for the next completed-trial iteration.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from mutation_engine import Mutator
from trial_models import MutationCase


BOUNDARIES = (0x00, 0x01, 0x7F, 0x80, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF)


@dataclass(frozen=True)
class StrategyDecision:
    mode: str
    strategy: str
    parent_mutation_id: Optional[int]
    focus_byte: Optional[int]
    reason: str


class TrialStrategySelector:
    def __init__(self, config: Mapping[str, Any]):
        self.no_anomaly_exploration = self._probability(
            config.get("no_anomaly", {}).get("exploration_probability", 0.8),
            "no_anomaly.exploration_probability",
        )
        self.interesting_exploitation = self._probability(
            config.get("interesting", {}).get("exploitation_probability", 0.7),
            "interesting.exploitation_probability",
        )
        self.bit_operation_ratio = self._probability(
            config.get("bit_operation_ratio", 0.75), "bit_operation_ratio"
        )
        self.max_operations = max(1, int(config.get("max_operations", 1)))

    @staticmethod
    def _probability(value: Any, name: str) -> float:
        parsed = float(value)
        if not 0.0 <= parsed <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return parsed

    @staticmethod
    def _rng(random_seed: int, state: Mapping[str, Any]) -> random.Random:
        def semantic(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {
                    str(key): semantic(item)
                    for key, item in value.items()
                    if key not in {"updated_at", "created_at"}
                }
            if isinstance(value, list):
                return [semantic(item) for item in value]
            return value

        stable_state = json.dumps(
            semantic(state), sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(f"{random_seed}:{stable_state}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def decide(self, state: Mapping[str, Any], random_seed: int) -> StrategyDecision:
        if int(state.get("total_trials", 0)) == 0:
            return StrategyDecision("EXPLORE", "INITIAL", None, None, "No previous feedback")

        rng = self._rng(random_seed, state)
        last = state.get("last_feedback") or {}
        interesting = bool(last.get("interesting"))
        if interesting and rng.random() < self.interesting_exploitation:
            parent = self._best_parent(state)
            region = parent.get("mutation", {}).get("changed_byte_indexes", []) if parent else []
            return StrategyDecision(
                "EXPLOIT", "ANOMALY_NEIGHBORHOOD",
                int(parent["mutation_id"]) if parent else None,
                int(region[0]) if region else None,
                "Previous completed trial produced an interesting anomaly",
            )
        if not interesting and rng.random() >= self.no_anomaly_exploration:
            history = state.get("mutation_history", [])
            parent = history[-1] if history else None
            region = parent.get("changed_byte_indexes", []) if parent else []
            return StrategyDecision(
                "EXPLOIT", "REVISIT", int(parent["mutation_id"]) if parent else None,
                int(region[0]) if region else None,
                "No anomaly; configured revisit branch selected",
            )
        return StrategyDecision(
            "EXPLORE", "GENERAL_MUTATION", None, None,
            "Expand exploration after completed-trial feedback",
        )

    @staticmethod
    def _best_parent(state: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        candidates = list(state.get("interesting_mutations", []))
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (float(item.get("score", 0.0)), int(item.get("mutation_id", 0))),
        )

    def select_mutation(
        self,
        *,
        state: Mapping[str, Any],
        original_payload: bytes,
        source_bus: str,
        can_id: int,
        random_seed: int,
    ) -> tuple[MutationCase, StrategyDecision]:
        decision = self.decide(state, random_seed)
        mutation_id = int(state.get("next_mutation_id", 1))
        rng = self._rng(random_seed, state)

        parent = self._parent_mutation(state, decision.parent_mutation_id)
        candidate: Optional[bytes] = None
        operator = "BIT_FLIP"
        parameters: dict[str, Any] = {}

        if decision.mode == "EXPLOIT" and parent is not None:
            candidate, operator, parameters = self._exploit_candidate(
                original_payload, parent, state, rng
            )
        if candidate is None or candidate == original_payload:
            candidate, operator, parameters = self._explore_candidate(
                original_payload, rng
            )
            if decision.mode == "EXPLOIT":
                decision = StrategyDecision(
                    "EXPLORE", "GENERAL_MUTATION", None, None,
                    "No valid follow-up candidate; deterministic exploration fallback",
                )

        return MutationCase(
            mutation_id=mutation_id,
            source_bus=source_bus.lower(),
            can_id=can_id,
            operator=operator,
            original_payload=original_payload,
            mutated_payload=candidate,
            random_seed=random_seed,
            parent_mutation_id=decision.parent_mutation_id,
            generation_reason=decision.reason,
            strategy_mode=decision.mode,
            parameters=parameters,
        ), decision

    @staticmethod
    def _parent_mutation(
        state: Mapping[str, Any], mutation_id: Optional[int]
    ) -> Optional[MutationCase]:
        if mutation_id is None:
            return None
        for item in reversed(list(state.get("mutation_history", []))):
            if int(item.get("mutation_id", -1)) == mutation_id:
                return MutationCase.from_dict(item)
        for item in state.get("interesting_mutations", []):
            mutation = item.get("mutation", {})
            if int(mutation.get("mutation_id", -1)) == mutation_id:
                return MutationCase.from_dict(mutation)
        return None

    def _explore_candidate(
        self, original: bytes, rng: random.Random
    ) -> tuple[bytes, str, dict[str, Any]]:
        state = random.getstate()
        try:
            random.seed(rng.getrandbits(64))
            mutator = Mutator(original, {
                "manager.budget": 2,
                "manager.max_ops": self.max_operations,
                "manager.structural": False,
                "manager.include_original": False,
                "manager.bit_operation_ratio": self.bit_operation_ratio,
            })
            payloads = mutator.mutate_manager()
        finally:
            random.setstate(state)
        for payload, operators in zip(payloads, mutator.generated_operators):
            if payload != original and len(payload) == len(original):
                normalized = [name.upper() for name in operators]
                operator = normalized[0] if len(normalized) == 1 else "MULTI"
                return payload, operator, {"operators": normalized}
        raise RuntimeError("Unable to generate a non-noop exploration mutation")

    def _exploit_candidate(
        self,
        original: bytes,
        parent: MutationCase,
        state: Mapping[str, Any],
        rng: random.Random,
    ) -> tuple[Optional[bytes], str, dict[str, Any]]:
        if len(parent.mutated_payload) != len(original):
            return None, "", {}
        focus = list(parent.changed_bytes)
        if not focus:
            return None, "", {}
        byte_index = rng.choice(focus)
        anomaly_types = set()
        for item in state.get("interesting_mutations", []):
            if int(item.get("mutation_id", -1)) == parent.mutation_id:
                anomaly_types.update(str(value).upper() for value in item.get("anomaly_types", []))

        candidates: list[tuple[bytes, str, dict[str, Any]]] = []
        interesting_value = parent.mutated_payload[byte_index]
        if anomaly_types & {"TIMING", "FREQUENCY_CHANGE"}:
            values = list(BOUNDARIES) + [
                max(0, interesting_value - 2), max(0, interesting_value - 1),
                min(255, interesting_value + 1), min(255, interesting_value + 2),
            ]
            for value in values:
                changed = bytearray(original)
                changed[byte_index] = value
                candidates.append((bytes(changed), "BOUNDARY", {"byte_index": byte_index, "value": value}))

        for bit_index in range(8):
            changed = bytearray(original)
            changed[byte_index] ^= 1 << bit_index
            candidates.append((bytes(changed), "BIT_FLIP", {"byte_index": byte_index, "bit_index": bit_index}))

        for delta in (-2, -1, 1, 2):
            changed = bytearray(original)
            changed[byte_index] = max(0, min(255, interesting_value + delta))
            candidates.append((bytes(changed), "NEIGHBOR", {"byte_index": byte_index, "delta": delta}))

        valid = [item for item in candidates if item[0] != original]
        return rng.choice(valid) if valid else (None, "", {})
