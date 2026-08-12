from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataSnapshot,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.sector_first_scope import (
    build_sector_first_scope,
    current_gics_diagnostic_summary,
)
from tools.backtest_qmt_fixed_year import _catalog_scope


CN = ZoneInfo("Asia/Shanghai")


def _membership(code: str, sector: str, known: datetime) -> SectorMembershipChange:
    industry = sector.rsplit(":", 1)[-1]
    return SectorMembershipChange(
        code=code,
        sector_id=sector,
        sector_name=sector,
        industry_code=industry,
        source_changed_on=(known - timedelta(days=1)).date(),
        known_at=known,
    )


def test_scope_is_all_period_securities_then_point_in_time_sector_members() -> None:
    start = date(2025, 8, 1)
    end = date(2026, 7, 24)
    securities = tuple(sorted((
        SecurityMasterRecord("SH.600001", "A", date(2020, 1, 1), None),
        SecurityMasterRecord("SZ.000002", "B", date(2025, 9, 1), None),
        SecurityMasterRecord("SH.600003", "C", date(2020, 1, 1), date(2026, 1, 1)),
        SecurityMasterRecord("SZ.000004", "D", date(2020, 1, 1), None),
        SecurityMasterRecord("SH.600005", "OUT", date(2010, 1, 1), date(2024, 1, 1)),
    ), key=lambda value: value.code))
    memberships = (
        _membership("SH.600001", "qmt-sw1:S11", datetime(2020, 1, 2, tzinfo=CN)),
        _membership("SH.600003", "qmt-sw1:S22", datetime(2020, 1, 2, tzinfo=CN)),
        _membership("SZ.000002", "qmt-sw1:S11", datetime(2025, 9, 2, tzinfo=CN)),
    )
    snapshot = PITMetadataSnapshot(
        source_start=date(2025, 5, 1),
        source_end=end,
        captured_at=datetime(2026, 7, 27, tzinfo=CN),
        securities=securities,
        memberships=memberships,
        factors=(),
        qmt_sw1_sector_names=(
            ("qmt-sw1:S11", "one"),
            ("qmt-sw1:S22", "two"),
        ),
        source_hashes=(("fixture", "sha256:" + "1" * 64),),
    )

    scope = build_sector_first_scope(
        snapshot,
        requested_start=start,
        requested_end=end,
    )

    assert tuple(value.code for value in scope.symbols) == (
        "SH.600001",
        "SH.600003",
        "SZ.000002",
        "SZ.000004",
    )
    assert scope.selected_symbols == ("SH.600001", "SH.600003", "SZ.000002")
    assert scope.rejected_symbols == ("SZ.000004",)
    assert dict(scope.start_members_by_sector) == {
        "qmt-sw1:S11": ("SH.600001",),
        "qmt-sw1:S22": ("SH.600003",),
    }
    assert dict(scope.end_members_by_sector) == {
        "qmt-sw1:S11": ("SH.600001", "SZ.000002"),
        "qmt-sw1:S22": (),
    }
    assert scope.document()["pipeline"][0] == "POINT_IN_TIME_SECTOR_TRIGGER"

    replay_scope, catalog = _catalog_scope(
        snapshot,
        requested_start=start,
        requested_end=end,
    )
    assert tuple(code for code, _sector in replay_scope) == scope.selected_symbols
    assert catalog["selection_path"] == "INDIVIDUAL_THREE_PROGRAM"
    assert catalog["selection_order"][0] == "POINT_IN_TIME_SECTOR_TRIGGER"
    assert catalog["sector_first_scope_sha256"] == scope.content_sha256
    assert catalog["etf_proxy_role"] == "SEPARATE_COMPONENT_CONTROL_ONLY"


def test_current_gics_catalog_is_explicitly_diagnostic_only() -> None:
    summary = current_gics_diagnostic_summary(
        {
            "entries": [
                {
                    "captured_at": "2026-07-27T15:00:00+08:00",
                    "sectors": [
                        {"member_codes": ["SH.600001", "SZ.000002"]},
                        {"member_codes": ["SH.600001"]},
                    ],
                }
            ]
        }
    )

    assert summary["sector_count"] == 2
    assert summary["membership_edge_count"] == 3
    assert summary["unique_member_count"] == 2
    assert summary["historical_backfill_allowed"] is False
