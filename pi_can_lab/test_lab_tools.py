from __future__ import annotations

import json
import math
import random
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analyze_fuzz_response import analyse_bus, build_parser as build_analysis_parser, run as run_analysis
from can_common import ConfigurationError
from can_receiver import build_parser as build_receiver_parser, run as run_receiver, validate_runtime_number
from can_sender import (
    build_parser as build_sender_parser,
    capture_live_payload,
    generate_mutations,
    generate_mutation_entries,
    mutation_summary,
    resolve_random_seed,
    run as run_sender,
    transmission_schedule,
)
from mutation_engine import Mutator
from mutation_feedback import (
    FeedbackHint,
    generate_guided_mutations,
    load_feedback_hints,
)


class ShortLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.launcher = runpy.run_path(str(Path(__file__).resolve().parent / "lab"))

    def test_receiver_preset_uses_bus_specific_yaml(self) -> None:
        command = self.launcher["build_command"](["rx", "i", "--duration", "120"])
        self.assertTrue(command[1].endswith("can_receiver.py"))
        self.assertTrue(command[3].endswith("receiver_i_can.yaml"))
        self.assertEqual(command[4:], ["--duration", "120"])

    def test_tx_defaults_to_safe_mutation_preset(self) -> None:
        command = self.launcher["build_command"](["tx"])
        self.assertTrue(command[1].endswith("can_sender.py"))
        self.assertTrue(command[3].endswith("sender_hazard_mutation.yaml"))
        self.assertNotIn("--execute", command)

    def test_tx_execute_and_named_preset_are_forwarded(self) -> None:
        execute = self.launcher["build_command"](["tx", "--execute"])
        status = self.launcher["build_command"](["tx", "status", "--count", "2"])
        self.assertEqual(execute[-1], "--execute")
        self.assertTrue(status[3].endswith("sender_hazard_status.yaml"))
        self.assertEqual(status[4:], ["--count", "2"])

    def test_raw_tx_does_not_inject_a_preset_config(self) -> None:
        command = self.launcher["build_command"](["tx", "raw", "--id", "0x123"])
        self.assertTrue(command[1].endswith("can_sender.py"))
        self.assertNotIn("--config", command)
        self.assertEqual(command[2:], ["--id", "0x123"])

    def test_invalid_role_or_preset_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown role"):
            self.launcher["build_command"](["monitor", "i"])
        with self.assertRaisesRegex(ValueError, "unknown sender preset"):
            self.launcher["build_command"](["tx", "unknown"])


