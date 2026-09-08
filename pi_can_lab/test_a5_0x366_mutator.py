from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from a5_0x366_mutator import A5BlinkmodiMutator, BASELINE_PAYLOAD
from can_sender import (
    build_parser as build_sender_parser,
    generate_mutation_entries,
    run as run_sender,
)
from strategy_selector import TrialStrategySelector


DBC = Path(__file__).resolve().parent.parent / "A5.dbc"


class A5BlinkmodiMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = A5BlinkmodiMutator(DBC, BASELINE_PAYLOAD)

    def assert_only_signal_changed(self, signal_name: str, expected: int) -> None:
        candidate = next(
            item for item in self.generator.signal_single()
            if item.metadata["signal"] == signal_name
            and item.metadata["after"] == expected
        )
        before = self.generator.decode_raw(BASELINE_PAYLOAD)
        after = self.generator.decode_raw(candidate.mutated_payload)
        differences = {name for name in before if before[name] != after[name]}
        self.assertEqual(differences, {signal_name})
        self.assertEqual(after[signal_name], expected)

    def test_single_bit_signals_change_independently(self) -> None:
        for signal in (
            "BM_Crash",
            "BM_Warnblinken",
            "BM_links",
            "BM_rechts",
        ):
            with self.subTest(signal=signal):
                self.assert_only_signal_changed(signal, 1)

    def test_nba_status_two_encodes_and_is_undefined_enum(self) -> None:
        candidate = next(
            item for item in self.generator.undefined_enum()
            if item.metadata["signal"] == "BM_NBA_Status"
            and item.metadata["raw_value"] == 2
        )
        self.assertEqual(
            self.generator.decode_raw(candidate.mutated_payload)["BM_NBA_Status"], 2
        )
        self.assertFalse(candidate.undefined_enum["dbc_choice_defined"])

    def test_occupancy_has_no_overlap(self) -> None:
        self.assertFalse(self.generator.defined_bits & self.generator.undefined_bits)
        self.assertEqual(
            self.generator.defined_bits | self.generator.undefined_bits,
            frozenset(range(64)),
        )
        self.assertEqual(
            sorted(self.generator.undefined_bits),
            list(range(0, 12)) + list(range(45, 59)),
        )

    def test_undefined_single_preserves_all_defined_signals(self) -> None:
        baseline = self.generator.decode_raw(BASELINE_PAYLOAD)
        for candidate in self.generator.undefined_bit_single():
            self.assertNotEqual(candidate.mutated_payload, BASELINE_PAYLOAD)
            self.assertEqual(
                self.generator.decode_raw(candidate.mutated_payload), baseline
            )
            self.assertTrue(
                set(candidate.undefined_bits_changed) <= self.generator.undefined_bits
            )

    def test_undefined_multi_respects_cap_and_preserves_signals(self) -> None:
        baseline = self.generator.decode_raw(BASELINE_PAYLOAD)
        candidates = self.generator.undefined_bit_multi(2)
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertLessEqual(len(candidate.undefined_bits_changed), 2)
            self.assertEqual(
                self.generator.decode_raw(candidate.mutated_payload), baseline
            )

    def test_defined_undefined_mix_changes_only_requested_parts(self) -> None:
        candidate = next(
            item for item in self.generator.defined_undefined_mix()
            if item.metadata["signal"] == "BM_Crash"
            and item.undefined_bits_changed == (0,)
        )
        before = self.generator.decode_raw(BASELINE_PAYLOAD)
        after = self.generator.decode_raw(candidate.mutated_payload)
        differences = {name for name in before if before[name] != after[name]}
        self.assertEqual(differences, {"BM_Crash"})
        self.assertEqual(after["BM_Crash"], 1)
        self.assertEqual(
            BASELINE_PAYLOAD[0] ^ candidate.mutated_payload[0], 0x01
        )

    def test_required_semantic_and_safety_cases_are_present(self) -> None:
        combinations = {item.case for item in self.generator.signal_combination()}
        contradictions = {item.case for item in self.generator.state_contradiction()}
        self.assertTrue({
            "LEFT_VALID",
            "RIGHT_VALID",
            "BOTH_WITHOUT_HAZARD",
            "HAZARD_WITHOUT_DIRECTION_STATE",
        } <= combinations)
        self.assertTrue({
            "LEFT_STATE_WITHOUT_PHASE",
            "LEFT_PHASE_WITHOUT_STATE",
            "RIGHT_STATE_WITHOUT_PHASE",
            "RIGHT_PHASE_WITHOUT_STATE",
            "CRASH_WITHOUT_HAZARD",
            "CRASH_WITH_HAZARD",
            "EMERGENCY_BRAKE_WITH_NBA_INACTIVE",
            "NO_EMERGENCY_BRAKE_WITH_NBA_HELLPHASE",
            "EMERGENCY_BRAKE_WITH_UNDEFINED_NBA",
        } <= contradictions)

    def test_same_case_always_has_same_payload(self) -> None:
        first = A5BlinkmodiMutator(DBC, BASELINE_PAYLOAD)
        second = A5BlinkmodiMutator(DBC, BASELINE_PAYLOAD)
        first_cases = {
            (item.mutation_family, item.case): item.mutated_payload
            for item in first.generate_profile("all-0x366")
        }
        second_cases = {
            (item.mutation_family, item.case): item.mutated_payload
            for item in second.generate_profile("all-0x366")
        }
        self.assertEqual(first_cases, second_cases)

    def test_temporal_sequence_is_explicit_and_deterministic(self) -> None:
        candidate = next(
            item for item in self.generator.temporal_sequence()
            if item.case == "ASYMMETRIC_FZG_KOMBI_50MS"
        )
        self.assertEqual(candidate.sequence.interval_ms, 50)
        self.assertEqual(candidate.metadata["dbc_timing"], {
            "normal_cycle_ms": 1000,
            "fast_cycle_ms": 50,
            "delay_ms": 10,
            "repetition": 5,
        })
        self.assertEqual(len(candidate.sequence.frames), 4)
        decoded = [
            self.generator.decode_raw(frame) for frame in candidate.sequence.frames
        ]
        self.assertEqual([item["Blinken_li_Fzg_Takt"] for item in decoded], [0, 1, 0, 1])
        self.assertEqual([item["Blinken_li_Kombi_Takt"] for item in decoded], [0, 0, 1, 1])

    def test_existing_random_mutation_is_deterministic_and_unchanged(self) -> None:
        args = (BASELINE_PAYLOAD, 5, 1, False, False, 366, 0.75)
        first = generate_mutation_entries(*args)
        second = generate_mutation_entries(*args)
        self.assertEqual(first, second)
        self.assertTrue(all(payload != BASELINE_PAYLOAD for payload, _ in first))

    def test_profile_selection_is_deterministic_and_has_uid(self) -> None:
        state = {
            "total_trials": 0,
            "next_mutation_id": 31,
            "interesting_mutations": [],
            "mutation_history": [],
            "last_feedback": None,
        }
        selector = TrialStrategySelector({})
        arguments = dict(
            state=state,
            original_payload=BASELINE_PAYLOAD,
            source_bus="b_can",
            can_id=0x366,
            random_seed=366,
            mutation_profile="signal-aware",
            dbc_path=DBC,
            undefined_max_bits=2,
        )
        first, _ = selector.select_mutation(**arguments)
        second, _ = selector.select_mutation(**arguments)
        self.assertEqual(first.mutated_payload, second.mutated_payload)
        self.assertEqual(first.parameters, second.parameters)
        self.assertEqual(first.mutation_uid, "MUT-000031")
        self.assertIn(
            first.parameters["mutation_family"],
            {
                "signal_single",
                "signal_combination",
                "state_contradiction",
                "undefined_enum",
            },
        )

    def test_temporal_sender_writes_flat_manifest_provenance(self) -> None:
        candidate = next(
            item for item in self.generator.temporal_sequence()
            if item.case == "NORMAL_TOGGLE_50MS"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sender.yaml"
            config.write_text(
                """
bus:
  interface: virtual
  channel: trial
sender:
  bus_name: b_can
  id: 0x366
  data: 00000000200000F0
  output: tx.jsonl
  mutation:
    enabled: true
  campaign:
    enabled: true
    baseline_duration_seconds: 0.01
    normal_duration_seconds: 0.01
    mutation_duration_seconds: 0.01
    recovery_duration_seconds: 0.01
  transmit:
    count: 1
    interval_ms: 10
    restore_original: false
  safety:
    max_count: 1
    max_duration_seconds: 10
    max_campaign_duration_seconds: 1
    min_interval_ms: 10
""".strip(),
                encoding="utf-8",
            )
            args = build_sender_parser().parse_args([
                "--config", str(config),
                "--mutation-data", candidate.mutated_payload.hex(),
                "--mutation-id", "31",
                "--mutation-uid", "MUT-000031",
                "--mutation-operator", "TEMPORAL_SEQUENCE",
                "--mutation-metadata-json", json.dumps(candidate.parameters()),
                "--random-seed", "366",
            ])
            self.assertEqual(run_sender(args), 0)
            records = [
                json.loads(line)
                for line in (root / "tx.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        mutations = [
            item for item in records
            if item.get("record_type") == "can_tx"
            and item.get("phase") == "mutation"
        ]
        self.assertEqual(len(mutations), len(candidate.sequence.frames))
        self.assertTrue(all(item["mutation_id"] == "MUT-000031" for item in mutations))
        self.assertTrue(all(item["can_id"] == "0x366" for item in mutations))
        self.assertTrue(all(item["interface"] == "trial" for item in mutations))
        self.assertTrue(all(item["timestamp_ns"] > 0 for item in mutations))
        self.assertTrue(all(item["mutation_family"] == "temporal_sequence" for item in mutations))
        self.assertTrue(all(item["case"] == "NORMAL_TOGGLE_50MS" for item in mutations))


if __name__ == "__main__":
    unittest.main()
