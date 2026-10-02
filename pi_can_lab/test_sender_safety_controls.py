"""Offline campaign safety and no-op control tests for the CAN sender."""

from __future__ import annotations

import json
import io
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from can_common import ConfigurationError
from can_sender import SenderInterrupted, build_parser, run


ORIGINAL = "00000000200000F0"
MUTATED = "0000000020000018"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBus:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.sent: list[bytes] = []
        self.closed = False

    def send(self, message: SimpleNamespace, timeout: float) -> None:
        del timeout
        payload = bytes(message.data)
        if payload.hex().upper() == MUTATED and self.failure:
            failure = self.failure
            self.failure = None
            if failure == "error":
                raise RuntimeError("injected send failure")
            if failure == "keyboard":
                raise KeyboardInterrupt()
            if failure == "sigterm":
                signal.raise_signal(signal.SIGTERM)
        self.sent.append(payload)

    def shutdown(self) -> None:
        self.closed = True


class SenderSafetyControlTests(unittest.TestCase):
    def make_config(self, root: Path, *, safety: str = "") -> Path:
        config = root / "sender.yaml"
        config.write_text(f"""
bus:
  interface: virtual
  channel: test
sender:
  id: 0x366
  data: {ORIGINAL}
  output: tx.jsonl
  mutation:
    enabled: true
    seed_source: normal
  campaign:
    enabled: true
    baseline_duration_seconds: 0.1
    normal_duration_seconds: 0.2
    mutation_duration_seconds: 0.1
    recovery_duration_seconds: 0.1
  transmit:
    count: 1
    interval_ms: 50
    send_timeout_seconds: 1
    restore_original: true
    restore_count: 1
    restore_delay_ms: 0
  safety:
    max_count: 1
    max_campaign_duration_seconds: 120
    min_interval_ms: 10
{safety}
""".strip(), encoding="utf-8")
        return config

    def args(self, config: Path, *extra: str):
        return build_parser().parse_args([
            "--config", str(config), "--trial-contract-version", "1",
            "--mutation-data", MUTATED, "--execute", *extra,
        ])

    def records(self, root: Path) -> list[dict]:
        return [json.loads(line) for line in (root / "tx.jsonl").read_text().splitlines()]

    def run_fake(self, args, bus: FakeBus, clock: FakeClock) -> int:
        with (
            patch("can_sender.open_can_bus", return_value=bus),
            patch("can_sender.create_message", side_effect=lambda _id, data, *_: SimpleNamespace(data=data)),
            patch("can_sender.time.monotonic", clock.monotonic),
            patch("can_sender.time.sleep", clock.sleep),
        ):
            with redirect_stdout(io.StringIO()):
                return run(args)

    def test_noop_uses_only_original_and_is_explicitly_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            args = self.args(config, "--mutation-data", ORIGINAL, "--control-noop")
            bus, clock = FakeBus(), FakeClock()
            self.assertEqual(self.run_fake(args, bus, clock), 0)
            records = self.records(root)

        start = records[0]
        end = records[-1]
        tx = [item for item in records if item["record_type"] == "can_tx"]
        self.assertEqual(start["trial_kind"], "noop")
        self.assertEqual(start["trial_contract_version"], 1)
        self.assertEqual(start["effective_safety"]["min_interval_ms"], 50.0)
        self.assertFalse(start["mutation"]["enabled"])
        self.assertEqual([item["kind"] for item in tx if item["phase"] == "mutation"], ["control", "control"])
        self.assertTrue(all(item["data_hex"] == ORIGINAL for item in tx))
        self.assertEqual([item["phase"] for item in tx], ["normal"] * 4 + ["mutation"] * 2 + ["recovery"])
        self.assertEqual(end["status"], "completed")
        self.assertEqual(end["restore"]["sent"], 1)
        self.assertEqual(end["phase_sent"], {"normal": 4, "mutation": 2})
        self.assertEqual(len(bus.sent), 7)
        self.assertTrue(bus.closed)

    def test_temporal_noop_matches_requested_control_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            sequence = {"sequence": {"name": "MATCHED_NOOP", "interval_ms": 100,
                                     "frames": [ORIGINAL, ORIGINAL]}}
            args = self.args(
                config, "--mutation-data", ORIGINAL, "--control-noop",
                "--mutation-duration", "0.2",
                "--mutation-metadata-json", json.dumps(sequence),
            )
            bus, clock = FakeBus(), FakeClock()
            self.assertEqual(self.run_fake(args, bus, clock), 0)
            records = self.records(root)
        control = [item for item in records
                   if item.get("record_type") == "can_tx" and item.get("phase") == "mutation"]
        self.assertEqual(len(control), 2)
        self.assertTrue(all(item["data_hex"] == ORIGINAL and item["kind"] == "control"
                            for item in control))
        self.assertEqual(records[0]["mutation"]["targeted_metadata"]["sequence"], sequence["sequence"])

    def test_temporal_noop_rejects_changed_payload_before_opening_bus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            sequence = {"sequence": {"interval_ms": 100, "frames": [ORIGINAL, MUTATED]}}
            args = self.args(
                config, "--mutation-data", ORIGINAL, "--control-noop",
                "--mutation-metadata-json", json.dumps(sequence),
            )
            with patch("can_sender.open_can_bus") as open_bus:
                with self.assertRaisesRegex(ConfigurationError, "모든 프레임이 원본"):
                    run(args)
            open_bus.assert_not_called()

    def test_identical_payload_without_noop_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            args = self.args(config, "--mutation-data", ORIGINAL)
            with patch("can_sender.open_can_bus") as open_bus:
                with self.assertRaisesRegex(ConfigurationError, "달라야"):
                    run(args)
            open_bus.assert_not_called()

    def test_contract_and_config_caps_reject_before_opening_bus(self) -> None:
        cases = [
            (("--mutation-duration", "1.05"), "mutation_duration_seconds"),
            (("--normal-duration", "10.05"), "normal 송신 계획"),
            (("--interval-ms", "49"), "interval_ms"),
        ]
        for extra, error in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                config = self.make_config(Path(directory))
                with patch("can_sender.open_can_bus") as open_bus:
                    with self.assertRaisesRegex(ConfigurationError, error):
                        run(self.args(config, *extra))
                open_bus.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), safety="    max_mutation_frames: 1")
            with patch("can_sender.open_can_bus") as open_bus:
                with self.assertRaisesRegex(ConfigurationError, "mutation 송신 계획"):
                    run(self.args(config))
            open_bus.assert_not_called()

    def test_contract_rejects_sender_side_feedback_before_bus_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            with patch("can_sender.open_can_bus") as open_bus:
                with self.assertRaisesRegex(ConfigurationError, "sender-side feedback"):
                    run(self.args(config, "--feedback", "old_feedback.json"))
            open_bus.assert_not_called()
            config.write_text(
                config.read_text().replace(
                    "    seed_source: normal",
                    "    seed_source: normal\n    feedback:\n      path: old_feedback.json",
                ),
                encoding="utf-8",
            )
            with patch("can_sender.open_can_bus") as open_bus:
                with self.assertRaisesRegex(ConfigurationError, "sender-side feedback"):
                    run(self.args(config))
            open_bus.assert_not_called()

    def test_contract_requires_one_restore_before_bus_open(self) -> None:
        for extra in (("--no-restore",), ()):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = self.make_config(root)
                if not extra:
                    config.write_text(
                        config.read_text().replace("restore_count: 1", "restore_count: 2"),
                        encoding="utf-8",
                    )
                with patch("can_sender.open_can_bus") as open_bus:
                    with self.assertRaisesRegex(ConfigurationError, "exactly one original restore"):
                        run(self.args(config, *extra))
                open_bus.assert_not_called()

    def test_calibrated_30_10_1_20_trial_sends_at_most_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            args = self.args(
                config, "--baseline-duration", "30", "--normal-duration", "10",
                "--mutation-duration", "1", "--recovery-duration", "20",
            )
            bus, clock = FakeBus(), FakeClock()
            self.assertEqual(self.run_fake(args, bus, clock), 0)
            end = self.records(root)[-1]
        self.assertEqual(end["phase_sent"], {"normal": 200, "mutation": 20})
        self.assertEqual(end["restore"]["sent"], 1)
        self.assertEqual(len(bus.sent), 221)
        self.assertAlmostEqual(clock.now, 61.0)

    def test_failure_during_mutation_attempts_restore_and_logs_abort(self) -> None:
        for failure, exception in (
            ("error", RuntimeError),
            ("keyboard", KeyboardInterrupt),
            ("sigterm", SenderInterrupted),
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = self.make_config(root)
                bus, clock = FakeBus(failure), FakeClock()
                with self.assertRaises(exception):
                    self.run_fake(self.args(config), bus, clock)
                records = self.records(root)
                end = records[-1]
                self.assertEqual(end["record_type"], "tx_session_end")
                self.assertEqual(end["status"], "aborted")
                self.assertEqual(end["restore"]["status"], "sent")
                self.assertEqual(end["restore"]["sent"], 1)
                self.assertEqual(bus.sent[-1].hex().upper(), ORIGINAL)
                self.assertTrue(bus.closed)

    def test_runtime_frame_cap_aborts_without_exceeding_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root, safety="    max_mutation_frames: 2")
            bus, clock = FakeBus(), FakeClock()
            from can_sender import transmission_schedule as real_schedule

            def overrun_mutation(payloads, interval_seconds, *args, **kwargs):
                if payloads[0].hex().upper() == MUTATED:
                    for index in range(3):
                        yield index + 1, payloads[0]
                else:
                    yield from real_schedule(payloads, interval_seconds, *args, **kwargs)

            with patch("can_sender.transmission_schedule", side_effect=overrun_mutation):
                with self.assertRaisesRegex(RuntimeError, "frame limit 2"):
                    self.run_fake(self.args(config), bus, clock)
            records = self.records(root)
        end = records[-1]
        self.assertEqual(end["status"], "aborted")
        self.assertEqual(end["phase_sent"]["mutation"], 2)
        self.assertEqual(end["restore"]["sent"], 1)

    def test_slow_mutation_send_exceeding_deadline_aborts_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            clock = FakeClock()

            class SlowBus(FakeBus):
                def send(self, message: SimpleNamespace, timeout: float) -> None:
                    super().send(message, timeout)
                    if bytes(message.data).hex().upper() == MUTATED:
                        clock.now += 0.11

            bus = SlowBus()
            with self.assertRaisesRegex(RuntimeError, "deadline exceeded during send"):
                self.run_fake(self.args(config), bus, clock)
            end = self.records(root)[-1]
        self.assertEqual(end["status"], "aborted")
        self.assertEqual(end["phase_sent"]["mutation"], 1)
        self.assertEqual(end["restore"]["sent"], 1)

    def test_second_sigterm_does_not_interrupt_abort_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)

            class RepeatedSignalBus(FakeBus):
                def __init__(self) -> None:
                    super().__init__()
                    self.mutation_seen = False

                def send(self, message: SimpleNamespace, timeout: float) -> None:
                    if bytes(message.data).hex().upper() == MUTATED:
                        self.mutation_seen = True
                        signal.raise_signal(signal.SIGTERM)
                    if self.mutation_seen:
                        signal.raise_signal(signal.SIGTERM)
                    super().send(message, timeout)

            bus, clock = RepeatedSignalBus(), FakeClock()
            with self.assertRaises(SenderInterrupted):
                self.run_fake(self.args(config), bus, clock)
            end = self.records(root)[-1]
        self.assertEqual(end["status"], "aborted")
        self.assertIn("SenderInterrupted", end["error"])
        self.assertEqual(end["restore"]["sent"], 1)

    def test_restore_failure_preserves_original_mutation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)

            class FailingRestoreBus(FakeBus):
                def __init__(self) -> None:
                    super().__init__()
                    self.mutation_seen = False

                def send(self, message: SimpleNamespace, timeout: float) -> None:
                    payload = bytes(message.data).hex().upper()
                    if payload == MUTATED:
                        self.mutation_seen = True
                        raise RuntimeError("original mutation failure")
                    if self.mutation_seen:
                        raise RuntimeError("restore failure")
                    super().send(message, timeout)

            bus, clock = FailingRestoreBus(), FakeClock()
            with self.assertRaisesRegex(RuntimeError, "original mutation failure"):
                self.run_fake(self.args(config), bus, clock)
            end = self.records(root)[-1]
        self.assertEqual(end["status"], "aborted")
        self.assertIn("original mutation failure", end["error"])
        self.assertEqual(end["restore"]["status"], "failed")
        self.assertEqual(end["restore"]["attempted"], 1)
        self.assertIn("restore failure", end["restore"]["error"])


if __name__ == "__main__":
    unittest.main()
