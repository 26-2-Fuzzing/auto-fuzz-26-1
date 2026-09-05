# src/pipeline.py

import time
from typing import Optional, Dict, Any, List

from .seeds.dbc_parser import DbcParser
from .seeds.seed_manager import SeedManager, Seed
from .seeds.seed_queue import SeedQueue

from .monitor.monitor_manager import MonitorManager
from .monitor.timing_monitor import TimingMonitor
from .monitor.uds_monitor import UDSMonitor
from .monitor.dbc_monitor import DBCMonitor

from .mutation.mutator import Mutator
from .experiments.repository import ExperimentRepository
from .feedback.strategy_selector import StrategySelector
from .interface.base_interface import CANFrame
from .models.experiment import MutationRecord, TrialRecord


class AutoFuzzPipeline:
    
    # 시드 등록
    @staticmethod
    def register_seeds(dbc_path: str, db_path: str = "seeds.db") -> int:
        parser = DbcParser(dbc_path)
        parsed = parser.parse()

        manager = SeedManager(db_path)
        seed_groups = manager.from_dbc(parsed)
        queue = SeedQueue(db_path)

        total = 0
        for group in seed_groups:
            queue.push_group(group)
            total += len(group)

        queue.close()
        manager.close()
        return total

    
    # 시드 목록
    @staticmethod
    def list_seeds(db_path: str = "seeds.db"):
        manager = SeedManager(db_path)
        seeds = manager.get_all()
        manager.close()
        return seeds


    # config
    def __init__(
        self,
        cfg: Dict[str, Any],
        can_iface: Optional[object] = None,
        monitor_manager: Optional[MonitorManager] = None,
        experiment_repository: Optional[ExperimentRepository] = None,
    ):
        """
        cfg: config dict (default.yaml 또는 사용자 config)
        """

        self.cfg = cfg
        self.can = can_iface
        self.running = False
        self.source_bus = cfg["can"].get("logical_bus", cfg["can"].get("channel", "can0"))
        experiment_db = cfg["paths"].get("experiment_db", "experiments.db")
        self.experiments = experiment_repository or ExperimentRepository(experiment_db)
        feedback_cfg = cfg.get("feedback", {})
        self.strategy_selector = StrategySelector(
            exploration_rate=float(feedback_cfg.get("exploration_rate", 0.2))
        )
        self.associations: List[Dict[str, Any]] = []
        self.feedback_plans: List[Dict[str, Any]] = []
        self.feedback_weights: Dict[str, float] = {}

        # 시드 큐 초기화
        seed_db_path = cfg["paths"]["seed_db"]
        self.queue = SeedQueue(seed_db_path)

        # 모니터 매니저
        self.monitor_manager = monitor_manager or MonitorManager(
            timing_monitor=TimingMonitor(channel=cfg["can"]["channel"]),
            uds_monitor=UDSMonitor(),
            dbc_monitor=DBCMonitor(
                channel=cfg["can"]["channel"],
                dbc_path=cfg["paths"]["dbc"],
                target_id=int(cfg["can"]["default_id"], 16),
                seed_db_path=cfg["paths"]["seed_db"]
            )
        )


    # CAN 송신
    def send_raw_payload(self, data: bytes, arb_id: int, source_bus: Optional[str] = None):
        """실제 CAN raw 송신 또는 스텁 출력"""
        if not self.can:
            print(f"[Stub:Tx] ID={hex(arb_id)} | Data={data.hex()}")
            return

        try:
            if hasattr(self.can, "send_frame"):
                self.can.send_frame(
                    CANFrame(
                        bus=source_bus or self.source_bus,
                        arbitration_id=arb_id,
                        data=data,
                    )
                )
            else:
                self.can.send_raw(data, arb_id=arb_id)
        except Exception as e:
            print(f"[!] CAN send error: {e}")

    
    # 시드
    def fuzz_seed(self, seed: Seed, monitor_weights: Dict[str, float]):
        """
        1. seed → base bytes 생성
        2. Mutator 생성 & mutate_manager 실행
        3. Mutated payload 각각 CAN 송신
        """

        try:
            value = int(seed.metadata.offset or 0)
            base_data = value.to_bytes(8, "little", signed=True)
        except Exception:
            base_data = (0).to_bytes(8, "little")

        effective_weights = {**monitor_weights, **self.feedback_weights}
        mut = Mutator(
            data=base_data,
            weights=effective_weights,
            min_length=1
        )

        if self.cfg["can"].get("force_default_id", False):
            arb_id = int(self.cfg["can"]["default_id"], 16)
        else:
            arb_id = seed.message_id or int(self.cfg["can"]["default_id"], 16)

        records = mut.mutate_records(self.source_bus, arb_id, seed.id)
        for record in records:
            self._execute_mutation(record)
        # Follow-ups are bounded and executed one generation deep to prevent a
        # single finding from starving exploration.
        followup_limit = int(
            self.cfg.get("feedback", {}).get("max_followups_per_seed", 16)
        )
        followups = getattr(self, "_pending_followups", [])[:followup_limit]
        self._pending_followups = []
        for record in followups:
            self._execute_mutation(record, allow_followups=False)
        records.extend(followups)
        return records

    def _execute_mutation(
        self, mutation: MutationRecord, allow_followups: bool = True
    ) -> None:
        """Run controlled trials and persist evidence for one mutation."""
        self.experiments.save_mutation(mutation)
        fuzz_cfg = self.cfg.get("fuzz", {})
        control_trials = max(0, int(fuzz_cfg.get("control_trials", 0)))
        reproduction_trials = max(1, int(fuzz_cfg.get("reproduction_trials", 1)))
        post_window = max(0.0, float(fuzz_cfg.get("post_injection_window", 0.01)))

        # Drain stale pre-window events. They remain monitor evidence, but must not
        # be attributed to the next injection.
        self.monitor_manager.collect_observations()
        observations_seen = {}

        def run_trials(is_control: bool, start_index: int, count: int) -> None:
            for index in range(start_index, start_index + count):
                trial = TrialRecord(
                    mutation_id=mutation.mutation_id,
                    trial_index=index,
                    is_control=is_control,
                    state_signature=fuzz_cfg.get("state_signature"),
                )
                payload = mutation.original_data if is_control else mutation.mutated_data
                self.send_raw_payload(payload, mutation.message_id, mutation.source_bus)
                if post_window:
                    time.sleep(post_window)
                trial.observations = self.monitor_manager.collect_observations()
                observations_seen.update(
                    (obs.fingerprint, obs) for obs in trial.observations
                )
                trial.completed_at = time.time()
                self.experiments.save_trial(trial)

        run_trials(True, 0, control_trials)
        run_trials(False, 0, reproduction_trials)

        # Exploration stays cheap (one mutation trial by default). Only a finding
        # triggers controlled verification, preventing a 6x campaign explosion.
        if observations_seen and fuzz_cfg.get("auto_reproduce", True):
            target_trials = max(3, int(fuzz_cfg.get("verification_trials", 3)))
            recovery_window = max(
                0.0, float(fuzz_cfg.get("recovery_window", post_window))
            )
            if recovery_window:
                time.sleep(recovery_window)
            if control_trials < target_trials:
                run_trials(True, control_trials, target_trials - control_trials)
            if reproduction_trials < target_trials:
                run_trials(
                    False,
                    reproduction_trials,
                    target_trials - reproduction_trials,
                )

        fingerprints = set(observations_seen)
        for fingerprint in fingerprints:
            summary = self.experiments.association(mutation.mutation_id, fingerprint)
            self.associations.append(summary.to_dict())
            observation = observations_seen.get(fingerprint)
            if observation is not None:
                plan = self.strategy_selector.select(mutation, observation, summary)
                self.feedback_plans.append(
                    {
                        "mutation_id": mutation.mutation_id,
                        "anomaly_fingerprint": fingerprint,
                        "strategy": plan.strategy,
                        "focus_bytes": list(plan.focus_bytes),
                        "focus_bits": [list(item) for item in plan.focus_bits],
                        "reason": plan.reason,
                    }
                )
                if summary.eligible_for_feedback:
                    self.feedback_weights.update(
                        self.strategy_selector.weights_for(
                            plan, len(mutation.mutated_data)
                        )
                    )
                    if allow_followups:
                        generated = self.strategy_selector.generate_followups(
                            mutation, plan
                        )
                        pending = getattr(self, "_pending_followups", [])
                        pending.extend(generated)
                        self._pending_followups = pending


    # 실행
    def run(self) -> Dict[str, Any]:
        """
        0. config 기반 timeout 읽기
        1. 모니터 시작
        2. Seed pop → fuzz → Tx
        3. 모니터 종료
        4. 점수/상태 반환
        """

        self.running = True

        timing_timeout = float(self.cfg["fuzz"]["timing_timeout"])
        dbc_timeout = float(self.cfg["fuzz"]["dbc_timeout"])

        # 1. 모니터 시작
        self.monitor_manager.start_monitors(
            timing_timeout=timing_timeout,
            dbc_timeout=dbc_timeout
        )

        # 2. CAN Listener 시작
        if self.can:
            try:
                self.can.start_listener()
            except Exception as e:
                print(f"[!] CAN listener start failed: {e}")

        monitor_weights = {}  # Mutator 가중치 (추후 확장)

        # 3. fuzz loop
        while self.running:
            seed = self.queue.pop()
            if not seed:
                break

            self.fuzz_seed(seed, monitor_weights)
            time.sleep(0.2)

        # 4. CAN listener 종료
        if self.can:
            self.can.stop_listener()

        # 5. monitor 종료 대기
        self.monitor_manager.wait_for_completion()

        scores = self.monitor_manager.get_scores()
        completed = self.monitor_manager.get_completion_status()
        status = self.monitor_manager.get_status()

        self.queue.close()
        self.experiments.close()

        # 6. 결과 리턴
        return {
            "timing": {
                "score": scores["timing"],
                "completed": completed["timing"],
                "status": status["timing"],
            },
            "uds": {
                "score": scores["uds"],
                "completed": completed["uds"],
                "status": status["uds"],
            },
            "dbc": {
                "score": scores["dbc"],
                "completed": completed["dbc"],
                "status": status["dbc"],
            },
            "associations": self.associations,
            "feedback_plans": self.feedback_plans,
        }
