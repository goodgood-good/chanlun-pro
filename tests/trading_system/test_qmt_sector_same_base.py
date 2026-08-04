from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT,
    derive_qmt_sector_thirty_minute_frame,
)


CN = ZoneInfo("Asia/Shanghai")


def _closes(session: date) -> tuple[datetime, ...]:
    return tuple(
        datetime.combine(
            session,
            time(hour=minute // 60, minute=minute % 60),
            tzinfo=CN,
        )
        for start, end in (
            (9 * 60 + 35, 11 * 60 + 30),
            (13 * 60 + 5, 15 * 60),
        )
        for minute in range(start, end + 1, 5)
    )


def _frame(sessions: tuple[date, ...]) -> pd.DataFrame:
    dates = tuple(close for session in sessions for close in _closes(session))
    result = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0 + index / 100 for index in range(len(dates))],
            "high": [10.2 + index / 100 for index in range(len(dates))],
            "low": [9.8 + index / 100 for index in range(len(dates))],
            "close": [10.1 + index / 100 for index in range(len(dates))],
            "volume": [24 - index % 3 for index in range(len(dates))],
            "member_mask": [(1 << 24) - 1 for _ in dates],
        }
    )
    result.attrs.update(
        sector_id="qmt-gics3:test",
        sector_membership_revision="sha256:" + "a" * 64,
        sector_composite_members=tuple(f"SH.{600000 + index:06d}" for index in range(24)),
        sector_composite_member_path_revision="sha256:" + "b" * 64,
        price_basis_provider="qmt-gics3-composite",
        price_basis_adjustment="causal-factor-stable-24-member-median-v5",
        price_basis_revision="sha256:" + "c" * 64,
        structure_price_quantum="0.000001",
    )
    return result


def test_sector_thirty_minute_is_derived_from_six_completed_five_minute_bars() -> None:
    source = _frame((date(2026, 7, 20),))

    result = derive_qmt_sector_thirty_minute_frame(source)

    assert tuple(value.time() for value in result["date"]) == (
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(11, 30),
        time(13, 30),
        time(14, 0),
        time(14, 30),
        time(15, 0),
    )
    assert result.iloc[0]["open"] == source.iloc[0]["open"]
    assert result.iloc[0]["close"] == source.iloc[5]["close"]
    assert result.iloc[0]["high"] == source.iloc[:6]["high"].max()
    assert result.iloc[0]["low"] == source.iloc[:6]["low"].min()
    assert result.iloc[0]["volume"] == source.iloc[:6]["volume"].min()
    assert result.attrs["source_base_frequency"] == "5m"
    assert result.attrs["derived_frequency"] == "30m"
    assert result.attrs["sector_thirty_minute_derivation_contract"] == (
        QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT
    )
    assert result.attrs["source_base_stream_revision"].startswith("sha256:")


def test_oldest_count_boundary_suffix_is_not_reanchored_as_thirty_minutes() -> None:
    source = _frame((date(2026, 7, 20), date(2026, 7, 21)))
    bounded = source.iloc[38:].copy().reset_index(drop=True)
    bounded.attrs = dict(source.attrs)

    result = derive_qmt_sector_thirty_minute_frame(
        bounded,
        request_bars=8,
    )

    assert len(result) == 8
    assert set(result["date"].dt.date) == {date(2026, 7, 21)}
    assert result["date"].iloc[0].time() == time(10, 0)


def test_interior_five_minute_gap_cannot_form_a_sector_thirty_minute_bar() -> None:
    source = _frame((date(2026, 7, 20),)).drop(index=7).reset_index(drop=True)

    with pytest.raises(ValueError, match="calendar-grid prefix"):
        derive_qmt_sector_thirty_minute_frame(source)
