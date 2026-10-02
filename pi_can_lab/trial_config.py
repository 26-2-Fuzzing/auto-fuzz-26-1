"""Shared conservative collection settings for local and distributed runners."""

from __future__ import annotations

import math
from typing import Any, Mapping

from can_common import ConfigurationError


TRIAL_CONTRACT_VERSION = 1
DEFAULT_TRIAL = {
    "baseline_seconds": 30.0,
    "normal_seconds": 10.0,
    "mutation_seconds": 1.0,
    "post_seconds": 20.0,
    "interval_ms": 50.0,
    "runner_timeout_seconds": 120.0,
    "watchdog_poll_seconds": 0.5,
}


def trial_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    settings = {**DEFAULT_TRIAL, **value}
    for key in DEFAULT_TRIAL:
        try:
            parsed = float(settings[key])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"trial.{key} must be a positive finite number") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ConfigurationError(f"trial.{key} must be a positive finite number")
        settings[key] = parsed
    if settings["normal_seconds"] < settings["mutation_seconds"]:
        raise ConfigurationError("normal_seconds must cover an equal-length mutation comparison window")
    if settings["mutation_seconds"] > 1.0:
        raise ConfigurationError("Calibration contract 1 limits mutation exposure to 1 second")
    if settings["interval_ms"] < 50.0:
        raise ConfigurationError("Calibration contract 1 requires interval_ms >= 50")
    frames = math.ceil(settings["normal_seconds"] * 1000 / settings["interval_ms"] - 1e-9)
    if frames > 200:
        raise ConfigurationError("Calibration contract 1 limits normal transmission to 200 frames")
    total = sum(settings[key] for key in (
        "baseline_seconds", "normal_seconds", "mutation_seconds", "post_seconds"
    ))
    if total > 120:
        raise ConfigurationError("Calibration contract 1 limits total phase duration to 120 seconds")
    if settings["runner_timeout_seconds"] <= total:
        raise ConfigurationError("runner_timeout_seconds must exceed total phase duration")
    return settings
