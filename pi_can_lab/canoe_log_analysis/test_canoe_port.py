"""Regression checks for the descriptive analysis port from Pi recordings."""

import unittest

from analyze_canoe import Frame, analyze


def event(t, frame_id, data=b"\x00"):
    return Frame(t, 0, frame_id, data)


def burst(start, end, hz, frame_id):
    return [event(start + i / hz, frame_id) for i in range((end - start) * hz)]


def report(frames, **windows):
    return analyze(sorted(frames, key=lambda f: f.timestamp),
                   {"timestamp_origin": 0, "channel_map": {"0": "B_CAN"}, "windows": windows})


class PortTests(unittest.TestCase):
    def test_earlier_presence_is_not_a_new_id_candidate(self):
        f = burst(0, 12, 10, 1) + [event(i, 2) for i in (1, 8, 9, 10)]
        out = report(f, baseline=[0, 4], reference=[4, 8], observation=[8, 12])
        self.assertFalse(any(x["id"] == "0x2" for x in out["candidates"]))
        evidence = next(x for x in out["observations"] if x["id"] == "0x2")
        self.assertEqual(evidence["classification"], "background")
        self.assertEqual(evidence["seen_in_windows"], ["baseline"])

    def test_rate_drop_with_already_transitioning_reference_is_inconclusive(self):
        f = burst(0, 12, 10, 1) + burst(0, 4, 5, 2) + burst(4, 8, 10, 2) + burst(8, 12, 3, 2)
        out = report(f, previous_reference=[0, 4], reference=[4, 8], observation=[8, 12])
        self.assertFalse(any(x["id"] == "0x2" for x in out["candidates"]))
        self.assertIn("reference_transitioning", {x["reason"] for x in out["observations"] if x["id"] == "0x2"})

    def test_stable_reference_allows_rate_and_cycle_candidates(self):
        f = burst(0, 12, 10, 1) + burst(0, 8, 10, 2) + burst(8, 12, 3, 2)
        out = report(f, previous_reference=[0, 4], reference=[4, 8], observation=[8, 12])
        kinds = {x["type"] for x in out["candidates"] if x["id"] == "0x2"}
        self.assertEqual(kinds, {"rate_change", "timing_shift"})

    def test_unequal_previous_window_does_not_imply_stability(self):
        f = burst(0, 12, 10, 1) + burst(0, 8, 10, 2) + burst(8, 12, 3, 2)
        out = report(f, previous_reference=[1, 4], reference=[4, 8], observation=[8, 12])
        self.assertIn("reference_unmeasured", {x["reason"] for x in out["observations"] if x["id"] == "0x2"})

    def test_unequal_main_window_lengths_compare_rates_not_raw_counts(self):
        f = burst(0, 12, 10, 1)
        out = report(f, reference=[0, 4], observation=[4, 12])
        self.assertEqual(out["candidates"], [])
        phases = out["per_id"][0]["windows"]
        self.assertEqual((phases["reference"]["message_count"], phases["observation"]["message_count"]), (40, 80))
        self.assertEqual(phases["reference"]["frequency_hz"], phases["observation"]["frequency_hz"])

    def test_recovery_matching_is_based_on_samples_not_only_last_value(self):
        f = [event(i / 10, 1, bytes([int(50 <= i < 90)])) for i in range(120)]
        out = analyze(f, {"timestamp_origin": 0, "channel_map": {"0": "B_CAN"},
                          "windows": {"reference": [0, 4], "observation": [4, 8], "recovery": [8, 12]}},
                      lambda bus, frame: {"status": frame.data[0]})
        recovery = out["signals"][0]["recovery_vs_reference"]
        self.assertEqual(recovery["status"], "mixed")
        self.assertEqual((recovery["matching_samples"], recovery["recovery_samples"]), (30, 40))


if __name__ == "__main__":
    unittest.main()
