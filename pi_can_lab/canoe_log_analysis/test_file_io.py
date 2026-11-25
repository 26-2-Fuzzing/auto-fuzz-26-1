"""Round-trip synthetic files and a synthetic DBC; no CAN device is opened."""

from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyze_canoe import Frame, analyze, annotate_control, dbc_decoder, read_log


DBC = """
VERSION ""
NS_ :
BS_:
BU_: Sensor Monitor

BO_ 272 Status: 1 Sensor
 SG_ FaultFlag : 0|1@1+ (1,0) [0|1] "" Monitor
"""


def settings():
    return dict(timestamp_origin=0, channel_map={"0": "CAN_A"},
                windows={"reference": [0, 4], "observation": [4, 8], "recovery": [8, 12]})


def frames(transition_at):
    # Fixed synthetic status field, with the already-captured transition at
    # an arbitrary time; these records are used only by the offline readers.
    return [Frame(i / 8, 0, 272, bytes([int(i / 8 >= transition_at)]))
            for i in range(96)]


def decode_fixture(bus, frame):
    return {"Status.FaultFlag": frame.data[0]}


class ControlTests(unittest.TestCase):
    def test_observed_transition_also_present_in_control(self):
        report = analyze(frames(5), settings(), decode_fixture)
        control = analyze(frames(6), settings(), decode_fixture)
        annotate_control(report, control)
        self.assertEqual(report["candidates"][0]["control"]["status"], "also_flagged_in_control")

    def test_control_with_different_initial_state_cannot_rule_out(self):
        report = analyze(frames(5), settings(), decode_fixture)
        control = analyze(frames(0), settings(), decode_fixture)
        annotate_control(report, control)
        self.assertEqual(report["candidates"][0]["control"]["status"], "control_not_comparable")

    def test_reported_capture_loss_blocks_candidate(self):
        config = settings()
        config["capture_loss_reported"] = True
        report = analyze(frames(5), config, decode_fixture)
        self.assertEqual(report["quality"]["CAN_A"]["observation"]["status"], "inconclusive")
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["signals"][0]["transition_count"], 1)


@unittest.skipUnless(find_spec("can") and find_spec("cantools"), "requires python-can and cantools")
class ReaderTests(unittest.TestCase):
    def test_blf_and_asc_read_same_synthetic_dbc_transition(self):
        import can

        with TemporaryDirectory() as temp:
            root = Path(temp)
            dbc = root / "status.dbc"
            dbc.write_text(DBC, encoding="ascii")
            decoder = dbc_decoder(dbc, [{"bus": "CAN_A", "message": "Status", "signal": "FaultFlag"}],
                                  {"CAN_A"})
            for ext in ("blf", "asc"):
                with self.subTest(format=ext):
                    path = root / f"capture.{ext}"
                    with can.Logger(path) as writer:
                        for frame in frames(5):
                            writer.on_message_received(can.Message(
                                timestamp=1700000000 + frame.timestamp, channel=frame.channel,
                                arbitration_id=frame.arbitration_id, is_extended_id=False,
                                data=frame.data, is_rx=frame.is_rx,
                            ))
                    config = settings()
                    if ext == "blf":
                        config["timestamp_origin"] = 1700000000
                    report = analyze(read_log(path), config, decoder)
                    self.assertEqual(report["quality"]["CAN_A"]["observation"]["rx"]["count"], 32)
                    self.assertEqual(report["candidates"][0]["signal"], "Status.FaultFlag")
                    self.assertEqual(report["candidates"][0]["type"], "signal_transition")
                    self.assertAlmostEqual(report["candidates"][0]["after_s"], 5)


if __name__ == "__main__":
    unittest.main()
