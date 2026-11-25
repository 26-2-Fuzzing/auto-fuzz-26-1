"""Offline tests for recorded window bounds (not a source of vehicle ground truth)."""

import tempfile
import unittest
from pathlib import Path

from canoe_phase_evidence import inspect_phase_metadata


HEAD = ("base dec  timestamps absolute\nBegin TriggerBlock Wed Nov 25 14:00:00.000 2026\n"
        "   0.000000 Start of measurement\n")
TAIL = "End TriggerBlock\n"


def marker(when, name, value):
    return f"{when:11.6f}    SV: 6 0 1 ::FuzzSchedule::{name} = {value}\n"


def inspect(content):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.ASC"
        path.write_bytes(content.replace("\n", "\r\n").encode("ascii"))
        return inspect_phase_metadata(path)


class PhaseEvidenceTests(unittest.TestCase):
    def test_pair_in_single_absolute_clock(self):
        text = (HEAD + marker(0, "StartNs", "L0") + marker(0, "EndNs", "L0")
                + marker(4, "StartNs", "L4000000000") + marker(8, "EndNs", "L8000000333")
                + marker(9, "StartNs", "L9000000000") + marker(11, "EndNs", "L11000000000") + TAIL)
        got = inspect(text)
        self.assertEqual(got["status"], "recorded_intervals")
        self.assertEqual(got["intervals_s"], [[4, 8.000000333], [9, 11]])
        self.assertEqual(got["marker_counts"]["start_positive"], 2)

    def test_initial_values_and_ready_only(self):
        text = (HEAD + marker(0, "StartNs", "L0") + marker(0, "EndNs", "L0")
                + '   0.000000    SV: 3 0 1 ::FuzzDashboard::PhaseText = "READY / awaiting experiment"\n'
                + '   0.100000    SV: 3 0 1 ::FuzzDashboard::SourceText = "No active injection"\n' + TAIL)
        got = inspect(text)
        self.assertEqual(got["status"], "no_recorded_interval")
        self.assertEqual(got["intervals_s"], [])
        for key in ("ready_messages", "no_active_messages", "start_zero", "end_zero"):
            self.assertEqual(got["marker_counts"][key], 1)

    def test_untrusted_or_incomplete_boundaries(self):
        a, b = marker(4, "StartNs", "L4000000000"), marker(8, "EndNs", "L8000000000")
        for text in (HEAD+a+TAIL, HEAD+b+TAIL, HEAD+a+a+b+TAIL, HEAD+a+b, HEAD+a+b+TAIL+HEAD+TAIL,
                     HEAD+a+marker(5, "EndNs", "L0")+b+TAIL, HEAD+a+marker(8, "EndNs", "L9000000000")+TAIL,
                     HEAD.replace("   0.000000 Start", "   3.000000 Start")+a+b+TAIL,
                     HEAD+marker(4, "StartNs", "pending")+b+TAIL,
                     HEAD+a+b+marker(8.0001, "StartNs", "L7999999999")+marker(9, "EndNs", "L9000000000")+TAIL):
            with self.subTest(text=text):
                got = inspect(text)
                self.assertEqual(got["intervals_s"], [])
                self.assertEqual(got["status"], "ambiguous")

    def test_other_formats_not_asserted_marker_free(self):
        self.assertEqual(inspect(HEAD.replace("absolute", "relative")+TAIL)["status"], "unsupported")
        self.assertEqual(inspect_phase_metadata("example.blf")["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
