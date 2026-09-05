import random
from typing import Dict, List, Optional
from ..logger.base_logger import log_event  
from ..models.experiment import MutationRecord

DEFAULT_BYTE_K_PROB = 0.3  
DEFAULT_BYTE_WEIGHT = 1.0  
DEFAULT_EDGE_BIAS   = 0.0  

DEFAULT_BIT_K_PROB  = 0.4  
DEFAULT_BIT_WEIGHT  = 1.0  
DEFAULT_MSB_BIAS    = 0.0  
DEFAULT_LSB_BIAS    = 0.0  
DEFAULT_BUDGET      = 256
DEFAULT_MAX_OPS     = 3

MAX_CAN_DLC = 8  

class Mutator:
    def __init__(self, data: bytes, weights: Dict[str, float], min_length: int = 1):
        self.data = bytearray(data)
        self.weights = weights
        self.min_length = min_length
        self.generated: list[bytes] = []
        self.generated_operators: list[tuple[str, ...]] = []

    # Helpers
    def _log_fail(self, name: str, e: Exception):
        print(f"[!] {name} 예외 발생: {type(e).__name__}")
        log_event("mutator", 0x00, name, str(type(e).__name__), "FAIL")

    def _as_list(self, v) -> List[int]:
        if isinstance(v, int):
            return [v]
        return v or []

    def _safe_random_choice(self, seq: List[int]) -> int | None:
        if not seq:
            return None
        return random.choice(seq)

    # Manager
    def mutate_manager(self) -> list[bytes]:
        budget        = int(self.weights.get("manager.budget", DEFAULT_BUDGET))
        max_ops       = int(self.weights.get("manager.max_ops", DEFAULT_MAX_OPS))
        enable_struct = bool(self.weights.get("manager.structural", True))
        include_orig  = bool(self.weights.get("manager.include_original", False))

        out: list[bytes] = []
        traces: list[tuple[str, ...]] = []
        seen: set[bytes] = set()
        base = bytes(self.data)
        current_trace: list[str] = []

        def push():
            b = bytes(self.data)

            if len(b) > MAX_CAN_DLC:
                return

            if len(b) < self.min_length:
                return

            if b not in seen:
                seen.add(b)
                out.append(b)
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
                k = random.randint(1, max_ops)
                for _ in range(k):
                    op = random.choice([
                        "flip_bit",
                        "increment_bit",
                        "decrement_bit",
                        "increment_byte",
                        "decrement_byte",
                        "insert_byte" if enable_struct else "increment_byte",
                        "delete_byte" if enable_struct else "decrement_byte",
                    ])
                    current_trace.append(op)
                    try:
                        getattr(self, op)()
                    except Exception as e:
                        self._log_fail(op, e)

                push()

            except Exception as e:
                self._log_fail("mutate_manager", e)

            finally:
                self.data = saved

        self.generated = out
        self.generated_operators = traces
        return out

    def mutate_records(
        self,
        source_bus: str,
        message_id: int,
        seed_id: Optional[int] = None,
    ) -> list[MutationRecord]:
        """Generate payloads with provenance. ``mutate_manager`` remains compatible."""
        payloads = self.mutate_manager()
        records: list[MutationRecord] = []
        for payload, operators in zip(payloads, self.generated_operators):
            records.append(
                MutationRecord(
                    source_bus=source_bus,
                    message_id=message_id,
                    original_data=bytes(self.data),
                    mutated_data=payload,
                    operator="+".join(operators),
                    seed_id=seed_id,
                    parameters={"operators": list(operators)},
                )
            )
        return records

    # Bit Ops
    def flip_bit(self):
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return

            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return

            self.data[byte_idx] ^= (1 << bit_idx)
        except Exception as e:
            self._log_fail("flip_bit", e)

    def increment_bit(self):
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return

            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return

            mask = (1 << bit_idx)
            if (self.data[byte_idx] & mask) == 0:
                self.data[byte_idx] |= mask
        except Exception as e:
            self._log_fail("increment_bit", e)

    def decrement_bit(self):
        try:
            byte_idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if byte_idx is None:
                return

            bit_idx = self._safe_random_choice(self._as_list(self.select_random_bit()))
            if bit_idx is None:
                return

            mask = (1 << bit_idx)
            if (self.data[byte_idx] & mask) != 0:
                self.data[byte_idx] &= ~mask
        except Exception as e:
            self._log_fail("decrement_bit", e)

    # Byte Ops
    def increment_byte(self):
        try:
            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return

            self.data[idx] = (self.data[idx] + 1) & 0xFF
        except Exception as e:
            self._log_fail("increment_byte", e)

    def decrement_byte(self):
        try:
            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return

            self.data[idx] = (self.data[idx] - 1) & 0xFF
        except Exception as e:
            self._log_fail("decrement_byte", e)

    def insert_byte(self):
        try:
            if len(self.data) >= MAX_CAN_DLC:
                return

            pos = random.randint(0, len(self.data))
            val = random.randint(0, 255)
            self.data.insert(pos, val)
        except Exception as e:
            self._log_fail("insert_byte", e)

    def delete_byte(self):
        try:
            if len(self.data) <= self.min_length:
                return

            idx = self._safe_random_choice(self._as_list(self.select_random_byte()))
            if idx is None:
                return

            del self.data[idx]
        except Exception as e:
            self._log_fail("delete_byte", e)

    # Selectors
    def select_random_byte(self) -> list[int]:
        n = len(self.data)
        if n == 0:
            raise ValueError("빈 데이터에서는 바이트를 선택할 수 없습니다.")

        p = float(self.weights.get("byte_k_p", DEFAULT_BYTE_K_PROB))
        k = 1
        while random.random() > p and k < n:
            k += 1
        k = min(k, max(1, n // 3))

        ws = [float(self.weights.get(f"byte:{i}", DEFAULT_BYTE_WEIGHT)) for i in range(n)]
        edge = float(self.weights.get("edge_bias", DEFAULT_EDGE_BIAS))
        if edge > 0 and n >= 2:
            ws[0] += edge
            ws[-1] += edge

        if sum(ws) <= 0:
            return random.sample(range(n), k)

        return sorted(set(random.choices(range(n), weights=ws, k=k)))

    def select_random_bit(self) -> list[int]:
        p = float(self.weights.get("bit_k_p", DEFAULT_BIT_K_PROB))
        k = 1
        while random.random() > p and k < 8:
            k += 1
        k = min(k, 8)

        w = [float(self.weights.get(f"bit:{b}", DEFAULT_BIT_WEIGHT)) for b in range(8)]
        msb = float(self.weights.get("msb_bias", DEFAULT_MSB_BIAS))
        lsb = float(self.weights.get("lsb_bias", DEFAULT_LSB_BIAS))

        if msb > 0:
            w = [wb + msb * (b / 7.0) for b, wb in enumerate(w)]
        if lsb > 0:
            w = [wb + lsb * ((7 - b) / 7.0) for b, wb in enumerate(w)]

        if sum(w) <= 0:
            return random.sample(range(8), k)

        return sorted(set(random.choices(range(8), weights=w, k=k)))
