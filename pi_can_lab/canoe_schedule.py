"""Run bounded CANoe FuzzSchedule phases in an already running measurement.

The CAPL FuzzHarness owns millisecond frame timing. This bridge only commits a
single schedule and waits for its result; it never retries an ambiguous send.
"""

from __future__ import annotations

import math
import msvcrt
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Sequence


class CANoeScheduleError(RuntimeError):
    """A schedule could not be verified as safely completed."""


class CANoeScheduleInterface:
    def __init__(
        self,
        application: Any,
        channel: int,
        expected_config: Path,
        *,
        completion_timeout_seconds: float | None = None,
        poll_seconds: float = 0.05,
        lease_refresh_seconds: float = 0.25,
    ) -> None:
        if channel not in (1, 2, 3):
            raise ValueError("channel must be 1, 2 or 3")
        if completion_timeout_seconds is not None and (
            not math.isfinite(completion_timeout_seconds)
            or completion_timeout_seconds <= 0
        ):
            raise ValueError("completion timeout must be positive and finite")
        if not 0 < poll_seconds <= 0.5 or not 0 < lease_refresh_seconds < 1:
            raise ValueError("poll and lease intervals must be positive and bounded")
        self.application = application
        self.channel = channel
        self.expected_config = Path(expected_config).resolve()
        self.completion_timeout_seconds = completion_timeout_seconds
        self.poll_seconds = poll_seconds
        self.lease_refresh_seconds = lease_refresh_seconds
        self._lock_file = None
        self._heartbeat = None

    def __enter__(self) -> "CANoeScheduleInterface":
        self._check_connection()
        lock_path = Path(tempfile.gettempdir()) / "canoe_fuzzio_send.lock"
        self._lock_file = lock_path.open("a+b")
        try:
            if self._lock_file.seek(0, os.SEEK_END) == 0:
                self._lock_file.write(b"\0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            deadline = time.monotonic() + 3
            while int(self._dashboard_value("Heartbeat")) == 0:
                if time.monotonic() >= deadline or not self.application.Measurement.Running:
                    raise CANoeScheduleError("FuzzHarness heartbeat did not start")
                time.sleep(self.poll_seconds)
            self._heartbeat = int(self._dashboard_value("Heartbeat"))
            self._check_health()
            self._check_idle()
        except BaseException:
            self._lock_file.close()
            self._lock_file = None
            raise
        return self

    def __exit__(self, *unused: object) -> None:
        if self._lock_file is not None:
            try:
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def _variable(self, namespace: str, name: str) -> Any:
        return self.application.System.Namespaces.Item(namespace).Variables.Item(name)

    def _read(self, namespace: str, name: str) -> Any:
        return self._variable(namespace, name).Value

    def _write(self, name: str, value: Any) -> None:
        self._variable("FuzzSchedule", name).Value = value

    def _schedule_value(self, name: str) -> Any:
        return self._read("FuzzSchedule", name)

    def _dashboard_value(self, name: str) -> Any:
        return self._read("FuzzDashboard", name)

    def _check_connection(self) -> None:
        current = Path(str(self.application.Configuration.FullName)).resolve()
        if os.path.normcase(str(current)) != os.path.normcase(str(self.expected_config)):
            raise CANoeScheduleError(f"Unexpected CANoe configuration: {current}")
        if not bool(self.application.Measurement.Running):
            raise CANoeScheduleError("CANoe measurement is stopped")

    def _check_health(self) -> None:
        self._check_connection()
        heartbeat = int(self._dashboard_value("Heartbeat"))
        if self._heartbeat is not None and heartbeat < self._heartbeat:
            raise CANoeScheduleError("FuzzHarness heartbeat reset during measurement")
        # A slow or queued CANoe COM read can repeat a heartbeat value even
        # while the CAPL timer is running. The bounded schedule, CAPL lease,
        # request result, measurement state, and bus flags gate progression.
        self._heartbeat = heartbeat
        for channel in (1, 2, 3):
            if int(self._read("FuzzIO", f"BusOff{channel}")) != 0:
                raise CANoeScheduleError(f"CAN{channel} is bus-off")
            if int(self._read("FuzzIO", f"ErrorFrame{channel}")) != 0:
                raise CANoeScheduleError(f"CAN{channel} has error frames")
        if int(self._read("FuzzIO", "TxRequestSeq")) != int(
            self._read("FuzzIO", "TxDoneSeq")
        ):
            raise CANoeScheduleError("FuzzIO has an unfinished send request")

    def _check_idle(self) -> int:
        requested = int(self._schedule_value("RequestSeq"))
        completed = int(self._schedule_value("DoneSeq"))
        if requested != completed or completed < 0 or completed >= 2_147_483_647:
            raise CANoeScheduleError("FuzzSchedule has an unfinished or exhausted request")
        return completed

    @staticmethod
    def _prepare(
        duration_seconds: float,
        frames: Sequence[tuple[float, bytes]],
        frame_id: int,
    ) -> tuple[int, tuple[int, ...], tuple[int, ...]]:
        if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 120:
            raise ValueError("phase duration must be between 1 and 120 seconds")
        duration_ms = round(duration_seconds * 1000)
        if abs(duration_ms / 1000 - duration_seconds) > 1e-8:
            raise ValueError("phase duration must be in whole milliseconds")
        if not 0 <= frame_id <= 0x7FF or isinstance(frame_id, bool):
            raise ValueError("FuzzSchedule requires a Standard CAN identifier")
        if len(frames) > 200:
            raise ValueError("FuzzSchedule supports at most 200 frames per phase")
        data: list[int] = []
        offsets: list[int] = []
        for when, payload in frames:
            if not math.isfinite(when):
                raise ValueError("frame offsets must be finite")
            offset = round(when * 1000)
            if abs(offset / 1000 - when) > 1e-8 or not 0 <= offset < duration_ms:
                raise ValueError("frame offsets must be whole milliseconds within the phase")
            if offsets and offset - offsets[-1] < 50:
                raise ValueError("frame offsets must be at least 50 ms apart")
            frame = bytes(payload)
            if len(frame) != 8:
                raise ValueError("FuzzSchedule requires 8-byte Classic CAN frames")
            offsets.append(offset)
            data.extend(frame)
        return (
            duration_ms,
            tuple(data + [0] * (1600 - len(data))),
            tuple(offsets + [0] * (200 - len(offsets))),
        )

    def run_phase(
        self,
        label: str,
        duration_seconds: float,
        frames: Sequence[tuple[float, bytes]],
        frame_id: int = 0x366,
    ) -> dict[str, Any]:
        if self._lock_file is None:
            raise CANoeScheduleError("FuzzSchedule producer lock is not held")
        duration_ms, data, offsets = self._prepare(duration_seconds, frames, frame_id)
        self._check_health()
        sequence = self._check_idle() + 1
        previous_start = int(self._schedule_value("StartNs"))
        fields = {
            "Count": len(frames), "Channel": self.channel, "Id": frame_id,
            "Extended": 0, "Dlc": 8, "DurationMs": duration_ms,
            "Data": data, "OffsetsMs": offsets, "CancelSeq": 0,
            "Lease": int(self._schedule_value("Lease")) + 1,
        }
        for name, value in fields.items():
            try:
                self._write(name, value)
            except Exception as exc:
                raise CANoeScheduleError(f"Could not write FuzzSchedule {name}") from exc
        triggered = False
        try:
            triggered = True
            self._write("RequestSeq", sequence)
            deadline = time.monotonic() + (
                self.completion_timeout_seconds or duration_seconds + 5
            )
            last_lease = time.monotonic()
            last_health = last_lease
            last_requested = sequence - 1
            last_done = sequence - 1
            ack_seen_at: float | None = None
            while time.monotonic() < deadline or (
                ack_seen_at is not None and time.monotonic() - ack_seen_at < 1
            ):
                now = time.monotonic()
                if not self.application.Measurement.Running:
                    raise CANoeScheduleError(f"CANoe measurement stopped during {label}")
                if ack_seen_at is None and now - last_lease >= self.lease_refresh_seconds:
                    self._write("Lease", int(self._schedule_value("Lease")) + 1)
                    last_lease = time.monotonic()
                if now - last_health >= 0.5:
                    self._check_health()
                    last_health = time.monotonic()
                last_done = int(self._schedule_value("DoneSeq"))
                last_requested = int(self._schedule_value("RequestSeq"))
                if last_done > sequence or last_requested > sequence:
                    raise CANoeScheduleError(
                        f"{label} request {sequence} was changed by another producer"
                    )
                if last_done == sequence:
                    if ack_seen_at is None:
                        ack_seen_at = time.monotonic()
                    result = int(self._schedule_value("Result"))
                    submitted = int(self._schedule_value("Submitted"))
                    start_ns = int(self._schedule_value("StartNs"))
                    end_ns = int(self._schedule_value("EndNs"))
                    if result < 0:
                        raise CANoeScheduleError(
                            f"{label} rejected: result={result}, submitted={submitted}/{len(frames)}"
                        )
                    if (result == 1 and submitted == len(frames)
                            and start_ns > previous_start and end_ns > start_ns):
                        measured = (end_ns - start_ns) / 1_000_000_000
                        if abs(measured - duration_seconds) > 0.1:
                            raise CANoeScheduleError(
                                f"{label} CAPL duration {measured:.3f}s differs "
                                f"from {duration_seconds:g}s"
                            )
                        self._check_health()
                        return {
                            "label": label,
                            "request_seq": sequence,
                            "result": result,
                            "submitted_frames": submitted,
                            "start_ns": start_ns,
                            "end_ns": end_ns,
                            "duration_seconds": measured,
                        }
                    if time.monotonic() - ack_seen_at >= 1:
                        raise CANoeScheduleError(
                            f"{label} completion fields did not settle "
                            f"(result={result}, submitted={submitted}/{len(frames)}, "
                            f"start={start_ns}, end={end_ns})"
                        )
                    time.sleep(self.poll_seconds)
                    continue
                time.sleep(self.poll_seconds)
            raise CANoeScheduleError(
                f"{label} request {sequence} timed out (request={last_requested}, "
                f"done={last_done}); outcome is unknown"
            )
        except BaseException:
            if triggered:
                try:
                    self._write("CancelSeq", sequence)
                except Exception:
                    pass
            raise
