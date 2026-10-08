"""Check a serialized first-class point against its actual comparison center.

This is an evidence-identity invariant for downstream readers. L103:514-520
describes a bounded warrant case in which the later comparison uses the new
center; the author did not define these JSON fields or a universal upgrade
formula. Formal center construction remains in the strict structure engine.
"""

from __future__ import annotations


def first_class_comparison_basis_valid(point: dict, center: dict | None) -> bool:
    """Reject a first-class reference whose trend comparison names another center."""

    if not isinstance(point, dict) or point.get("point_type") not in {"1buy", "1sell"}:
        return False
    if not isinstance(center, dict):
        return False
    divergence = point.get("divergence")
    if not isinstance(divergence, dict) or divergence.get("kind") != "trend":
        return False
    center_id = point.get("center_id")
    if (not isinstance(center_id, str) or not center_id
            or center.get("center_id") != center_id
            or divergence.get("comparison_center_id") != center_id):
        return False
    for field in ("structural_level", "source_kind", "price_basis_revision"):
        if (point.get(field) != center.get(field)
                or point.get(field) != divergence.get(field)):
            return False
    established = center.get("established_at")
    divergence_established = divergence.get("comparison_center_established_at")
    comparison_known = divergence.get("comparison_center_known_at")
    divergence_available = divergence.get("available_at")
    point_available = point.get("available_at")
    if point.get("status") == "confirmed" and (
        not isinstance(divergence.get("divergence_id"), str)
        or center.get("boundary_divergence_id") != divergence["divergence_id"]
        or center.get("boundary_anchor_unit_id") != divergence.get("signal_unit_id")
    ):
        return False
    return (
        all(type(value) is int for value in (
            established, divergence_established, comparison_known,
            divergence_available, point_available,
        ))
        and established == divergence_established
        and established <= comparison_known <= divergence_available <= point_available
    )
