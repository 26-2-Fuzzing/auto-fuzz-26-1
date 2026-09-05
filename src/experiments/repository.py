from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from ..models.experiment import (
    AssociationSummary,
    MutationRecord,
    TrialRecord,
)


class ExperimentRepository:
    """SQLite evidence store linking mutations, trials and observations."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mutations (
                mutation_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                source_bus TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                seed_id INTEGER,
                operator TEXT NOT NULL,
                original_data BLOB NOT NULL,
                mutated_data BLOB NOT NULL,
                changed_bytes TEXT NOT NULL,
                changed_bits TEXT NOT NULL,
                parameters TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trials (
                trial_id TEXT PRIMARY KEY,
                mutation_id TEXT NOT NULL REFERENCES mutations(mutation_id),
                trial_index INTEGER NOT NULL,
                is_control INTEGER NOT NULL,
                state_signature TEXT,
                started_at REAL NOT NULL,
                completed_at REAL
            );
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                fingerprint TEXT NOT NULL,
                target_bus TEXT NOT NULL,
                target_id INTEGER,
                anomaly_type TEXT NOT NULL,
                magnitude REAL NOT NULL,
                confidence REAL NOT NULL,
                evidence TEXT NOT NULL,
                observed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_observation_association
                ON observations(fingerprint, trial_id);
            """
        )
        self.conn.commit()

    def save_mutation(self, mutation: MutationRecord) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mutation.mutation_id,
                mutation.fingerprint,
                mutation.source_bus,
                mutation.message_id,
                mutation.seed_id,
                mutation.operator,
                mutation.original_data,
                mutation.mutated_data,
                json.dumps(mutation.changed_bytes),
                json.dumps(mutation.changed_bits),
                json.dumps(mutation.parameters),
                mutation.created_at,
            ),
        )
        self.conn.commit()

    def save_trial(self, trial: TrialRecord) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO trials VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                trial.trial_id,
                trial.mutation_id,
                trial.trial_index,
                int(trial.is_control),
                trial.state_signature,
                trial.started_at,
                trial.completed_at,
            ),
        )
        self.conn.executemany(
            """INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    obs.observation_id,
                    trial.trial_id,
                    obs.fingerprint,
                    obs.target_bus,
                    obs.target_id,
                    obs.anomaly_type.value,
                    obs.magnitude,
                    obs.confidence,
                    json.dumps(obs.evidence, default=str),
                    obs.observed_at,
                )
                for obs in trial.observations
            ],
        )
        self.conn.commit()

    def association(
        self, mutation_id: str, anomaly_fingerprint: str
    ) -> AssociationSummary:
        row = self.conn.execute(
            """
            SELECT
              SUM(CASE WHEN t.is_control = 0 THEN 1 ELSE 0 END),
              SUM(CASE WHEN t.is_control = 0 AND EXISTS (
                    SELECT 1 FROM observations o
                    WHERE o.trial_id = t.trial_id AND o.fingerprint = ?
                  ) THEN 1 ELSE 0 END),
              SUM(CASE WHEN t.is_control = 1 THEN 1 ELSE 0 END),
              SUM(CASE WHEN t.is_control = 1 AND EXISTS (
                    SELECT 1 FROM observations o
                    WHERE o.trial_id = t.trial_id AND o.fingerprint = ?
                  ) THEN 1 ELSE 0 END)
            FROM trials t WHERE t.mutation_id = ?
            """,
            (anomaly_fingerprint, anomaly_fingerprint, mutation_id),
        ).fetchone()
        values = tuple(int(value or 0) for value in row)
        return AssociationSummary(mutation_id, anomaly_fingerprint, *values)

    def close(self) -> None:
        self.conn.close()
