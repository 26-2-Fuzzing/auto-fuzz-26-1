from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CANFrame:
    bus: str
    arbitration_id: int
    data: bytes
    timestamp: float = field(default_factory=time.time)
    is_extended_id: bool = False


class BaseCANInterface(ABC):
    """Transport contract shared by SocketCAN and future CANoe backends."""

    @abstractmethod
    def send_frame(self, frame: CANFrame) -> None:
        raise NotImplementedError

    @abstractmethod
    def recv_frame(self, timeout: float = 0.1) -> Optional[CANFrame]:
        raise NotImplementedError

    def start_listener(self) -> None:
        pass

    def stop_listener(self, join_timeout: float = 1.0) -> None:
        pass

    def close(self) -> None:
        pass
