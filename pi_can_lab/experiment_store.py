"""Filesystem repository for immutable raw trials and cumulative feedback."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from trial_models import MutationCase, utc_now


FEEDBACK_SCHEMA_VERSION = 1


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class ExperimentStore:
    def __init__(self, root: Path, experiment_id: int, config_snapshot: Mapping[str, Any]):
        self.experiment_id = int(experiment_id)
        self.path = root.expanduser().resolve() / f"experiment_{self.experiment_id:04d}"
        self.path.mkdir(parents=True, exist_ok=True)
        self.feedback_path = self.path / "feedback_state.json"
        experiment_path = self.path / "experiment.json"
        if not experiment_path.exists():
            _atomic_json(experiment_path, {
                "schema_version": 1,
                "experiment_id": self.experiment_id,
                "created_at": utc_now(),
                "status": "running",
                "config": dict(config_snapshot),
            })
        if not self.feedback_path.exists():
            _atomic_json(self.feedback_path, self.empty_feedback_state())

    def empty_feedback_state(self) -> dict[str, Any]:
        return {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "total_trials": 0,
            "next_mutation_id": 1,
            "interesting_mutations": [],
            "mutation_history": [],
            "mutation_statistics": {},
            "last_feedback": None,
            "updated_at": utc_now(),
        }

    def load_feedback_state(self) -> dict[str, Any]:
        with self.feedback_path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if int(state.get("experiment_id", -1)) != self.experiment_id:
            raise ValueError("feedback_state experiment_id mismatch")
        return state

    def save_feedback_state(self, state: Mapping[str, Any]) -> None:
        updated = dict(state)
        updated["updated_at"] = utc_now()
        _atomic_json(self.feedback_path, updated)

    def next_trial_id(self) -> int:
        indexes = []
        for path in self.path.glob("trial_*"):
            if path.is_dir() and path.name.removeprefix("trial_").isdigit():
                indexes.append(int(path.name.removeprefix("trial_")))
        return max(indexes, default=0) + 1

    def next_mutation_id(self) -> int:
        identifiers = [int(self.load_feedback_state().get("next_mutation_id", 1)) - 1]
        for path in self.path.glob("trial_*/mutation.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    identifiers.append(int(json.load(handle)["mutation_id"]))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return max(identifiers, default=0) + 1

    def create_trial(self, trial_id: int) -> Path:
        path = self.path / f"trial_{int(trial_id):04d}"
        path.mkdir(parents=False, exist_ok=False)
        return path

    def write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        if path.exists():
            _atomic_json(path, value)
        else:
            _atomic_json(path, value)

    def record_completed_trial(
        self,
        mutation: MutationCase,
        feedback: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self.load_feedback_state()
        history = list(state.get("mutation_history", []))
        history.append(mutation.to_dict())
        state["mutation_history"] = history
        state["total_trials"] = int(state.get("total_trials", 0)) + 1
        state["next_mutation_id"] = max(
            int(state.get("next_mutation_id", 1)), mutation.mutation_id + 1
        )
        operator_stats = dict(state.get("mutation_statistics", {}))
        entry = dict(operator_stats.get(mutation.operator, {}))
        entry["executed"] = int(entry.get("executed", 0)) + 1
        entry["interesting"] = int(entry.get("interesting", 0)) + int(
            bool(feedback.get("interesting"))
        )
        operator_stats[mutation.operator] = entry
        state["mutation_statistics"] = operator_stats
        if feedback.get("interesting"):
            interesting = list(state.get("interesting_mutations", []))
            interesting.append({
                "mutation_id": mutation.mutation_id,
                "score": float(feedback.get("anomaly_score", 0.0)),
                "mutation": mutation.to_dict(),
                "anomaly_types": list(feedback.get("anomaly_types", [])),
                "trial_id": feedback.get("trial_id"),
            })
            interesting.sort(key=lambda item: (-float(item["score"]), int(item["mutation_id"])))
            state["interesting_mutations"] = interesting
        state["last_feedback"] = dict(feedback)
        self.save_feedback_state(state)
        return self.load_feedback_state()

    def complete(self) -> None:
        path = self.path / "experiment.json"
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        value["status"] = "completed"
        value["completed_at"] = utc_now()
        _atomic_json(path, value)
