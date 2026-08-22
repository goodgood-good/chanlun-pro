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
        market_data_source="qmt_gics3_component_composite",
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


def test_one_minute_context_does_not_change_sector_selection_priority() -> None:
    common = {
        "sector_name": "执行定位隔离",
        "market_data_source": "qmt_gics3_component_composite",
        "thirty": neutral_context("30m"),
        "five": neutral_context("5m"),
        "data_complete": True,
    }
    neutral_one = assess_sector(
        sector_id="TDX.880301",
        one=neutral_context("1m"),
        **common,
    )
    supportive_one = assess_sector(
        sector_id="TDX.880302",
        one=supportive_context("1m"),
        **common,
    )

    assert neutral_one.rank_components == supportive_one.rank_components
    assert neutral_one.rank_score == supportive_one.rank_score == 5
    assert neutral_one.regime == supportive_one.regime == "neutral"
    assert "one_support" not in dict(supportive_one.rank_components)


def test_certified_qmt_sw1_pit_composite_is_an_allowed_sector_source() -> None:
    assessment = assess_sector(
        sector_id="qmt-sw1:S27",
        sector_name="electronics",
        market_data_source="qmt-sw1-pit-composite",
        thirty=neutral_context("30m"),
        five=neutral_context("5m"),
        one=neutral_context("1m"),
        data_complete=True,
    )

    assert assessment.eligible is True
    assert "non_native_sector_kline" not in assessment.reason_codes


def test_higher_level_hostile_sector_is_blocked() -> None:
    assessment = assess_sector(
        sector_id="TDX.880301",
        sector_name="煤炭",
        market_data_source="qmt_gics3_component_composite",
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
        market_data_source="qmt_gics3_component_composite",
        thirty=supportive_context("30m"),
        five=supportive_context("5m"),
        one=neutral_context("1m"),
        data_complete=True,
    )
    neutral = assess_sector(
        sector_id="TDX.880301",
        sector_name="煤炭",
        market_data_source="qmt_gics3_component_composite",
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
