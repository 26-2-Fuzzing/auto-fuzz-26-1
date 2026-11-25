"""Bounded, read-only per-window telemetry summaries for recorded CAN frames."""

import math
import statistics
from array import array
from dataclasses import dataclass, field


MAX_EXACT_INTERVALS = 4096
MAX_EXACT_PAYLOADS = 4096


@dataclass
class Activity:
    """Accumulate one window; callers keep different buses/IDs/windows separate."""

    count: int = 0
    first_s: float | None = None
    last_s: float | None = None
    intervals: int = 0
    mean_interval_s: float = 0.0
    squared_deviations: float = 0.0
    max_interval_s: float = 0.0
    track_frame_details: bool = False
    min_interval_s: float | None = None
    _interval_samples: array | None = field(default=None, init=False, repr=False)
    _distinct_payloads: set[bytes] | None = field(default=None, init=False, repr=False)
    _last_payload: bytes | None = field(default=None, init=False, repr=False)
    _payload_samples: int = field(default=0, init=False, repr=False)
    _payload_changes: int = field(default=0, init=False, repr=False)
    _payloads_truncated: bool = field(default=False, init=False, repr=False)

    def add(self, timestamp: float, payload: bytes | None = None) -> None:
        if self.last_s is not None and timestamp >= self.last_s:
            gap = timestamp - self.last_s
            self.intervals += 1
            delta = gap - self.mean_interval_s
            self.mean_interval_s += delta / self.intervals
            self.squared_deviations += delta * (gap - self.mean_interval_s)
            self.max_interval_s = max(self.max_interval_s, gap)
            if self.min_interval_s is None or gap < self.min_interval_s:
                self.min_interval_s = gap
            if self.track_frame_details and self.intervals <= MAX_EXACT_INTERVALS:
                if self._interval_samples is None:
                    self._interval_samples = array("d")
                self._interval_samples.append(gap)
        if self.first_s is None:
            self.first_s = timestamp
        self.last_s = timestamp
        self.count += 1

        if self.track_frame_details:
            if payload is not None:
                payload = bytes(payload)
                self._payload_samples += 1
                if self._last_payload is not None and self._last_payload != payload:
                    self._payload_changes += 1
                if not self._payloads_truncated:
                    if self._distinct_payloads is None:
                        self._distinct_payloads = set()
                    if payload not in self._distinct_payloads:
                        if len(self._distinct_payloads) < MAX_EXACT_PAYLOADS:
                            self._distinct_payloads.add(payload)
                        else:
                            self._payloads_truncated = True
            # A missing payload breaks adjacency rather than bridging its gap.
            self._last_payload = payload

    @property
    def jitter(self) -> float | None:
        """Sample standard deviation in seconds, matching existing CANoe rules."""
        if self.intervals > 1:
            return math.sqrt(max(0.0, self.squared_deviations / (self.intervals - 1)))
        return None

    def report(self, duration: float) -> dict:
        hz = self.count / duration
        mean = self.mean_interval_s * 1000 if self.intervals else None
        maximum = self.max_interval_s * 1000 if self.intervals else None
        minimum = self.min_interval_s * 1000 if self.min_interval_s is not None else None
        sample_stddev = self.jitter
        population_stddev = (
            math.sqrt(max(0.0, self.squared_deviations / self.intervals)) * 1000
            if self.intervals else None
        )
        median_truncated = self.track_frame_details and self.intervals > MAX_EXACT_INTERVALS
        median = (
            statistics.median(self._interval_samples) * 1000
            if self._interval_samples and not median_truncated else None
        )
        complete_payloads = self.track_frame_details and self._payload_samples == self.count
        distinct_count = len(self._distinct_payloads or ())
        return dict(
            count=self.count, hz=hz, first_s=self.first_s, last_s=self.last_s,
            interval_count=self.intervals, mean_interval_ms=mean, max_interval_ms=maximum,
            jitter_ms=sample_stddev * 1000 if sample_stddev is not None else None,
            message_count=self.count, frequency_hz=hz,
            mean_cycle_time_ms=mean, median_cycle_time_ms=median,
            cycle_time_stddev_ms=population_stddev,
            minimum_cycle_time_ms=minimum, maximum_cycle_time_ms=maximum,
            median_cycle_time_truncated=median_truncated,
            payload_sample_count=self._payload_samples if self.track_frame_details else None,
            payload_unique_count=(
                distinct_count if complete_payloads and not self._payloads_truncated else None
            ),
            payload_unique_count_at_least=(
                distinct_count + int(self._payloads_truncated)
                if self.track_frame_details and (self._payloads_truncated or not complete_payloads)
                else None
            ),
            payload_unique_truncated=self._payloads_truncated,
            payload_change_count=self._payload_changes if complete_payloads else None,
        )
