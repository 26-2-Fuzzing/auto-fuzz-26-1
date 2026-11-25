"""Offline tests for the CANoe capture lifecycle and file ownership."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from canoe_capture import CaptureError, capture_measurement


class FakeLogging:
    def __init__(self, name: str, path: Path):
        self.Name = name
        self._full_name = str(path)
        self.active_name = str(path)
        self.FileNameOptions = type("Options", (), {"IncrementAtMeasurementStart": False})()
        self.writes = 0

    @property
    def FullName(self):
        return self._full_name

    @FullName.setter
    def FullName(self, path):
        self.writes += 1
        self._full_name = str(path)


class FakeOnlineSetup:
    WorkingMode = 0
    OfflineMode = False

    def __init__(self, logs):
        self.LoggingCollection = logs
        self.activation_calls = 0

    def ActivateEndBlock(self, name, active):
        self.activation_calls += 1
        raise AssertionError("Capture must preserve end-block activation")


class FakeMeasurement:
    def __init__(self, config):
        self.config = config
        self.Running = False
        self.starts = 0
        self.stops = 0

    def Start(self):
        self.starts += 1
        self.Running = True
        for block in self.config.OnlineSetup.LoggingCollection:
            path = Path(block.FullName)
            if path.exists() and block.FileNameOptions.IncrementAtMeasurementStart:
                stem, number = path.stem.rsplit("M", 1)
                path = path.with_name(f"{stem}M{int(number) + 1:0{len(number)}d}{path.suffix}")
            block.active_name = str(path)

    def Stop(self):
        self.stops += 1
        self.Running = False
        for block in self.config.OnlineSetup.LoggingCollection:
            destination = Path(block.active_name)
            content = b"BLF payload" if destination.suffix == ".blf" else b"Begin TriggerBlock\nEnd TriggerBlock\n"
            destination.write_bytes(content)
            block._full_name = str(destination)


class FakeApplication:
    def __init__(self, root: Path):
        log_dir = root / "canoe_logs"
        log_dir.mkdir()
        self.Configuration = type("Config", (), {})()
        self.Configuration.FullName = str(root / "ThreeChannelCAN.cfg")
        self.Configuration.Modified = False
        self.Configuration.OnlineSetup = FakeOnlineSetup([
            FakeLogging("BLF", log_dir / "raw_M0001.blf"),
            FakeLogging("ASC", log_dir / "raw_M0001.asc"),
        ])
        self.Measurement = FakeMeasurement(self.Configuration)


class CaptureTests(unittest.TestCase):
    def test_copies_finalized_pair_without_modifying_configuration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            original = [block.FullName for block in app.Configuration.OnlineSetup.LoggingCollection]
            output = root / "baseline" / "raw"

            blf, asc = capture_measurement(
                output,
                0.01,
                expected_config=Path(app.Configuration.FullName),
                attach_factory=lambda: app,
            )

            self.assertEqual(blf.read_bytes(), b"BLF payload")
            self.assertIn(b"End TriggerBlock", asc.read_bytes())
            self.assertEqual([block.FullName for block in app.Configuration.OnlineSetup.LoggingCollection], original)
            self.assertTrue(all(block.writes == 0 for block in app.Configuration.OnlineSetup.LoggingCollection))
            self.assertFalse(app.Configuration.Modified)
            self.assertEqual(app.Configuration.OnlineSetup.activation_calls, 0)
            self.assertEqual((app.Measurement.starts, app.Measurement.stops), (1, 1))

    def test_callback_failure_still_stops_and_copies_logs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            output = root / "mutation" / "raw"

            def action(application):
                self.assertIs(application, app)
                self.assertTrue(application.Measurement.Running)
                raise ValueError("transmit failed")

            with self.assertRaisesRegex(ValueError, "transmit failed"):
                capture_measurement(output, 0, attach_factory=lambda: app, action=action)

            self.assertFalse(app.Measurement.Running)
            self.assertEqual((app.Measurement.starts, app.Measurement.stops), (1, 1))
            self.assertEqual(len(list(output.glob("*.blf"))), 1)
            self.assertEqual(len(list(output.glob("*.asc"))), 1)
            self.assertFalse(app.Configuration.Modified)

    def test_existing_measurement_is_left_running(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            app.Measurement.Running = True
            output = root / "baseline" / "raw"

            with self.assertRaisesRegex(CaptureError, "Stop the existing"):
                capture_measurement(output, 0.01, attach_factory=lambda: app)

            self.assertTrue(app.Measurement.Running)
            self.assertEqual((app.Measurement.starts, app.Measurement.stops), (0, 0))
            self.assertFalse(output.exists())

    def test_existing_incremented_pair_creates_next_pair_without_overwrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            blocks = app.Configuration.OnlineSetup.LoggingCollection
            original = {}
            for block in blocks:
                block.FileNameOptions.IncrementAtMeasurementStart = True
                path = Path(block.FullName)
                path.write_bytes(b"older log")
                original[path] = (path.read_bytes(), path.stat().st_mtime_ns)

            output = root / "baseline" / "raw"
            blf, asc = capture_measurement(output, 0.01, attach_factory=lambda: app)

            self.assertEqual(blf.name, "raw_M0002.blf")
            self.assertEqual(asc.name, "raw_M0002.asc")
            self.assertEqual(blf.read_bytes(), b"BLF payload")
            self.assertEqual((app.Measurement.starts, app.Measurement.stops), (1, 1))
            for path, (content, mtime_ns) in original.items():
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(path.stat().st_mtime_ns, mtime_ns)

    def test_two_consecutive_captures_increment_both_loggers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            blocks = app.Configuration.OnlineSetup.LoggingCollection
            for block in blocks:
                block.FileNameOptions.IncrementAtMeasurementStart = True
                Path(block.FullName).write_bytes(b"older log")

            first = capture_measurement(root / "baseline" / "raw", 0.01, attach_factory=lambda: app)
            second = capture_measurement(root / "mutation" / "raw", 0.01, attach_factory=lambda: app)

            self.assertEqual([path.name for path in first], ["raw_M0002.blf", "raw_M0002.asc"])
            self.assertEqual([path.name for path in second], ["raw_M0003.blf", "raw_M0003.asc"])
            self.assertEqual([Path(block.FullName).name for block in blocks], ["raw_M0003.blf", "raw_M0003.asc"])
            self.assertTrue(all(block.writes == 0 for block in blocks))
            self.assertEqual((app.Measurement.starts, app.Measurement.stops), (2, 2))

    def test_existing_pair_without_increment_is_rejected_before_start(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            for block in app.Configuration.OnlineSetup.LoggingCollection:
                Path(block.FullName).write_bytes(b"older log")

            output = root / "baseline" / "raw"
            with self.assertRaisesRegex(CaptureError, "would overwrite"):
                capture_measurement(output, 0.01, attach_factory=lambda: app)

            self.assertEqual(app.Measurement.starts, 0)
            self.assertFalse(output.exists())

    def test_next_incremented_pair_is_rejected_before_start(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = FakeApplication(root)
            for block in app.Configuration.OnlineSetup.LoggingCollection:
                block.FileNameOptions.IncrementAtMeasurementStart = True
                path = Path(block.FullName)
                path.write_bytes(b"older log")
                path.with_name(path.name.replace("M0001", "M0002")).write_bytes(b"occupied")

            output = root / "baseline" / "raw"
            with self.assertRaisesRegex(CaptureError, "could overwrite the next log"):
                capture_measurement(output, 0.01, attach_factory=lambda: app)

            self.assertEqual(app.Measurement.starts, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
