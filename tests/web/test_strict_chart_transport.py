from __future__ import annotations

from cl_app.services import chart_cache
from cl_app.services.chart_compute import (
    _merge_chart_data,
    strict_structure_history_fields,
)


def _strict_payload(revision: str = "sha256:one") -> dict[str, object]:
    return {
        "schema": "chanlun-chart-structure/v4",
        "structure_revision": revision,
        "snapshot_revision": revision + "-snapshot",
        "render_revision": revision + "-render",
    }


def test_v39_cache_is_rejected_after_strict_schema_cutover() -> None:
    assert chart_cache._CHART_CACHE_SCHEMA_VERSION == "v40"
    assert not chart_cache._build_cache_key(
        "a",
        "SH.600519",
        "5m",
        {},
    ).startswith("v37_")


def test_historical_pagination_omits_structure_instead_of_replacing_it() -> None:
    fields = strict_structure_history_fields(
        {
            "strict_structure_mode": "replace",
            "strict_structure": _strict_payload(),
        },
        authoritative=False,
    )

    assert fields == {"strict_structure_mode": "unchanged"}


def test_current_authoritative_response_carries_atomic_replace_snapshot() -> None:
    strict = _strict_payload()

    fields = strict_structure_history_fields(
        {
            "strict_structure_mode": "replace",
            "strict_structure": strict,
        },
        authoritative=True,
    )

    assert fields == {
        "strict_structure_mode": "replace",
        "strict_structure": strict,
    }


def test_unavailable_authoritative_response_clears_prior_structure() -> None:
    error = {"code": "strict_evidence_invalid"}

    fields = strict_structure_history_fields(
        {
            "strict_structure_mode": "unavailable",
            "strict_structure_error": error,
        },
        authoritative=True,
    )

    assert fields == {
        "strict_structure_mode": "unavailable",
        "strict_structure_error": error,
    }


def test_merge_replaces_strict_snapshot_as_one_atomic_value() -> None:
    old = _strict_payload("sha256:old")
    new = _strict_payload("sha256:new")
    existing = {
        "t": [1],
        "strict_structure_mode": "replace",
        "strict_structure": old,
    }

    replaced = _merge_chart_data(
        existing,
        {
            "t": [2],
            "strict_structure_mode": "replace",
            "strict_structure": new,
        },
    )
    unchanged = _merge_chart_data(
        existing,
        {"t": [2], "strict_structure_mode": "unchanged"},
    )
    unavailable = _merge_chart_data(
        existing,
        {
            "t": [2],
            "strict_structure_mode": "unavailable",
            "strict_structure_error": {"code": "strict_evidence_invalid"},
        },
    )

    assert replaced["strict_structure"] is new
    assert unchanged["strict_structure"] is old
    assert "strict_structure" not in unavailable
    assert unavailable["strict_structure_mode"] == "unavailable"
