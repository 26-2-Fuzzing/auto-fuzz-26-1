"""Standalone CAN payload mutator used by pi_can_lab.

Mutation behavior is kept in sync with the original src mutation engine,
without importing the src package.
"""

from __future__ import annotations

import random
from typing import Dict, List


DEFAULT_BYTE_K_PROB = 0.3
DEFAULT_BYTE_WEIGHT = 1.0
DEFAULT_EDGE_BIAS = 0.0

DEFAULT_BIT_K_PROB = 0.4
DEFAULT_BIT_WEIGHT = 1.0
DEFAULT_MSB_BIAS = 0.0
DEFAULT_LSB_BIAS = 0.0
DEFAULT_BUDGET = 256
DEFAULT_MAX_OPS = 3
DEFAULT_BIT_OPERATION_RATIO = None

MAX_CAN_DLC = 8


class Mutator:
    def __init__(self, data: bytes, weights: Dict[str, float], min_length: int = 1):
        self.data = bytearray(data)
        self.weights = weights
        self.min_length = min_length
        self.generated: list[bytes] = []
        self.generated_operators: list[tuple[str, ...]] = []

    def _log_fail(self, name: str, error: Exception) -> None:
        print(f"[!] {name} 예외 발생: {type(error).__name__}")

    def _as_list(self, value) -> List[int]:
        if isinstance(value, int):
            return [value]
        return value or []

    def _safe_random_choice(self, seq: List[int]) -> int | None:
        if not seq:
            return None
        return random.choice(seq)

    def mutate_manager(self) -> list[bytes]:
        budget = int(self.weights.get("manager.budget", DEFAULT_BUDGET))
        max_ops = int(self.weights.get("manager.max_ops", DEFAULT_MAX_OPS))
        enable_struct = bool(self.weights.get("manager.structural", True))
        include_orig = bool(self.weights.get("manager.include_original", False))
        bit_operation_ratio = self.weights.get(
            "manager.bit_operation_ratio", DEFAULT_BIT_OPERATION_RATIO
        )
        if bit_operation_ratio is not None:
            bit_operation_ratio = float(bit_operation_ratio)
            if not 0.0 <= bit_operation_ratio <= 1.0:
                raise ValueError("manager.bit_operation_ratio must be between 0 and 1")

        out: list[bytes] = []
        traces: list[tuple[str, ...]] = []
        seen: set[bytes] = set()
        base = bytes(self.data)
        current_trace: list[str] = []

        def push() -> None:
            payload = bytes(self.data)
            if len(payload) > MAX_CAN_DLC:
                return
            if len(payload) < self.min_length:
                return
            if payload not in seen:
                seen.add(payload)
                out.append(payload)
                traces.append(tuple(current_trace) or ("original",))

        if include_orig:
            self.data = bytearray(base)
            current_trace = ["original"]
            push()

        attempt_cap = budget * 20
        attempts = 0

        while len(out) < budget and attempts < attempt_cap:
            attempts += 1
            saved = self.data
            self.data = bytearray(base)
            current_trace = []

            try:
                operation_count = random.randint(1, max_ops)
                for _ in range(operation_count):
                    if bit_operation_ratio is None:
                        # Compatibility mode: preserve the original src Mutator
                        # operator selection and random call sequence.
                        operation = random.choice([
                            "flip_bit",
                            "increment_bit",
                            "decrement_bit",
                            "increment_byte",
                            "decrement_byte",
                            "insert_byte" if enable_struct else "increment_byte",
                            "delete_byte" if enable_struct else "decrement_byte",
                        ])
                    elif random.random() < bit_operation_ratio:
                        operation = random.choice([
                            "flip_bit",
                            "increment_bit",
                            "decrement_bit",
                        ])
                    else:
                        byte_operations = ["increment_byte", "decrement_byte"]
                        if enable_struct:
                            byte_operations.extend(["insert_byte", "delete_byte"])
                        operation = random.choice(byte_operations)
                    current_trace.append(operation)
                    try:
                        getattr(self, operation)()
                    except Exception as error:
                        self._log_fail(operation, error)
                push()
            except Exception as error:
                self._log_fail("mutate_manager", error)
            finally:
                self.data = saved

        self.generated = out
        self.generated_operators = traces
        return out

    def flip_bit(self) -> None:
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return
            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return
            self.data[byte_idx] ^= 1 << bit_idx
        except Exception as error:
            self._log_fail("flip_bit", error)

    def increment_bit(self) -> None:
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return
            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return
            mask = 1 << bit_idx
            if (self.data[byte_idx] & mask) == 0:
                self.data[byte_idx] |= mask
        except Exception as error:
            self._log_fail("increment_bit", error)

    def decrement_bit(self) -> None:
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return
            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return
            mask = 1 << bit_idx
            if (self.data[byte_idx] & mask) != 0:
                self.data[byte_idx] &= ~mask
        except Exception as error:
            self._log_fail("decrement_bit", error)

    def increment_byte(self) -> None:
        try:
            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return
            self.data[idx] = (self.data[idx] + 1) & 0xFF
        except Exception as error:
            self._log_fail("increment_byte", error)

    def decrement_byte(self) -> None:
        try:
            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return
            self.data[idx] = (self.data[idx] - 1) & 0xFF
        except Exception as error:
            self._log_fail("decrement_byte", error)

    def insert_byte(self) -> None:
        try:
            if len(self.data) >= MAX_CAN_DLC:
                return
            position = random.randint(0, len(self.data))
            value = random.randint(0, 255)
            self.data.insert(position, value)
        except Exception as error:
            self._log_fail("insert_byte", error)

    def delete_byte(self) -> None:
        try:
            if len(self.data) <= self.min_length:
                return
            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return
            del self.data[idx]
        except Exception as error:
            self._log_fail("delete_byte", error)

    def select_random_byte(self) -> list[int]:
        length = len(self.data)
        if length == 0:
            raise ValueError("빈 데이터에서는 바이트를 선택할 수 없습니다.")

        probability = float(self.weights.get("byte_k_p", DEFAULT_BYTE_K_PROB))
        count = 1
        while random.random() > probability and count < length:
            count += 1
        count = min(count, max(1, length // 3))

        weights = [
            float(self.weights.get(f"byte:{index}", DEFAULT_BYTE_WEIGHT))
            for index in range(length)
        ]
        edge_bias = float(self.weights.get("edge_bias", DEFAULT_EDGE_BIAS))
        if edge_bias > 0 and length >= 2:
            weights[0] += edge_bias
            weights[-1] += edge_bias

        if sum(weights) <= 0:
            return random.sample(range(length), count)
        return sorted(set(random.choices(range(length), weights=weights, k=count)))

    def select_random_bit(self) -> list[int]:
        probability = float(self.weights.get("bit_k_p", DEFAULT_BIT_K_PROB))
        count = 1
        while random.random() > probability and count < 8:
            count += 1
        count = min(count, 8)

        weights = [
            float(self.weights.get(f"bit:{bit}", DEFAULT_BIT_WEIGHT))
            for bit in range(8)
        ]
        msb_bias = float(self.weights.get("msb_bias", DEFAULT_MSB_BIAS))
        lsb_bias = float(self.weights.get("lsb_bias", DEFAULT_LSB_BIAS))

        if msb_bias > 0:
            weights = [
                weight + msb_bias * (bit / 7.0)
                for bit, weight in enumerate(weights)
            ]
        if lsb_bias > 0:
            weights = [
                weight + lsb_bias * ((7 - bit) / 7.0)
                for bit, weight in enumerate(weights)
            ]

        if sum(weights) <= 0:
            return random.sample(range(8), count)
        return sorted(set(random.choices(range(8), weights=weights, k=count)))
