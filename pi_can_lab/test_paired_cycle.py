from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from a5_0x366_mutator import A5BlinkmodiMutator, BASELINE_PAYLOAD, PROFILE_FAMILIES
from paired_cycle import (
    advance_cycle,
    build_cycle_plan,
    make_cycle_mutation,
    next_cycle_entry,
    validate_cycle_plan,
)


DBC = Path(__file__).resolve().parent.parent / "A5.dbc"


class PairedCycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = build_cycle_plan(
            DBC, BASELINE_PAYLOAD, source_bus="b_can", random_seed=366,
        )

    def test_catalogue_covers_eight_actual_families_without_aggregate_repeat(self) -> None:
        plan = self.plan
        self.assertEqual(plan["families"], list(PROFILE_FAMILIES["all-0x366"]))
        self.assertEqual(len(plan["families"]), 8)
        self.assertEqual(len(plan["entries"]), 361)
        self.assertEqual(plan["scheduled_count"], 281)
        self.assertEqual(plan["skipped_count"], 80)
        self.assertEqual(sum(item["raw"] for item in plan["family_counts"].values()), 361)
        self.assertEqual(
            [entry["family"] for entry in plan["entries"]],
            sorted(
                (entry["family"] for entry in plan["entries"]),
                key=list(PROFILE_FAMILIES["all-0x366"]).index,
            ),
        )

    def test_plan_is_deterministic_json_safe_and_rejects_tampering(self) -> None:
        second = build_cycle_plan(
            DBC, BASELINE_PAYLOAD, source_bus="b_can", random_seed=366,
        )
        self.assertEqual(second, self.plan)
        round_trip = json.loads(json.dumps(self.plan))
        self.assertEqual(round_trip, self.plan)
        validate_cycle_plan(round_trip, dbc_path=DBC)
        round_trip["entries"][0]["mutated_payload"] = "FFFFFFFFFFFFFFFF"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            validate_cycle_plan(round_trip)

    def test_every_scheduled_stimulus_is_unique_and_skips_are_explicit(self) -> None:
        entries = self.plan["entries"]
        scheduled = [entry for entry in entries if entry["disposition"] == "scheduled"]
        self.assertEqual(
            len({entry["tx_fingerprint"] for entry in scheduled}),
            len(scheduled),
        )
        self.assertTrue(all(entry["reason"] is None for entry in scheduled))
        skipped = [entry for entry in entries if entry["disposition"] == "skipped"]
        self.assertTrue(all(entry["reason"] for entry in skipped))
        self.assertEqual(Counter(entry["reason"] for entry in skipped), {
            "duplicate_tx_stimulus": 62,
            "fewer_than_two_complete_temporal_cycles": 12,
            "temporal_interval_below_50ms_safety_limit": 6,
        })
        duplicate = next(entry for entry in skipped if entry["case"] == "LEFT_VALID")
        self.assertEqual(duplicate["reason"], "duplicate_tx_stimulus")
        self.assertEqual(
            duplicate["tx_fingerprint"],
            entries[duplicate["duplicate_of_index"]]["tx_fingerprint"],
        )
        self.assertEqual(self.plan["family_counts"]["undefined_enum"]["scheduled"], 0)

    def test_temporal_cases_require_safe_interval_and_two_complete_cycles(self) -> None:
        cases = {entry["case"]: entry for entry in self.plan["entries"]
                 if entry["family"] == "temporal_sequence"}
        self.assertEqual(
            cases["NORMAL_TOGGLE_10MS"]["reason"],
            "temporal_interval_below_50ms_safety_limit",
        )
        self.assertEqual(
            cases["NORMAL_TOGGLE_500MS"]["reason"],
            "fewer_than_two_complete_temporal_cycles",
        )
        self.assertEqual(cases["NORMAL_TOGGLE_50MS"]["disposition"], "scheduled")
        self.assertEqual(cases["NORMAL_TOGGLE_100MS"]["disposition"], "scheduled")
        self.assertEqual(cases["STUCK_OFF_50MS"]["reason"], "duplicate_tx_stimulus")

    def test_temporal_candidate_starting_with_original_is_not_sent(self) -> None:
        generator = A5BlinkmodiMutator(DBC, BASELINE_PAYLOAD)
        original, _ = generator._patch_raw(BASELINE_PAYLOAD, {
            "BM_links": 1,
            "Blinken_li_Fzg_Takt": 0,
            "Blinken_li_Kombi_Takt": 0,
        })
        plan = build_cycle_plan(DBC, original, source_bus="b_can", random_seed=366)
        constant = next(entry for entry in plan["entries"]
                        if entry["case"] == "STUCK_OFF_50MS")
        self.assertEqual(constant["reason"], "first_payload_equals_original_sender_contract")

    def test_cursor_only_advances_in_order_after_pair_and_can_resume(self) -> None:
        plan = copy.deepcopy(self.plan)
        first = next_cycle_entry(plan)
        self.assertEqual(first["index"], 0)
        with self.assertRaises(ValueError):
            advance_cycle(plan, 1, "pair_0001")
        advanced = advance_cycle(plan, 0, "pair_0001")
        self.assertEqual(advanced["cursor"], 1)
        self.assertEqual(advanced["status"], "active")
        self.assertEqual(next_cycle_entry(advanced)["index"], 1)
        self.assertEqual(next_cycle_entry(plan)["index"], 0)
        with self.assertRaises(ValueError):
            advance_cycle(advanced, 1, "pair_0001")
        replayed = json.loads(json.dumps(advanced))
        self.assertEqual(next_cycle_entry(replayed)["index"], 1)

    def test_inconclusive_pair_advances_with_auditable_legacy_compatible_ledger(self) -> None:
        legacy = advance_cycle(copy.deepcopy(self.plan), 0, "pair_0001")
        self.assertNotIn("comparability_status", legacy["completed_pairs"][0])
        validate_cycle_plan(legacy, dbc_path=DBC)

        explicit_legacy = copy.deepcopy(legacy)
        explicit_legacy["completed_pairs"][0]["comparability_status"] = "comparable"
        validate_cycle_plan(explicit_legacy, dbc_path=DBC)

        second = next_cycle_entry(legacy)
        advanced = advance_cycle(
            legacy, second["index"], "pair_0002",
            comparability_status="inconclusive",
        )
        self.assertEqual(advanced["schema_version"], 1)
        self.assertEqual(advanced["catalog_sha256"], self.plan["catalog_sha256"])
        self.assertEqual(advanced["completed_pairs"][-1], {
            "entry_index": second["index"], "pair_id": "pair_0002",
            "comparability_status": "inconclusive",
        })
        self.assertEqual(advanced["cursor"], second["index"] + 1)
        self.assertEqual(next_cycle_entry(advanced)["index"], second["index"] + 1)
        validate_cycle_plan(json.loads(json.dumps(advanced)), dbc_path=DBC)

        with self.assertRaisesRegex(ValueError, "comparability status"):
            advance_cycle(legacy, second["index"], "pair_0002", comparability_status="failed")
        for invalid in ("failed", None, True):
            tampered = copy.deepcopy(advanced)
            tampered["completed_pairs"][-1]["comparability_status"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "comparability status"
            ):
                validate_cycle_plan(tampered)

    def test_case_is_frozen_exploration_without_parent_or_feedback(self) -> None:
        entry = next_cycle_entry(self.plan)
        mutation = make_cycle_mutation(
            entry, mutation_id=37, source_bus="b_can",
            original_payload=BASELINE_PAYLOAD, random_seed=366,
        )
        self.assertEqual(mutation.mutation_id, 37)
        self.assertEqual(mutation.trial_kind, "mutation")
        self.assertEqual(mutation.strategy_mode, "EXPLORE")
        self.assertIsNone(mutation.parent_mutation_id)
        self.assertEqual(mutation.parameters["cycle_entry_index"], entry["index"])
        self.assertEqual(mutation.mutated_payload.hex().upper(), entry["mutated_payload"])
        with self.assertRaisesRegex(ValueError, "frozen source"):
            make_cycle_mutation(
                entry, mutation_id=37, source_bus="b_can",
                original_payload=b"\x00" * 8, random_seed=366,
            )
        skipped = next(item for item in self.plan["entries"]
                       if item["disposition"] == "skipped")
        with self.assertRaisesRegex(ValueError, "scheduled"):
            make_cycle_mutation(
                skipped, mutation_id=38, source_bus="b_can",
                original_payload=BASELINE_PAYLOAD, random_seed=366,
            )

    def test_changed_dbc_and_unsafe_phase_settings_fail_before_transmission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "modified.dbc"
            other.write_bytes(DBC.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "DBC changed"):
                validate_cycle_plan(self.plan, dbc_path=other)
        for kwargs in (
            {"mutation_duration_s": 2.0},
            {"mutation_interval_ms": 10},
            {"undefined_max_bits": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_cycle_plan(
                    DBC, BASELINE_PAYLOAD,
                    source_bus="b_can", random_seed=366, **kwargs,
                )


if __name__ == "__main__":
    unittest.main()