class MutationTests(unittest.TestCase):
    BASE = bytes.fromhex("00001000200000F0")

    def test_local_mutations_are_deterministic_and_fixed_dlc(self) -> None:
        first = generate_mutations(self.BASE, 32, 3, False, True, 366)
        second = generate_mutations(self.BASE, 32, 3, False, True, 366)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual(first[0], self.BASE)
        self.assertEqual(len(set(first)), 32)
        self.assertTrue(all(len(payload) == 8 for payload in first))

    def test_local_mutation_output_matches_original_engine(self) -> None:
        payloads = generate_mutations(self.BASE, 8, 3, False, False, 366)
        self.assertEqual(
            [payload.hex().upper() for payload in payloads],
            [
                "00001000200100F0",
                "FF0010022000FFF0",
                "04000E00200000F0",
                "00001000200001EF",
                "00011001200000F1",
                "00FF1000200000F0",
                "0000100020FF00F0",
                "01001000200000F0",
            ],
        )

    def test_original_is_removed_when_not_requested(self) -> None:
        payloads = generate_mutations(self.BASE, 32, 3, False, False, 366)
        self.assertEqual(len(payloads), 32)
        self.assertNotIn(self.BASE, payloads)

    def test_missing_seed_gets_a_logged_per_run_seed(self) -> None:
        with patch("can_sender.secrets.randbits", side_effect=[101, 202]):
            first, first_generated = resolve_random_seed(None)
            second, second_generated = resolve_random_seed(None)
        self.assertEqual((first, second), (101, 202))
        self.assertTrue(first_generated)
        self.assertTrue(second_generated)
        self.assertEqual(resolve_random_seed(366), (366, False))

    def test_bit_operation_ratio_controls_exploration_granularity(self) -> None:
        bit_mutator = Mutator(
            self.BASE,
            {
                "manager.budget": 32,
                "manager.max_ops": 1,
                "manager.structural": False,
                "manager.bit_operation_ratio": 1.0,
            },
        )
        byte_mutator = Mutator(
            self.BASE,
            {
                "manager.budget": 32,
                "manager.max_ops": 1,
                "manager.structural": False,
                "manager.bit_operation_ratio": 0.0,
            },
        )
        random.seed(11)
        bit_mutator.mutate_manager()
        random.seed(11)
        byte_mutator.mutate_manager()
        self.assertTrue(all(
            operators[0] in {"flip_bit", "increment_bit", "decrement_bit"}
            for operators in bit_mutator.generated_operators
        ))
        self.assertTrue(all(
            operators[0] in {"increment_byte", "decrement_byte"}
            for operators in byte_mutator.generated_operators
        ))

    def test_random_entries_preserve_operator_provenance(self) -> None:
        entries = generate_mutation_entries(
            self.BASE, 8, 2, False, False, 366, 0.75
        )
        self.assertEqual(len(entries), 8)
        self.assertTrue(all(metadata["source"] == "exploration" for _, metadata in entries))
        self.assertTrue(all(metadata["operators"] for _, metadata in entries))

    def test_sender_preview_mixes_guided_and_exploration_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feedback = root / "feedback.json"
            feedback.write_text(json.dumps({
                "buses": [{
                    "bus": "i_can",
                    "reaction_candidates": [{
                        "can_id": "0x450",
                        "score": 80,
                        "confidence": "high",
                        "anomaly_type": "new_message",
                        "source_mutation": {
                            "source_bus": "b_can",
                            "sequence": 7,
                            "payload": "00000300200000F0",
                            "latency_ms": 5.0,
                            "mutation": {
                                "changed_byte_indexes": [2],
                                "xor_hex": "0000030000000000",
                            },
                        },
                    }],
                }],
            }), encoding="utf-8")
            config = root / "sender.yaml"
            config.write_text(
                """
bus:
  interface: virtual
  channel: test
sender:
  bus_name: b_can
  id: 0x366
  data: 00000000200000F0
  output: tx.jsonl
  mutation:
    enabled: true
    max_operations: 2
    include_original: false
    random_seed: 366
    bit_operation_ratio: 0.75
    feedback:
      path: feedback.json
      guided_ratio: 0.5
  transmit:
    count: 8
    interval_ms: 10
    restore_original: false
  safety:
    max_count: 8
    max_duration_seconds: 10
    min_interval_ms: 10
""".strip(),
                encoding="utf-8",
            )
            args = build_sender_parser().parse_args(["--config", str(config)])
            self.assertEqual(run_sender(args), 0)
            records = [
                json.loads(line)
                for line in (root / "tx.jsonl").read_text().splitlines()
            ]
        mutations = [
            record["mutation"]
            for record in records
            if record.get("record_type") == "can_tx"
        ]
        self.assertEqual(len(mutations), 8)
        self.assertTrue(any(item.get("source") == "guided" for item in mutations))
        self.assertTrue(any(item.get("source") == "exploration" for item in mutations))
        self.assertTrue(any(
            item.get("strategy") == "trigger_minimization" for item in mutations
        ))


