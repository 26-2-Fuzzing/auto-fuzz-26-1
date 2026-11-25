from __future__ import annotations

import unittest
from types import SimpleNamespace

from canoe_fuzzer import (
    DEFAULT_CONFIG,
    FuzzerSettings,
    build_mutation_cases,
    build_parser,
    case_schedule,
    load_settings,
    transmit_cases,
)
from canoe_interface import (
    CANoeComInterface,
    CANoeFrame,
    CANoeFrameError,
    CANoeNotReadyError,
    CANoeSendError,
)
from can_common import ConfigurationError
from paired_cycle import CYCLE_FAMILIES
from trial_models import MutationCase


class FakeFunction:
    def __init__(self, result=None, *, parameter_count=7, call_error=None):
        self.result = result
        self.ParameterCount = parameter_count
        self.call_error = call_error
        self.calls = []

    def Call(self, *args):
        self.calls.append(args)
        if self.call_error is not None:
            raise self.call_error
        return self.result


class FakeCAPL:
    def __init__(self, function, event_log, *, compile_error=None, get_error=None):
        self.function = function
        self.event_log = event_log
        self.compile_error = compile_error
        self.get_error = get_error
        self.requested = []
        self.compiled = 0
        self.CompileResult = SimpleNamespace(
            Result=0,
            ErrorMessage="",
            NodeName="",
            SourceFile="",
        )

    def Compile(self, fullname):
        if fullname is not None:
            raise AssertionError("CAPL.Compile expects a null optional filename")
        self.compiled += 1
        self.event_log.append("compile")
        if self.compile_error is not None:
            raise self.compile_error

    def GetFunction(self, name):
        self.event_log.append("get_function")
        self.requested.append(name)
        if self.get_error is not None:
            raise self.get_error
        return self.function


class FakeMeasurement:
    def __init__(self, event_log, running=False):
        self.event_log = event_log
        self.Running = running
        self.on_init = None
        self.started = 0
        self.stopped = 0

    def Start(self):
        self.started += 1
        self.event_log.append("start")
        if self.on_init is not None:
            self.event_log.append("on_init")
            self.on_init()
        self.Running = True

    def Stop(self):
        self.stopped += 1
        self.event_log.append("stop")
        self.Running = False


class FakeApplication:
    def __init__(
        self, function, running=False, *, compile_error=None, get_error=None
    ):
        self.event_log = []
        self.Measurement = FakeMeasurement(self.event_log, running)
        self.CAPL = FakeCAPL(
            function,
            self.event_log,
            compile_error=compile_error,
            get_error=get_error,
        )
        self.Visible = False


