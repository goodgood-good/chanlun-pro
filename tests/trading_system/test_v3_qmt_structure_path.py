from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalStructureEventLedger,
)
from chanlun.decision_support.trading_system.v3_qmt_same_base_stream import (
    QmtSameBaseStreamFrames,
)
from chanlun.decision_support.trading_system.v3_qmt_structure_path import (
    build_qmt_v3_structure_path,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    CompletedL1TrendFact,
)
from tests.trading_system.helpers import POINT_AT, confirmed_point


BASE = "sha256:" + "b" * 64
BASIS = "test-raw-v1"


def _frame(frequency: str, *, minutes_after: int = 120) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": [POINT_AT + timedelta(minutes=minutes_after)],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            "volume": [1000.0],
        }
    )
    frame.attrs.update(
        {
            "source_base_stream_revision": BASE,
            "derived_frequency": frequency,
            "structure_price_quantum": "0.01",
            "price_basis_provider": "qmt",
            "price_basis_adjustment": "front",
            "price_basis_revision": BASIS,
        }
    )
    return frame


def _source() -> QmtSameBaseStreamFrames:
    return QmtSameBaseStreamFrames(
        symbol="SH.600000",
        observed_at=POINT_AT + timedelta(minutes=120),
        one_minute=_frame("1m"),
        five_minute=_frame("5m"),
        thirty_minute=_frame("30m"),
        daily=_frame("d"),
        source_base_stream_revision=BASE,
        price_basis_revision=BASIS,
        complete_sessions=(date(2026, 7, 20),),
        partial_session=None,
        session_issues=(),
        grade="FULL_SYSTEM_ELIGIBLE",
        blockers=(),
    )


def _trend(
    trend_id: str,
    direction: str,
    *,
    start: int,
    end: int,
    available: int,
    start_price: str,
    end_price: str,
    low: str,
) -> CompletedL1TrendFact:
    return CompletedL1TrendFact(
        trend_id=trend_id,
        source_frequency="5m",
        recursive_level=0,
        price_basis_revision=BASIS,
        direction=direction,
        market_start=POINT_AT + timedelta(minutes=start),
        market_end=POINT_AT + timedelta(minutes=end),
        confirmed_at=POINT_AT + timedelta(minutes=available),
        available_at=POINT_AT + timedelta(minutes=available),
        start_price=Decimal(start_price),
        end_price=Decimal(end_price),
        low_price=Decimal(low),
        high_price=Decimal("10.3"),
        terminal_start=POINT_AT + timedelta(minutes=end - 5),
        terminal_end=POINT_AT + timedelta(minutes=end),
        evidence_unit_ids=(f"{trend_id}-a", f"{trend_id}-b"),
    )


def _point_ledger(point) -> CausalStructureEventLedger:
    """Mock the causal point with the exact anchor lineage now required."""

    unit_id = f"unit:{point.point_id}"
    unit = SimpleNamespace(
        unit_id=unit_id,
        structural_level=point.recursive_level,
        available_at=point.available_at,
    )
    return CausalStructureEventLedger(
        points=(point,),
        completed_trends=(),
        completed_units=(unit,),
        point_anchor_unit_ids=((point.point_id, unit_id),),
    )


def test_same_base_path_routes_existing_causal_ledgers_into_shared_snapshot(
    monkeypatch,
) -> None:
    l0 = confirmed_point(
        "3buy",
        frequency="30m",
        center_zd=9.0,
        center_zg=9.8,
        center_ordinal=1,
        available_minutes_after=120,
    )
    locator = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=58,
        available_minutes_after=7,
    )
    departure = _trend(
        "departure",
        "up",
        start=5,
        end=30,
        available=35,
        start_price="9.5",
        end_price="10.2",
        low="9.4",
    )
    returned = _trend(
        "return",
        "down",
        start=30,
        end=60,
        available=65,
        start_price="10.2",
        end_price="9.9",
        low="9.8",
    )
    raw_departure = SimpleNamespace(
        trend_id="departure",
        available_at=departure.available_at,
        structural_level=0,
        complete=True,
    )
    raw_return = SimpleNamespace(
        trend_id="return",
        available_at=returned.available_at,
        structural_level=0,
        complete=True,
    )

    def ledger(_symbol, frequency, _frame):
        if frequency == "30m":
            return _point_ledger(l0)
        if frequency == "5m":
            return CausalStructureEventLedger(
                points=(),
                completed_trends=(raw_departure, raw_return),
            )
        return _point_ledger(locator)

    converted = {"departure": departure, "return": returned}
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system.v3_qmt_structure_path."
        "final_confirmed_structure_events",
        ledger,
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system.v3_qmt_structure_path."
        "completed_l1_trend_fact",
        lambda value, price_quantum: converted[value.trend_id],
    )

    result = build_qmt_v3_structure_path(source=_source())

    assert result.grade == "RESEARCH_ONLY"
    assert result.aligned_entry_count == 1
    assert result.historical_aligned_chain_count == 1
    assert result.technical_entries[0].l0_source_frequency == "30m"
    assert result.technical_entries[0].l1_source_frequency == "5m"
    assert result.technical_entries[0].l2_source_frequency == "1m"
    assert result.technical_entries[0].level_relation_mode == (
        "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES"
    )
    assert result.live_status == "LIVE_DISABLED"


def test_structure_path_rejects_crossed_base_stream() -> None:
    source = _source()
    source.five_minute.attrs["source_base_stream_revision"] = "sha256:" + "c" * 64

    with pytest.raises(ValueError, match="crossed base streams"):
        build_qmt_v3_structure_path(source=source)


def test_old_aligned_chain_remains_audit_only_and_cannot_be_reused(
    monkeypatch,
) -> None:
    l0 = confirmed_point(
        "3buy",
        frequency="30m",
        center_zd=9.0,
        center_zg=9.8,
        center_ordinal=1,
        available_minutes_after=120,
    )
    locator = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=58,
        available_minutes_after=7,
    )
    departure = _trend(
        "departure",
        "up",
        start=5,
        end=30,
        available=35,
        start_price="9.5",
        end_price="10.2",
        low="9.4",
    )
    returned = _trend(
        "return",
        "down",
        start=30,
        end=60,
        available=65,
        start_price="10.2",
        end_price="9.9",
        low="9.8",
    )
    raw = (
        SimpleNamespace(
            trend_id="departure",
            available_at=departure.available_at,
            structural_level=0,
            complete=True,
        ),
        SimpleNamespace(
            trend_id="return",
            available_at=returned.available_at,
            structural_level=0,
            complete=True,
        ),
    )

    def ledger(_symbol, frequency, _frame):
        if frequency == "30m":
            return _point_ledger(l0)
        if frequency == "5m":
            return CausalStructureEventLedger(points=(), completed_trends=raw)
        return _point_ledger(locator)

    converted = {"departure": departure, "return": returned}
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system.v3_qmt_structure_path."
        "final_confirmed_structure_events",
        ledger,
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system.v3_qmt_structure_path."
        "completed_l1_trend_fact",
        lambda value, price_quantum: converted[value.trend_id],
    )
    source = _source()
    source.one_minute.loc[0, "date"] = POINT_AT + timedelta(minutes=121)

    result = build_qmt_v3_structure_path(source=source)

    assert result.historical_aligned_chain_count == 1
    assert result.aligned_entry_count == 0


def test_short_but_valid_stream_is_zero_signal_not_fabricated_entry() -> None:
    result = build_qmt_v3_structure_path(source=_source())

    assert result.aligned_entry_count == 0
    assert result.alignment_decisions == ()
    assert result.grade == "RESEARCH_ONLY"
