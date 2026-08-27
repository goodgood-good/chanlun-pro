import copy
import inspect
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
    parse_position_recommendation_document,
)


def recommendation(**overrides: object):
    values: dict[str, object] = {
        "side": "buy",
        "recommendation": "READY",
        "risk_multiplier": "1.00",
        "context_risk_scale": "1.00",
        "entry_price": "10.00",
        "structural_stop": "9.50",
        "exit_action": "none",
    }
    values.update(overrides)
    return build_position_recommendation(**values)


def test_buy_ratio_uses_structural_risk_budget_and_symbol_cap() -> None:
    value = recommendation()

    assert value.status == "RECOMMENDED"
    assert value.basis == "STRUCTURAL_RISK_MODEL_UPPER_BOUND"
    assert value.recommended_ratio == Decimal("0.0200")
    assert value.document()["recommended_percent"] == "2"
    assert value.automated_order_authorized is False
    assert "仅作结构模型比较" in value.label


def test_buy_ratio_applies_point_and_context_risk_scales() -> None:
    value = recommendation(
        risk_multiplier="0.50",
        context_risk_scale="0.75",
    )

    assert value.recommended_ratio == Decimal("0.0075")
    assert value.document()["recommended_percent"] == "0.75"


def test_buy_ratio_uses_current_price_to_structural_stop_when_anchor_is_explicit() -> (
    None
):
    value = recommendation(
        entry_price="10.25",
        structure_anchor_price="10.00",
        structural_stop="9.80",
    )

    assert value.status == "RECOMMENDED"
    assert value.recommended_ratio == Decimal("0.0227")
    assert "当前价至5分钟防守位" in value.label
    assert "CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED" in value.reason_codes


def test_current_price_resolves_anchor_equal_to_stop_without_division_by_zero() -> None:
    value = recommendation(
        entry_price="10.20",
        structure_anchor_price="10.00",
        structural_stop="10.00",
    )

    assert value.status == "RECOMMENDED"
    assert value.recommended_ratio == Decimal("0.0500")


def test_buy_price_above_anchor_protection_blocks_chasing() -> None:
    value = recommendation(
        entry_price="10.51",
        structure_anchor_price="10.00",
        structural_stop="9.50",
    )

    assert value.status == "BLOCKED"
    assert value.recommended_ratio == Decimal("0")
    assert value.reason_codes == ("BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",)
    assert parse_position_recommendation_document(value.document()) == value


def test_preexisting_one_minute_point_cannot_authorize_precise_entry() -> None:
    value = recommendation(
        five_minute_available_at="2026-07-20T10:30:00+08:00",
        one_minute_available_at="2026-07-20T10:05:00+08:00",
    )

    assert value.status == "BLOCKED"
    assert value.recommended_ratio == Decimal("0")
    assert value.reason_codes == ("ONE_MINUTE_PRECISION_PRECEDES_FIVE_MINUTE_SETUP",)
    assert parse_position_recommendation_document(value.document()) == value


def test_new_one_minute_point_after_five_minute_setup_remains_eligible() -> None:
    value = recommendation(
        five_minute_available_at="2026-07-20T10:05:00+08:00",
        one_minute_available_at="2026-07-20T10:30:00+08:00",
    )

    assert value.status == "RECOMMENDED"


def test_initial_structural_risk_above_five_percent_is_blocked() -> None:
    value = recommendation(structural_stop="9.49")

    assert value.status == "BLOCKED"
    assert value.recommended_ratio == Decimal("0")
    assert value.reason_codes == ("INITIAL_STRUCTURAL_RISK_TOO_WIDE",)


def test_position_sizing_has_no_fixed_setup_age_gate() -> None:
    parameters = inspect.signature(build_position_recommendation).parameters

    assert "signal_age_seconds" not in parameters
    assert "max_buy_signal_age_seconds" not in parameters


def test_missing_realtime_price_remains_unresolved() -> None:
    value = recommendation(
        entry_price=None,
        structure_anchor_price="10.00",
        structural_stop="9.50",
    )

    assert value.status == "UNRESOLVED"
    assert value.recommended_ratio is None
    assert value.reason_codes == ("POSITION_RATIO_INPUT_UNRESOLVED",)


