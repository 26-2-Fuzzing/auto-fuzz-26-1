"""Small CANoe COM adapter used by the standalone mutation sender.

The adapter deliberately supports only Classic CAN frames.  A CAPL function in
the active CANoe configuration owns the actual ``output()`` call; Python only
validates and forwards a frame through CANoe's automation interface.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


class CANoeError(RuntimeError):
    """Base error for CANoe automation failures."""


class CANoeConnectionError(CANoeError):
    """CANoe could not be attached to or started."""


class CANoeNotReadyError(CANoeError):
    """The CANoe measurement or CAPL bridge is not ready."""


class CANoeFrameError(CANoeError, ValueError):
    """A frame is outside the supported Classic CAN contract."""


class CANoeSendError(CANoeError):
    """The CAPL bridge call failed before CANoe accepted the request."""


@dataclass(frozen=True)
class CANoeFrame:
    channel: int
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_fd: bool = False


DispatchFactory = Callable[[str, bool], Any]
EventBinder = Callable[[Any, Callable[[], None]], Any]


class CANoeComInterface:
    """Forward validated frames to one CAPL function through CANoe COM.

    The CAPL call contract is intentionally compact so it stays below the
    automation interface's positional-argument limits::

        void AutoFuzzSend(channel, canId, dlc, word0, word1, word2, word3)

    Each word carries two payload bytes in little-endian order.

    CANoe only permits ``CAPL.GetFunction()`` to be resolved from the
    measurement ``OnInit`` event.  The adapter therefore registers an event
    sink before starting the measurement and waits until both the measurement
    and the CAPL function are ready.
    """

    def __init__(
        self,
        *,
        channel: int,
        prog_id: str = "CANoe.Application",
        attach_existing: bool = True,
        configuration_path: Optional[str | Path] = None,
        open_configuration: bool = False,
        start_measurement: bool = True,
        restart_running_measurement: bool = False,
        stop_measurement_on_close: bool = True,
        compile_capl: bool = True,
        measurement_timeout_seconds: float = 10.0,
        capl_function: str = "AutoFuzzSend",
        visible: bool = True,
        dispatch_factory: Optional[DispatchFactory] = None,
        event_binder: Optional[EventBinder] = None,
        message_pump: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 1:
            raise ValueError("CANoe channel must be an integer greater than zero")
        if (
            isinstance(measurement_timeout_seconds, bool)
            or not math.isfinite(float(measurement_timeout_seconds))
            or measurement_timeout_seconds <= 0
        ):
            raise ValueError("measurement timeout must be greater than zero")
        if not capl_function.strip():
            raise ValueError("CAPL function name cannot be empty")
        if restart_running_measurement and not start_measurement:
            raise ValueError(
                "restart_running_measurement requires start_measurement=true"
            )
        self.channel = int(channel)
        self.prog_id = str(prog_id)
        self.attach_existing = bool(attach_existing)
        self.configuration_path = (
            Path(configuration_path).expanduser().resolve()
            if configuration_path else None
        )
        self.open_configuration = bool(open_configuration)
        self.start_measurement = bool(start_measurement)
        self.restart_running_measurement = bool(restart_running_measurement)
        self.stop_measurement_on_close = bool(stop_measurement_on_close)
        self.compile_capl = bool(compile_capl)
        self.measurement_timeout_seconds = float(measurement_timeout_seconds)
        self.capl_function = capl_function.strip()
        self.visible = bool(visible)
        self._dispatch_factory = dispatch_factory
        self._event_binder = event_binder
        self._message_pump = message_pump
        self._clock = clock
        self._sleeper = sleeper
        self._pythoncom: Any = None
        self._win32_client: Any = None
        self._com_initialized = False
        self._app: Any = None
        self._measurement: Any = None
        self._measurement_events: Any = None
        self._send_function: Any = None
        self._capl_bind_error: Optional[Exception] = None
        self._started_measurement = False

    @property
    def connected(self) -> bool:
        return self._app is not None and self._send_function is not None

    def _default_dispatch(self, prog_id: str, attach_existing: bool) -> Any:
        try:
            import pythoncom  # type: ignore
            from win32com import client as win32_client  # type: ignore
        except ImportError as exc:
            raise CANoeConnectionError(
                "CANoe COM control requires Windows and pywin32; "
                "install pi_can_lab/requirements.txt in a Windows Python environment"
            ) from exc

        pythoncom.CoInitialize()
        self._pythoncom = pythoncom
        self._win32_client = win32_client
        self._com_initialized = True
        try:
            if attach_existing:
                return win32_client.GetActiveObject(prog_id)
            return win32_client.DispatchEx(prog_id)
        except Exception as exc:
            raise CANoeConnectionError(
                "running CANoe instance was not found"
                if attach_existing else "CANoe could not be started"
            ) from exc

    def _default_event_binder(
        self, measurement: Any, callback: Callable[[], None]
    ) -> Any:
        if self._win32_client is None:
            try:
                from win32com import client as win32_client  # type: ignore
            except ImportError as exc:
                raise CANoeConnectionError(
                    "CANoe measurement events require pywin32"
                ) from exc
            self._win32_client = win32_client

        class MeasurementEvents:
            def OnInit(event_self) -> None:  # noqa: N802 - COM event name
                del event_self
                callback()

        try:
            return self._win32_client.WithEvents(measurement, MeasurementEvents)
        except Exception as exc:
            raise CANoeConnectionError(
                f"could not subscribe to CANoe Measurement.OnInit: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _pump_com_events(self) -> None:
        if self._message_pump is not None:
            self._message_pump()
        elif self._pythoncom is not None:
            self._pythoncom.PumpWaitingMessages()

    def _wait_for_measurement(self, expected_running: bool) -> None:
        deadline = self._clock() + self.measurement_timeout_seconds
        while True:
            self._pump_com_events()
            if bool(self._measurement.Running) == expected_running:
                return
            if self._clock() >= deadline:
                state = "start" if expected_running else "stop"
                raise CANoeNotReadyError(
                    f"CANoe measurement did not {state} within "
                    f"{self.measurement_timeout_seconds:g} seconds"
                )
            self._sleeper(0.05)

    def _on_measurement_init(self) -> None:
        try:
            function = self._app.CAPL.GetFunction(self.capl_function)
            if function is None:
                raise CANoeNotReadyError(
                    f"CAPL function was not found: {self.capl_function}"
                )
            try:
                parameter_count = int(function.ParameterCount)
            except Exception as exc:
                raise CANoeNotReadyError(
                    f"could not inspect CAPL function {self.capl_function!r} "
                    "parameter count"
                ) from exc
            if parameter_count != 7:
                raise CANoeNotReadyError(
                    f"CAPL function {self.capl_function!r} must have 7 parameters; "
                    f"found {parameter_count}"
                )
            self._send_function = function
        except Exception as exc:
            self._capl_bind_error = exc

    def _wait_until_ready(self) -> None:
        deadline = self._clock() + self.measurement_timeout_seconds
        while True:
            self._pump_com_events()
            if self._capl_bind_error is not None:
                error = self._capl_bind_error
                if isinstance(error, CANoeError):
                    raise error
                raise CANoeNotReadyError(
                    f"CAPL function binding failed: {type(error).__name__}: {error}"
                ) from error
            if bool(self._measurement.Running) and self._send_function is not None:
                return
            if self._clock() >= deadline:
                missing = (
                    "Measurement.OnInit/CAPL function"
                    if self._send_function is None
                    else "running measurement"
                )
                raise CANoeNotReadyError(
                    f"CANoe did not provide {missing} within "
                    f"{self.measurement_timeout_seconds:g} seconds"
                )
            self._sleeper(0.05)

    def _cleanup_failed_connect(self) -> None:
        if self._measurement is not None and self._started_measurement:
            try:
                if bool(self._measurement.Running):
                    self._measurement.Stop()
                    self._wait_for_measurement(False)
            except Exception:
                pass
        self._started_measurement = False
        self._release_com()

    def connect(self) -> None:
        if self.connected:
            return
        factory = self._dispatch_factory or self._default_dispatch
        try:
            self._app = factory(self.prog_id, self.attach_existing)
            try:
                self._app.Visible = self.visible
            except Exception:
                # Some run-only CANoe editions do not expose a writable Visible
                # property.  Visibility is not part of the transmission contract.
                pass

            self._measurement = self._app.Measurement
            if self.open_configuration:
                if self.configuration_path is None:
                    raise CANoeConnectionError(
                        "open_configuration requires canoe.configuration_path"
                    )
                if not self.configuration_path.is_file():
                    raise CANoeConnectionError(
                        f"CANoe configuration does not exist: {self.configuration_path}"
                    )
                if bool(self._measurement.Running):
                    raise CANoeNotReadyError(
                        "cannot replace a CANoe configuration while measurement is running"
                    )
                self._app.Open(str(self.configuration_path))
                self._measurement = self._app.Measurement

            if bool(self._measurement.Running):
                if not self.restart_running_measurement:
                    raise CANoeNotReadyError(
                        "CANoe measurement is already running, so the CAPL function "
                        "cannot be acquired in Measurement.OnInit; stop the measurement "
                        "first or set canoe.restart_running_measurement: true"
                    )
                self._measurement.Stop()
                self._wait_for_measurement(False)

            if self.compile_capl:
                try:
                    self._app.CAPL.Compile(None)
                    compile_result = self._app.CAPL.CompileResult
                    if int(compile_result.Result) != 0:
                        details = str(compile_result.ErrorMessage).strip()
                        node = str(compile_result.NodeName).strip()
                        source = str(compile_result.SourceFile).strip()
                        context = ", ".join(
                            value for value in (node, source) if value
                        )
                        suffix = f" ({context})" if context else ""
                        raise CANoeNotReadyError(
                            f"CAPL compilation failed{suffix}: "
                            f"{details or 'unknown compiler error'}"
                        )
                except CANoeError:
                    raise
                except Exception as exc:
                    raise CANoeNotReadyError(
                        f"CAPL compilation failed: {type(exc).__name__}: {exc}"
                    ) from exc

            binder = self._event_binder or self._default_event_binder
            self._measurement_events = binder(
                self._measurement, self._on_measurement_init
            )
            if self._measurement_events is None:
                raise CANoeConnectionError(
                    "CANoe Measurement.OnInit event subscription returned no sink"
                )

            if self.start_measurement:
                self._started_measurement = True
                self._measurement.Start()
            self._wait_until_ready()
        except CANoeError:
            self._cleanup_failed_connect()
            raise
        except Exception as exc:
            self._cleanup_failed_connect()
            raise CANoeConnectionError(
                f"CANoe automation initialization failed: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _validate_frame(frame: CANoeFrame) -> bytes:
        if (
            isinstance(frame.channel, bool)
            or not isinstance(frame.channel, int)
            or frame.channel < 1
        ):
            raise CANoeFrameError("CANoe channel must be an integer greater than zero")
        if frame.is_extended_id:
            raise CANoeFrameError("this CANoe bridge supports Standard CAN IDs only")
        if frame.is_fd:
            raise CANoeFrameError("this CANoe bridge supports Classic CAN only")
        if (
            isinstance(frame.arbitration_id, bool)
            or not isinstance(frame.arbitration_id, int)
            or not 0 <= frame.arbitration_id <= 0x7FF
        ):
            raise CANoeFrameError("Standard CAN ID must be between 0x000 and 0x7FF")
        if not isinstance(frame.data, (bytes, bytearray, memoryview)):
            raise CANoeFrameError("CAN payload must be bytes-like")
        payload = bytes(frame.data)
        if not 0 <= len(payload) <= 8:
            raise CANoeFrameError("Classic CAN payload must contain 0 to 8 bytes")
        return payload

    @staticmethod
    def _payload_words(payload: bytes) -> tuple[int, int, int, int]:
        padded = payload.ljust(8, b"\x00")
        return tuple(
            padded[index] | (padded[index + 1] << 8)
            for index in range(0, 8, 2)
        )  # type: ignore[return-value]

    def send_frame(self, frame: CANoeFrame) -> None:
        payload = self._validate_frame(frame)
        if int(frame.channel) != self.channel:
            raise CANoeFrameError(
                f"frame channel {frame.channel} does not match configured CANoe "
                f"channel {self.channel}"
            )
        if not self.connected:
            raise CANoeNotReadyError("CANoe interface is not connected")
        if not bool(self._measurement.Running):
            raise CANoeNotReadyError("CANoe measurement stopped before transmission")
        words = self._payload_words(payload)
        try:
            self._send_function.Call(
                int(frame.channel),
                int(frame.arbitration_id),
                len(payload),
                *words,
            )
        except Exception as exc:
            raise CANoeSendError(
                f"CAPL send call failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _release_com(self) -> None:
        self._send_function = None
        self._capl_bind_error = None
        self._measurement_events = None
        self._measurement = None
        self._app = None
        if self._com_initialized and self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            finally:
                self._com_initialized = False
                self._pythoncom = None
        self._win32_client = None

    def close(self) -> None:
        stop_error: Optional[Exception] = None
        if (
            self._measurement is not None
            and self._started_measurement
            and self.stop_measurement_on_close
        ):
            try:
                if bool(self._measurement.Running):
                    self._measurement.Stop()
                    self._wait_for_measurement(False)
            except Exception as exc:
                stop_error = exc
        self._started_measurement = False
        self._release_com()
        if stop_error is not None:
            raise CANoeNotReadyError(
                f"failed to stop CANoe measurement: {type(stop_error).__name__}: {stop_error}"
            ) from stop_error

    def __enter__(self) -> "CANoeComInterface":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()
