"""Capture a finalized BLF/ASC pair using CANoe's enabled logging blocks."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Callable


class CaptureError(RuntimeError):
    """A measurement or its paired logging files could not be verified."""


def _canoe_process_present() -> bool:
    """Guard the COM Dispatch fallback with a same-session process check."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    current_session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(current_session)):
        raise CaptureError("Could not inspect this Windows session")
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise CaptureError("Could not inspect running processes")
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() in ("canoe.exe", "canoe32.exe", "canoe64.exe"):
                process_session = wintypes.DWORD()
                if (
                    kernel.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(process_session))
                    and process_session.value == current_session.value
                ):
                    return True
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise CaptureError("Process enumeration failed")
        return False
    finally:
        kernel.CloseHandle(snapshot)


def attach_existing_canoe() -> Any:
    """Attach on a COM-initialized thread, including CANoe 20 ROT fallback."""
    if sys.platform != "win32":
        raise CaptureError("CANoe COM capture requires Windows")
    try:
        from win32com import client
    except ImportError as exc:
        raise CaptureError("Install pywin32 in the Windows Python environment") from exc
    try:
        return client.GetActiveObject("CANoe.Application")
    except Exception as active_error:
        if not _canoe_process_present():
            raise CaptureError("No CANoe instance is running in this Windows session") from active_error
        try:
            return client.Dispatch("CANoe.Application")
        except Exception as exc:
            raise CaptureError("Could not attach to the running CANoe instance") from exc


def _wait_for_state(measurement: Any, running: bool, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while bool(measurement.Running) != running:
        if time.monotonic() >= deadline:
            raise CaptureError("CANoe measurement state did not change in time")
        time.sleep(0.05)


def _increments_at_measurement_start(block: Any) -> bool:
    """Read the live logging option without changing the CANoe configuration."""
    try:
        options = block.FileNameOptions
    except Exception:
        try:
            from win32com import client

            options = client.CastTo(block, "ILogging5").FileNameOptions
        except Exception as exc:
            raise CaptureError("Could not verify CANoe log filename incrementing") from exc
    try:
        return bool(options.IncrementAtMeasurementStart)
    except Exception as exc:
        raise CaptureError("Could not verify CANoe log filename incrementing") from exc


def _next_increment_path(path: Path) -> Path:
    match = re.fullmatch(r"(.*?)(\d+)", path.stem)
    if match is None:
        raise CaptureError(f"Could not predict the next CANoe log name: {path}")
    prefix, number = match.groups()
    return path.with_name(f"{prefix}{int(number) + 1:0{len(number)}d}{path.suffix}")


def _verify_preexisting(files: dict[Path, tuple[int, int]]) -> None:
    for path, expected in files.items():
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise CaptureError(f"A preexisting CANoe log disappeared: {path}") from exc
        if (stat.st_size, stat.st_mtime_ns) != expected:
            raise CaptureError(f"A preexisting CANoe log changed: {path}")


def _finalized_pair(
    paths: dict[str, Path],
    before: dict[str, set[Path]],
    timeout: float = 15.0,
) -> tuple[Path, Path]:
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        blfs = list(set(paths[".blf"].parent.glob("*.blf")) - before[".blf"])
        ascs = list(set(paths[".asc"].parent.glob("*.asc")) - before[".asc"])
        if len(blfs) == len(ascs) == 1 and blfs[0].stem == ascs[0].stem:
            pair = blfs[0], ascs[0]
            sizes = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in pair)
            stable = stable + 1 if sizes == previous else 0
            previous = sizes
            if stable >= 3 and all(size > 0 for size, _ in sizes):
                with ascs[0].open("rb") as source:
                    source.seek(max(0, ascs[0].stat().st_size - 65536))
                    if b"end triggerblock" in source.read().lower():
                        return pair
        time.sleep(0.1)
    raise CaptureError("The paired BLF/ASC logs did not finalize in time")


def _copy_pair(source_pair: tuple[Path, Path], output_dir: Path) -> tuple[Path, Path]:
    result = []
    for source in source_pair:
        target = output_dir / source.name
        initial = source.stat()
        with source.open("rb") as input_file, target.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file)
        final = source.stat()
        if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise CaptureError(f"Source log changed during copy: {source}")
        if target.stat().st_size != initial.st_size:
            raise CaptureError(f"Copied log has an unexpected size: {target}")
        result.append(target)
    return result[0], result[1]


