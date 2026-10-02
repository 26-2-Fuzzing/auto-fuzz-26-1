"""Tests for stopping a remote sender when local supervision fails."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from remote_watchdog import run_supervised_sender


class WatchdogManager:
    def __init__(self, *, alive: bool = True):
        self.alive = alive
        self.started = []
        self.stopped = []

    def start_process(self, command, stdout_path):
        self.started.append((list(command), stdout_path))
        return SimpleNamespace(pid=42)

    def process_alive(self, process):
        return self.alive and process.pid not in self.stopped

    def stop_process(self, process):
        self.stopped.append(process.pid)


class RemoteWatchdogTests(unittest.TestCase):
    def test_remote_and_local_deadlines_are_both_active(self) -> None:
        manager = WatchdogManager()
        with self.assertRaisesRegex(TimeoutError, "monitored deadline"):
            run_supervised_sender(
                manager, ["python3", "can_sender.py"], "/tmp/sender.log",
                timeout=0.01, poll_seconds=0.002,
            )
        self.assertEqual(manager.stopped, [42])
        self.assertEqual(manager.started[0][0][:4], [
            "timeout", "--signal=TERM", "--kill-after=5s", "0.01s",
        ])

    def test_capture_health_failure_stops_sender(self) -> None:
        manager = WatchdogManager()

        def failed_capture():
            raise RuntimeError("receiver exited")

        with self.assertRaisesRegex(RuntimeError, "receiver exited"):
            run_supervised_sender(
                manager, ["python3", "can_sender.py"], "/tmp/sender.log",
                timeout=1.0, health_check=failed_capture,
            )
        self.assertEqual(manager.stopped, [42])

    def test_normal_exit_leaves_remote_sender_untouched(self) -> None:
        manager = WatchdogManager(alive=False)
        run_supervised_sender(
            manager, ["python3", "can_sender.py"], "/tmp/sender.log",
            timeout=1.0,
        )
        self.assertEqual(manager.stopped, [])


if __name__ == "__main__":
    unittest.main()
