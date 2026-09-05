from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict, Dict, Iterable, List, Set, Tuple

from ..interface.base_interface import CANFrame
from ..models.experiment import AnomalyObservation, AnomalyType

FrameKey = Tuple[str, int]


@dataclass
class BaselineProfile:
    """Traffic properties learned under a controlled, mutation-free window."""

    ids_by_bus: DefaultDict[str, Set[int]] = field(
        default_factory=lambda: defaultdict(set)
    )
    periods_ms: Dict[FrameKey, float] = field(default_factory=dict)
    byte_ranges: Dict[Tuple[str, int, int], Tuple[int, int]] = field(default_factory=dict)
    byte_sample_counts: Dict[Tuple[str, int, int], int] = field(default_factory=dict)

    @classmethod
    def from_frames(cls, frames: Iterable[CANFrame]) -> "BaselineProfile":
        profile = cls()
        timestamps: DefaultDict[FrameKey, List[float]] = defaultdict(list)
        byte_values: DefaultDict[Tuple[str, int, int], List[int]] = defaultdict(list)
        for frame in sorted(frames, key=lambda item: item.timestamp):
            key = (frame.bus, frame.arbitration_id)
            profile.ids_by_bus[frame.bus].add(frame.arbitration_id)
            timestamps[key].append(frame.timestamp)
            for index, value in enumerate(frame.data):
                byte_values[(frame.bus, frame.arbitration_id, index)].append(value)

        for key, values in timestamps.items():
            intervals = [(b - a) * 1000.0 for a, b in zip(values, values[1:])]
            if intervals:
                profile.periods_ms[key] = statistics.median(intervals)
        profile.byte_ranges = {
            key: (min(values), max(values)) for key, values in byte_values.items()
        }
        profile.byte_sample_counts = {
            key: len(values) for key, values in byte_values.items()
        }
        return profile


class MultiBusWindowDetector:
    """Pure anomaly detector: no SocketCAN, CANoe or thread dependency."""

    def __init__(
        self,
        baseline: BaselineProfile,
        timing_relative_tolerance: float = 0.25,
        minimum_expected_occurrences: int = 1,
        minimum_timing_intervals: int = 3,
        minimum_payload_samples: int = 5,
    ):
        self.baseline = baseline
        self.timing_relative_tolerance = timing_relative_tolerance
        self.minimum_expected_occurrences = minimum_expected_occurrences
        self.minimum_timing_intervals = minimum_timing_intervals
        self.minimum_payload_samples = minimum_payload_samples

    def detect(self, frames: Iterable[CANFrame]) -> List[AnomalyObservation]:
        window = sorted(frames, key=lambda item: item.timestamp)
        grouped: DefaultDict[FrameKey, List[CANFrame]] = defaultdict(list)
        seen_by_bus: DefaultDict[str, Set[int]] = defaultdict(set)
        for frame in window:
            grouped[(frame.bus, frame.arbitration_id)].append(frame)
            seen_by_bus[frame.bus].add(frame.arbitration_id)

        observations: List[AnomalyObservation] = []
        window_duration_ms = (
            (window[-1].timestamp - window[0].timestamp) * 1000.0
            if len(window) >= 2 else 0.0
        )
        for bus, seen_ids in seen_by_bus.items():
            for message_id in seen_ids - self.baseline.ids_by_bus.get(bus, set()):
                observations.append(
                    AnomalyObservation(
                        bus, message_id, AnomalyType.NEW_MESSAGE,
                        magnitude=1.0, confidence=0.8,
                        evidence={"metric": "new_message_id"},
                    )
                )

        for bus, baseline_ids in self.baseline.ids_by_bus.items():
            for message_id in baseline_ids:
                expected_period = self.baseline.periods_ms.get((bus, message_id))
                # Absence is meaningful only for periodic IDs observed over a
                # window long enough that the message should have appeared.
                if expected_period is None or window_duration_ms < expected_period * 1.5:
                    continue
                count = len(grouped.get((bus, message_id), []))
                if count < self.minimum_expected_occurrences:
                    observations.append(
                        AnomalyObservation(
                            bus, message_id, AnomalyType.MESSAGE_DISAPPEARANCE,
                            magnitude=float(self.minimum_expected_occurrences - count),
                            confidence=0.6,
                            evidence={"metric": "message_count", "observed": count},
                        )
                    )

        for key, expected in self.baseline.periods_ms.items():
            samples = grouped.get(key, [])
            intervals = [
                (b.timestamp - a.timestamp) * 1000.0
                for a, b in zip(samples, samples[1:])
            ]
            if len(intervals) < self.minimum_timing_intervals or expected <= 0:
                continue
            observed = statistics.median(intervals)
            relative_error = abs(observed - expected) / expected
            if relative_error > self.timing_relative_tolerance:
                observations.append(
                    AnomalyObservation(
                        key[0], key[1], AnomalyType.TIMING,
                        magnitude=relative_error, confidence=min(1.0, 0.5 + len(intervals) / 20),
                        evidence={
                            "metric": "median_period_ms",
                            "normal_period_ms": expected,
                            "observed_period_ms": observed,
                            "sample_count": len(intervals),
                        },
                    )
                )

        for frame in window:
            for index, value in enumerate(frame.data):
                bounds = self.baseline.byte_ranges.get(
                    (frame.bus, frame.arbitration_id, index)
                )
                sample_count = self.baseline.byte_sample_counts.get(
                    (frame.bus, frame.arbitration_id, index), 0
                )
                if (
                    bounds
                    and sample_count >= self.minimum_payload_samples
                    and not bounds[0] <= value <= bounds[1]
                ):
                    distance = min(abs(value - bounds[0]), abs(value - bounds[1]))
                    observations.append(
                        AnomalyObservation(
                            frame.bus, frame.arbitration_id, AnomalyType.PAYLOAD,
                            magnitude=float(distance), confidence=0.6,
                            evidence={
                                "metric": "byte_range", "byte": index,
                                "value": value, "baseline_min": bounds[0],
                                "baseline_max": bounds[1],
                            },
                        )
                    )
        return observations
