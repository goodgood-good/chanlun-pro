"""把结构风险转换为只供人工复核的建议交易比例。

本模块不读取资金、持有数量，也不创建订单。买入比例只使用生产回测已经采用的
单笔风险预算、结构失效距离和单标的上限计算；卖出比例只表达同级退出或段差处理
的结构规则。所有比例都是模型比较值，不代表任何外部资金或数量信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.parameters import (
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION,
)
from chanlun.decision_support.trading_system.portfolio_risk import RiskLimits


_RATIO_QUANTUM = Decimal("0.0001")
_MAX_BUY_ANCHOR_DRIFT_RATE = Decimal("0.05")
_MAX_FRESH_BUY_SIGNAL_AGE_SECONDS = Decimal("600")
_CN = ZoneInfo("Asia/Shanghai")
BUY_SIGNAL_PROTECTION_REASON_CODES = frozenset(
    {
        "BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE",
        "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",
        "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",
    }
)
_UNCONFIRMED_STRUCTURE_BASIS = "UNCONFIRMED_5M_STRUCTURE"
_GEOMETRY_AWAITING_CONFIRMATION_BASIS = (
    "GEOMETRIC_5M_CANDIDATE_AWAITING_CONFIRMATION"
)
_UNCONFIRMED_STRUCTURE_REASON = "FIVE_MINUTE_TRADE_SIGNAL_NOT_CONFIRMED"
_GEOMETRY_AWAITING_CONFIRMATION_REASON = (
    "FIVE_MINUTE_GEOMETRIC_CANDIDATE_AWAITING_CONFIRMATION"
)


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _ratio(value: Decimal) -> Decimal:
    return max(Decimal("0"), value).quantize(_RATIO_QUANTUM, rounding=ROUND_DOWN)


def _percent_text(value: Decimal) -> str:
    rendered = format(value * Decimal("100"), ".2f").rstrip("0").rstrip(".")
    return rendered or "0"


def active_signal_age_seconds(
    signal_at: datetime,
    observed_at: datetime,
    *,
    market: str,
) -> Decimal | None:
    """Return causal signal age, excluding only the same A-share lunch closure."""

    if (
        signal_at.tzinfo is None
        or signal_at.utcoffset() is None
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or observed_at < signal_at
    ):
        return None
    started = signal_at.astimezone(_CN)
    ended = observed_at.astimezone(_CN)
    elapsed = ended - started
    if market == "a" and started.date() == ended.date():
        lunch_started = started.replace(
            hour=11,
            minute=31,
            second=0,
            microsecond=0,
        )
        lunch_ended = started.replace(
            hour=13,
            minute=1,
            second=0,
            microsecond=0,
        )
        overlap_started = max(started, lunch_started)
        overlap_ended = min(ended, lunch_ended)
        if overlap_ended > overlap_started:
            elapsed -= overlap_ended - overlap_started
    return Decimal(str(max(timedelta(0), elapsed).total_seconds()))


@dataclass(frozen=True, slots=True)
class PositionRecommendation:
    side: str
    status: str
    basis: str
    recommended_ratio: Decimal | None
    recommended_percent: str | None
    label: str
    reason_codes: tuple[str, ...]
    segment_difference_max_ratio: Decimal
    conditional_options: tuple[tuple[str, Decimal], ...] = ()
    manual_confirmation_required: bool = True
    automated_order_authorized: bool = False

    def document(self) -> dict[str, object]:
        return {
            "side": self.side,
            "status": self.status,
            "basis": self.basis,
            "recommended_ratio": (
                None
                if self.recommended_ratio is None
                else format(self.recommended_ratio, "f")
            ),
            "recommended_percent": self.recommended_percent,
            "label": self.label,
            "reason_codes": list(self.reason_codes),
            "segment_difference_max_ratio": format(
                self.segment_difference_max_ratio,
                "f",
            ),
            "segment_difference_max_percent": _percent_text(
                self.segment_difference_max_ratio
            ),
            "conditional_options": [
                {
                    "condition": condition,
                    "recommended_ratio": format(ratio, "f"),
                    "recommended_percent": _percent_text(ratio),
                }
                for condition, ratio in self.conditional_options
            ],
            "manual_confirmation_required": self.manual_confirmation_required,
            "automated_order_authorized": self.automated_order_authorized,
        }


_POSITION_RECOMMENDATION_FIELDS = frozenset(
    {
        "side",
        "status",
        "basis",
        "recommended_ratio",
        "recommended_percent",
        "label",
        "reason_codes",
        "segment_difference_max_ratio",
        "segment_difference_max_percent",
        "conditional_options",
        "manual_confirmation_required",
        "automated_order_authorized",
    }
)
_STATUS_BASIS = {
    "BLOCKED": "NO_TRADE",
    "UNRESOLVED": "STRUCTURAL_RISK_INPUTS",
    "CONDITIONAL": "STRUCTURAL_EXIT_LEVEL_REQUIRED",
}


def _pending_setup_label(*, side: str, geometry_ready: bool) -> str:
    prefix = "结构风险参考比例" if side == "buy" else "结构退出参考比例"
    point = "买点" if side == "buy" else "卖点"
    state = (
        f"5分钟{point}仅为几何候选，尚未达到操作确认"
        if geometry_ready
        else f"5分钟{point}仍在形成"
    )
    return f"{prefix}：暂不计算（{state}）"


def parse_position_recommendation_document(
    raw: object,
) -> PositionRecommendation:
    """Parse one exact, manual-only recommendation document.

    The recommendation is part of the review evidence, so accepting a loose
    mapping here would let a caller change a percentage while retaining an
    otherwise valid candidate.  Legacy review candidates may omit the whole
    document, but a document that is present must be complete and canonical.
    """

    if not isinstance(raw, Mapping) or set(raw) != _POSITION_RECOMMENDATION_FIELDS:
        raise ValueError("position recommendation document is malformed")
    if raw.get("side") not in {"buy", "sell"}:
        raise ValueError("position recommendation side is invalid")
    if raw.get("manual_confirmation_required") is not True or raw.get(
        "automated_order_authorized"
    ) is not False:
        raise ValueError("position recommendation cannot authorize an order")
    if not isinstance(raw.get("label"), str) or not str(raw["label"]).strip():
        raise ValueError("position recommendation label is missing")
    reason_codes = raw.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or any(not isinstance(value, str) or not value for value in reason_codes)
        or len(reason_codes) != len(set(reason_codes))
    ):
        raise ValueError("position recommendation reasons are invalid")

    segment_ratio = _decimal(raw.get("segment_difference_max_ratio"))
    if segment_ratio is None or not Decimal("0") < segment_ratio <= Decimal("1"):
        raise ValueError("position recommendation segment ratio is invalid")
    if raw.get("segment_difference_max_percent") != _percent_text(segment_ratio):
        raise ValueError("position recommendation segment percent changed")

    ratio = (
        None
        if raw.get("recommended_ratio") is None
        else _decimal(raw.get("recommended_ratio"))
    )
    if ratio is not None and not Decimal("0") <= ratio <= Decimal("1"):
        raise ValueError("position recommendation ratio is invalid")
    expected_percent = None if ratio is None else _percent_text(ratio)
    if raw.get("recommended_percent") != expected_percent:
        raise ValueError("position recommendation percent changed")

    raw_options = raw.get("conditional_options")
    if not isinstance(raw_options, list):
        raise ValueError("position recommendation conditions are invalid")
    options: list[tuple[str, Decimal]] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping) or set(raw_option) != {
            "condition",
            "recommended_ratio",
            "recommended_percent",
        }:
            raise ValueError("position recommendation condition is malformed")
        condition = raw_option.get("condition")
        option_ratio = _decimal(raw_option.get("recommended_ratio"))
        if (
            not isinstance(condition, str)
            or not condition
            or option_ratio is None
            or not Decimal("0") <= option_ratio <= Decimal("1")
            or raw_option.get("recommended_percent") != _percent_text(option_ratio)
        ):
            raise ValueError("position recommendation condition is invalid")
        options.append((condition, option_ratio))
    if len(options) != len({condition for condition, _ratio_value in options}):
        raise ValueError("position recommendation conditions are duplicated")

    side = str(raw["side"])
    status = raw.get("status")
    basis = raw.get("basis")
    if not isinstance(status, str) or not isinstance(basis, str):
        raise ValueError("position recommendation state is invalid")
    if status == "RECOMMENDED":
        expected_basis = (
            "STRUCTURAL_RISK_MODEL_UPPER_BOUND"
            if side == "buy"
            else "STRUCTURAL_EXIT_RULE"
        )
        if basis != expected_basis or ratio is None or ratio <= 0 or options:
            raise ValueError("recommended position ratio is inconsistent")
    elif status == "CONDITIONAL":
        if (
            side != "sell"
            or basis != _STATUS_BASIS[status]
            or ratio is not None
            or options
            != [
                ("FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT", Decimal("1")),
                (
                    "FIVE_MINUTE_LOWER_OR_DIFFERENT_STRUCTURE_REDUCTION",
                    segment_ratio,
                ),
            ]
        ):
            raise ValueError("conditional sell recommendation is inconsistent")
    elif status == "NOT_ACTIONABLE":
        expected_pending = {
            (
                _UNCONFIRMED_STRUCTURE_BASIS,
                (_UNCONFIRMED_STRUCTURE_REASON,),
            ): _pending_setup_label(side=side, geometry_ready=False),
            (
                _GEOMETRY_AWAITING_CONFIRMATION_BASIS,
                (_GEOMETRY_AWAITING_CONFIRMATION_REASON,),
            ): _pending_setup_label(side=side, geometry_ready=True),
        }
        expected_label = expected_pending.get((basis, tuple(reason_codes)))
        if ratio is not None or options or expected_label is None:
            raise ValueError("non-actionable recommendation is inconsistent")
        if raw.get("label") != expected_label:
            raise ValueError("non-actionable recommendation label changed")
    elif status in _STATUS_BASIS:
        if basis != _STATUS_BASIS[status] or options:
            raise ValueError("position recommendation state is inconsistent")
        if status == "BLOCKED" and ratio != 0:
            raise ValueError("blocked recommendation must have zero ratio")
        if status == "UNRESOLVED" and ratio is not None:
            raise ValueError("non-actionable recommendation must not have a ratio")
    else:
        raise ValueError("position recommendation status is invalid")

    parsed = PositionRecommendation(
        side=side,
        status=status,
        basis=basis,
        recommended_ratio=ratio,
        recommended_percent=expected_percent,
        label=str(raw["label"]),
        reason_codes=tuple(reason_codes),
        segment_difference_max_ratio=segment_ratio,
        conditional_options=tuple(options),
    )
    if parsed.document() != dict(raw):
        raise ValueError("position recommendation document is not canonical")
    return parsed


def build_position_recommendation(
    *,
    side: str,
    recommendation: str,
    risk_multiplier: object,
    context_risk_scale: object,
    entry_price: object,
    structural_stop: object,
    exit_action: str,
    structure_anchor_price: object | None = None,
    signal_age_seconds: object | None = None,
    max_buy_anchor_drift_rate: object = _MAX_BUY_ANCHOR_DRIFT_RATE,
    max_buy_signal_age_seconds: object = _MAX_FRESH_BUY_SIGNAL_AGE_SECONDS,
    risk_limits: RiskLimits = RiskLimits(),
) -> PositionRecommendation:
    """返回可审计的建议比例；数据不足时明确返回“待核对”。"""

    if side not in {"buy", "sell"}:
        raise ValueError("position recommendation side must be buy or sell")
    segment_ratio = individual_parameter_snapshot().tactical_ratio
    if recommendation == "BLOCKED":
        return PositionRecommendation(
            side=side,
            status="BLOCKED",
            basis="NO_TRADE",
            recommended_ratio=Decimal("0"),
            recommended_percent="0",
            label=(
                f"{'结构风险' if side == 'buy' else '结构退出'}参考："
                "本条不纳入操作计划（具体限制见诊断）"
            ),
            reason_codes=("HARD_BLOCKED_NO_TRADE",),
            segment_difference_max_ratio=segment_ratio,
        )

    if recommendation == "WAITING_STRUCTURE":
        return PositionRecommendation(
            side=side,
            status="NOT_ACTIONABLE",
            basis=_UNCONFIRMED_STRUCTURE_BASIS,
            recommended_ratio=None,
            recommended_percent=None,
            label=_pending_setup_label(side=side, geometry_ready=False),
            reason_codes=(_UNCONFIRMED_STRUCTURE_REASON,),
            segment_difference_max_ratio=segment_ratio,
        )

    if recommendation == GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION:
        return PositionRecommendation(
            side=side,
            status="NOT_ACTIONABLE",
            basis=_GEOMETRY_AWAITING_CONFIRMATION_BASIS,
            recommended_ratio=None,
            recommended_percent=None,
            label=_pending_setup_label(side=side, geometry_ready=True),
            reason_codes=(_GEOMETRY_AWAITING_CONFIRMATION_REASON,),
            segment_difference_max_ratio=segment_ratio,
        )

    if side == "buy":
        multiplier = _decimal(risk_multiplier)
        context_scale = _decimal(context_risk_scale)
        signal_age = (
            None
            if signal_age_seconds is None
            else _decimal(signal_age_seconds)
        )
        maximum_signal_age = _decimal(max_buy_signal_age_seconds)
        # 信号已经过期是一个独立且更强的 0% 结论，不依赖当前价、结构止损或
        # 其他比例输入。即使实时行情同时不可用，也必须先保留“不追价”，不能
        # 把确定的禁止条件降级成普通的参数待核对。
        if (
            maximum_signal_age is None
            or maximum_signal_age <= 0
            or (signal_age_seconds is not None and signal_age is None)
            or (signal_age is not None and signal_age < 0)
        ):
            return PositionRecommendation(
                side=side,
                status="UNRESOLVED",
                basis="STRUCTURAL_RISK_INPUTS",
                recommended_ratio=None,
                recommended_percent=None,
                label="结构风险参考比例：待核对（结构价格或风险参数不足）",
                reason_codes=("POSITION_RATIO_INPUT_UNRESOLVED",),
                segment_difference_max_ratio=segment_ratio,
            )
        if signal_age is not None and signal_age > maximum_signal_age:
            return PositionRecommendation(
                side=side,
                status="BLOCKED",
                basis="NO_TRADE",
                recommended_ratio=Decimal("0"),
                recommended_percent="0",
                label=(
                    "结构风险参考：本条买入不纳入操作计划（监听发现已超过5分钟信号的10分钟新鲜窗口；"
                    "仅作延迟复核，不追价）"
                ),
                reason_codes=("BUY_SIGNAL_DISCOVERY_TOO_LATE_NO_CHASE",),
                segment_difference_max_ratio=segment_ratio,
            )

        price = _decimal(entry_price)
        stop = _decimal(structural_stop)
        anchor = (
            price
            if structure_anchor_price is None
            else _decimal(structure_anchor_price)
        )
        maximum_anchor_drift = _decimal(max_buy_anchor_drift_rate)
        if (
            multiplier is None
            or context_scale is None
            or price is None
            or stop is None
            or anchor is None
            or maximum_signal_age is None
            or maximum_anchor_drift is None
            or multiplier <= 0
            or context_scale <= 0
            or price <= 0
            or stop <= 0
            or anchor <= 0
            or maximum_anchor_drift <= 0
        ):
            return PositionRecommendation(
                side=side,
                status="UNRESOLVED",
                basis="STRUCTURAL_RISK_INPUTS",
                recommended_ratio=None,
                recommended_percent=None,
                label="结构风险参考比例：待核对（结构价格或风险参数不足）",
                reason_codes=("POSITION_RATIO_INPUT_UNRESOLVED",),
                segment_difference_max_ratio=segment_ratio,
            )
        if price <= stop:
            if structure_anchor_price is None:
                return PositionRecommendation(
                    side=side,
                    status="UNRESOLVED",
                    basis="STRUCTURAL_RISK_INPUTS",
                    recommended_ratio=None,
                    recommended_percent=None,
                    label="结构风险参考比例：待核对（结构价格或风险参数不足）",
                    reason_codes=("POSITION_RATIO_INPUT_UNRESOLVED",),
                    segment_difference_max_ratio=segment_ratio,
                )
            return PositionRecommendation(
                side=side,
                status="BLOCKED",
                basis="NO_TRADE",
                recommended_ratio=Decimal("0"),
                recommended_percent="0",
                label="结构风险参考：本条买入不纳入操作计划（当前价已到达或跌破5分钟结构防守位）",
                reason_codes=("CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",),
                segment_difference_max_ratio=segment_ratio,
            )
        anchor_drift_rate = (price - anchor) / anchor
        if anchor_drift_rate > maximum_anchor_drift:
            return PositionRecommendation(
                side=side,
                status="BLOCKED",
                basis="NO_TRADE",
                recommended_ratio=Decimal("0"),
                recommended_percent="0",
                label=(
                    "结构风险参考：本条买入不纳入操作计划（当前价较5分钟结构锚点上浮 "
                    f"{_percent_text(anchor_drift_rate)}%，超过"
                    f"{_percent_text(maximum_anchor_drift)}% 追价保护线）"
                ),
                reason_codes=("BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",),
                segment_difference_max_ratio=segment_ratio,
            )
        structural_risk_rate = (price - stop) / price
        risk_budget_fraction = (
            risk_limits.base_trade_risk * multiplier * context_scale
        )
        recommended = _ratio(
            min(
                risk_limits.max_symbol_fraction,
                risk_budget_fraction / structural_risk_rate,
            )
        )
        percent = _percent_text(recommended)
        return PositionRecommendation(
            side=side,
            status="RECOMMENDED",
            basis="STRUCTURAL_RISK_MODEL_UPPER_BOUND",
            recommended_ratio=recommended,
            recommended_percent=percent,
            label=(
                f"结构风险参考比例：{percent}% 以内（按"
                f"{'当前价至5分钟防守位' if structure_anchor_price is not None else '5分钟结构锚点'}"
                "测算；仅作结构模型比较）"
            ),
            reason_codes=(
                (
                    "CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED"
                    if structure_anchor_price is not None
                    else "STRUCTURAL_RISK_BUDGET_SIZED"
                ),
                "STRUCTURAL_MODEL_CAP_REQUIRES_MANUAL_REVIEW",
            ),
            segment_difference_max_ratio=segment_ratio,
        )

    if side == "sell" and exit_action in {"exit_full", "reduce_tactical"}:
        recommended = (
            Decimal("1") if exit_action == "exit_full" else segment_ratio
        )
        percent = _percent_text(recommended)
        return PositionRecommendation(
            side=side,
            status="RECOMMENDED",
            basis="STRUCTURAL_EXIT_RULE",
            recommended_ratio=recommended,
            recommended_percent=percent,
            label=(
                f"结构退出参考比例：{percent}%（按5分钟"
                + (
                    "同级或更高级别卖点完整退出规则"
                    if exit_action == "exit_full"
                    else "低级别或不同结构卖点段差规则"
                )
                + "；仅作结构模型比较）"
            ),
            reason_codes=(
                "SAME_OR_HIGHER_STRUCTURE_FULL_EXIT"
                if exit_action == "exit_full"
                else "LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION",
            ),
            segment_difference_max_ratio=segment_ratio,
        )

    # 尚未指定要比较的结构级别时，只表达两条 5 分钟结构分支：同级/更高级别
    # 按完整退出规则处理，低级别或不同结构只按段差规则处理。1 分钟只提供定位证据，
    # 不能独立授权卖出。
    return PositionRecommendation(
        side=side,
        status="CONDITIONAL",
        basis="STRUCTURAL_EXIT_LEVEL_REQUIRED",
        recommended_ratio=None,
        recommended_percent=None,
        label=(
            "结构退出参考：卖点与目标结构的级别关系待人工核对；5分钟同级或"
            "更高级别卖点按完整退出规则复核，低级别或不同结构卖点仅作段差处理；"
            "关系未确认前不生成退出比例"
        ),
        reason_codes=("SELL_STRUCTURE_RELATION_REQUIRED",),
        segment_difference_max_ratio=segment_ratio,
        conditional_options=(
            ("FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT", Decimal("1")),
            ("FIVE_MINUTE_LOWER_OR_DIFFERENT_STRUCTURE_REDUCTION", segment_ratio),
        ),
    )


__all__ = (
    "BUY_SIGNAL_PROTECTION_REASON_CODES",
    "PositionRecommendation",
    "active_signal_age_seconds",
    "build_position_recommendation",
    "parse_position_recommendation_document",
)
