"""Recover historical symbol membership without publishing old-rule signals."""

from chanlun.screening.confirmation import (
    confirmation_catalog,
    native_selection_lifetime_reasons,
)
from chanlun.screening.evidence import load_evidence
from chanlun.core.strict_structure.native_center_signals import NATIVE_SIGNAL_RULE_BASIS


def historical_native_period(directory, row, revision):
    candidates = row.get("selected") or []
    versions = {
        NATIVE_SIGNAL_RULE_BASIS,
        *(f"user_declared_native_center_macd_v{n}" for n in (1, 2, 3)),
    }
    if (
        not revision
        or not candidates
        or any(
            c.get("point", {}).get("evidence_scope") != "native_center_signal"
            or c.get("point", {}).get("rule_basis") not in versions
            for c in candidates
        )
    ):
        return None
    _, snapshot, health = load_evidence(
        directory, row["code"], row["frequency"], row.get("evidence", {})
    )
    if (
        not health["verified"]
        or snapshot.get("analysis_scope") != "native_centers"
        or type(row.get("source_closed_at")) is not int
        or row["source_closed_at"] != snapshot["source_closed_at"]
    ):
        raise ValueError("historical pool evidence is incomplete")
    points = {
        p["point_id"]: p
        for level in snapshot["levels"]
        for p in level.get("points", [])
    }
    centers = {
        c["center_id"]: c
        for level in snapshot["levels"]
        for c in level.get("centers", [])
    }
    for candidate in candidates:
        point = candidate["point"]
        if (
            points.get(point.get("point_id")) != point
            or centers.get(point.get("center_id")) != candidate.get("center")
            or point.get("source_kind") != "segment"
            or point.get("structural_level") != 0
            or point.get("status") != "confirmed"
            or point.get("formal_point_issued") is not True
            or candidate.get("reasons") != []
            or any(
                candidate.get("audit", {}).get(key) is not True
                for key in (
                    "cold_rebuild",
                    "confirmation_replay",
                    "confirmation_boundary",
                )
            )
        ):
            raise ValueError("historical candidate disagrees with its saved proof")
    following = confirmation_catalog(snapshot)
    # This tests the historical selection window only. It does not certify
    # those points under today's geometry; the live worker must recalculate.
    historical_points = {
        identity: p.get("evidence_scope") == "native_center_signal"
        and p.get("rule_basis") in versions
        and p.get("status") == "confirmed"
        for identity, p in points.items()
    }
    if not any(
        not native_selection_lifetime_reasons(
            c["point"], points.values(), following, historical_points
        )
        for c in candidates
    ):
        return None
    return {
        "market": row.get("market", "a"),
        "code": row["code"],
        "name": row.get("name", row["code"]),
        "frequency": row["frequency"],
        "cutoff": row["source_closed_at"],
        "revision": revision,
    }
