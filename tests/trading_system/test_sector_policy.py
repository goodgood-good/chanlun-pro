import inspect

from chanlun.decision_support.trading_system.sector_policy import (
    assess_sector,
    rank_sectors,
)
from tests.trading_system.helpers import (
    hostile_context,
    neutral_context,
    supportive_context,
)


def test_sector_neutral_five_minute_does_not_require_a_synchronous_buy() -> None:
    thirty = neutral_context("30m")
    five = neutral_context("5m")
    assessment = assess_sector(
        sector_id="TDX.880301",
        sector_name="煤炭",
        market_data_source="tdx_native_880_index",
        thirty=thirty,
        five=five,
        one=neutral_context("1m"),
        data_complete=True,
    )

    assert assessment.eligible is True
    assert assessment.hard_block is False
    assert "missing_sector_buy_point" not in assessment.reason_codes
    assert assessment.thirty_context == thirty
    assert assessment.five_context == five


def test_sector_price_return_is_not_an_input() -> None:
    parameters = inspect.signature(assess_sector).parameters
    assert "price_change" not in parameters
    assert "return_pct" not in parameters


def test_higher_level_hostile_sector_is_blocked() -> None:
    assessment = assess_sector(
        sector_id="TDX.880301",
        sector_name="煤炭",
        market_data_source="tdx_native_880_index",
        thirty=hostile_context("30m"),
        five=supportive_context("5m"),
        one=supportive_context("1m"),
        data_complete=True,
    )

    assert assessment.eligible is False
    assert assessment.hard_block is True
    assert assessment.reason_codes == ("higher_structure_sell_risk",)


def test_sector_ranking_is_deterministic_and_explainable() -> None:
    supportive = assess_sector(
        sector_id="TDX.880302",
        sector_name="电力",
        market_data_source="tdx_native_880_index",
        thirty=supportive_context("30m"),
        five=supportive_context("5m"),
        one=neutral_context("1m"),
        data_complete=True,
    )
    neutral = assess_sector(
        sector_id="TDX.880301",
        sector_name="煤炭",
        market_data_source="tdx_native_880_index",
        thirty=neutral_context("30m"),
        five=neutral_context("5m"),
        one=neutral_context("1m"),
        data_complete=True,
    )

    ranked = rank_sectors((neutral, supportive))

    assert [item.assessment.sector_id for item in ranked] == [
        "TDX.880302",
        "TDX.880301",
    ]
    assert [item.ordinal for item in ranked] == [1, 2]
    assert ranked[0].assessment.rank_components
