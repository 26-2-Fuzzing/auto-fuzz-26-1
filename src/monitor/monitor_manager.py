import threading
from typing import Dict, Optional
from .timing_monitor import TimingMonitor
from .uds_monitor import UDSMonitor
from .dbc_monitor import DBCMonitor
from .observation_adapter import events_to_observations
from ..models.experiment import AnomalyObservation


class MonitorManager:

    def __init__(
        self,
        timing_monitor: Optional[TimingMonitor] = None,
        uds_monitor: Optional[UDSMonitor] = None,
        dbc_monitor: Optional[DBCMonitor] = None
    ):
        self.timing_monitor = timing_monitor
        self.uds_monitor = uds_monitor
        self.dbc_monitor = dbc_monitor

        # 각 모니터 스레드
        self.threads: Dict[str, threading.Thread] = {}

        # 모니터 상태 및 점수
        # status: "pending" | "running" | "ok" | "timeout" | "crashed" | "skipped"
        self.scores: Dict[str, float] = {
            "timing": 0.0,
            "uds": 0.0,
            "dbc": 0.0
        }
        self.status: Dict[str, str] = {
            "timing": "pending",
            "uds": "pending",
            "dbc": "pending"
        }
        self.completed: Dict[str, bool] = {
            "timing": False,
            "uds": False,
            "dbc": False
        }

        # scores + status + completed를 함께 보호하는 락
        self.state_lock = threading.Lock()

        self.running = False


    # 모니터 시작
    def start_monitors(
        self,
        timing_timeout: Optional[float] = 5.0,
        dbc_timeout: Optional[float] = 5.0
    ):
        """
        각 모니터를 별도 스레드로 시작
        여러 번 호출될 수 있으므로, 호출 시 상태 초기화 + thread dict 초기화
        """
        if self.running:
            print("[WARN] Monitors are already running")
            return

        self.running = True
        print("[INFO] Starting monitors...")

        # threads 재사용 방지: 매번 초기화
        self.threads = {}

        # 상태 초기화
        with self.state_lock:
            self.scores = {"timing": 0.0, "uds": 0.0, "dbc": 0.0}
            self.completed = {"timing": False, "uds": False, "dbc": False}
            self.status = {"timing": "pending", "uds": "pending", "dbc": "pending"}

        # Timing Monitor
        if self.timing_monitor:
            with self.state_lock:
                self.status["timing"] = "running"

            thread = threading.Thread(
                target=self._run_timing_monitor,
                args=(timing_timeout,),
                daemon=True,
                name="TimingMonitor"
            )
            self.threads["timing"] = thread
            thread.start()
            print("[INFO] ✓ Timing Monitor started")
        else:
            with self.state_lock:
                self.completed["timing"] = True
                self.status["timing"] = "skipped"

        # UDS Monitor
        if self.uds_monitor:
            with self.state_lock:
                self.status["uds"] = "running"

            thread = threading.Thread(
                target=self._run_uds_monitor,
                daemon=True,
                name="UDSMonitor"
            )
            self.threads["uds"] = thread
            thread.start()
            print("[INFO] ✓ UDS Monitor started")
        else:
            with self.state_lock:
                self.completed["uds"] = True
                self.status["uds"] = "skipped"

        # DBC Monitor
        if self.dbc_monitor:
            with self.state_lock:
                self.status["dbc"] = "running"

            thread = threading.Thread(
                target=self._run_dbc_monitor,
                args=(dbc_timeout,),
                daemon=True,
                name="DBCMonitor"
            )
            self.threads["dbc"] = thread
            thread.start()
            print("[INFO] ✓ DBC Monitor started")
        else:
            with self.state_lock:
                self.completed["dbc"] = True
                self.status["dbc"] = "skipped"

        print(f"[INFO] Total {len(self.threads)} monitor(s) running")

    # 각 모니터 실행 함수
    def _run_timing_monitor(self, timeout: Optional[float]):
        try:
            fail_score = self.timing_monitor.start(timeout=timeout)

            with self.state_lock:
                # 이미 timeout으로 처리된 경우 덮어쓰지 않음
                if self.status["timing"] == "timeout":
                    return
                self.scores["timing"] = fail_score
                self.completed["timing"] = True
                self.status["timing"] = "ok"

            print(f"[INFO] Timing Monitor completed - Score: {fail_score}")

        except Exception as e:
            print(f"[ERROR] Timing Monitor crashed: {e}")
            with self.state_lock:
                if self.status["timing"] == "timeout":
                    return
                self.completed["timing"] = True
                self.status["timing"] = "crashed"

    def _run_uds_monitor(self):
        try:
            fail_score = self.uds_monitor.start()

            with self.state_lock:
                if self.status["uds"] == "timeout":
                    return
                self.scores["uds"] = fail_score
                self.completed["uds"] = True
                self.status["uds"] = "ok"

            print(f"[INFO] UDS Monitor completed - Score: {fail_score}")

        except Exception as e:
            print(f"[ERROR] UDS Monitor crashed: {e}")
            with self.state_lock:
                if self.status["uds"] == "timeout":
                    return
                self.completed["uds"] = True
                self.status["uds"] = "crashed"

    def _run_dbc_monitor(self, timeout: Optional[float]):
        try:
            fail_score = self.dbc_monitor.start(timeout=timeout)

            with self.state_lock:
                if self.status["dbc"] == "timeout":
                    return
                self.scores["dbc"] = fail_score
                self.completed["dbc"] = True
                self.status["dbc"] = "ok"

            print(f"[INFO] DBC Monitor completed - Score: {fail_score}")

        except Exception as e:
            print(f"[ERROR] DBC Monitor crashed: {e}")
            with self.state_lock:
                if self.status["dbc"] == "timeout":
                    return
                self.completed["dbc"] = True
                self.status["dbc"] = "crashed"


    # 종료 대기 + timeout 처리
    def wait_for_completion(self, timeout: Optional[float] = None):
        """
        각 모니터 스레드가 종료될 때까지 대기.
        timeout이 주어지면, join(timeout) 이후에도 살아있는 스레드는
        status를 'timeout'으로 마킹하고 completed=True로 설정한다.
        (실제 스레드를 kill 하진 못하지만, 파이프라인 관점에서는 timeout 처리)
        """
        for name, thread in self.threads.items():
            if not thread.is_alive():
                continue

            thread.join(timeout=timeout)

            if thread.is_alive() and timeout is not None:
                print(f"[WARN] {name} thread is still running (timeout reached)")
                with self.state_lock:
                    self.completed[name] = True
                    self.status[name] = "timeout"

        self.running = False
        print("[INFO] All monitors completed (or timed out)")


    # 상태 조회
    def is_all_completed(self) -> bool:
        with self.state_lock:
            return all(self.completed.values())

    def get_completion_status(self) -> Dict[str, bool]:
        """
        기존 코드 호환용: 여전히 bool만 리턴
        상세 상태는 get_status()를 통해 확인
        """
        with self.state_lock:
            return self.completed.copy()

    def get_status(self) -> Dict[str, str]:
        """
        각 모니터의 상세 상태:
        "pending" | "running" | "ok" | "timeout" | "crashed" | "skipped"
        """
        with self.state_lock:
            return self.status.copy()

    def get_scores(self) -> Dict[str, float]:
        with self.state_lock:
            return self.scores.copy()

    def collect_observations(self) -> list[AnomalyObservation]:
        """Drain detector event buffers into a backend-independent representation."""
        observations: list[AnomalyObservation] = []
        for monitor in (self.timing_monitor, self.uds_monitor, self.dbc_monitor):
            fetch = getattr(monitor, "fetch_events", None)
            if not fetch:
                continue
            bus_name = getattr(monitor, "logical_bus", None) or getattr(
                monitor, "channel", "unknown"
            )
            observations.extend(events_to_observations(fetch(), bus_name))
        return observations

    def is_running(self) -> bool:
        return self.running
