from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    MinuteBar,
    SectorMembershipAt,
    SecurityStatus,
)


CN = ZoneInfo("Asia/Shanghai")
BAR_OPEN = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
BAR_AT = BAR_OPEN + timedelta(minutes=1)
SESSION = date(2026, 7, 20)


def minute_bar(**overrides: Any) -> MinuteBar:
    values: dict[str, Any] = {
        "code": "SZ.000001",
        "opened_at": BAR_OPEN,
        "closed_at": BAR_AT,
        "raw_open": Decimal("10.00"),
        "raw_high": Decimal("10.30"),
        "raw_low": Decimal("9.90"),
        "raw_close": Decimal("10.20"),
        "analysis_open": Decimal("5.00"),
        "analysis_high": Decimal("5.15"),
        "analysis_low": Decimal("4.95"),
        "analysis_close": Decimal("5.10"),
        "previous_raw_close": Decimal("10.00"),
        "volume": Decimal("100000"),
        "turnover": Decimal("1000000"),
        "adjustment_known_at": BAR_AT,
    }
    decimal_fields = {
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_close",
        "analysis_open",
        "analysis_high",
        "analysis_low",
        "analysis_close",
        "previous_raw_close",
        "volume",
        "turnover",
    }
    values.update(
        {
            key: Decimal(value)
            if key in decimal_fields and isinstance(value, str)
            else value
            for key, value in overrides.items()
        }
    )
    if "opened_at" in overrides and "closed_at" not in overrides:
        values["closed_at"] = values["opened_at"] + timedelta(minutes=1)
    if (
        "opened_at" in overrides or "closed_at" in overrides
    ) and "adjustment_known_at" not in overrides:
        values["adjustment_known_at"] = values["closed_at"]
    return MinuteBar(**values)


def normal_status(**overrides: Any) -> SecurityStatus:
    values: dict[str, Any] = {
        "session": SESSION,
        "code": "SZ.000001",
        "listed": True,
        "st": False,
        "suspended": False,
        "limit_pct": Decimal("0.10"),
        "lot_size": 100,
        "t_plus_days": 1,
    }
    values.update(overrides)
    return SecurityStatus(**values)


def dataset(**overrides: Any) -> BacktestDataset:
    values: dict[str, Any] = {
        "bars": (minute_bar(),),
        "statuses": (normal_status(),),
        "memberships": (
            SectorMembershipAt(
                session=SESSION,
                sector_id="TDX.880301",
                code="SZ.000001",
                known_at=BAR_OPEN - timedelta(hours=1),
            ),
        ),
        "corporate_actions": (),
        "membership_as_of_each_session": True,
        "point_in_time_adjustment": True,
        "source_hashes": (("bars", "sha256:bars"),),
    }
    values.update(overrides)
    return BacktestDataset(**values)
