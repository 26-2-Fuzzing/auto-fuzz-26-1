# src/interface/can_interface.py
import can
import threading
import queue
import time
from typing import Optional
from .base_interface import BaseCANInterface, CANFrame


class CANInterface(BaseCANInterface):
    """
    단일 CAN ID 기반 송수신 인터페이스 (socketcan)
    - 기본 ID: 0x6A6
    - CLI에서 전달받은 ID를 Tx/Rx에 모두 사용
    - pipeline은 seed.message_id를 우선 사용하고, 없으면 이 기본 ID를 fallback으로 사용
    """

    def __init__(
        self,
        channel: str = "vcan0",
        bustype: str = "socketcan",
        can_id: int = 0x6A6,
        logical_bus: Optional[str] = None,
    ):
        self.channel = channel
        self.bustype = bustype
        self.can_id = can_id
        self.logical_bus = logical_bus or channel

        try:
            self.bus = can.interface.Bus(channel=self.channel, bustype=self.bustype)
        except Exception as e:
            raise RuntimeError(f"[!] Failed to open CAN channel '{channel}': {e}")

        self._rx_queue: "queue.Queue[can.Message]" = queue.Queue()
        self._running = False
        self._listener_thread: Optional[threading.Thread] = None


    # Listener
    def start_listener(self) -> None:
        """CAN 수신 스레드 시작"""
        if self._running:
            return
        self._running = True

        def _listener_loop():
            while self._running:
                try:
                    msg = self.bus.recv(timeout=1.0)
                    if msg:
                        self._rx_queue.put(msg)
                except Exception as e:
                    print(f"[CAN:Rx] Listener error: {e}")
                    time.sleep(0.5)

        self._listener_thread = threading.Thread(target=_listener_loop, daemon=True)
        self._listener_thread.start()
        print(f"[CAN] Listener started on {self.channel} (ID={hex(self.can_id)})")

    def stop_listener(self, join_timeout: float = 1.0) -> None:
        """수신 스레드 종료"""
        self._running = False
        if self._listener_thread:
            self._listener_thread.join(timeout=join_timeout)
            self._listener_thread = None
        print("[CAN] Listener stopped")


    # 송신/수신
    def send_raw(self, data: bytes, arb_id: Optional[int] = None, is_extended: bool = False) -> None:
        """raw CAN frame 송신"""
        arb = arb_id if arb_id is not None else self.can_id
        msg = can.Message(arbitration_id=arb, data=data, is_extended_id=is_extended)
        try:
            self.bus.send(msg)
            print(f"[CAN:Tx] ID={hex(arb)} -> {data.hex().upper()}")
        except Exception as e:
            print(f"[!] CAN send error: {e}")

    def send_frame(self, frame: CANFrame) -> None:
        if frame.bus != self.logical_bus:
            raise ValueError(
                f"Frame for {frame.bus!r} cannot be sent by {self.logical_bus!r}"
            )
        self.send_raw(frame.data, frame.arbitration_id, frame.is_extended_id)

    def recv(self, timeout: float = 0.1) -> Optional["can.Message"]:
        """대기 중 수신된 메시지 반환"""
        try:
            return self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def recv_frame(self, timeout: float = 0.1) -> Optional[CANFrame]:
        msg = self.recv(timeout)
        if msg is None:
            return None
        return CANFrame(
            bus=self.logical_bus,
            arbitration_id=msg.arbitration_id,
            data=bytes(msg.data),
            timestamp=getattr(msg, "timestamp", time.time()),
            is_extended_id=msg.is_extended_id,
        )

    def close(self) -> None:
        self.stop_listener()
        shutdown = getattr(self.bus, "shutdown", None)
        if shutdown:
            shutdown()

    def __repr__(self) -> str:
        return (
            f"<CANInterface bus={self.logical_bus} channel={self.channel} "
            f"id={hex(self.can_id)}>"
        )