def test_confirmed_setup_without_nesting_witness_is_not_actionable() -> None:
    value = recommendation(
        recommendation="WAITING_SEGMENT_DIFFERENCE",
    )

    assert value.status == "NOT_ACTIONABLE"
    assert value.recommended_ratio is None
    assert value.reason_codes == (
        "ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_FOR_PRECISE_EXECUTION",
    )


def test_unconfirmed_five_minute_structure_has_no_trade_ratio() -> None:
    value = recommendation(recommendation="WAITING_STRUCTURE")

    assert value.status == "NOT_ACTIONABLE"
    assert value.basis == "UNCONFIRMED_5M_STRUCTURE"
    assert value.recommended_ratio is None
    assert "5分钟买点仍在形成" in value.label
    assert value.reason_codes == ("FIVE_MINUTE_TRADE_SIGNAL_NOT_CONFIRMED",)


def test_geometric_candidate_has_no_trade_ratio_before_confirmation() -> None:
    value = recommendation(recommendation="GEOMETRY_AWAITING_CONFIRMATION")

    assert value.status == "NOT_ACTIONABLE"
    assert value.basis == "GEOMETRIC_5M_CANDIDATE_AWAITING_CONFIRMATION"
    assert value.recommended_ratio is None
    assert "5分钟买点仅为几何候选，尚未达到操作确认" in value.label
    assert value.reason_codes == (
        "FIVE_MINUTE_GEOMETRIC_CANDIDATE_AWAITING_CONFIRMATION",
    )
    assert parse_position_recommendation_document(value.document()) == value


def test_sell_ratio_is_exact_when_position_structure_is_known() -> None:
    full = recommendation(side="sell", exit_action="exit_full")
    segment = recommendation(side="sell", exit_action="reduce_tactical")

    assert full.recommended_ratio == Decimal("1")
    assert full.document()["recommended_percent"] == "100"
    assert segment.recommended_ratio == Decimal("0.25")
    assert segment.document()["recommended_percent"] == "25"


def test_sell_without_position_structure_exposes_conditional_ratios() -> None:
    value = recommendation(side="sell")
    document = value.document()

    assert value.status == "CONDITIONAL"
    assert value.recommended_ratio is None
    assert document["conditional_options"] == [
        {
            "condition": "FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT",
            "recommended_ratio": "1",
            "recommended_percent": "100",
        },
        {
            "condition": "FIVE_MINUTE_LOWER_OR_DIFFERENT_STRUCTURE_REDUCTION",
            "recommended_ratio": "0.25",
            "recommended_percent": "25",
        },
    ]
    assert "卖点与目标结构的级别关系" in value.label
    assert "5分钟同级或更高级别卖点" in value.label
    assert "低级别或不同结构" in value.label
    assert "关系未确认前不生成退出比例" in value.label
    assert "%" not in value.label


def test_blocked_signal_recommends_zero_and_invalid_side_fails() -> None:
    blocked = recommendation(recommendation="BLOCKED")

    assert blocked.recommended_ratio == Decimal("0")
    assert blocked.document()["recommended_percent"] == "0"
    assert "存在结构或数据硬阻断" not in blocked.label
    assert "本条不纳入操作计划（具体限制见诊断）" in blocked.label
    with pytest.raises(ValueError, match="side must be buy or sell"):
        recommendation(side="exit")


@pytest.mark.parametrize(
    "value",
    (
        recommendation(),
        recommendation(side="sell"),
        recommendation(recommendation="BLOCKED"),
        recommendation(recommendation="WAITING_STRUCTURE"),
        recommendation(recommendation="GEOMETRY_AWAITING_CONFIRMATION"),
    ),
)
def test_recommendation_document_round_trips_canonically(value) -> None:
    restored = parse_position_recommendation_document(value.document())

    assert restored == value
    assert restored.document() == value.document()
    assert not any(
        term in restored.label
        for term in ("账户", "现金", "持仓", "仓位", "虚拟", "组合热度")
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("recommended_percent", "99"),
        ("automated_order_authorized", True),
    ),
)
def test_recommendation_document_rejects_tampering(
    field: str,
    replacement: object,
) -> None:
    document = copy.deepcopy(recommendation().document())
    document[field] = replacement

    with pytest.raises(ValueError):
        parse_position_recommendation_document(document)
