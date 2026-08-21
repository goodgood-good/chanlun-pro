import copy
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.position_recommendation import (
    active_signal_age_seconds,
    build_position_recommendation,
    parse_position_recommendation_document,
)


CN = ZoneInfo("Asia/Shanghai")


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
    assert value.recommended_ratio == Decimal("0.1000")
    assert value.document()["recommended_percent"] == "10"
    assert value.automated_order_authorized is False
    assert "仅作结构模型比较" in value.label


def test_buy_ratio_applies_point_and_context_risk_scales() -> None:
    value = recommendation(
        risk_multiplier="0.50",
        context_risk_scale="0.75",
    )

    assert value.recommended_ratio == Decimal("0.0375")
    assert value.document()["recommended_percent"] == "3.75"


def test_buy_ratio_uses_current_price_to_structural_stop_when_anchor_is_explicit() -> None:
    value = recommendation(
        entry_price="10.25",
        structure_anchor_price="10.00",
        structural_stop="9.50",
    )

    assert value.status == "RECOMMENDED"
    assert value.recommended_ratio == Decimal("0.0683")
    assert "当前价至5分钟防守位" in value.label
    assert "CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED" in value.reason_codes


def test_current_price_resolves_anchor_equal_to_stop_without_division_by_zero() -> None:
    value = recommendation(
        entry_price="10.20",
        structure_anchor_price="10.00",
        structural_stop="10.00",
    )

    assert value.status == "RECOMMENDED"
    assert value.recommended_ratio == Decimal("0.1000")


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


def test_late_buy_discovery_is_review_only_and_never_recommends_chasing() -> None:
    value = recommendation(
        entry_price="10.20",
        structure_anchor_price="10.00",
        structural_stop="9.50",
        signal_age_seconds=601,
    )

    assert value.status == "BLOCKED"
    assert value.recommended_ratio == Decimal("0")
    assert value.reason_codes == ("BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE",)
    assert "延迟复核" in value.label


def test_stale_buy_remains_zero_percent_when_realtime_price_is_missing() -> None:
    value = recommendation(
        entry_price=None,
        structure_anchor_price="10.00",
        structural_stop="9.50",
        signal_age_seconds=601,
    )

    assert value.status == "BLOCKED"
    assert value.recommended_ratio == Decimal("0")
    assert value.reason_codes == ("BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE",)


def test_active_signal_age_excludes_only_same_day_a_share_lunch() -> None:
    started = datetime(2026, 7, 20, 11, 25, tzinfo=CN)
    ended = datetime(2026, 7, 20, 13, 5, tzinfo=CN)

    assert active_signal_age_seconds(started, ended, market="a") == Decimal("600.0")
    assert active_signal_age_seconds(started, ended, market="us") == Decimal("6000.0")
    assert active_signal_age_seconds(
        started,
        datetime(2026, 7, 21, 13, 5, tzinfo=CN),
        market="a",
    ) == Decimal("92400.0")


def test_active_signal_age_rejects_naive_or_reversed_chronology() -> None:
    aware = datetime(2026, 7, 20, 10, 0, tzinfo=CN)

    assert active_signal_age_seconds(
        datetime(2026, 7, 20, 9, 55),
        aware,
        market="a",
    ) is None
    assert active_signal_age_seconds(
        aware,
        datetime(2026, 7, 20, 9, 55, tzinfo=CN),
        market="a",
    ) is None


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
