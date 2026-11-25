"""Guard the paired catalogue against changing the completed campaign cases."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest import TestCase

from can_common import ConfigurationError
from canoe_catalog import (
    DEFAULT_CONFIG, build_slot_frames, load_settings, select_mutation_cases,
)
from paired_cycle import CYCLE_FAMILIES


class CANoeCatalogParityTests(TestCase):
    def test_each_family_has_its_own_safe_cases_and_schedule(self):
        settings = load_settings(argparse.Namespace(
            config=DEFAULT_CONFIG, method="signal_single", cases=200,
        ))
        expected_counts = {
            "signal_single": 122,
            "signal_combination": 4,
            "state_contradiction": 13,
            "undefined_enum": 53,
            "undefined_bit_single": 26,
            "undefined_bit_multi": 71,
            "defined_undefined_mix": 42,
            "temporal_sequence": 12,
        }
        self.assertEqual(set(expected_counts), set(CYCLE_FAMILIES))
        for family, expected in expected_counts.items():
            with self.subTest(family=family):
                selected_settings = replace(settings, method=family)
                selection = select_mutation_cases(selected_settings)
                self.assertEqual(selection.total_available_cases, expected)
                self.assertEqual(len(selection.cases), expected)
                for case in selection.cases:
                    frames = build_slot_frames(case, selected_settings)
                    self.assertTrue(1 <= len(frames) <= 20)
                    self.assertEqual(frames[0][1], case.mutated_payload)
        temporal = replace(settings, method="temporal_sequence")
        temporal_selection = select_mutation_cases(temporal)
        cases = temporal_selection.cases
        hundred_ms = next(case for case in cases if case.parameters["sequence"]["interval_ms"] == 100)
        frames = build_slot_frames(hundred_ms, temporal)
        self.assertEqual(len(frames), 10)
        self.assertEqual([round(offset * 1000) for offset, _ in frames], list(range(0, 1000, 100)))
        source = [bytes.fromhex(payload) for payload in hundred_ms.parameters["sequence"]["frames"]]
        self.assertEqual([payload for _, payload in frames],
                         [source[index % len(source)] for index in range(10)])
        unsupported_sequence = dict(hundred_ms.parameters["sequence"], interval_ms=75)
        unsupported_case = replace(
            hundred_ms,
            parameters={**hundred_ms.parameters, "sequence": unsupported_sequence},
        )
        with self.assertRaisesRegex(ConfigurationError, "50ms or 100ms"):
            build_slot_frames(unsupported_case, temporal)
        with TemporaryDirectory() as temporary:
            copied_dbc = Path(temporary) / "temporal-same-bytes.dbc"
            shutil.copyfile(temporal.dbc_path, copied_dbc)
            moved = select_mutation_cases(replace(temporal, dbc_path=copied_dbc))
        self.assertEqual(moved.catalog_sha256, temporal_selection.catalog_sha256)

    def test_all_122_signal_single_cases_match_frozen_reference(self):
        reference = json.loads(
            Path(__file__).with_name("canoe_catalog_reference.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            reference["dbc_sha256"],
            "6a5452ebd0fcc7cf2bafaea733d7299bf3d7ea12f8b51debc47b68368d9eb840",
        )
        self.assertEqual(
            reference["portable_catalog_sha256"],
            "fc2b941027312b3239ca2f999716d5797b48dd1adbf60b62c72d9c237a8f8c37",
        )
        self.assertEqual(reference["baseline_payload_hex"], "00000000200000F0")

        settings = load_settings(argparse.Namespace(
            config=DEFAULT_CONFIG, method="signal_single", cases=122,
        ))
        self.assertEqual(settings.baseline_payload.hex().upper(), reference["baseline_payload_hex"])
        selection = select_mutation_cases(settings)
        self.assertEqual(selection.total_available_cases, 122)
        self.assertEqual(selection.case_start, 1)
        self.assertEqual(selection.case_end, 122)
        self.assertEqual(selection.catalog_sha256, reference["portable_catalog_sha256"])
        self.assertEqual(selection.dbc_sha256, reference["dbc_sha256"])
        observed = [
            {
                "number": case.mutation_id,
                "name": case.parameters["case"],
                "payload_hex": case.mutated_payload.hex().upper(),
            }
            for case in selection.cases
        ]
        self.assertEqual(observed, reference["cases"])

        with TemporaryDirectory() as temporary:
            copied_dbc = Path(temporary) / "same-bytes-different-path.dbc"
            shutil.copyfile(settings.dbc_path, copied_dbc)
            moved_selection = select_mutation_cases(replace(settings, dbc_path=copied_dbc))
        self.assertEqual(moved_selection.dbc_sha256, selection.dbc_sha256)
        self.assertEqual(moved_selection.catalog_sha256, selection.catalog_sha256)
        self.assertEqual(
            [
                (case.mutation_id, case.parameters["case"], case.mutated_payload)
                for case in moved_selection.cases
            ],
            [
                (case.mutation_id, case.parameters["case"], case.mutated_payload)
                for case in selection.cases
            ],
        )