class GuidedMutationTests(unittest.TestCase):
    BASE = bytes.fromhex("00000000200000F0")

    def hint(self, anomaly_type: str, source_payload: str, xor_hex: str) -> FeedbackHint:
        xor = bytes.fromhex(xor_hex)
        bits = tuple(
            (byte_index, bit_index)
            for byte_index, value in enumerate(xor)
            for bit_index in range(8)
            if value & (1 << bit_index)
        )
        return FeedbackHint(
            anomaly_type=anomaly_type,
            confidence="high",
            score=50,
            source_sequence=7,
            source_payload=bytes.fromhex(source_payload),
            changed_byte_indexes=tuple(sorted({index for index, _ in bits})),
            changed_bits=bits,
            target_bus="i_can",
            target_id="0x450",
            cross_bus=True,
            mapping_method="nearest_preceding_tx",
            latency_ms=5.0,
        )

    def test_timing_feedback_generates_boundary_search(self) -> None:
        hint = self.hint("timing", "0000FF00200000F0", "0000FF0000000000")
        guided = generate_guided_mutations(self.BASE, [hint], 12)
        self.assertTrue(guided)
        self.assertTrue(any(item.strategy == "boundary_search" for item in guided))
        self.assertTrue(all(item.payload != self.BASE for item in guided))
        self.assertTrue(all(
            sum(left != right for left, right in zip(self.BASE, item.payload)) == 1
            for item in guided
        ))

    def test_new_message_feedback_minimizes_to_bits(self) -> None:
        hint = self.hint("new_message", "00000300200000F0", "0000030000000000")
        guided = generate_guided_mutations(self.BASE, [hint], 8)
        strategies = {item.strategy for item in guided}
        self.assertIn("trigger_minimization", strategies)
        self.assertTrue(any(len(item.focus_bits) == 1 for item in guided))

    def test_analysis_json_loads_mutation_anomaly_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text(json.dumps({
                "buses": [{
                    "bus": "i_can",
                    "reaction_candidates": [{
                        "can_id": "0x450",
                        "score": 60,
                        "confidence": "high",
                        "anomaly_type": "new_message",
                        "source_mutation": {
                            "source_bus": "b_can",
                            "sequence": 7,
                            "payload": "00000300200000F0",
                            "latency_ms": 5.0,
                            "mapping_method": "nearest_preceding_tx",
                            "mutation": {
                                "changed_byte_indexes": [2],
                                "xor_hex": "0000030000000000",
                            },
                        },
                    }],
                }],
            }), encoding="utf-8")
            hints = load_feedback_hints(path)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].anomaly_type, "new_message")
        self.assertEqual(hints[0].changed_bits, ((2, 0), (2, 1)))
        self.assertTrue(hints[0].cross_bus)

    def test_mutation_summary(self) -> None:
        payload = bytes.fromhex("00000000200000F0")
        summary = mutation_summary(
            bytes.fromhex("00001000200000F0"), payload
        )
        self.assertEqual(summary["changed_byte_indexes"], [2])
        self.assertEqual(summary["xor_hex"], "0000100000000000")
        self.assertEqual(summary["changed_bit_count"], 1)

    def test_duration_schedule_cycles_mutations_until_deadline(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.now = 0.0
                self.sleeps: list[float] = []

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                self.now += seconds

        clock = FakeClock()
        first = bytes.fromhex("01")
        second = bytes.fromhex("02")
        scheduled = list(
            transmission_schedule(
                [first, second],
                interval_seconds=0.4,
                duration_seconds=1.0,
                clock=clock.monotonic,
                sleeper=clock.sleep,
            )
        )
        self.assertEqual(scheduled, [(1, first), (2, second), (3, first)])
        self.assertEqual(clock.now, 1.0)
        self.assertEqual(clock.sleeps[:2], [0.4, 0.4])
        self.assertAlmostEqual(clock.sleeps[2], 0.2)

    def test_count_schedule_still_sends_corpus_once(self) -> None:
        sleeps: list[float] = []
        payloads = [bytes.fromhex("01"), bytes.fromhex("02")]
        scheduled = list(
            transmission_schedule(
                payloads,
                interval_seconds=0.01,
                sleeper=sleeps.append,
            )
        )
        self.assertEqual(scheduled, [(1, payloads[0]), (2, payloads[1])])
        self.assertEqual(sleeps, [0.01])

    def test_live_baseline_requires_configured_stability(self) -> None:
        class FakeBus:
            def __init__(self, payloads: list[bytes]) -> None:
                self.payloads = list(payloads)

            def recv(self, timeout: float):
                del timeout
                payload = self.payloads.pop(0)
                return SimpleNamespace(
                    arbitration_id=0x366,
                    is_extended_id=False,
                    data=payload,
                )

        stable = bytes.fromhex("00000000200000F0")
        selected = capture_live_payload(
            FakeBus([stable, stable, stable]), 0x366, False, 1.0, 3, 1.0
        )
        self.assertEqual(selected, stable)
        with self.assertRaisesRegex(RuntimeError, "baseline이 불안정"):
            capture_live_payload(
                FakeBus([stable, bytes(8), stable]), 0x366, False, 1.0, 3, 1.0
            )

    def test_campaign_preview_logs_all_phases_and_restores_raw_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "tx.jsonl"
            config = root / "sender.yaml"
            config.write_text(
                """
bus:
  interface: virtual
  channel: test
sender:
  id: 0x366
  data: 00000000200000F0
  output: tx.jsonl
  mutation:
    enabled: true
    seed_source: normal
    max_operations: 2
    include_original: false
    random_seed: 366
  campaign:
    enabled: true
    baseline_duration_seconds: 1
    normal_duration_seconds: 60
    mutation_duration_seconds: 60
    recovery_duration_seconds: 1
  transmit:
    count: 4
    interval_ms: 10
    restore_original: true
  safety:
    max_count: 4
    max_duration_seconds: 60
    max_campaign_duration_seconds: 122
    min_interval_ms: 10
""".strip(),
                encoding="utf-8",
            )
            args = build_sender_parser().parse_args(["--config", str(config)])
            self.assertEqual(run_sender(args), 0)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            phases = [
                (item["phase"], item["event"])
                for item in records if item["record_type"] == "tx_phase"
            ]
            self.assertEqual(phases, [
                ("baseline", "start"), ("baseline", "end"),
                ("normal", "start"), ("normal", "end"),
                ("mutation", "start"), ("mutation", "end"),
                ("recovery", "start"), ("recovery", "end"),
            ])
            tx = [item for item in records if item["record_type"] == "can_tx"]
            self.assertEqual([item["phase"] for item in tx], [
                "normal", "mutation", "mutation", "mutation", "mutation",
                "recovery",
            ])
            self.assertEqual(tx[-1]["kind"], "restore")
            self.assertEqual(tx[-1]["data_hex"], "00000000200000F0")


class ReceiverValidationTests(unittest.TestCase):
    def test_non_finite_timing_is_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ConfigurationError):
                validate_runtime_number(value, "test")

    def test_receiver_records_raw_frame_and_session_id(self) -> None:
        class FakeBus:
            def __init__(self) -> None:
                self.sent_frame = False
                self.closed = False

            def recv(self, timeout: float):
                del timeout
                if self.sent_frame:
                    return None
                self.sent_frame = True
                return SimpleNamespace(
                    timestamp=1.0,
                    arbitration_id=0x366,
                    dlc=8,
                    data=bytes.fromhex("00000000200000F0"),
                    is_extended_id=False,
                    is_remote_frame=False,
                    is_error_frame=False,
                    is_fd=False,
                    bitrate_switch=False,
                    error_state_indicator=False,
                    is_rx=True,
                )

            def shutdown(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rx.jsonl"
            args = build_receiver_parser().parse_args([
                "--bus-name", "i_can",
                "--interface", "virtual",
                "--channel", "test",
                "--duration", "0.01",
                "--output", str(output),
                "--print-mode", "none",
            ])
            bus = FakeBus()
            with patch("can_receiver.open_can_bus", return_value=bus):
                self.assertEqual(run_receiver(args), 0)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            frame = next(item for item in records if item["record_type"] == "can_rx")
            self.assertEqual(frame["data_hex"], "00000000200000F0")
            self.assertTrue(frame["session_id"])
            self.assertEqual(records[0]["session_id"], frame["session_id"])
            self.assertTrue(output.with_suffix(".md").is_file())
            self.assertTrue(bus.closed)


class ResponseAnalysisTests(unittest.TestCase):
    def test_direct_route_and_stable_reaction_are_ranked(self) -> None:
        second = 1_000_000_000
        tx = [{
            "time_ns": 20 * second,
            "sequence": 1,
            "arbitration_id": 0x366,
            "is_extended_id": False,
            "payload": bytes.fromhex("00001000200000F0"),
            "source_bus": "b_can",
            "mutation": {
                "changed_byte_indexes": [2],
                "xor_hex": "0000100000000000",
                "strategy": "random",
                "operators": ["flip_bit"],
            },
        }]
        rx = [
            {
                "time_ns": 12 * second,
                "arbitration_id": 0x366,
                "is_extended_id": False,
                "payload": bytes.fromhex("00000000200000F0"),
            },
            {
                "time_ns": 13 * second,
                "arbitration_id": 0x123,
                "is_extended_id": False,
                "payload": bytes.fromhex("00"),
            },
            {
                "time_ns": 20 * second + 5_000_000,
                "arbitration_id": 0x366,
                "is_extended_id": False,
                "payload": bytes.fromhex("00001000200000F0"),
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
                "payload": bytes.fromhex("00"),
            },
        ]
        result = analyse_bus(
            "i_can",
            rx,
            tx,
            None,
            10 * second,
            20 * second,
            22 * second,
            30 * second,
            250_000_000,
        )
        direct = result["direct_correlation"]
        self.assertEqual(direct["matched_tx_count"], 1)
        self.assertEqual(direct["novel_matched_tx_count"], 1)
        candidates = {item["can_id"]: item for item in result["candidates"]}
        self.assertIn("0x366", candidates)
        self.assertIn("0x123", candidates)
        self.assertIn("stable baseline payload changed", candidates["0x123"]["reasons"])
        self.assertEqual(candidates["0x123"]["recovery_baseline_payload_ratio"], 1.0)
        self.assertEqual(candidates["0x123"]["anomaly_type"], "payload_signal")
        self.assertEqual(candidates["0x123"]["source_mutation"]["sequence"], 1)
        self.assertEqual(candidates["0x123"]["source_mutation"]["source_bus"], "b_can")
        self.assertEqual(len(result["mutation_anomaly_mappings"]), 1)

    def test_end_to_end_analysis_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tx_path = root / "tx.jsonl"
            rx_path = root / "rx.jsonl"
            output = root / "result.json"
            tx_records = [
                {"record_type": "tx_session_start", "wall_time_ns": 19_000_000_000},
                {
                    "record_type": "can_tx",
                    "status": "sent",
                    "sequence": 1,
                    "send_attempt_wall_time_ns": 19_000_000_000,
                    "arbitration_id": 0x366,
                    "is_extended_id": False,
                    "data_hex": "00000000200000F0",
                    "kind": "normal",
                    "phase": "normal",
                },
                {
                    "record_type": "can_tx",
                    "status": "sent",
                    "sequence": 2,
                    "send_attempt_wall_time_ns": 20_000_000_000,
                    "arbitration_id": 0x366,
                    "is_extended_id": False,
                    "data_hex": "00001000200000F0",
                    "kind": "mutation",
                    "phase": "mutation",
                    "bus": "b_can",
                    "mutation": {
                        "changed_byte_indexes": [2],
                        "xor_hex": "0000100000000000",
                        "strategy": "random",
                        "operators": ["flip_bit"],
                    },
                },
            ]
            rx_records = [
                {
                    "record_type": "can_rx",
                    "wall_time_ns": 15_000_000_000,
                    "arbitration_id": 0x366,
                    "is_extended_id": False,
                    "data_hex": "00000000200000F0",
                },
                {
                    "record_type": "can_rx",
                    "wall_time_ns": 20_005_000_000,
                    "arbitration_id": 0x366,
                    "is_extended_id": False,
                    "data_hex": "00001000200000F0",
                },
            ]
            tx_path.write_text(
                "".join(json.dumps(item) + "\n" for item in tx_records),
                encoding="utf-8",
            )
            rx_path.write_text(
                "".join(json.dumps(item) + "\n" for item in rx_records),
                encoding="utf-8",
            )
            args = build_analysis_parser().parse_args([
                "--tx", str(tx_path),
                "--rx", f"i_can={rx_path}",
                "--baseline-seconds", "10",
                "--response-seconds", "2",
                "--recovery-seconds", "10",
                "--output", str(output),
            ])
            self.assertEqual(run_analysis(args), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["tx"]["frame_count"], 1)
            self.assertEqual(result["tx"]["first_tx_ns"], 20_000_000_000)
            self.assertEqual(
                result["buses"][0]["direct_correlation"]["novel_matched_tx_count"],
                1,
            )
            self.assertTrue(output.with_suffix(".md").is_file())


if __name__ == "__main__":
    unittest.main()
