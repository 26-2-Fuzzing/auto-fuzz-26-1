"""Offline receiver continuity and bounded report-history regression checks."""

from __future__ import annotations

import gc
import io
import json
import tempfile
import time
import tracemalloc
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import can_receiver


class GeneratedBus:
    """Generate raw traffic on demand; never open a CAN interface."""

    def __init__(self, count: int, *, memory_probes=()):
        self.count = count
        self.index = 0
        self.closed = False
        self.memory_probes = set(memory_probes)
        self.memory_bytes = {}

    def recv(self, timeout):
        del timeout
        if self.index in self.memory_probes:
            gc.collect()
            self.memory_bytes[self.index] = tracemalloc.get_traced_memory()[0]
        if self.index == self.count:
            raise KeyboardInterrupt  # Exercise orderly operator-stop log finalization.
        index = self.index
        self.index += 1
        return SimpleNamespace(
            timestamp=time.time(), arbitration_id=0x4 if index % 10 == 9 else 0x123,
            dlc=8, data=index.to_bytes(8, "little"), is_extended_id=False,
            is_remote_frame=False, is_error_frame=index % 10 == 9, is_fd=False,
            bitrate_switch=False, error_state_indicator=False, is_rx=True,
        )

    def shutdown(self):
        self.closed = True


class ToyMessage:
    name = "Toy"

    def decode(self, payload, **kwargs):
        return {"Sample": int.from_bytes(payload, "little")}


class ToyDatabase:
    messages = ()

    def get_message_by_frame_id(self, can_id):
        if can_id != 0x123:
            raise KeyError(can_id)
        return ToyMessage()


class ReceiverContinuityTests(unittest.TestCase):
    def run_capture(self, directory: Path, count: int, *, report: bool, probes=()):
        output = directory / "rx.jsonl"
        options = [
            "--bus-name", "i_can", "--interface", "virtual", "--channel", "offline",
            "--output", str(output), "--output-policy", "fail", "--experiment-id", "42",
            "--print-mode", "none", "--watch-id", "0x123", "--dbc", str(directory / "toy.dbc"),
        ]
        if not report:
            options.append("--no-report")
        bus = GeneratedBus(count, memory_probes=probes)
        with patch("can_receiver.open_can_bus", return_value=bus), \
                patch("can_receiver.load_dbc", return_value=ToyDatabase()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(can_receiver.run(can_receiver.build_parser().parse_args(options)), 0)
        self.assertTrue(bus.closed)
        seen = 0
        with output.open(encoding="utf-8") as handle:
            start = json.loads(next(handle))
            self.assertEqual(start["record_type"], "session_start")
            self.assertEqual(start["capture_options"]["log_all"], True)
            for line in handle:
                row = json.loads(line)
                if row["record_type"] == "session_end":
                    end = row
                    self.assertEqual(handle.read(), "")
                    break
                self.assertEqual(row["record_type"], "can_rx")
                self.assertEqual(row["session_id"], start["session_id"])
                self.assertEqual(row["rx_sequence"], seen + 1)
                self.assertEqual(row["data_hex"], seen.to_bytes(8, "little").hex().upper())
                if seen % 10 == 9:
                    self.assertTrue(row["is_error_frame"])
                    self.assertIn("can_error", row)
                else:
                    self.assertEqual(row["signals"], {"Sample": seen})
                seen += 1
            else:
                self.fail("receiver did not finalize its log")
        self.assertEqual(seen, count)
        self.assertEqual(end["received"], count)
        self.assertEqual(end["logged"], count)
        self.assertEqual(end["decoded"], count - count // 10)
        self.assertEqual(end["can_error_frames"], count // 10)
        self.assertEqual(end["decode_errors"], 0)
        self.assertEqual(end["reason"], "user_interrupt")
        self.assertEqual(end["report"] is not None, report)
        self.assertEqual(output.with_suffix(".md").exists(), report)
        return bus, output

    def test_no_report_preserves_stream_without_growing_report_history(self):
        self.assertFalse(tracemalloc.is_tracing())
        with tempfile.TemporaryDirectory() as directory:
            tracemalloc.start()
            try:
                bus, _ = self.run_capture(Path(directory), 12_000, report=False, probes=(2_000, 12_000))
            finally:
                tracemalloc.stop()
        growth = bus.memory_bytes[12_000] - bus.memory_bytes[2_000]
        self.assertLess(growth, 512 * 1024, f"receiver retained {growth:,} bytes of history")

    def test_report_enabled_still_preserves_payload_and_signal_history(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output = self.run_capture(Path(directory), 20, report=True)
            report = output.with_suffix(".md").read_text(encoding="utf-8")
            self.assertIn("CAN error frames: 2", report)
            self.assertIn("Sample", report)
            self.assertIn("Toy", report)
            self.assertIn("0x123", report)


if __name__ == "__main__":
    unittest.main()
