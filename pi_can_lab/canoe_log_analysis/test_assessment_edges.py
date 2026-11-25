"""Timing provenance and missing DBC evidence in completed recordings."""
import unittest

from analyze_canoe import Frame, analyze, annotate_control
from canoe_assessment import assess_window


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(timestamp_origin=0, channel_map={"0": "test"},
                        windows={"reference": [0, 4], "observation": [4, 8]})

    def timing(self, pairs, origin=0):
        return assess_window(self.cfg, [4, 8], origin,
                             {"status": "recorded_intervals", "intervals_s": pairs})

    def test_marker_pair_and_nonzero_origin_align(self):
        self.assertTrue(self.timing([[24, 28]], origin=20)["supported"])
        self.assertFalse(self.timing([[24, 28]])["supported"])

    def test_reference_intersecting_an_earlier_marked_interval_is_not_clean(self):
        result = self.timing([[1, 3], [4, 8]])
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "reference_overlaps_a_recorded_interval")

    def test_operator_cannot_override_contradictory_file_markers(self):
        self.cfg["window_evidence"] = dict(source="operator_record", description="worksheet",
                                           observation_s=[4, 8])
        self.assertFalse(self.timing([[12, 16]])["supported"])
        bad = {"status": "ambiguous", "intervals_s": []}
        self.assertFalse(assess_window(self.cfg, [4, 8], 0, bad)["supported"])

    def watched(self, times):
        self.cfg["watch_signals"] = [dict(bus="test", message="Status", signal="Flag")]
        frames = [Frame(i/4, 0, 1, b"") for i in range(32)]
        frames += [Frame(t, 0, 2, bytes([v])) for t,v in times]
        return analyze(sorted(frames, key=lambda f:f.timestamp), self.cfg,
                       lambda _, frame: {"Status.Flag": frame.data[0]} if frame.arbitration_id==2 else {})

    def test_no_samples_vs_no_observable_pair_vs_observed_unchanged(self):
        for times, status, count in (
                ([], "no_samples", None),
                ([(1, 0), (5, 0)], "insufficient_samples_or_quality", None),
                ([(1, 0), (4.1, 0), (7.8, 0)], "insufficient_samples_or_quality", None),
                ([(1, 0), (5, 0), (5.2, 0)], "observed", 0)):
            with self.subTest(times=times):
                dbc = self.watched(times)["assessment"]["dbc"]
                self.assertEqual((dbc["status"], dbc["transition_candidate_count"]), (status, count))

    def test_sparse_noop_does_not_establish_absence_of_transition(self):
        record = self.watched([(1, 0), (5, 0), (5.2, 1)])
        self.cfg["capture_role"] = "no_op"
        control = self.watched([(1, 0), (5, 0)])
        annotate_control(record, control)
        item = next(c for c in record["candidates"] if c["type"] == "signal_transition")
        self.assertEqual(item["control"]["reason"], "insufficient_control_signal_samples")
        self.assertEqual(item["control"]["scope"], "descriptive_windows_only")
        self.assertFalse(record["assessment"]["control"]["both_windows_supported"])

    def test_no_control_period_measurements_is_incomparable_not_a_negative(self):
        background = [Frame(i/4, 0, 1, b"") for i in range(32)]
        ref = [Frame(i/2, 0, 2, b"") for i in range(8)]
        obs = [Frame(4+i/4, 0, 2, b"") for i in range(16)]
        run = lambda rows: analyze(sorted(background+rows, key=lambda f:f.timestamp), self.cfg)
        report = run(ref + obs)
        annotate_control(report, run(ref))
        change = next(c for c in report["candidates"] if c["type"] == "timing_shift")
        self.assertEqual(change["control"]["status"], "control_not_comparable")
        self.assertEqual(change["control"]["reason"], "insufficient_control_intervals")
