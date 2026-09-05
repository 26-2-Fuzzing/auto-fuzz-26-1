from __future__ import annotations

from typing import Dict, Optional

from .base_interface import BaseCANInterface, CANFrame


class MultiBusCANInterface(BaseCANInterface):
    """Routes logical bus names (P-CAN/B-CAN/C-CAN) to transport backends."""

    def __init__(self, interfaces: Dict[str, BaseCANInterface]):
        if not interfaces:
            raise ValueError("At least one CAN interface is required")
        self.interfaces = dict(interfaces)

    def send_frame(self, frame: CANFrame) -> None:
        try:
            backend = self.interfaces[frame.bus]
        except KeyError as exc:
            raise KeyError(f"Unknown logical CAN bus: {frame.bus}") from exc
        backend.send_frame(frame)

    def recv_frame(self, timeout: float = 0.1) -> Optional[CANFrame]:
        # Non-blocking round-robin prevents one quiet bus from blocking all others.
        per_bus_timeout = timeout / max(len(self.interfaces), 1)
        for backend in self.interfaces.values():
            frame = backend.recv_frame(per_bus_timeout)
            if frame is not None:
                return frame
        return None

    def start_listener(self) -> None:
        for backend in self.interfaces.values():
            backend.start_listener()

    def stop_listener(self, join_timeout: float = 1.0) -> None:
        for backend in self.interfaces.values():
            backend.stop_listener(join_timeout)

    def close(self) -> None:
        for backend in self.interfaces.values():
            backend.close()
