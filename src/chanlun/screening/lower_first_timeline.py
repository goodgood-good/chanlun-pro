"""U295: nontrading evidence that a lower structural point was known first.

L105:2719-2731 and L105:2794-2809 place a small-grade interval-suite entry
before the later test of parent critical prices. A 1m formal point is only a
structural prerequisite here; it is not itself certified as that interval
suite, an entry, or a parent-price hold/withdraw decision.
"""

from __future__ import annotations

from decimal import Decimal

from chanlun.core.strict_structure.identity import stable_structure_id


_PROOF_FIELDS = (
    "point_id", "point_type", "side", "status", "structural_level",
    "source_kind", "price_basis_revision", "anchor_unit_id", "anchor_at",
    "anchor_tick", "confirmed_at", "available_at", "center_id",
    "parent_point_id", "evidence_codes", "missing_conditions",
)


def _point(snapshot: dict, identifier: str) -> dict | None:
    return next((
        point for level in snapshot.get("levels", ())
        for point in level.get("points", ())
        if point.get("point_id") == identifier
    ), None)


def observe_independent_lower_structural_witnesses(
    snapshot: dict, *, known_from: int,
) -> tuple[dict, ...]:
    """Preserve recent confirmed 1m buy-point facts before any 5m selection.

    A historical point can remain in this ledger after it ceases to be a
    current selection candidate. Its presence alone proves no interval-suite
    entry or parent-price follow-up.
    """

    if (
        snapshot.get("source_frequency") != "1m"
        or not snapshot.get("symbol")
        or not snapshot.get("price_basis_revision")
        or type(snapshot.get("source_closed_at")) is not int
        or type(known_from) is not int
        or known_from > snapshot["source_closed_at"]
    ):
        raise ValueError("U295 lower source or witness window is invalid")
    output = []
    for level in snapshot.get("levels", ()):
        for point in level.get("points", ()):
            known_at = point.get("available_at")
            if (
                point.get("status") != "confirmed"
                or point.get("point_type") not in {"1buy", "2buy", "3buy"}
                or point.get("side") != "buy"
                or point.get("source_kind") != "segment"
                or point.get("structural_level") != 0
                or point.get("price_basis_revision")
                != snapshot["price_basis_revision"]
                or type(point.get("anchor_at")) is not int
                or type(point.get("anchor_tick")) is not int
                or type(known_at) is not int
                or not known_from <= known_at <= snapshot["source_closed_at"]
            ):
                continue
            output.append({
                "rule_id": "U295",
                "rule_source_refs": ["L105:2719-2731", "L105:2794-2809"],
                "record_id": stable_structure_id(
                    "u295-independent-lower-structural-witness-v1",
                    snapshot["symbol"], snapshot["price_basis_revision"],
                    point["point_id"], known_at,
                ),
                "status": "lower_formal_buy_point_only",
                "symbol": snapshot["symbol"],
                "source_frequency": "1m",
                "price_basis_revision": snapshot["price_basis_revision"],
                "point_id": point["point_id"],
                "point_type": point["point_type"],
                "anchor_at": point["anchor_at"],
                "anchor_tick": point["anchor_tick"],
                "known_at": known_at,
                "source_closed_at": snapshot["source_closed_at"],
                "source_snapshot_revision": snapshot.get("snapshot_revision"),
                "historical_structural_witness": True,
                "independent_lower_point_source": True,
                "independent_lower_first_selection_proven": False,
                "full_interval_nesting_certified": False,
                "author_interval_entry_certified": False,
                "parent_critical_price_followup_certified": False,
                "current_selection_eligible": None,
                "missing_conditions": [
                    "full_interval_nesting_certificate",
                    "parent_critical_price_context",
                    "parent_critical_price_followup",
                ],
                "tradable": False,
            })
    return tuple(sorted(output, key=lambda item: (item["known_at"], item["point_id"])))