def bind_measurement_events(measurement, callback):
    measurement.on_init = callback
    return object()


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class CANoeInterfaceTests(unittest.TestCase):
    @staticmethod
    def interface(app, **options):
        return CANoeComInterface(
            channel=options.pop("channel", 1),
            dispatch_factory=lambda *_: app,
            event_binder=bind_measurement_events,
            **options,
        )

    def test_connect_starts_measurement_and_packs_payload_words(self):
        function = FakeFunction()
        app = FakeApplication(function)
        interface = self.interface(app, channel=2)

        self.assertEqual(app.CAPL.requested, [])
        interface.connect()
        interface.send_frame(CANoeFrame(
            channel=2,
            arbitration_id=0x366,
            data=bytes.fromhex("0102030405060708"),
        ))

        self.assertEqual(app.Measurement.started, 1)
        self.assertEqual(
            app.event_log, ["compile", "start", "on_init", "get_function"]
        )
        self.assertEqual(app.CAPL.requested, ["AutoFuzzSend"])
        self.assertEqual(
            function.calls,
            [(2, 0x366, 8, 0x0201, 0x0403, 0x0605, 0x0807)],
        )
        interface.close()
        self.assertEqual(app.Measurement.stopped, 1)

    def test_running_measurement_is_rejected_before_get_function(self):
        app = FakeApplication(FakeFunction(), running=True)
        interface = self.interface(app)

        with self.assertRaisesRegex(CANoeNotReadyError, "already running"):
            interface.connect()

        self.assertEqual(app.Measurement.stopped, 0)
        self.assertEqual(app.CAPL.requested, [])
        self.assertEqual(app.CAPL.compiled, 0)

    def test_explicit_restart_stops_then_starts_and_binds(self):
        app = FakeApplication(FakeFunction(), running=True)
        interface = self.interface(app, restart_running_measurement=True)

        interface.connect()

        self.assertEqual(app.Measurement.stopped, 1)
        self.assertEqual(app.Measurement.started, 1)
        self.assertEqual(app.CAPL.requested, ["AutoFuzzSend"])
        interface.close()

    def test_bad_capl_signature_stops_failed_measurement_start(self):
        app = FakeApplication(FakeFunction(parameter_count=6))
        interface = self.interface(app)

        with self.assertRaisesRegex(CANoeNotReadyError, "must have 7 parameters"):
            interface.connect()

        self.assertFalse(interface.connected)
        self.assertEqual(app.Measurement.started, 1)
        self.assertEqual(app.Measurement.stopped, 1)

    def test_compile_failure_prevents_measurement_start(self):
        app = FakeApplication(FakeFunction(), compile_error=RuntimeError("syntax"))
        interface = self.interface(app)

        with self.assertRaisesRegex(CANoeNotReadyError, "compilation failed"):
            interface.connect()

        self.assertEqual(app.Measurement.started, 0)

    def test_reported_compile_result_failure_prevents_measurement_start(self):
        app = FakeApplication(FakeFunction())
        app.CAPL.CompileResult = SimpleNamespace(
            Result=1,
            ErrorMessage="syntax error",
            NodeName="AutoFuzzTx",
            SourceFile="AutoFuzzTx.can",
        )
        interface = self.interface(app)

        with self.assertRaisesRegex(
            CANoeNotReadyError, "AutoFuzzTx.*syntax error"
        ):
            interface.connect()

        self.assertEqual(app.Measurement.started, 0)

    def test_get_function_error_stops_failed_measurement_start(self):
        app = FakeApplication(
            FakeFunction(), get_error=RuntimeError("function missing")
        )
        interface = self.interface(app)

        with self.assertRaisesRegex(CANoeNotReadyError, "function missing"):
            interface.connect()

        self.assertEqual(app.Measurement.started, 1)
        self.assertEqual(app.Measurement.stopped, 1)

    def test_rejects_non_classic_or_extended_frames(self):
        function = FakeFunction()
        app = FakeApplication(function)
        interface = self.interface(app)
        interface.connect()
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(1, 0x366, b"\x00" * 9))
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(1, 0x366, b"", is_extended_id=True))
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(2, 0x366, b"\x00" * 8))
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(1.5, 0x366, b""))  # type: ignore[arg-type]
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(1, 1.5, b""))  # type: ignore[arg-type]
        with self.assertRaises(CANoeFrameError):
            interface.send_frame(CANoeFrame(1, 0x366, 5))  # type: ignore[arg-type]
        interface.close()

    def test_capl_call_exception_is_a_send_error(self):
        function = FakeFunction(call_error=RuntimeError("COM failure"))
        app = FakeApplication(function)
        interface = self.interface(app)
        interface.connect()
        with self.assertRaisesRegex(CANoeSendError, "COM failure"):
            interface.send_frame(CANoeFrame(1, 0x366, b"\x00" * 8))
        interface.close()


def settings(**overrides):
    values = {
        "dbc_path": DEFAULT_CONFIG.parent.parent / "A5.dbc",
        "source_bus": "b_can",
        "frame_id": 0x366,
        "baseline_payload": bytes.fromhex("00000000200000F0"),
        "method": "signal_single",
        "max_cases": 10,
        "random_seed": 366,
        "undefined_max_bits": 2,
        "mutation_duration_seconds": 0.1,
        "interval_ms": 50.0,
        "restore_original": True,
        "restore_count": 1,
        "case_gap_ms": 100.0,
        "canoe_options": {"channel": 1},
    }
    values.update(overrides)
    return FuzzerSettings(**values)


def mutation(*, parameters=None):
    return MutationCase(
        mutation_id=1,
        source_bus="b_can",
        can_id=0x366,
        operator="SIGNAL_SINGLE",
        original_payload=bytes.fromhex("00000000200000F0"),
        mutated_payload=bytes.fromhex("00000000200100F0"),
        random_seed=366,
        parameters={"case": "TEST", **(parameters or {})},
    )


