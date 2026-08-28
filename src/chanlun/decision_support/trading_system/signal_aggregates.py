"""Canonical aggregates for the current 5m trading-signal population."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from chanlun.decision_support.trading_system.models import CANONICAL_POINT_TYPES


POINT_DISTRIBUTION_CONTRACT_ID = "chanlun-5m-point-distribution-v1"
_CURRENT_LIFECYCLE_STAGES = frozenset(
    {"observed", "monitoring", "approaching", "triggered", "executable", "active"}
)
_CONFIRMED_LIFECYCLE_STAGES = frozenset({"triggered", "executable", "active"})
_BUCKETS = (
    "all_signals",
    "candidate",
    "operational_confirmed",
    "audit_locked",
    "executable",
)


def _empty_bucket() -> dict[str, object]:
    return {
        "total": 0,
        "counts_by_point_type": {
            point_type: 0 for point_type in CANONICAL_POINT_TYPES
        },
    }


def _increment(bucket: dict[str, object], point_type: str) -> None:
    bucket["total"] = int(bucket["total"]) + 1
    counts = bucket["counts_by_point_type"]
    if not isinstance(counts, dict):  # pragma: no cover - construction invariant
        raise TypeError("point distribution bucket is invalid")
    counts[point_type] = int(counts[point_type]) + 1


def point_distribution_document(
    signals: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Separate candidates from confirmed, locked and executable 5m points.

    The six canonical point types belong exclusively to the 5m trading level.
    A 1m nesting witness refines execution timing and is deliberately absent
    from these counts.
    """

    buckets = {name: _empty_bucket() for name in _BUCKETS}
    for signal in signals:
        point_type = signal.get("point_type")
        if point_type not in CANONICAL_POINT_TYPES:
            continue
        stage = str(signal.get("lifecycle_stage") or "")
        if stage not in _CURRENT_LIFECYCLE_STAGES:
            continue
        setup = signal.get("setup_5m")
        setup_mapping = setup if isinstance(setup, Mapping) else {}
        setup_status = str(setup_mapping.get("status") or "")
        formation_state = str(setup_mapping.get("formation_state") or "")
        operational_confirmed = (
            setup_status == "confirmed"
            or formation_state == "confirmed"
            or stage in _CONFIRMED_LIFECYCLE_STAGES
        )

        _increment(buckets["all_signals"], point_type)
        if operational_confirmed:
            _increment(buckets["operational_confirmed"], point_type)
            if setup_mapping.get("lock_state") == "locked":
                _increment(buckets["audit_locked"], point_type)
            if signal.get("entry_allowed") is True or signal.get("exit_allowed") is True:
                _increment(buckets["executable"], point_type)
        else:
            _increment(buckets["candidate"], point_type)

    return {
        "contract_id": POINT_DISTRIBUTION_CONTRACT_ID,
        "trading_level": "5m",
        "precision_level": "1m",
        "precision_points_included": False,
        **buckets,
    }


__all__ = (
    "POINT_DISTRIBUTION_CONTRACT_ID",
    "point_distribution_document",
)
