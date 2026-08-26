from __future__ import annotations

from datetime import datetime
import json

from tools.audit_strict_5m_partitions import (
    build_audit_document,
    load_universe_snapshot,
    stratified_symbols,
)


AS_OF = datetime.fromisoformat("2026-08-24T15:00:00+08:00")


def test_full_market_universe_uses_complete_discovery_manifest(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "screening_scope_mode": "FULL_MARKET",
                "admitted_universe_codes": ["SH.600000"],
                "market_data_as_of": AS_OF.isoformat(),
                "coverage_manifest": {
                    "complete": True,
                    "discovered_codes": ["SH.600000", "SZ.000001"],
                    "universe_revision": "sha256:" + "b" * 64,
                },
            }
        ),
        encoding="utf-8",
    )

    symbols, as_of, revision, source = load_universe_snapshot(snapshot)

    assert symbols == ("SH.600000", "SZ.000001")
    assert as_of == AS_OF
    assert revision == "sha256:" + "b" * 64
    assert source == "coverage_manifest.discovered_codes"


def _row(code: str, *, pending: bool = False) -> dict[str, object]:
    return {
        "code": code,
        "status": "ok",
        "board": "test",
        "level_zero": {
            "unit_count": 9,
            "center_count": 1,
            "physical_center_role_violation_count": 0,
            "movement_direction_alignment_violation_count": 0,
            "formal_trend_count": 1,
            "centerless_trend_count": 0,
            "pending_partition_count": int(pending),
            "pending_unit_count": 2 if pending else 0,
            "all_units_partitioned": True,
            "trends": [
                {
                    "kind": "consolidation",
                    "state": "locked",
                    "completion_basis": "center_lifecycle",
                    "center_count": 1,
                    "direction_aligned": True,
                }
            ],
            "pending_movements": (
                [{"role": "suffix", "unit_count": 2}] if pending else []
            ),
        },
    }


def test_stratified_sample_covers_every_board_before_repeating() -> None:
    symbols = (
        "SH.600002",
        "SZ.300002",
        "SH.688002",
        "SZ.000002",
        "SH.513100",
        "SH.600001",
        "SZ.300001",
        "SH.688001",
        "SZ.000001",
    )

    assert stratified_symbols(symbols, 5) == (
        "SH.513100",
        "SH.600001",
        "SH.688001",
        "SZ.000001",
        "SZ.300001",
    )


def test_full_universe_document_retains_every_symbol_and_aggregates_partitions() -> (
    None
):
    rows = (
        _row("SZ.000001", pending=True),
        _row("SH.600000"),
        {
            "code": "SH.600001",
            "status": "error",
            "board": "test",
            "error_type": "ValueError",
            "error": "bad structure",
        },
    )

    document = build_audit_document(
        rows=rows,
        as_of=AS_OF,
        universe_revision="sha256:" + "a" * 64,
        universe_symbol_count=3,
        selected_by_limit=False,
    )

    assert document["scope"] == "FULL_UNIVERSE"
    assert [row["code"] for row in document["symbols"]] == [
        "SH.600000",
        "SH.600001",
        "SZ.000001",
    ]
    assert document["summary"] == {
        "requested_symbol_count": 3,
        "successful_symbol_count": 2,
        "error_symbol_count": 1,
        "all_units_partitioned_symbol_count": 2,
        "symbols_with_pending_movements": 1,
        "symbols_without_formal_trends": 0,
        "total_level_zero_units": 18,
        "total_centers": 2,
        "physical_center_role_violation_count": 0,
        "movement_direction_alignment_violation_count": 0,
        "total_formal_trends": 2,
        "total_centerless_trends": 0,
        "total_pending_partitions": 1,
        "total_pending_units": 2,
        "trend_kind_counts": {"consolidation": 2},
        "trend_state_counts": {"locked": 2},
        "trend_completion_basis_counts": {"center_lifecycle": 2},
        "pending_role_counts": {"suffix": 1},
        "error_type_counts": {"ValueError": 1},
    }
    assert document["status"] == "PARTIAL"
    assert str(document["content_sha256"]).startswith("sha256:")
