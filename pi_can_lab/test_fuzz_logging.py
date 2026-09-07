from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

import yaml

from analyze_fuzz_response import analyse_bus
from can_common import reserve_output_path, socketcan_error_details
from can_receiver import capture_markdown_report


class NumberedOutputTests(unittest.TestCase):
    def test_numbered_output_reserves_new_file_for_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "b_can.jsonl"
            first = reserve_output_path(base, "numbered")
            second = reserve_output_path(base, "numbered")
            self.assertEqual(first.name, "b_can_1.jsonl")
            self.assertEqual(second.name, "b_can_2.jsonl")
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_b_i_p_receivers_create_numbered_jsonl_and_reports(self) -> None:
        root = Path(__file__).resolve().parent
        for bus in ("b", "i", "p"):
            config = yaml.safe_load(
                (root / f"receiver_{bus}_can.yaml").read_text(encoding="utf-8")
            )["receiver"]
            self.assertEqual(config["output_policy"], "numbered")
            self.assertTrue(config["write_report"])
            self.assertEqual(config["output"], f"logs/{bus}_can.jsonl")

    def test_hazard_campaign_has_requested_phases_and_fresh_seed(self) -> None:
        root = Path(__file__).resolve().parent
        sender = yaml.safe_load(
            (root / "sender_hazard_mutation.yaml").read_text(encoding="utf-8")
        )["sender"]
        self.assertIsNone(sender["mutation"]["random_seed"])
        self.assertEqual(sender["mutation"]["seed_source"], "normal")
        self.assertFalse(sender["mutation"]["include_original"])
        campaign = sender["campaign"]
        self.assertTrue(campaign["enabled"])
        self.assertEqual(campaign["normal_duration_seconds"], 60.0)
        self.assertEqual(campaign["mutation_duration_seconds"], 60.0)
        self.assertGreater(campaign["recovery_duration_seconds"], 0)

    def test_trial_sender_is_single_case_and_dbc_independent(self) -> None:
        root = Path(__file__).resolve().parent
        sender = yaml.safe_load(
            (root / "sender_trial.yaml").read_text(encoding="utf-8")
        )["sender"]
        self.assertTrue(sender["mutation"]["enabled"])
        self.assertEqual(sender["transmit"]["count"], 1)
        self.assertEqual(sender["safety"]["max_count"], 1)
        self.assertNotIn("dbc", sender)
        self.assertNotIn("set", sender)


class CaptureReportTests(unittest.TestCase):
    def test_socketcan_rx_overflow_is_human_readable(self) -> None:
        details = socketcan_error_details(0x4, bytes.fromhex("0001000000000000"))
        self.assertIn("controller_problem", details["classes"])
        self.assertIn("rx_buffer_overflow", details["controller_status"])
        self.assertEqual(details["severity"], "warning")

    def test_report_summarises_signal_transition(self) -> None:
        key = (0x366, False)
        report = capture_markdown_report(
            bus_name="b_can",
            channel="can0",
            session_id="session",
            experiment_id="hazard_001",
            jsonl_path=Path("b_can_1.jsonl"),
            duration_seconds=1.0,
            total=2,
            logged=2,
            decoded_count=2,
            decode_errors=0,
            id_counts=Counter({key: 2}),
            id_payloads={key: {"00000000200000F0", "00001000200000F0"}},
            id_first_ns={key: 1_000_000_000},
            id_last_ns={key: 1_100_000_000},
            can_errors=[],
            signal_transitions=[{
                "wall_time": "2026-08-21T14:27:20.700000000",
                "can_id": "0x366",
                "message": "Blinkmodi_02",
                "signal": "BM_Warnblinken",
                "before": 0,
                "after": 1,
                "data_hex": "00001000200000F0",
            }],
            max_capture_lag_ns=900_000,
        )
        self.assertIn("Watch 신호 변화 관측", report)
        self.assertIn("BM_Warnblinken", report)
        self.assertIn("b_can_1.jsonl", report)


class ConservativeCorrelationTests(unittest.TestCase):
    def test_unrecovered_raw_change_stays_low_confidence(self) -> None:
        second = 1_000_000_000
        tx = [{
            "time_ns": 20 * second,
            "sequence": 1,
            "arbitration_id": 0x366,
            "is_extended_id": False,
            "payload": bytes.fromhex("01"),
        }]
        rx = []
        for stamp in range(10, 20):
            rx.append({
                "time_ns": stamp * second,
                "arbitration_id": 0x123,
                "is_extended_id": False,
                "payload": bytes.fromhex("00"),
            })
        rx.extend([
            {
                "time_ns": 20 * second + 1_000_000,
                "arbitration_id": 0x366,
                "is_extended_id": False,
                "payload": bytes.fromhex("01"),
            },
            {
                "time_ns": 20 * second + 10_000_000,
                "arbitration_id": 0x123,
                "is_extended_id": False,
                "payload": bytes.fromhex("01"),
            },
            {
                "time_ns": 24 * second,
                "arbitration_id": 0x123,
                "is_extended_id": False,
                "payload": bytes.fromhex("01"),
            },
        ])
        result = analyse_bus(
            "b_can", rx, tx, None,
            10 * second, 20 * second, 22 * second, 30 * second, 250_000_000,
        )
        candidate = next(
            item for item in result["reaction_candidates"]
            if item["can_id"] == "0x123"
        )
        self.assertEqual(candidate["confidence"], "low")
        self.assertEqual(result["verdict"]["meaningful_reaction_candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
