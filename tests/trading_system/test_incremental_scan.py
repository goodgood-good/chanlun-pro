from datetime import datetime
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.incremental_scan import (
    BarKey,
    ScanCursor,
    build_scan_plan,
)


CLOSED_AT = datetime(2026, 7, 20, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_one_changed_sector_bar_does_not_rescan_the_whole_market() -> None:
    plan = build_scan_plan(
        changed_bars=(BarKey("TDX.880301", "5m", CLOSED_AT),),
        sector_members={"TDX.880301": ("SH.600000", "SZ.000001")},
        active_watchlist=("SH.600519",),
        previous=ScanCursor.empty(),
    )

    assert set(plan.symbols) == {"SH.600000", "SZ.000001", "SH.600519"}
    assert plan.full_market_history_scan is False


def test_one_changed_stock_only_schedules_that_stock() -> None:
    plan = build_scan_plan(
        changed_bars=(BarKey("SH.600000", "5m", CLOSED_AT),),
        sector_members={"TDX.880301": ("SH.600000", "SZ.000001")},
        active_watchlist=(),
        previous=ScanCursor.current(),
    )

    assert plan.symbols == ("SH.600000",)
    assert plan.frequencies_for("SH.600000") == ("1m", "5m")


def test_daily_change_refreshes_all_four_independent_physical_periods() -> None:
    plan = build_scan_plan(
        changed_bars=(BarKey("SH.600000", "d", CLOSED_AT),),
        sector_members={},
        active_watchlist=(),
        previous=ScanCursor.current(),
    )

    assert plan.frequencies_for("SH.600000") == ("1m", "5m", "30m", "d")


def test_ineligible_sector_change_is_not_misclassified_as_a_stock() -> None:
    blocked_sector = "qmt-gics3:blocked"

    plan = build_scan_plan(
        changed_bars=(BarKey(blocked_sector, "5m", CLOSED_AT),),
        sector_members={"qmt-gics3:eligible": ("SH.600000",)},
        known_sector_ids=(blocked_sector, "qmt-gics3:eligible"),
        active_watchlist=(),
        previous=ScanCursor.current(),
    )

    assert plan.sectors == (blocked_sector,)
    assert plan.symbols == ()


def test_watchlist_and_holdings_remain_in_sell_risk_scope() -> None:
    plan = build_scan_plan(
        changed_bars=(),
        sector_members={},
        active_watchlist=("SH.600519",),
        holdings=("SZ.000001",),
        previous=ScanCursor.current(),
    )

    assert plan.symbols == ("SH.600519", "SZ.000001")
    assert all(plan.frequencies_for(code) == ("1m",) for code in plan.symbols)


def test_version_change_only_requests_background_refresh() -> None:
    plan = build_scan_plan(
        changed_bars=(),
        sector_members={},
        active_watchlist=(),
        previous=ScanCursor.current(
            structure_version="old",
            parameter_version="v1",
        ),
        structure_version="new",
        parameter_version="v1",
    )

    assert plan.full_market_history_scan is False
    assert plan.background_full_refresh_required is True


def test_scan_plan_deduplicates_and_sorts_deterministically() -> None:
    changed = (
        BarKey("SZ.000001", "1m", CLOSED_AT),
        BarKey("SZ.000001", "1m", CLOSED_AT),
        BarKey("TDX.880301", "30m", CLOSED_AT),
    )
    kwargs = dict(
        changed_bars=changed,
        sector_members={"TDX.880301": ("SZ.000001", "SH.600000")},
        active_watchlist=("SH.600000",),
        previous=ScanCursor.current(),
    )

    first = build_scan_plan(**kwargs)
    second = build_scan_plan(**kwargs)

    assert first == second
    assert first.symbols == tuple(sorted(set(first.symbols)))
    assert first.sectors == ("TDX.880301",)
    assert first.frequencies_for("SH.600000") == ("1m", "5m", "30m", "d")
