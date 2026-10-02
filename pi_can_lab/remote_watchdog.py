"""Bound a remote sender even if the controlling SSH connection is lost."""

from __future__ import annotations

import math
import time
from typing import Callable, Sequence, Any


def run_supervised_sender(
    manager: Any,
    command: Sequence[str],
    stdout_path: str,
    *,
    timeout: float,
    health_check: Callable[[], None] | None = None,
    poll_seconds: float = 0.5,
) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("sender timeout must be positive and finite")
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("watchdog poll interval must be positive and finite")
    # The remote deadline survives control-PC disconnection. GNU timeout relays
    # SIGTERM so the sender can attempt restoration before the hard stop.
    bounded = ["timeout", "--signal=TERM", "--kill-after=5s", f"{timeout:g}s", *command]
    process = manager.start_process(bounded, stdout_path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if health_check is not None:
                health_check()
            if not manager.process_alive(process):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Sender exceeded its monitored deadline")
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
    except BaseException as exc:
        try:
            manager.stop_process(process)
        except Exception as cleanup_error:
            if hasattr(exc, "add_note"):
                exc.add_note(f"Remote sender stop could not be confirmed: {cleanup_error}")
        raise
