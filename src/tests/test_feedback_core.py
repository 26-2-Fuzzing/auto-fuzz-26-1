import tempfile
import unittest
from pathlib import Path

from src.experiments.repository import ExperimentRepository
from src.feedback.strategy_selector import StrategySelector
from src.models.experiment import (
    AnomalyObservation,
    AnomalyType,
    MutationRecord,
    TrialRecord,
)
from src.interface.base_interface import CANFrame
from src.monitor.multi_bus_detector import BaselineProfile, MultiBusWindowDetector


class FeedbackCoreTests(unittest.TestCase):
    def setUp(self):
        self.mutation = MutationRecord(
            source_bus="P-CAN",
            message_id=0x123,
            original_data=b"\x30\x00",
            mutated_data=b"\xff\x00",
            operator="boundary",
        )

    def test_mutation_derives_changed_positions(self):
        self.assertEqual(self.mutation.changed_bytes, (0,))
        self.assertTrue(self.mutation.changed_bits)

    def test_association_uses_control_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = ExperimentRepository(str(Path(directory) / "experiments.db"))
            repo.save_mutation(self.mutation)
            fingerprint = None
            for index in range(5):
                observations = []
                if index < 4:
                    observation = AnomalyObservation(
                        "B-CAN", 0x456, AnomalyType.TIMING,
                        evidence={"metric": "cycle_time"},
                    )
                    fingerprint = observation.fingerprint
                    observations = [observation]
                repo.save_trial(
                    TrialRecord(self.mutation.mutation_id, index, observations=observations)
                )
            for index in range(5):
                repo.save_trial(
                    TrialRecord(self.mutation.mutation_id, index, is_control=True)
                )

            summary = repo.association(self.mutation.mutation_id, fingerprint)
            self.assertEqual(summary.reproduction_rate, 0.8)
            self.assertEqual(summary.baseline_rate, 0.0)
            self.assertTrue(summary.eligible_for_feedback)
            repo.close()

    def test_timing_plan_generates_boundary_candidates(self):
        selector = StrategySelector(exploration_rate=0.0)
        observation = AnomalyObservation(
            "B-CAN", 0x456, AnomalyType.TIMING,
            evidence={"metric": "cycle_time"},
        )
        plan = selector.select(self.mutation, observation)
        candidates = selector.generate_followups(self.mutation, plan)
        self.assertEqual(plan.strategy, "value_boundary_search")
        self.assertGreater(len(candidates), 1)
        self.assertTrue(
            all(item.parameters["parent_mutation_id"] == self.mutation.mutation_id
                for item in candidates)
        )

    def test_multi_bus_detector_finds_cross_bus_new_id(self):
        baseline = BaselineProfile.from_frames(
            [
                CANFrame("B-CAN", 0x100, b"\x10", timestamp=0.0),
                CANFrame("B-CAN", 0x100, b"\x10", timestamp=0.1),
            ]
        )
        observations = MultiBusWindowDetector(baseline).detect(
            [
                CANFrame("B-CAN", 0x100, b"\x10", timestamp=1.0),
                CANFrame("B-CAN", 0x450, b"\x01", timestamp=1.01),
            ]
        )
        self.assertTrue(
            any(item.target_bus == "B-CAN" and item.target_id == 0x450
                and item.anomaly_type == AnomalyType.NEW_MESSAGE
                for item in observations)
        )


if __name__ == "__main__":
    unittest.main()
