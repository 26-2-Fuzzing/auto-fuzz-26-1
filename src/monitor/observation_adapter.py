from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..models.experiment import AnomalyObservation, AnomalyType


_TYPE_MAP = {
    "timing": AnomalyType.TIMING,
    "dbc": AnomalyType.SIGNAL_RANGE,
    "uds": AnomalyType.UDS_RESPONSE,
    "new_message": AnomalyType.NEW_MESSAGE,
    "message_disappearance": AnomalyType.MESSAGE_DISAPPEARANCE,
    "payload": AnomalyType.PAYLOAD,
    "cross_bus": AnomalyType.CROSS_BUS,
}


def events_to_observations(
    events: Iterable[Dict[str, Any]], target_bus: str
) -> List[AnomalyObservation]:
    """Convert legacy monitor events into the common anomaly contract."""
    observations: List[AnomalyObservation] = []
    for event in events:
        if str(event.get("status", "")).upper() not in {"FAIL", "WARN"}:
            continue
        value = event.get("value")
        magnitude = float(value) if isinstance(value, (int, float)) else 1.0
        observations.append(
            AnomalyObservation(
                target_bus=event.get("bus", target_bus),
                target_id=event.get("id"),
                anomaly_type=_TYPE_MAP.get(event.get("type"), AnomalyType.UNKNOWN),
                magnitude=abs(magnitude),
                confidence=float(event.get("confidence", 0.5)),
                evidence=dict(event),
            )
        )
    return observations
