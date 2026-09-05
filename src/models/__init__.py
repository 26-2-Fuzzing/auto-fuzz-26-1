"""Backend-independent domain models used by the fuzzing pipeline."""

from .experiment import (
    AnomalyObservation,
    AnomalyType,
    AssociationSummary,
    MutationRecord,
    TrialRecord,
)

__all__ = [
    "AnomalyObservation",
    "AnomalyType",
    "AssociationSummary",
    "MutationRecord",
    "TrialRecord",
]