class CANoeFuzzerTests(unittest.TestCase):
    def test_short_options_override_yaml_defaults(self):
        args = build_parser().parse_args([
            "-c", str(DEFAULT_CONFIG), "-m", "undefined_enum", "-n", "3", "-x"
        ])
        loaded = load_settings(args)
        self.assertEqual(loaded.method, "undefined_enum")
        self.assertEqual(loaded.max_cases, 3)
        self.assertTrue(args.execute)

    def test_yaml_defaults_use_on_init_safe_measurement_lifecycle(self):
        loaded = load_settings(build_parser().parse_args([
            "-c", str(DEFAULT_CONFIG)
        ]))
        options = loaded.canoe_options
        self.assertTrue(options["compile_capl"])
        self.assertTrue(options["start_measurement"])
        self.assertFalse(options["restart_running_measurement"])
        self.assertTrue(options["stop_measurement_on_close"])
        self.assertNotIn("require_ack", options)

    def test_existing_catalogue_is_filtered_by_method_and_case_limit(self):
        loaded = load_settings(build_parser().parse_args([
            "-c", str(DEFAULT_CONFIG), "-m", "signal_single", "-n", "3"
        ]))
        cases = build_mutation_cases(loaded)
        self.assertEqual(len(cases), 3)
        self.assertTrue(all(
            item.parameters["mutation_family"] == "signal_single"
            for item in cases
        ))

    def test_each_catalogue_family_has_expected_baseline_availability(self):
        unavailable = set()
        for family in CYCLE_FAMILIES:
            loaded = load_settings(build_parser().parse_args([
                "-c", str(DEFAULT_CONFIG), "-m", family, "-n", "1"
            ]))
            try:
                generated = build_mutation_cases(loaded)
            except ConfigurationError:
                unavailable.add(family)
            else:
                self.assertEqual(len(generated), 1)
                self.assertEqual(
                    generated[0].parameters["mutation_family"], family
                )
        self.assertEqual(unavailable, {"undefined_enum"})

    def test_static_case_schedule_uses_duration_and_interval(self):
        payloads, interval_ms = case_schedule(mutation(), settings())
        self.assertEqual(interval_ms, 50.0)
        self.assertEqual(payloads, (mutation().mutated_payload,) * 2)

    def test_temporal_case_schedule_cycles_explicit_frames(self):
        first = "00000000200100F0"
        second = "00000000200000F0"
        item = mutation(parameters={
            "sequence": {"frames": [first, second], "interval_ms": 50.0}
        })
        payloads, _ = case_schedule(item, settings(mutation_duration_seconds=0.2))
        self.assertEqual(payloads, (
            bytes.fromhex(first), bytes.fromhex(second),
            bytes.fromhex(first), bytes.fromhex(second),
        ))

    def test_temporal_schedule_rejects_sub_50ms_interval(self):
        item = mutation(parameters={
            "sequence": {
                "frames": ["00000000200100F0", "00000000200000F0"],
                "interval_ms": 49.0,
            }
        })
        with self.assertRaisesRegex(ConfigurationError, "50"):
            case_schedule(item, settings())

    def test_transmission_restores_original_after_each_case(self):
        calls = []

        class Interface:
            def send_frame(self, frame):
                calls.append(frame)

        clock = FakeClock()
        count = transmit_cases(
            Interface(), settings(), [mutation()],
            clock=clock.monotonic, sleeper=clock.sleep, emit=lambda _: None,
        )
        self.assertEqual(count, 3)
        self.assertEqual([frame.data for frame in calls], [
            mutation().mutated_payload,
            mutation().mutated_payload,
            settings().baseline_payload,
        ])
        self.assertAlmostEqual(clock.value, 0.1)

    def test_transmission_attempts_restore_when_mutation_send_fails(self):
        calls = []

        class Interface:
            def send_frame(self, frame):
                calls.append(frame)
                if len(calls) == 1:
                    raise CANoeSendError("mutation failed")

        with self.assertRaisesRegex(CANoeSendError, "mutation failed"):
            transmit_cases(
                Interface(), settings(), [mutation()], emit=lambda _: None
            )
        self.assertEqual([frame.data for frame in calls], [
            mutation().mutated_payload,
            settings().baseline_payload,
        ])


if __name__ == "__main__":
    unittest.main()
