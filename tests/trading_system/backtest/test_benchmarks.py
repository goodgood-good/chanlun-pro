from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from chanlun.decision_support.trading_system.backtest.benchmarks import (
    build_required_benchmarks,
)
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    SectorMembershipAt,
)
from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_BENCHMARK_IDS,
)
from tests.trading_system.backtest.helpers import CN, minute_bar, normal_status


def _benchmark_dataset() -> BacktestDataset:
    sessions = (date(2026, 7, 20), date(2026, 7, 21))
    codes = ("SH.600000", "SZ.000001")
    closes = {
        (sessions[0], codes[0]): "10",
        (sessions[1], codes[0]): "11",
        (sessions[0], codes[1]): "20",
        (sessions[1], codes[1]): "20",
    }
    bars = []
    statuses = []
    memberships = []
    for session in sessions:
        for code in codes:
            opened_at = datetime.combine(
                session,
                datetime.min.time(),
                tzinfo=CN,
            ) + timedelta(hours=15)
            close = Decimal(closes[(session, code)])
            bars.append(
                minute_bar(
                    code=code,
                    opened_at=opened_at,
                    raw_open=close,
                    raw_high=close,
                    raw_low=close,
                    raw_close=close,
                    analysis_open=close,
                    analysis_high=close,
                    analysis_low=close,
                    analysis_close=close,
                    previous_raw_close=close,
                )
            )
            statuses.append(normal_status(code=code, session=session))
            memberships.append(
                SectorMembershipAt(
                    session=session,
                    sector_id="tdx-industry:SH.880301",
                    code=code,
                    known_at=opened_at - timedelta(hours=6),
                )
            )
    return BacktestDataset(
        bars=tuple(bars),
        statuses=tuple(statuses),
        memberships=tuple(memberships),
        corporate_actions=(),
        membership_as_of_each_session=True,
        point_in_time_adjustment=True,
        source_hashes=(("fixture", "sha256:fixture"),),
    )


def test_required_benchmarks_compute_point_in_time_equal_weight() -> None:
    rows = build_required_benchmarks(
        _benchmark_dataset(),
        data_grade="certified",
    )

    assert tuple(row.benchmark_id for row in rows) == REQUIRED_BENCHMARK_IDS
    equal_weight = rows[-1]
    assert equal_weight.data_grade == "certified"
    assert equal_weight.net_return == Decimal("0.05")
    assert equal_weight.max_drawdown == Decimal("0")
    assert rows[0].data_grade == rows[1].data_grade == "invalid"
    assert all("old" not in row.benchmark_id for row in rows)
