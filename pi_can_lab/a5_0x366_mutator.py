"""DBC-driven, deterministic mutations for Audi A5 ``Blinkmodi_02`` (0x366).

This module is deliberately separate from :mod:`mutation_engine`: the generic
bit/byte/random mutator remains the compatibility fallback for every message
and for runs without an explicit 0x366 profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


TARGET_CAN_ID = 0x366
TARGET_MESSAGE = "Blinkmodi_02"
TARGET_DLC = 8
BASELINE_PAYLOAD = bytes.fromhex("00000000200000F0")

PROFILE_FAMILIES = {
    "signal-aware": (
        "signal_single",
        "signal_combination",
        "state_contradiction",
        "undefined_enum",
    ),
    "undefined-only": ("undefined_bit_single", "undefined_bit_multi"),
    "semantic-plus-undefined": ("defined_undefined_mix",),
    "temporal": ("temporal_sequence",),
    "all-0x366": (
        "signal_single",
        "signal_combination",
        "state_contradiction",
        "undefined_enum",
        "undefined_bit_single",
        "undefined_bit_multi",
        "defined_undefined_mix",
        "temporal_sequence",
    ),
}

MIX_SIGNAL_NAMES = (
    "BM_Crash",
    "BM_Panik",
    "BM_Not_Bremsung",
    "BM_Warnblinken",
    "BM_links",
    "BM_rechts",
    "BM_NBA_Status",
)

SIGNAL_COMBINATIONS: tuple[tuple[str, Mapping[str, int]], ...] = (
    ("LEFT_VALID", {"BM_links": 1, "BM_rechts": 0}),
    ("RIGHT_VALID", {"BM_links": 0, "BM_rechts": 1}),
    (
        "BOTH_WITHOUT_HAZARD",
        {"BM_links": 1, "BM_rechts": 1, "BM_Warnblinken": 0},
    ),
    (
        "HAZARD_WITHOUT_DIRECTION_STATE",
        {"BM_Warnblinken": 1, "BM_links": 0, "BM_rechts": 0},
    ),
)

STATE_CONTRADICTIONS: tuple[tuple[str, Mapping[str, int]], ...] = (
    (
        "LEFT_STATE_WITHOUT_PHASE",
        {"BM_links": 1, "Blinken_li_Fzg_Takt": 0, "Blinken_li_Kombi_Takt": 0},
    ),
    (
        "LEFT_PHASE_WITHOUT_STATE",
        {"BM_links": 0, "Blinken_li_Fzg_Takt": 1, "Blinken_li_Kombi_Takt": 1},
    ),
    (
        "LEFT_FZG_KOMBI_MISMATCH_A",
        {"BM_links": 1, "Blinken_li_Fzg_Takt": 1, "Blinken_li_Kombi_Takt": 0},
    ),
    (
        "LEFT_FZG_KOMBI_MISMATCH_B",
        {"BM_links": 1, "Blinken_li_Fzg_Takt": 0, "Blinken_li_Kombi_Takt": 1},
    ),
    (
        "RIGHT_STATE_WITHOUT_PHASE",
        {"BM_rechts": 1, "Blinken_re_Fzg_Takt": 0, "Blinken_re_Kombi_Takt": 0},
    ),
    (
        "RIGHT_PHASE_WITHOUT_STATE",
        {"BM_rechts": 0, "Blinken_re_Fzg_Takt": 1, "Blinken_re_Kombi_Takt": 1},
    ),
    (
        "RIGHT_FZG_KOMBI_MISMATCH_A",
        {"BM_rechts": 1, "Blinken_re_Fzg_Takt": 1, "Blinken_re_Kombi_Takt": 0},
    ),
    (
        "RIGHT_FZG_KOMBI_MISMATCH_B",
        {"BM_rechts": 1, "Blinken_re_Fzg_Takt": 0, "Blinken_re_Kombi_Takt": 1},
    ),
    ("CRASH_WITHOUT_HAZARD", {"BM_Crash": 1, "BM_Warnblinken": 0}),
    ("CRASH_WITH_HAZARD", {"BM_Crash": 1, "BM_Warnblinken": 1}),
    (
        "EMERGENCY_BRAKE_WITH_NBA_INACTIVE",
        {"BM_Not_Bremsung": 1, "BM_NBA_Status": 0},
    ),
    (
        "NO_EMERGENCY_BRAKE_WITH_NBA_HELLPHASE",
        {"BM_Not_Bremsung": 0, "BM_NBA_Status": 3},
    ),
    (
        "EMERGENCY_BRAKE_WITH_UNDEFINED_NBA",
        {"BM_Not_Bremsung": 1, "BM_NBA_Status": 2},
    ),
)

TEMPORAL_PATTERNS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("NORMAL_TOGGLE", (0, 1, 0, 1)),
    ("DUPLICATED_PHASE", (0, 1, 1, 0)),
    ("SKIPPED_PHASE", (0, 0, 1)),
    ("STUCK_ON", (1, 1, 1, 1)),
    ("STUCK_OFF", (0, 0, 0, 0)),
)
TEMPORAL_INTERVALS_MS = (10, 50, 100, 500, 1000)


@dataclass(frozen=True)
class SignalLayout:
    name: str
    start_bit: int
    length: int
    byte_order: str
    is_signed: bool
    scale: float
    offset: float
    minimum: Optional[float]
    maximum: Optional[float]
    choices: dict[int, str]
    occupied_bits: tuple[int, ...]

    @property
    def raw_values(self) -> tuple[int, ...]:
        if self.is_signed:
            lower = -(1 << (self.length - 1))
            upper = 1 << (self.length - 1)
            return tuple(range(lower, upper))
        return tuple(range(1 << self.length))

    @property
    def undefined_choice_values(self) -> tuple[int, ...]:
        if not self.choices:
            return ()
        return tuple(value for value in self.raw_values if value not in self.choices)


@dataclass(frozen=True)
class MutationSequence:
    name: str
    interval_ms: int
    frames: tuple[bytes, ...]
    signals: tuple[str, ...]
    frame_signals_changed: tuple[tuple[dict[str, Any], ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_ms": self.interval_ms,
            "frames": [frame.hex().upper() for frame in self.frames],
            "signals": list(self.signals),
            "frame_signals_changed": [
                [dict(change) for change in changes]
                for changes in self.frame_signals_changed
            ],
        }


@dataclass(frozen=True)
class TargetedMutation:
    mutation_family: str
    case: str
    base_payload: bytes
    mutated_payload: bytes
    signals_changed: tuple[dict[str, Any], ...] = ()
    undefined_bits_changed: tuple[int, ...] = ()
    undefined_enum: Optional[dict[str, Any]] = None
    sequence: Optional[MutationSequence] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def parameters(self) -> dict[str, Any]:
        result = {
            "mutation_family": self.mutation_family,
            "case": self.case,
            "base_payload": self.base_payload.hex().upper(),
            "mutated_payload": self.mutated_payload.hex().upper(),
            "signals_changed": [dict(item) for item in self.signals_changed],
            "undefined_bits_changed": list(self.undefined_bits_changed),
            "undefined_enum": dict(self.undefined_enum) if self.undefined_enum else None,
            **self.metadata,
        }
        if self.sequence is not None:
            result["sequence"] = self.sequence.to_dict()
        return result


def signal_bit_positions(start: int, length: int, byte_order: str) -> tuple[int, ...]:
    """Return physical payload bits as ``byte * 8 + LSB-index`` values."""
    if length < 1:
        return ()
    if byte_order == "little_endian":
        return tuple(range(start, start + length))
    bit = start
    positions = []
    for _ in range(length):
        positions.append(bit)
        bit = bit + 15 if bit % 8 == 0 else bit - 1
    return tuple(positions)


def _contiguous_groups(values: Sequence[int]) -> list[tuple[int, ...]]:
    groups: list[list[int]] = []
    for value in sorted(values):
        if not groups or value != groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [tuple(group) for group in groups]


class A5BlinkmodiMutator:
    """Generate traceable 0x366 mutations from the actual loaded DBC."""

    def __init__(self, dbc_path: str | Path, base_payload: bytes = BASELINE_PAYLOAD):
        import cantools

        self.dbc_path = Path(dbc_path).expanduser().resolve()
        self.database = cantools.database.load_file(str(self.dbc_path))
        self.message = self.database.get_message_by_frame_id(TARGET_CAN_ID)
        if self.message.name != TARGET_MESSAGE or int(self.message.length) != TARGET_DLC:
            raise ValueError(
                f"DBC 0x366 mismatch: {self.message.name}, DLC={self.message.length}"
            )
        self.base_payload = bytes(base_payload)
        if len(self.base_payload) != TARGET_DLC:
            raise ValueError("Blinkmodi_02 baseline must be exactly 8 bytes")
        self.signals = tuple(self._layout(signal) for signal in self.message.signals)
        self.signal_by_name = {signal.name: signal for signal in self.signals}
        attributes = getattr(getattr(self.message, "dbc", None), "attributes", {}) or {}
        self.timing = {
            "normal_cycle_ms": self._attribute_value(attributes, "GenMsgCycleTime"),
            "fast_cycle_ms": self._attribute_value(attributes, "GenMsgCycleTimeFast"),
            "delay_ms": self._attribute_value(attributes, "GenMsgDelayTime"),
            "repetition": self._attribute_value(attributes, "GenMsgNrOfRepetition"),
        }
        self.defined_bits = frozenset(
            bit for signal in self.signals for bit in signal.occupied_bits
        )
        self.undefined_bits = frozenset(range(TARGET_DLC * 8)) - self.defined_bits
        if self.defined_bits & self.undefined_bits:
            raise AssertionError("defined and undefined DBC bits overlap")
        self.base_signals = self.decode_raw(self.base_payload)

    @staticmethod
    def _attribute_value(attributes: Mapping[str, Any], name: str) -> Optional[int]:
        attribute = attributes.get(name)
        if attribute is None:
            return None
        return int(getattr(attribute, "value", attribute))

    @staticmethod
    def _layout(signal: Any) -> SignalLayout:
        choices = {
            int(raw): str(name)
            for raw, name in (getattr(signal, "choices", None) or {}).items()
        }
        return SignalLayout(
            name=signal.name,
            start_bit=int(signal.start),
            length=int(signal.length),
            byte_order=str(signal.byte_order),
            is_signed=bool(signal.is_signed),
            scale=float(signal.scale),
            offset=float(signal.offset),
            minimum=signal.minimum,
            maximum=signal.maximum,
            choices=choices,
            occupied_bits=signal_bit_positions(
                int(signal.start), int(signal.length), str(signal.byte_order)
            ),
        )

    def decode_raw(self, payload: bytes) -> dict[str, int]:
        decoded = self.message.decode(
            payload, decode_choices=False, scaling=False, allow_truncated=False
        )
        return {name: int(value) for name, value in decoded.items()}

    def occupancy_rows(self) -> list[dict[str, Any]]:
        owners: dict[int, list[str]] = {}
        for signal in self.signals:
            for bit in signal.occupied_bits:
                owners.setdefault(bit, []).append(signal.name)
        return [
            {
                "global_bit": bit,
                "byte_index": bit // 8,
                "bit_index": bit % 8,
                "defined": bit in self.defined_bits,
                "signals": owners.get(bit, []),
            }
            for bit in range(TARGET_DLC * 8)
        ]

    def occupancy_text(self) -> str:
        return "\n".join(
            f"bit {row['global_bit']:2d} : "
            + (", ".join(row["signals"]) if row["defined"] else "undefined")
            for row in self.occupancy_rows()
        )

    def enum_report(self) -> list[dict[str, Any]]:
        return [
            {
                "signal": signal.name,
                "bit_length": signal.length,
                "possible_raw_values": list(signal.raw_values),
                "dbc_defined_values": sorted(signal.choices),
                "undefined_values": list(signal.undefined_choice_values),
            }
            for signal in self.signals
            if signal.length > 1
        ]

    def _patch_raw(
        self, payload: bytes, assignments: Mapping[str, int]
    ) -> tuple[bytes, tuple[dict[str, Any], ...]]:
        unknown = sorted(set(assignments) - set(self.signal_by_name))
        if unknown:
            raise KeyError("Unknown Blinkmodi_02 signals: " + ", ".join(unknown))
        before = self.decode_raw(payload)
        after = dict(before)
        after.update({name: int(value) for name, value in assignments.items()})
        encoded_before = bytes(
            self.message.encode(before, scaling=False, padding=False, strict=True)
        )
        encoded_after = bytes(
            self.message.encode(after, scaling=False, padding=False, strict=True)
        )
        # cantools clears bits outside the DBC model. XOR only the DBC-computed
        # delta into the live/base payload so undefined bits remain untouched.
        mutated = bytes(
            original ^ old ^ new
            for original, old, new in zip(payload, encoded_before, encoded_after)
        )
        verified = self.decode_raw(mutated)
        for name, expected in assignments.items():
            if verified[name] != int(expected):
                raise AssertionError(f"DBC raw patch verification failed for {name}")
        changes = tuple(
            {
                "signal": name,
                "before": before[name],
                "after": verified[name],
                "start_bit": self.signal_by_name[name].start_bit,
                "length": self.signal_by_name[name].length,
            }
            for name in assignments
            if before[name] != verified[name]
        )
        return mutated, changes

    @staticmethod
    def _flip_bits(payload: bytes, bits: Iterable[int]) -> bytes:
        changed = bytearray(payload)
        for bit in bits:
            changed[bit // 8] ^= 1 << (bit % 8)
        return bytes(changed)

    def _undefined_only_candidate(
        self, family: str, case: str, bits: Sequence[int]
    ) -> TargetedMutation:
        selected = tuple(sorted(set(int(bit) for bit in bits)))
        if not selected or not set(selected) <= self.undefined_bits:
            raise ValueError("undefined-only mutation selected a DBC-defined bit")
        payload = self._flip_bits(self.base_payload, selected)
        if payload == self.base_payload:
            raise AssertionError("undefined-only mutation must alter raw payload")
        if self.decode_raw(payload) != self.base_signals:
            raise AssertionError("undefined-only mutation altered a DBC-defined signal")
        return TargetedMutation(
            family,
            case,
            self.base_payload,
            payload,
            undefined_bits_changed=selected,
            metadata={"profile_validation": "defined_signals_unchanged"},
        )

    def signal_single(self) -> list[TargetedMutation]:
        results = []
        for signal in self.signals:
            before = self.base_signals[signal.name]
            for raw_value in signal.raw_values:
                if raw_value == before:
                    continue
                payload, changes = self._patch_raw(
                    self.base_payload, {signal.name: raw_value}
                )
                results.append(TargetedMutation(
                    "signal_single",
                    f"{signal.name}_RAW_{raw_value}",
                    self.base_payload,
                    payload,
                    signals_changed=changes,
                    metadata={
                        "signal": signal.name,
                        "before": before,
                        "after": raw_value,
                        "raw_value": raw_value,
                        "dbc_choice_defined": raw_value in signal.choices,
                        "dbc_choice": signal.choices.get(raw_value),
                    },
                ))
        return results

    def _assignment_cases(
        self,
        family: str,
        cases: Sequence[tuple[str, Mapping[str, int]]],
    ) -> list[TargetedMutation]:
        results = []
        for case, assignments in cases:
            payload, changes = self._patch_raw(self.base_payload, assignments)
            if payload == self.base_payload:
                continue
            results.append(TargetedMutation(
                family,
                case,
                self.base_payload,
                payload,
                signals_changed=changes,
                metadata={"assignments": dict(assignments)},
            ))
        return results

    def signal_combination(self) -> list[TargetedMutation]:
        return self._assignment_cases(
            "signal_combination", SIGNAL_COMBINATIONS
        )

    def state_contradiction(self) -> list[TargetedMutation]:
        return self._assignment_cases(
            "state_contradiction", STATE_CONTRADICTIONS
        )

    def undefined_enum(self) -> list[TargetedMutation]:
        results = []
        for signal in self.signals:
            for raw_value in signal.undefined_choice_values:
                if raw_value == self.base_signals[signal.name]:
                    continue
                payload, changes = self._patch_raw(
                    self.base_payload, {signal.name: raw_value}
                )
                results.append(TargetedMutation(
                    "undefined_enum",
                    f"{signal.name}_UNDEFINED_RAW_{raw_value}",
                    self.base_payload,
                    payload,
                    signals_changed=changes,
                    undefined_enum={
                        "signal": signal.name,
                        "raw_value": raw_value,
                        "dbc_choice_defined": False,
                    },
                    metadata={
                        "signal": signal.name,
                        "raw_value": raw_value,
                        "dbc_choice_defined": False,
                    },
                ))
        return results

    def undefined_bit_single(self) -> list[TargetedMutation]:
        return [
            self._undefined_only_candidate(
                "undefined_bit_single", f"DBC_UNDEFINED_BIT_{bit}", (bit,)
            )
            for bit in sorted(self.undefined_bits)
        ]

    def undefined_bit_multi(self, maximum_bits: int = 2) -> list[TargetedMutation]:
        if maximum_bits < 2:
            raise ValueError("undefined maximum bits must be at least 2")
        candidates: list[tuple[str, tuple[int, ...]]] = []
        undefined = sorted(self.undefined_bits)

        # Adjacent pairs, then every same-byte combination up to the configured cap.
        for left, right in zip(undefined, undefined[1:]):
            if right == left + 1:
                candidates.append((f"ADJACENT_{left}_{right}", (left, right)))
        for byte_index in range(TARGET_DLC):
            in_byte = tuple(bit for bit in undefined if bit // 8 == byte_index)
            for size in range(2, min(maximum_bits, len(in_byte)) + 1):
                for selected in combinations(in_byte, size):
                    candidates.append((
                        f"BYTE_{byte_index}_BITS_" + "_".join(map(str, selected)),
                        selected,
                    ))
            if 1 < len(in_byte) <= maximum_bits:
                candidates.append((f"BYTE_{byte_index}_MASK_INVERSION", in_byte))

        # The complete undefined-region inversion is available when the caller
        # explicitly raises the cap high enough; the default cap remains two.
        if len(undefined) <= maximum_bits:
            candidates.append(("UNDEFINED_ALL_ZERO_TO_ALL_ONE", tuple(undefined)))

        results = []
        seen: set[tuple[int, ...]] = set()
        for case, bits in candidates:
            normalized = tuple(sorted(bits))
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(self._undefined_only_candidate(
                "undefined_bit_multi", case, normalized
            ))
        return results

    def _representative_undefined_bits(self) -> tuple[int, ...]:
        selected: set[int] = set()
        for group in _contiguous_groups(sorted(self.undefined_bits)):
            selected.update((group[0], group[len(group) // 2], group[-1]))
        return tuple(sorted(selected))

    def defined_undefined_mix(self) -> list[TargetedMutation]:
        results = []
        for signal_name in MIX_SIGNAL_NAMES:
            signal = self.signal_by_name[signal_name]
            before = self.base_signals[signal_name]
            if signal_name == "BM_NBA_Status" and 2 in signal.raw_values:
                after = 2
            else:
                after = next(value for value in signal.raw_values if value != before)
            semantic_payload, changes = self._patch_raw(
                self.base_payload, {signal_name: after}
            )
            for bit in self._representative_undefined_bits():
                payload = self._flip_bits(semantic_payload, (bit,))
                verified = self.decode_raw(payload)
                expected = dict(self.base_signals)
                expected[signal_name] = after
                if verified != expected:
                    raise AssertionError("defined+undefined mutation changed extra signals")
                undefined_enum = None
                if signal_name == "BM_NBA_Status" and after not in signal.choices:
                    undefined_enum = {
                        "signal": signal_name,
                        "raw_value": after,
                        "dbc_choice_defined": False,
                    }
                results.append(TargetedMutation(
                    "defined_undefined_mix",
                    f"{signal_name}_PLUS_DBC_UNDEFINED_BIT_{bit}",
                    self.base_payload,
                    payload,
                    signals_changed=changes,
                    undefined_bits_changed=(bit,),
                    undefined_enum=undefined_enum,
                    metadata={"signal": signal_name, "raw_value": after},
                ))
        return results

    def temporal_sequence(self) -> list[TargetedMutation]:
        results = []

        def make_frames(
            fzg: Sequence[int], kombi: Sequence[int]
        ) -> tuple[tuple[bytes, ...], tuple[tuple[dict[str, Any], ...], ...]]:
            frames = []
            frame_changes = []
            for fzg_value, kombi_value in zip(fzg, kombi):
                payload, changes = self._patch_raw(self.base_payload, {
                    "BM_links": 1,
                    "Blinken_li_Fzg_Takt": fzg_value,
                    "Blinken_li_Kombi_Takt": kombi_value,
                })
                frames.append(payload)
                frame_changes.append(changes)
            return tuple(frames), tuple(frame_changes)

        patterns = [
            (name, pattern, pattern) for name, pattern in TEMPORAL_PATTERNS
        ]
        patterns.append((
            "ASYMMETRIC_FZG_KOMBI",
            (0, 1, 0, 1),
            (0, 0, 1, 1),
        ))
        for name, fzg, kombi in patterns:
            frames, frame_changes = make_frames(fzg, kombi)
            for interval_ms in TEMPORAL_INTERVALS_MS:
                sequence = MutationSequence(
                    name=name,
                    interval_ms=interval_ms,
                    frames=frames,
                    signals=(
                        "BM_links",
                        "Blinken_li_Fzg_Takt",
                        "Blinken_li_Kombi_Takt",
                    ),
                    frame_signals_changed=frame_changes,
                )
                results.append(TargetedMutation(
                    "temporal_sequence",
                    f"{name}_{interval_ms}MS",
                    self.base_payload,
                    frames[0],
                    signals_changed=frame_changes[0],
                    sequence=sequence,
                    metadata={
                        "interval_ms": interval_ms,
                        "dbc_timing": dict(self.timing),
                    },
                ))
        return results

    def generate_profile(
        self, profile: str, undefined_max_bits: int = 2
    ) -> list[TargetedMutation]:
        if profile not in PROFILE_FAMILIES:
            raise ValueError(
                f"unknown 0x366 mutation profile {profile!r}; "
                + ", ".join(PROFILE_FAMILIES)
            )
        by_family = {
            "signal_single": self.signal_single,
            "signal_combination": self.signal_combination,
            "state_contradiction": self.state_contradiction,
            "undefined_enum": self.undefined_enum,
            "undefined_bit_single": self.undefined_bit_single,
            "undefined_bit_multi": lambda: self.undefined_bit_multi(undefined_max_bits),
            "defined_undefined_mix": self.defined_undefined_mix,
            "temporal_sequence": self.temporal_sequence,
        }
        results = []
        for family in PROFILE_FAMILIES[profile]:
            results.extend(by_family[family]())
        if not results:
            raise RuntimeError(f"0x366 profile {profile!r} generated no mutations")
        return results
