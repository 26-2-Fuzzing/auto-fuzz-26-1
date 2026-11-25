"""Read-only CLI integration: a difference in two hand-picked windows is not attribution."""

import json
import subprocess
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory


CLI = Path(__file__).resolve().with_name("analyze_canoe.py")
DBC = '''VERSION ""
NS_ :
BS_:
BU_: Monitor
BO_ 100 Sensor: 1 Monitor
 SG_ State : 0|1@1+ (1,0) [0|1] "" Monitor
'''


def config():
    return {"timestamp_origin": 0, "channel_map": {"0": "TestChannel"},
            "windows": {"reference": [0, 4], "observation": [4, 8]}}


def record(start=4, end=8):
    return {"source": "operator_record", "description": "Observer log A, same time origin.",
            "observation_s": [start, end]}


def passive_asc(*, extra=True, tx=False):
    # Use the same decimal dialect as raw_M0028.asc. Directions below are labels
    # in a file; the test never creates a bus or sends a frame.
    header = ["date Wed Nov 25 14:40:57.441 2026", "base dec  timestamps absolute",
              "internal events logged", "// version 20.2.0",
              "Begin TriggerBlock Wed Nov 25 14:40:57.441 2026",
              "   0.000000 Start of measurement"]
    rows = []
    for i in range(32):
        rows.append((i / 4, 100, "Rx", int(i >= 20)))
    if extra:
        rows.extend((t, 101, "Rx", 0) for t in (4.1, 5.1, 6.1))
    if tx:
        rows.append((4.01, 102, "Tx", 1))
    return "\n".join(header + [f"{t:11.6f} 1  {ident}   {direction}   d 1 {data}"
                                 for t, ident, direction, data in sorted(rows)]
                     + ["End TriggerBlock", ""])


@unittest.skipUnless(find_spec("can"), "requires python-can")
class AssessmentWorkflowTests(unittest.TestCase):
    def analyze_cli(self, cfg, *, tx=False, control=None, dbc=False):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            def inputs(stem, asc, configuration):
                log, conf = root / f"{stem}.asc", root / f"{stem}.json"
                log.write_text(asc, encoding="ascii")
                conf.write_text(json.dumps(configuration), encoding="utf-8")
                return log, conf
            log, conf = inputs("measurement", passive_asc(tx=tx), cfg)
            out = root / "result.json"
            args = [sys.executable, "-B", str(CLI), str(log), str(conf), "--out", str(out)]
            if control is not None:
                ctrl, ctrl_conf = inputs("separate", passive_asc(extra=False), control)
                args.extend(["--control-log", str(ctrl), "--control-config", str(ctrl_conf)])
            if dbc:
                database = root / "test.dbc"
                database.write_text(DBC, encoding="ascii")
                args.extend(["--dbc", str(database)])
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(out.read_text(encoding="utf-8"))

    def test_unmarked_recording_keeps_candidates_without_attribution(self):
        out = self.analyze_cli(config())
        self.assertEqual([c["type"] for c in out["candidates"]], ["new_id"])
        self.assertEqual(out["assessment"]["statistical_candidate_count"], 1)
        self.assertEqual(out["assessment"]["status"], "descriptive_only")
        self.assertIn("판정 불가", out["assessment"]["headline_ko"])
        self.assertEqual(out["assessment"]["control"]["kind"], "not_provided")
        self.assertEqual(out["verdict"], "manual_review_only")

    def test_operator_record_only_supports_window_review(self):
        out = self.analyze_cli(dict(config(), window_evidence=record()))
        self.assertEqual(out["assessment"]["status"], "window_review_only")
        self.assertEqual(out["verdict"], "manual_review_only")

    def test_mismatching_operator_times_or_unusable_capture_are_descriptive(self):
        for cfg in (dict(config(), window_evidence=record(5, 9)),
                    dict(config(), window_evidence=record(), capture_loss_reported=True)):
            with self.subTest(config=cfg):
                out = self.analyze_cli(cfg)
                self.assertEqual(out["assessment"]["status"], "descriptive_only")

    def test_tx_or_control_designation_do_not_supply_trial_boundaries(self):
        for control, expected_kind in ((config(), "reference_recording"),
                                       (dict(config(), capture_role="no_op"), "declared_no_op")):
            with self.subTest(expected_kind=expected_kind):
                out = self.analyze_cli(config(), tx=True, control=control)
                self.assertEqual(out["quality"]["TestChannel"]["observation"]["tx_frames"], 1)
                self.assertEqual(out["assessment"]["status"], "descriptive_only")
                self.assertEqual(out["assessment"]["control"]["kind"], expected_kind)
                self.assertEqual(out["assessment"]["statistical_candidate_count"], 1)

    @unittest.skipUnless(find_spec("cantools"), "requires cantools")
    def test_dbc_transition_is_observed_but_is_not_a_statistical_flag(self):
        cfg = dict(config(), watch_signals=[{"bus": "TestChannel", "message": "Sensor",
                                             "signal": "State"}])
        out = self.analyze_cli(cfg, dbc=True)
        self.assertCountEqual([c["type"] for c in out["candidates"]],
                              ["new_id", "signal_transition"])
        self.assertEqual(out["assessment"]["statistical_candidate_count"], 1)
        self.assertEqual(out["assessment"]["status"], "descriptive_only")


if __name__ == "__main__":
    unittest.main()
