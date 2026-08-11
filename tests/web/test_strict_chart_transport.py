from __future__ import annotations

from chanlun.tools.cache_identity import source_fingerprint
from cl_app.services import chart_cache
from cl_app.services.chart_compute import strict_structure_history_fields


def _strict_payload(revision: str = "sha256:one") -> dict[str, object]:
    return {
        "schema": "chanlun-chart-structure",
        "structure_revision": revision,
        "snapshot_revision": revision + "-snapshot",
        "render_revision": revision + "-render",
    }


def test_cache_namespace_is_bound_to_the_current_strict_chart_schema() -> None:
    key = chart_cache._build_cache_key(
        "a",
        "SH.600519",
        "5m",
        {},
    )
    assert key.startswith(f"{source_fingerprint()}_")


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
