"""Regression tests for passive measurement summaries."""

import math
import unittest

from canoe_metrics import Activity, MAX_EXACT_INTERVALS, MAX_EXACT_PAYLOADS


class ActivityTests(unittest.TestCase):
    def test_legacy_cycle_stats_and_existing_aliases(self):
        activity = Activity(track_frame_details=True)
        for time, payload in zip((0.0, 0.1, 0.3, 0.6), (b"A", b"A", b"B", b"A")):
            activity.add(time, payload)
        result = activity.report(2.0)
        self.assertEqual(result["message_count"], result["count"])
        self.assertEqual(result["frequency_hz"], result["hz"])
        self.assertEqual(result["frequency_hz"], 2.0)
        self.assertEqual(result["interval_count"], 3)
        self.assertAlmostEqual(result["mean_cycle_time_ms"], 200.0)
        self.assertAlmostEqual(result["median_cycle_time_ms"], 200.0)
        self.assertAlmostEqual(result["minimum_cycle_time_ms"], 100.0)
        self.assertAlmostEqual(result["maximum_cycle_time_ms"], 300.0)
        self.assertAlmostEqual(result["cycle_time_stddev_ms"], math.sqrt(20_000 / 3))
        self.assertAlmostEqual(result["jitter_ms"], 100.0)
        self.assertEqual(result["payload_unique_count"], 2)
        self.assertEqual(result["payload_change_count"], 2)
        self.assertNotIn("payloads", result)

    def test_empty_and_one_interval_keep_distinct_nulls(self):
        activity = Activity(track_frame_details=True)
        empty = activity.report(1.0)
        self.assertIsNone(empty["minimum_cycle_time_ms"])
        self.assertIsNone(empty["cycle_time_stddev_ms"])
        self.assertEqual(empty["payload_unique_count"], 0)
        activity.add(0.0, b"")
        activity.add(0.25, b"")
        result = activity.report(1.0)
        self.assertEqual(result["median_cycle_time_ms"], 250)
        self.assertEqual(result["cycle_time_stddev_ms"], 0)
        self.assertIsNone(result["jitter_ms"])
        self.assertEqual(result["payload_unique_count"], 1)

    def test_explicit_bounds_do_not_report_approximate_as_exact(self):
        activity = Activity(track_frame_details=True)
        n = max(MAX_EXACT_INTERVALS, MAX_EXACT_PAYLOADS) + 2
        for tick in range(n):
            activity.add(tick / 4, tick.to_bytes(4, "big"))
        result = activity.report(n / 4)
        self.assertEqual(result["message_count"], n)
        self.assertEqual(result["frequency_hz"], 4)
        self.assertIsNone(result["median_cycle_time_ms"])
        self.assertTrue(result["median_cycle_time_truncated"])
        self.assertIsNone(result["payload_unique_count"])
        self.assertEqual(result["payload_unique_count_at_least"], MAX_EXACT_PAYLOADS + 1)
        self.assertTrue(result["payload_unique_truncated"])
        self.assertEqual(result["payload_change_count"], n - 1)
        self.assertAlmostEqual(result["mean_cycle_time_ms"], 250)

    def test_default_coverage_does_not_track_frame_details(self):
        activity = Activity()
        activity.add(0, b"A")
        activity.add(0.5, b"B")
        result = activity.report(1)
        self.assertEqual(result["frequency_hz"], 2)
        self.assertAlmostEqual(result["mean_cycle_time_ms"], 500)
        self.assertIsNone(result["median_cycle_time_ms"])
        self.assertIsNone(result["payload_unique_count"])
        self.assertIsNone(result["payload_change_count"])

    def test_incomplete_payload_stream_is_not_claimed_as_complete(self):
        activity = Activity(track_frame_details=True)
        activity.add(0, b"A")
        activity.add(0.5)
        activity.add(1, b"B")
        result = activity.report(2)
        self.assertEqual(result["payload_sample_count"], 2)
        self.assertEqual(result["payload_unique_count_at_least"], 2)
        self.assertIsNone(result["payload_unique_count"])
        self.assertIsNone(result["payload_change_count"])


if __name__ == "__main__":
    unittest.main()