def capture_measurement(
    output_dir: Path,
    duration_seconds: float,
    expected_config: Path | None = None,
    attach_factory: Callable[[], Any] | None = None,
    action: Callable[[Any], Any] | None = None,
) -> tuple[Path, Path]:
    """Capture a new measurement and return its finalized (BLF, ASC) paths.

    The optional action receives the application after measurement start. It
    runs on the same COM thread and owns any transmit timing. A nonzero duration
    waits after the action, providing a post-action observation period.
    """
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("duration_seconds must be finite and nonnegative")
    if action is None and duration_seconds == 0:
        raise ValueError("passive capture duration must be greater than zero")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Capture directory already exists: {output_dir}")

    pythoncom = None
    if attach_factory is None:
        if sys.platform != "win32":
            raise CaptureError("CANoe COM capture requires Windows")
        try:
            import pythoncom
        except ImportError as exc:
            raise CaptureError("Install pywin32 in the Windows Python environment") from exc
        pythoncom.CoInitialize()
        attach_factory = attach_existing_canoe

    app = None
    cfg = None
    blocks = []
    block = None
    options = None
    measurement = None
    logging_paths: dict[str, Path] = {}
    before: dict[str, set[Path]] = {}
    preexisting: dict[Path, tuple[int, int]] = {}
    failure: BaseException | None = None
    attempted_start = False
    pair: tuple[Path, Path] | None = None
    try:
        app = attach_factory()
        cfg = app.Configuration
        measurement = app.Measurement
        if bool(measurement.Running):
            raise CaptureError("Stop the existing CANoe measurement before capture")
        if expected_config is not None:
            actual = os.path.normcase(os.path.realpath(str(cfg.FullName)))
            expected = os.path.normcase(os.path.realpath(str(expected_config)))
            if actual != expected:
                raise CaptureError(f"Unexpected CANoe configuration: {cfg.FullName}")
        try:
            mode = int(cfg.OnlineSetup.WorkingMode)
        except (AttributeError, TypeError):
            mode = None
        if mode is not None and mode != 0:
            raise CaptureError("Set CANoe working mode to Real Bus before capture")
        try:
            offline = cfg.OnlineSetup.OfflineMode
        except Exception:
            offline = None
        if offline is not None and bool(offline):
            raise CaptureError("Set CANoe to Online Mode before capture")

        blocks = list(cfg.OnlineSetup.LoggingCollection)
        if len(blocks) != 2:
            raise CaptureError("Expected one BLF and one ASC logging block")
        extensions = {Path(str(block.FullName)).suffix.lower() for block in blocks}
        if extensions != {".blf", ".asc"}:
            raise CaptureError("Expected one BLF and one ASC logging block")
        logging_blocks: dict[str, Any] = {}
        for block in blocks:
            path = Path(str(block.FullName))
            if not path.is_absolute():
                path = Path(str(cfg.FullName)).parent / path
            path = path.resolve()
            if not path.parent.is_dir():
                raise CaptureError(f"Logging directory does not exist: {path.parent}")
            extension = path.suffix.lower()
            logging_paths[extension] = path
            logging_blocks[extension] = block
            before[extension] = set(path.parent.glob(f"*{extension}"))
            for existing in before[extension]:
                stat = existing.stat()
                preexisting[existing] = stat.st_size, stat.st_mtime_ns

        if any(path.exists() for path in logging_paths.values()):
            blf_path, asc_path = logging_paths[".blf"], logging_paths[".asc"]
            if (
                not all(path.exists() for path in logging_paths.values())
                or blf_path.stem != asc_path.stem
                or not all(_increments_at_measurement_start(block) for block in logging_blocks.values())
            ):
                raise CaptureError("CANoe would overwrite a log: existing filename is not safely incremented")
            for path in logging_paths.values():
                next_path = _next_increment_path(path)
                if next_path.exists():
                    raise CaptureError(f"CANoe could overwrite the next log: {next_path}")

        output_dir.mkdir(parents=True, exist_ok=False)

        attempted_start = True
        measurement.Start()
        _wait_for_state(measurement, True, 20)
        if action is not None:
            action(app)
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            if not bool(measurement.Running):
                raise CaptureError("CANoe measurement stopped during capture")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    except BaseException as exc:
        failure = exc
    finally:
        if measurement is not None and attempted_start:
            try:
                if bool(measurement.Running):
                    measurement.Stop()
                _wait_for_state(measurement, False, 20)
            except BaseException as exc:
                if failure is None:
                    failure = exc
                elif hasattr(failure, "add_note"):
                    failure.add_note(f"CANoe stop also failed: {exc}")
            if output_dir.is_dir():
                try:
                    _verify_preexisting(preexisting)
                    source_pair = _finalized_pair(logging_paths, before)
                    pair = _copy_pair(source_pair, output_dir)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                    elif hasattr(failure, "add_note"):
                        failure.add_note(f"Log finalization also failed: {exc}")
        app = None
        measurement = None
        cfg = None
        blocks.clear()
        block = None
        if pythoncom is not None:
            pythoncom.CoUninitialize()
    if failure is not None:
        raise failure
    assert pair is not None
    return pair
