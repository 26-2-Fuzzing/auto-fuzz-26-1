"""Offline tests of the CANoe CAPL schedule commit and acknowledgement."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from canoe_schedule import CANoeScheduleError, CANoeScheduleInterface


class FakeVariable:
    def __init__(self, owner, namespace, name):
        self.owner = owner
        self.namespace = namespace
        self.name = name

    @property
    def Value(self):
        if self.namespace == "FuzzDashboard" and self.name == "Heartbeat":
            if not self.owner.freeze_heartbeat:
                self.owner.values[self.namespace][self.name] += 1
        if self.namespace == "FuzzSchedule" and self.name == "DoneSeq":
            self.owner.done_reads += 1
            if self.owner.mode == "delayed" and self.owner.done_reads >= 3:
                self.owner.finish(1)
        if self.namespace == "FuzzSchedule" and self.name == "RequestSeq":
            if self.owner.stale_request_reads > 0 and self.owner.trigger_count:
                self.owner.stale_request_reads -= 1
                return 0
        if self.namespace == "FuzzSchedule" and self.owner.trigger_count:
            if self.owner.stale_completion_reads.get(self.name, 0) > 0:
                self.owner.stale_completion_reads[self.name] -= 1
                return 0
        return self.owner.values[self.namespace][self.name]

    @Value.setter
    def Value(self, value):
        self.owner.values[self.namespace][self.name] = value
        self.owner.writes.append((self.namespace, self.name, value))
        if self.namespace == "FuzzSchedule" and self.name == "RequestSeq":
            self.owner.trigger_count += 1
            self.owner.done_reads = 0
            if self.owner.mode == "immediate":
                self.owner.finish(1)
            elif self.owner.mode == "rejected":
                self.owner.finish(-22)
            elif self.owner.mode == "conflict":
                self.owner.values[self.namespace][self.name] = value + 1
            elif self.owner.mode == "conflict_after_done":
                self.owner.finish(1)
                self.owner.values[self.namespace][self.name] = value + 1


class FakeNamespaces:
    def __init__(self, owner):
        self.owner = owner

    def Item(self, namespace):
        return SimpleNamespace(Variables=SimpleNamespace(
            Item=lambda name: FakeVariable(self.owner, namespace, name)
        ))


class FakeSystem:
    def __init__(self):
        self.values = {
            "FuzzDashboard": {"Heartbeat": 1},
            "FuzzIO": {
                "TxRequestSeq": 0, "TxDoneSeq": 0,
                **{f"BusOff{i}": 0 for i in (1, 2, 3)},
                **{f"ErrorFrame{i}": 0 for i in (1, 2, 3)},
            },
            "FuzzSchedule": {
                "RequestSeq": 0, "DoneSeq": 0, "Result": 0,
                "Submitted": 0, "StartNs": 0, "EndNs": 0,
                "Lease": 0, "CancelSeq": 0,
            },
        }
        self.Namespaces = FakeNamespaces(self)
        self.mode = "immediate"
        self.trigger_count = 0
        self.done_reads = 0
        self.freeze_heartbeat = False
        self.stale_request_reads = 0
        self.stale_completion_reads = {}
        self.writes = []

    def finish(self, result):
        schedule = self.values["FuzzSchedule"]
        if schedule["DoneSeq"] == schedule["RequestSeq"]:
            return
        start = max(schedule["EndNs"] + 1_000_000_000, 1_000_000_000)
        schedule["StartNs"] = start
        schedule["EndNs"] = start + schedule["DurationMs"] * 1_000_000
        schedule["Submitted"] = schedule["Count"] if result == 1 else 0
        schedule["Result"] = result
        schedule["DoneSeq"] = schedule["RequestSeq"]


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "ThreeChannelCAN.cfg"
        self.config.touch()
        self.system = FakeSystem()
        self.app = SimpleNamespace(
            Configuration=SimpleNamespace(FullName=str(self.config)),
            Measurement=SimpleNamespace(Running=True),
            System=self.system,
        )

    def interface(self, **options):
        return CANoeScheduleInterface(self.app, 2, self.config, **options)

    def test_baseline_and_normal_phases_are_sequenced_with_padded_arrays(self):
        payload = bytes.fromhex("00000000200000F0")
        normal = [(index * 0.05, payload) for index in range(200)]
        with self.interface() as interface:
            baseline_result = interface.run_phase("baseline", 30.0, [])
            normal_result = interface.run_phase("normal", 10.0, normal)

        self.assertEqual((baseline_result["request_seq"], normal_result["request_seq"]), (1, 2))
        self.assertEqual((baseline_result["submitted_frames"], normal_result["submitted_frames"]), (0, 200))
        self.assertEqual((baseline_result["duration_seconds"], normal_result["duration_seconds"]), (30.0, 10.0))
        self.assertEqual(self.system.trigger_count, 2)
        data = [value for _, name, value in self.system.writes if name == "Data"][-1]
        offsets = [value for _, name, value in self.system.writes if name == "OffsetsMs"][-1]
        self.assertEqual(len(data), 1600)
        self.assertEqual(data[:8], tuple(payload))
        self.assertEqual(data[-8:], tuple(payload))
        self.assertEqual(len(offsets), 200)
        self.assertEqual((offsets[0], offsets[-1]), (0, 9950))

    def test_delayed_ack_and_stale_request_readback_do_not_duplicate(self):
        self.system.mode = "delayed"
        self.system.stale_request_reads = 1
        with self.interface(poll_seconds=0.01, lease_refresh_seconds=0.005) as interface:
            result = interface.run_phase("mutation", 1.0, [(0, b"\0" * 8)])

        self.assertEqual(result["submitted_frames"], 1)
        self.assertEqual(self.system.trigger_count, 1)
        self.assertGreaterEqual(
            len([name for _, name, _ in self.system.writes if name == "Lease"]), 2
        )

    def test_stale_completion_fields_settle_after_done_sequence(self):
        self.system.stale_completion_reads = {
            "Result": 1, "Submitted": 1, "StartNs": 1, "EndNs": 1,
        }
        with self.interface(poll_seconds=0.01) as interface:
            result = interface.run_phase("baseline", 1.0, [])
        self.assertEqual(result["result"], 1)
        self.assertEqual(self.system.trigger_count, 1)

    def test_stale_com_heartbeat_does_not_cancel_completed_schedule(self):
        with self.interface() as interface:
            self.system.freeze_heartbeat = True
            with patch("canoe_schedule.time.monotonic", return_value=time.monotonic() + 10):
                result = interface.run_phase("recovery", 1.0, [(0, b"\0" * 8)])
        self.assertEqual(result["result"], 1)
        self.assertEqual(result["submitted_frames"], 1)
        self.assertEqual(self.system.trigger_count, 1)
        self.assertFalse(any(name == "CancelSeq" and value == 1
                             for _, name, value in self.system.writes))

    def test_heartbeat_reset_still_blocks_request(self):
        with self.interface() as interface:
            self.system.values["FuzzDashboard"]["Heartbeat"] = 0
            with self.assertRaisesRegex(CANoeScheduleError, "heartbeat reset"):
                interface.run_phase("normal", 1.0, [])
        self.assertEqual(self.system.trigger_count, 0)

    def test_rejection_is_reported_without_retry(self):
        self.system.mode = "rejected"
        with self.interface() as interface:
            with self.assertRaisesRegex(CANoeScheduleError, "rejected"):
                interface.run_phase("normal", 1.0, [])
        self.assertEqual(self.system.trigger_count, 1)

    def test_newer_request_is_reported_as_conflict(self):
        self.system.mode = "conflict"
        with self.interface() as interface:
            with self.assertRaisesRegex(CANoeScheduleError, "another producer"):
                interface.run_phase("normal", 1.0, [])
        self.assertEqual(self.system.trigger_count, 1)

    def test_newer_request_after_ack_is_still_a_conflict(self):
        self.system.mode = "conflict_after_done"
        with self.interface() as interface:
            with self.assertRaisesRegex(CANoeScheduleError, "another producer"):
                interface.run_phase("normal", 1.0, [])
        self.assertEqual(self.system.trigger_count, 1)

    def test_timeout_cancels_ambiguous_request_without_retry(self):
        self.system.mode = "never"
        with self.interface(completion_timeout_seconds=0.04, poll_seconds=0.01) as interface:
            with self.assertRaisesRegex(CANoeScheduleError, "outcome is unknown"):
                interface.run_phase("normal", 1.0, [])
        self.assertEqual(self.system.values["FuzzSchedule"]["CancelSeq"], 1)
        self.assertEqual(self.system.trigger_count, 1)

    def test_invalid_schedule_does_not_trigger_capl(self):
        with self.interface() as interface:
            with self.assertRaisesRegex(ValueError, "at least 50 ms"):
                interface.run_phase("normal", 1.0, [(0, b"\0" * 8), (0.04, b"\0" * 8)])
        self.assertEqual(self.system.trigger_count, 0)


if __name__ == "__main__":
    unittest.main()