def observe_lower_first_structural_timing(
    main_snapshot: dict,
    lower_snapshot: dict,
    *,
    main_point_id: str,
    lower_point_id: str,
    lower_prefix_snapshot: dict | None = None,
    main_prefix_snapshot: dict | None = None,
    association_interval: dict | None = None,
    selection_strategy: str = "5m_with_1m_confirmation",
) -> dict:
    """Verify source, spatial relation, lower confirmation and parent absence.

    The four snapshots are exact current-code sources. Passing a lower prefix
    proves the lower point existed without later bars; passing a main prefix
    proves the parent point did not yet exist at that clock. Neither proof
    upgrades a single lower point into the author's full interval-suite entry.
    """

    if (
        main_snapshot.get("source_frequency") != "5m"
        or lower_snapshot.get("source_frequency") != "1m"
        or not main_snapshot.get("symbol")
        or main_snapshot["symbol"] != lower_snapshot.get("symbol")
        or main_snapshot.get("price_basis_revision")
        != lower_snapshot.get("price_basis_revision")
        or Decimal(str(main_snapshot.get("structure_price_quantum")))
        != Decimal(str(lower_snapshot.get("structure_price_quantum")))
        or type(main_snapshot.get("source_closed_at")) is not int
        or main_snapshot["source_closed_at"]
        != lower_snapshot.get("source_closed_at")
    ):
        raise ValueError("U295 paired source identity is inconsistent")
    main = _point(main_snapshot, main_point_id)
    lower = _point(lower_snapshot, lower_point_id)
    if main is None or lower is None:
        raise ValueError("U295 point is missing from its own source snapshot")
    if (
        any(point.get("status") != "confirmed"
            or point.get("source_kind") != "segment"
            or point.get("structural_level") != 0
            or type(point.get("available_at")) is not int
            or point["available_at"] > main_snapshot["source_closed_at"]
            or point.get("price_basis_revision")
            != main_snapshot["price_basis_revision"]
            for point in (main, lower))
        or main.get("side") != "buy" or lower.get("side") != "buy"
        or main.get("point_type") not in {"1buy", "2buy", "3buy"}
        or lower.get("point_type") not in {"1buy", "2buy", "3buy"}
    ):
        raise ValueError("U295 requires comparable confirmed buy segment points")
    same_price_anchor = (
        lower.get("anchor_at") == main.get("anchor_at")
        and lower.get("anchor_tick") == main.get("anchor_tick")
    )
    inside_declared_interval = (
        association_interval is not None
        and type(association_interval.get("start_after")) is int
        and type(association_interval.get("end_at")) is int
        and association_interval["start_after"]
        < lower.get("anchor_at", -1) <= association_interval["end_at"]
        and association_interval["end_at"] == main.get("anchor_at")
    )
    if not same_price_anchor and not inside_declared_interval:
        raise ValueError("U295 lower point has no proven parent interval association")
    lower_first = lower["available_at"] < main["available_at"]
    record_id = stable_structure_id(
        "u295-lower-first-structural-timing-v1",
        main_snapshot["symbol"], main_snapshot["price_basis_revision"],
        lower_point_id, main_point_id, lower["available_at"],
        main["available_at"],
    )
    base = {
        "rule_id": "U295",
        "rule_source_refs": ["L105:2719-2731", "L105:2794-2809"],
        "record_id": record_id,
        "symbol": main_snapshot["symbol"],
        "price_basis_revision": main_snapshot["price_basis_revision"],
        "structure_price_quantum": str(main_snapshot["structure_price_quantum"]),
        "side": lower["side"],
        "lower_point_id": lower_point_id,
        "main_point_id": main_point_id,
        "lower_point_known_at": lower["available_at"],
        "main_point_known_at": main["available_at"],
        "final_source_closed_at": main_snapshot["source_closed_at"],
        "main_snapshot_revision": main_snapshot.get("snapshot_revision"),
        "lower_snapshot_revision": lower_snapshot.get("snapshot_revision"),
        "association_basis": (
            "same_price_anchor" if same_price_anchor else "declared_parent_interval"
        ),
        "selection_strategy": selection_strategy,
        "independent_lower_first_selection_proven": False,
        "full_interval_nesting_certified": False,
        "author_interval_entry_certified": False,
        "parent_critical_price_followup_certified": False,
        "formal_point_from_observation": False,
        "tradable": False,
    }
    if not lower_first:
        return {
            **base, "status": "lower_point_not_known_before_main",
            "lower_prefix_replayed": False,
            "main_absent_at_lower_clock": False,
            "missing_conditions": ["lower_point_before_parent"],
        }
    if lower_prefix_snapshot is None or main_prefix_snapshot is None:
        return {
            **base, "status": "pending_causal_prefix_replay",
            "lower_prefix_replayed": False,
            "main_absent_at_lower_clock": False,
            "missing_conditions": ["lower_prefix_replay", "main_absence_replay",
                                   "full_interval_nesting_certificate",
                                   "parent_critical_price_followup"],
        }
    for snapshot, frequency in (
        (lower_prefix_snapshot, "1m"), (main_prefix_snapshot, "5m")
    ):
        if (
            snapshot.get("symbol") != main_snapshot["symbol"]
            or snapshot.get("source_frequency") != frequency
            or snapshot.get("price_basis_revision")
            != main_snapshot["price_basis_revision"]
            or Decimal(str(snapshot.get("structure_price_quantum")))
            != Decimal(str(main_snapshot["structure_price_quantum"]))
            or type(snapshot.get("source_closed_at")) is not int
            or snapshot["source_closed_at"] > lower["available_at"]
        ):
            raise ValueError("U295 prefix source or clock is inconsistent")
    replayed = _point(lower_prefix_snapshot, lower_point_id)
    lower_proof_matches = (
        replayed is not None
        and replayed.get("available_at") <= lower_prefix_snapshot["source_closed_at"]
        and all(replayed.get(key) == lower.get(key) for key in _PROOF_FIELDS)
    )
    main_absent = _point(main_prefix_snapshot, main_point_id) is None
    status = (
        "lower_structural_point_before_parent"
        if lower_proof_matches and main_absent
        else "lower_prefix_proof_mismatch"
        if not lower_proof_matches
        else "parent_point_already_present_at_lower_clock"
    )
    return {
        **base,
        "status": status,
        "lower_prefix_replayed": lower_proof_matches,
        "main_absent_at_lower_clock": main_absent,
        "lower_prefix_source_closed_at": lower_prefix_snapshot["source_closed_at"],
        "main_prefix_source_closed_at": main_prefix_snapshot["source_closed_at"],
        "lower_prefix_snapshot_revision": lower_prefix_snapshot.get("snapshot_revision"),
        "main_prefix_snapshot_revision": main_prefix_snapshot.get("snapshot_revision"),
        "missing_conditions": (
            ["full_interval_nesting_certificate", "parent_critical_price_followup"]
            if status == "lower_structural_point_before_parent"
            else [status]
        ),
    }


__all__ = (
    "observe_independent_lower_structural_witnesses",
    "observe_lower_first_structural_timing",
)
