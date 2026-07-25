from dataclasses import replace

from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
    TradingEngine,
)
from chanlun.decision_support.trading_system.models import TradingPolicy
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from tests.trading_system.helpers import eligible_sector
from tests.trading_system.strict_helpers import (
    DEFAULT_CLOSED_AT,
    strict_evidence_result,
    strict_point,
)


SIX_POINT_TYPES = ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")


def _mapped(frequency: str, raw_points):
    return extract_confirmed_points(
        strict_evidence_result(
            source_frequency=frequency,
            confirmed_points=tuple(raw_points),
        ),
        code="SZ.000001",
        source_frequency=frequency,
        as_of=DEFAULT_CLOSED_AT,
    )


def test_all_six_point_types_survive_30m_5m_and_1m_boundaries() -> None:
    raw_points = tuple(strict_point(point_type) for point_type in SIX_POINT_TYPES)
    thirty = _mapped("30m", raw_points)
    five = _mapped("5m", raw_points)
    one = _mapped("1m", raw_points)
    bundle = SymbolStructureBundle(
        code="SZ.000001",
        as_of=DEFAULT_CLOSED_AT,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=thirty,
        five_points=five,
        one_points=one,
        opposite_points=tuple(point for point in five if point.side == "sell"),
    )

    expected = set(SIX_POINT_TYPES)
    assert {point.point_type for point in bundle.thirty_points} == expected
    assert {point.point_type for point in bundle.five_points} == expected
    assert {point.point_type for point in bundle.one_points} == expected
    assert all(point.tower == "formal" for point in (*thirty, *five, *one))


def test_empty_strict_snapshot_stays_empty_on_every_frequency() -> None:
    assert _mapped("30m", ()) == ()
    assert _mapped("5m", ()) == ()
    assert _mapped("1m", ()) == ()


def test_first_center_policy_filters_entry_without_mutating_signal_ledger() -> None:
    later_three_buy = replace(
        strict_point("3buy"),
        center_ordinal=2,
        anchor_tick=120,
        invalidation_tick=110,
    )
    trigger = replace(
        strict_point("1buy"),
        anchor_tick=115,
        invalidation_tick=110,
    )
    five = _mapped("5m", (later_three_buy,))
    one = _mapped("1m", (trigger,))
    bundle = SymbolStructureBundle(
        code="SZ.000001",
        as_of=DEFAULT_CLOSED_AT,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        five_points=five,
        one_points=one,
        opposite_points=(),
    )

    [strict_decision] = TradingEngine().evaluate_symbol(bundle)
    [relaxed_decision] = TradingEngine(
        TradingPolicy(first_center_three_buy_only=False)
    ).evaluate_symbol(bundle)

    assert five[0].center_ordinal == 2
    assert strict_decision.entry is not None
    assert strict_decision.entry.allowed is False
    assert "three_buy_not_first_center" in strict_decision.entry.reason_codes
    assert relaxed_decision.entry is not None
    assert relaxed_decision.entry.allowed is True
