"""Regression tests for passive CAN capture assessment using synthetic data."""

import unittest

from analyze_canoe import Frame, analyze


HEARTBEAT = 0x100
MONITORED_ID = 0x101
STATE_ID = 0x102


def make_frame(at, can_id=HEARTBEAT, value=0, *, channel=0, rx=True):
    return Frame(float(at), channel, can_id, bytes([value]), is_rx=rx)


def heartbeat(start=0, end=12):
    return [make_frame(t / 8) for t in range(int(start * 8), int(end * 8))]


def decode_state(bus, frame):
    return {"status": frame.data[0]} if frame.arbitration_id == STATE_ID else {}


class OfflineAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "timestamp_origin": 0.0,
            "channel_map": {"0": "B_CAN"},
            "capture_loss_reported": False,
            "windows": {
                "reference": [0.0, 4.0],
                "observation": [4.0, 8.0],
                "recovery": [8.0, 12.0],
            },
        }

    def run_analysis(self, frames, decoder=None):
        return analyze(iter(sorted(frames, key=lambda frame: frame.timestamp)),
                       self.config, decoder=decoder)

    def test_low_rate_id_not_declared_missing(self):
        frames = heartbeat() + [make_frame(at, MONITORED_ID) for at in (0.5, 2.5)]
        report = self.run_analysis(frames)
        self.assertEqual(report["quality"]["B_CAN"]["observation"]["status"], "usable")
        self.assertFalse(any(x["type"] == "missing_id" for x in report["candidates"]))

    def test_frequent_id_missing_while_bus_capture_continues(self):
        frames = heartbeat() + [make_frame(t / 4, MONITORED_ID) for t in range(16)]
        report = self.run_analysis(frames)
        self.assertEqual(report["quality"]["B_CAN"]["observation"]["status"], "usable")
        self.assertTrue(any(x["type"] == "missing_id" for x in report["candidates"]))

    def test_missing_bus_marked_inconclusive(self):
        self.config["channel_map"]["1"] = "P_CAN"
        report = self.run_analysis(heartbeat())
        self.assertEqual(report["quality"]["B_CAN"]["observation"]["status"], "usable")
        self.assertEqual(report["quality"]["P_CAN"]["observation"]["status"], "inconclusive")

    def test_missing_window_is_not_message_loss(self):
        report = self.run_analysis(heartbeat(end=4) + heartbeat(start=8))
        self.assertEqual(report["quality"]["B_CAN"]["observation"]["status"], "inconclusive")
        self.assertFalse(any(x["type"] == "missing_id" for x in report["candidates"]))

    def test_tx_echo_does_not_create_an_rx_candidate(self):
        frames = heartbeat() + [make_frame(t / 4, MONITORED_ID, rx=False)
                                for t in range(16, 32)]
        report = self.run_analysis(frames)
        self.assertFalse(any(x["type"] == "new_id" for x in report["candidates"]))

    def test_transition_across_observation_boundary_is_ambiguous(self):
        frames = heartbeat() + [make_frame(t / 2, STATE_ID, int(t >= 8))
                                for t in range(24)]
        report = self.run_analysis(frames, decoder=decode_state)
        self.assertFalse(any(x["type"] == "signal_transition" for x in report["candidates"]))

    def test_transition_with_both_samples_in_observation_is_candidate(self):
        frames = heartbeat() + [make_frame(t / 2, STATE_ID, int(t >= 10))
                                for t in range(24)]
        report = self.run_analysis(frames, decoder=decode_state)
        self.assertTrue(any(x["type"] == "signal_transition" for x in report["candidates"]))


if __name__ == "__main__":
    unittest.main()
