from dataclasses import replace
from datetime import timedelta

from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
    TradingEngine,
)
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
    hostile_sector,
    provisional_point,
)


def symbol_bundle(
    *,
    five_points=(),
    one_points=(),
    opposite_points=(),
    sector=None,
    thirty_direction="neutral",
    thirty_points=(),
) -> SymbolStructureBundle:
    return SymbolStructureBundle(
        code="SZ.000001",
        as_of=AS_OF,
        sector=eligible_sector() if sector is None else sector,
        thirty_direction=thirty_direction,
        thirty_points=tuple(thirty_points),
        five_points=tuple(five_points),
        one_points=tuple(one_points),
        opposite_points=tuple(opposite_points),
    )


def test_engine_keeps_three_buy_lanes_and_triggers_independent() -> None:
    bundle = symbol_bundle(
        five_points=(
            confirmed_point("1buy"),
            confirmed_point("2buy"),
            confirmed_point("3buy", center_ordinal=1),
        ),
        one_points=(confirmed_point("1buy", frequency="1m", minutes_after=1),),
    )

    evaluated = TradingEngine().evaluate_symbol(bundle)

    assert {item.setup.point.point_type for item in evaluated} == {
        "1buy",
        "2buy",
        "3buy",
    }
    assert len({item.lifecycle.signal_id for item in evaluated}) == 3


def test_neutral_sector_is_retained() -> None:
    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(
            five_points=(confirmed_point("2buy"),),
            one_points=(
                confirmed_point("1buy", frequency="1m", minutes_after=1),
            ),
        )
    )

    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is True


def test_hostile_sector_blocks_new_entry() -> None:
    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(
            sector=hostile_sector(),
            five_points=(confirmed_point("2buy"),),
            one_points=(
                confirmed_point("1buy", frequency="1m", minutes_after=1),
            ),
        )
    )

    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is False
    assert "sector_hostile" in evaluated[0].entry.reason_codes


def test_lower_level_sell_is_risk_not_global_veto() -> None:
    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(
            five_points=(confirmed_point("2buy", tower="xd", level=1),),
            one_points=(
                confirmed_point("1buy", frequency="1m", minutes_after=1),
            ),
            opposite_points=(confirmed_point("1sell", tower="xd", level=0),),
        )
    )

    assert evaluated[0].conflict.hard_block is False
    assert evaluated[0].conflict.risk_only_point_ids
    assert evaluated[0].entry is not None and evaluated[0].entry.allowed is True


def test_confirmed_one_minute_trigger_is_required() -> None:
    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(five_points=(confirmed_point("2buy"),))
    )

    assert evaluated[0].lifecycle.stage == "armed"
    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is False


def test_engine_keeps_provisional_five_minute_points_as_approaching() -> None:
    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(five_points=(provisional_point("2buy"),))
    )

    assert len(evaluated) == 1
    assert evaluated[0].setup.point.point_type == "2buy"
    assert evaluated[0].lifecycle.stage == "approaching"
    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is False


def test_repeated_evaluation_is_deterministic() -> None:
    bundle = symbol_bundle(
        five_points=(confirmed_point("2buy"),),
        one_points=(confirmed_point("1buy", frequency="1m", minutes_after=1),),
    )
    engine = TradingEngine()

    first = engine.evaluate_symbol(bundle)
    second = engine.evaluate_symbol(replace(bundle))

    assert first == second


def test_engine_keeps_only_recent_terminal_point_per_independent_lane() -> None:
    stale_at = AS_OF - timedelta(days=8)
    stale = replace(
        confirmed_point("3buy", center_ordinal=1),
        anchor_at=stale_at,
        confirmed_at=stale_at,
    )
    older_one_buy = confirmed_point("1buy")
    latest_one_buy = confirmed_point("1buy", minutes_after=5)
    independent_two_sell = confirmed_point("2sell", minutes_after=3)

    evaluated = TradingEngine().evaluate_symbol(
        symbol_bundle(
            five_points=(
                stale,
                older_one_buy,
                latest_one_buy,
                independent_two_sell,
            )
        )
    )

    assert [item.setup.point.point_id for item in evaluated] == [
        independent_two_sell.point_id,
        latest_one_buy.point_id,
    ]
