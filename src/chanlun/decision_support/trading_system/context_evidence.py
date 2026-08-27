"""日线与 30 分钟同周期技术上下文。

这些事实只解释 5 分钟正式买卖点所处的环境，不能创造、否定或改写买卖点。
MA5/MA10 使用同一物理周期已完成 K 线；分型和笔状态直接投影严格 ``CL``
运行态，避免交易层维护第二套包含、分型或成笔规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite
from typing import Literal

import pandas as pd

from chanlun.core.bi_calculator import fractal_lock_witness
from chanlun.core.cl import CL
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    PointSide,
    PointType,
    TimeframeContext,
)


ContextEvidenceFrequency = Literal["d", "30m"]
PriceMa5Position = Literal["above", "below", "equal", "unresolved"]
MaRelation = Literal["ma5_above_ma10", "ma5_below_ma10", "equal", "unresolved"]
MaCross = Literal["golden", "death", "none", "unresolved"]
FractalType = Literal["top", "bottom", "none"]
FractalState = Literal[
    "forming",
    "confirmed",
    "pen_endpoint_pending_lock",
    "pen_locked",
    "continuation",
    "unresolved",
]
ContextStance = Literal["supportive", "neutral", "adverse", "unresolved"]
ContextGrade = Literal["A", "B", "C", "UNRESOLVED"]


_CONTEXT_RISK_SCALES: dict[ContextGrade, Decimal] = {
    "A": Decimal("1.00"),
    "B": Decimal("0.75"),
    "C": Decimal("0.50"),
    "UNRESOLVED": Decimal("0.50"),
}


_GRADE_LABELS: dict[ContextGrade, str] = {
    "A": "A级（双周期支持）",
    "B": "B级（混合或中性）",
    "C": "C级（逆风观察）",
    "UNRESOLVED": "待判定（证据不足）",
}


@dataclass(frozen=True, slots=True)
class SamePeriodTechnicalContext:
    frequency: ContextEvidenceFrequency
    observed_at: datetime
    source_bar_count: int
    close: float
    ma5: float | None
    ma10: float | None
    close_vs_ma5: PriceMa5Position
    ma5_vs_ma10: MaRelation
    ma_cross: MaCross
    consecutive_closes_vs_ma5: int
    fractal_type: FractalType
    fractal_state: FractalState
    fractal_anchor_at: datetime | None
    fractal_confirmed_at: datetime | None
    fractal_price: float | None
    latest_pen_direction: Literal["up", "down", "none"]
    latest_pen_locked: bool | None
    reason_codes: tuple[str, ...]
    latest_pen_end_at: datetime | None = None

    def __post_init__(self) -> None:
        observed_at = normalize_datetime(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        if self.frequency not in {"d", "30m"}:
            raise ValueError("same-period context only supports d and 30m")
        if type(self.source_bar_count) is not int or self.source_bar_count <= 0:
            raise ValueError("source_bar_count must be positive")
        for name in ("close", "ma5", "ma10", "fractal_price"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("fractal_anchor_at", "fractal_confirmed_at"):
            value = getattr(self, name)
            if value is not None:
                normalized = normalize_datetime(value, name)
                if normalized > observed_at:
                    raise ValueError(f"{name} cannot be after observed_at")
                object.__setattr__(self, name, normalized)
        if self.latest_pen_end_at is not None:
            pen_end_at = normalize_datetime(self.latest_pen_end_at, "latest_pen_end_at")
            if pen_end_at > observed_at:
                raise ValueError("latest_pen_end_at cannot be after observed_at")
            object.__setattr__(self, "latest_pen_end_at", pen_end_at)
        if self.fractal_type == "none" and any(
            value is not None
            for value in (
                self.fractal_anchor_at,
                self.fractal_confirmed_at,
                self.fractal_price,
            )
        ):
            raise ValueError("unresolved fractal cannot carry anchors")
        if self.fractal_state == "forming" and self.fractal_confirmed_at is not None:
            raise ValueError("forming fractal cannot be confirmed")
        if self.fractal_state not in {"forming", "unresolved"} and (
            self.fractal_type == "none"
            or self.fractal_anchor_at is None
            or self.fractal_confirmed_at is None
        ):
            raise ValueError("confirmed fractal state requires causal timestamps")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("context evidence reason codes must be unique")

    def document(self) -> dict[str, object]:
        return {
            "frequency": self.frequency,
            "role": (
                "背景过滤（不定义买卖点）"
                if self.frequency == "d"
                else "策略环境（不定义买卖点）"
            ),
            "observed_at": self.observed_at.isoformat(),
            "source_bar_count": self.source_bar_count,
            "close": self.close,
            "ma5": self.ma5,
            "ma10": self.ma10,
            "close_vs_ma5": self.close_vs_ma5,
            "ma5_vs_ma10": self.ma5_vs_ma10,
            "ma_cross": self.ma_cross,
            "consecutive_closes_vs_ma5": self.consecutive_closes_vs_ma5,
            "fractal_type": self.fractal_type,
            "fractal_state": self.fractal_state,
            "fractal_anchor_at": (
                None
                if self.fractal_anchor_at is None
                else self.fractal_anchor_at.isoformat()
            ),
            "fractal_confirmed_at": (
                None
                if self.fractal_confirmed_at is None
                else self.fractal_confirmed_at.isoformat()
            ),
            "fractal_price": self.fractal_price,
            "latest_pen_direction": self.latest_pen_direction,
            "latest_pen_locked": self.latest_pen_locked,
            "latest_pen_end_at": (
                None
                if self.latest_pen_end_at is None
                else self.latest_pen_end_at.isoformat()
            ),
            # 日线分型不再固定映射到 30m；5m 正式结构完成买卖点判断，1m
            # 只补充段差与精细定位。物理周期与递归层级保持两套独立坐标。
            "lower_confirmation_frequencies": ["5m", "1m"],
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class SignalContextAssessment:
    grade: ContextGrade
    daily_stance: ContextStance
    thirty_minute_stance: ContextStance
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("context assessment reason codes must be unique")

    @property
    def grade_label(self) -> str:
        return _GRADE_LABELS[self.grade]

    def document(self) -> dict[str, object]:
        return {
            "grade": self.grade,
            "grade_label": self.grade_label,
            "daily_stance": self.daily_stance,
            "thirty_minute_stance": self.thirty_minute_stance,
            "weekly_or_monthly_used": False,
            "ma_and_fractal_can_define_signal": False,
            "reason_codes": list(self.reason_codes),
        }


def signal_context_risk_scale(
    assessment: SignalContextAssessment | None,
) -> Decimal:
    """Return the shared sizing-only scale for one causal context grade.

    Context never creates or invalidates a 5m signal.  The scale is consumed
    by both the human position recommendation and the portfolio replay so the
    two execution views cannot silently size the same signal differently.
    """

    grade: ContextGrade = "UNRESOLVED" if assessment is None else assessment.grade
    return _CONTEXT_RISK_SCALES[grade]


def _position(left: float, right: float | None) -> PriceMa5Position:
    if right is None:
        return "unresolved"
    if left > right:
        return "above"
    if left < right:
        return "below"
    return "equal"


def _ma_relation(ma5: float | None, ma10: float | None) -> MaRelation:
    if ma5 is None or ma10 is None:
        return "unresolved"
    if ma5 > ma10:
        return "ma5_above_ma10"
    if ma5 < ma10:
        return "ma5_below_ma10"
    return "equal"


def _ma_cross(closes: pd.Series) -> MaCross:
    if len(closes) < 11:
        return "unresolved"
    ma5 = closes.rolling(5).mean()
    ma10 = closes.rolling(10).mean()
    previous = float(ma5.iloc[-2] - ma10.iloc[-2])
    current = float(ma5.iloc[-1] - ma10.iloc[-1])
    if previous <= 0 < current:
        return "golden"
    if previous >= 0 > current:
        return "death"
    return "none"


def _consecutive_close_position(closes: pd.Series) -> int:
    ma5 = closes.rolling(5).mean()
    if pd.isna(ma5.iloc[-1]):
        return 0
    latest_relation = (
        1
        if closes.iloc[-1] > ma5.iloc[-1]
        else -1
        if closes.iloc[-1] < ma5.iloc[-1]
        else 0
    )
    if latest_relation == 0:
        return 0
    count = 0
    for close, average in zip(reversed(closes.tolist()), reversed(ma5.tolist())):
        if pd.isna(average):
            break
        relation = 1 if close > average else -1 if close < average else 0
        if relation != latest_relation:
            break
        count += 1
    return count * latest_relation


def _fractal_key(value: object) -> tuple[object, object]:
    return (
        getattr(value, "type", None),
        getattr(getattr(value, "k", None), "index", None),
    )


def _forming_fractal(cl_state: CL) -> tuple[FractalType, datetime, float] | None:
    values = cl_state.get_cl_klines()
    if len(values) < 3:
        return None
    previous, middle, current = values[-3:]
    if (
        middle.h > previous.h
        and middle.h > current.h
        and middle.l > previous.l
        and middle.l > current.l
    ):
        return "top", middle.date, float(middle.h)
    if (
        middle.l < previous.l
        and middle.l < current.l
        and middle.h < previous.h
        and middle.h < current.h
    ):
        return "bottom", middle.date, float(middle.l)
    return None


def build_same_period_technical_context(
    *,
    frequency: str,
    frame: pd.DataFrame,
    cl_state: CL,
    as_of: datetime,
) -> SamePeriodTechnicalContext:
    """从一次已经完成的严格 CL 计算投影 MA、分型和笔上下文。"""

    if frequency not in {"d", "30m"}:
        raise ValueError("same-period context only supports d and 30m")
    observed_at = normalize_datetime(as_of, "as_of")
    closes = pd.to_numeric(frame["close"], errors="raise").astype(float)
    if closes.empty or closes.isna().any() or (closes <= 0).any():
        raise ValueError("same-period context requires positive closed prices")
    ma5_series = closes.rolling(5).mean()
    ma10_series = closes.rolling(10).mean()
    ma5 = None if pd.isna(ma5_series.iloc[-1]) else float(ma5_series.iloc[-1])
    ma10 = None if pd.isna(ma10_series.iloc[-1]) else float(ma10_series.iloc[-1])
    close = float(closes.iloc[-1])
    cross = _ma_cross(closes)
    reasons: list[str] = []
    prefix = frequency.upper()
    if ma5 is None:
        reasons.append(f"{prefix}_MA5_UNAVAILABLE")
    if ma10 is None:
        reasons.append(f"{prefix}_MA10_UNAVAILABLE")

    fxs = tuple(cl_state.get_fxs())
    bis = tuple(cl_state.get_bis())
    latest_fx = max(
        fxs,
        key=lambda value: (value.k.date, value.k.index, value.index),
        default=None,
    )
    forming = _forming_fractal(cl_state)
    latest_fx_at = None if latest_fx is None else latest_fx.k.date
    if forming is not None and (latest_fx_at is None or forming[1] > latest_fx_at):
        fractal_type: FractalType = forming[0]
        fractal_state: FractalState = "forming"
        fractal_anchor_at = forming[1]
        fractal_confirmed_at = None
        fractal_price = forming[2]
        reasons.append(f"{prefix}_FRACTAL_FORMING")
    elif latest_fx is None:
        fractal_type = "none"
        fractal_state = "unresolved"
        fractal_anchor_at = None
        fractal_confirmed_at = None
        fractal_price = None
        reasons.append(f"{prefix}_FRACTAL_UNAVAILABLE")
    else:
        fractal_type = "top" if latest_fx.type == "ding" else "bottom"
        fractal_anchor_at = latest_fx.k.date
        fractal_confirmed_at = fractal_lock_witness(latest_fx)
        fractal_price = float(latest_fx.val)
        key = _fractal_key(latest_fx)
        locked_ends = {
            _fractal_key(value.end)
            for value in bis
            if value.end is not None and value.is_done()
        }
        pending_ends = {
            _fractal_key(value.end)
            for value in bis
            if value.end is not None and not value.is_done()
        }
        if fractal_confirmed_at is None:
            # 核心列表偶遇中间态或历史脏数据时，不把缺失的因果见证伪装成
            # “已确认”。上下文证据应降级为待判定，不能让整个实时快照失败。
            fractal_state = "unresolved"
        elif key in locked_ends:
            fractal_state = "pen_locked"
        elif key in pending_ends:
            fractal_state = "pen_endpoint_pending_lock"
        else:
            fractal_state = "confirmed"
        reasons.append(
            f"{prefix}_{'TOP' if fractal_type == 'top' else 'BOTTOM'}_FRACTAL_{fractal_state.upper()}"
        )

    latest_bi = bis[-1] if bis else None
    return SamePeriodTechnicalContext(
        frequency=frequency,  # type: ignore[arg-type]
        observed_at=observed_at,
        source_bar_count=len(closes),
        close=close,
        ma5=ma5,
        ma10=ma10,
        close_vs_ma5=_position(close, ma5),
        ma5_vs_ma10=_ma_relation(ma5, ma10),
        ma_cross=cross,
        consecutive_closes_vs_ma5=_consecutive_close_position(closes),
        fractal_type=fractal_type,
        fractal_state=fractal_state,
        fractal_anchor_at=fractal_anchor_at,
        fractal_confirmed_at=fractal_confirmed_at,
        fractal_price=fractal_price,
        latest_pen_direction=("none" if latest_bi is None else latest_bi.type),
        latest_pen_locked=(None if latest_bi is None else latest_bi.is_done()),
        reason_codes=tuple(dict.fromkeys(reasons)),
        latest_pen_end_at=(
            None if latest_bi is None or latest_bi.end is None else latest_bi.end.k.date
        ),
    )


def _desired(side: PointSide, positive: bool) -> bool:
    return positive if side == "buy" else not positive


def _fresh_context_event(
    evidence: SamePeriodTechnicalContext,
    event_at: datetime | None,
) -> bool:
    if event_at is None:
        return False
    maximum_age = timedelta(days=21) if evidence.frequency == "d" else timedelta(days=4)
    return evidence.observed_at - event_at <= maximum_age


def _frequency_stance(
    evidence: SamePeriodTechnicalContext | None,
    structure: TimeframeContext,
    *,
    side: PointSide,
    point_type: PointType,
) -> tuple[ContextStance, tuple[str, ...]]:
    votes: list[int] = []
    reasons: list[str] = []
    prefix = structure.frequency.upper()
    if structure.disposition == "supportive":
        votes.append(1 if side == "buy" else -1)
    elif structure.disposition == "hostile":
        votes.append(-1 if side == "buy" else 1)
    if evidence is None:
        reasons.append(f"{prefix}_SAME_PERIOD_CONTEXT_UNAVAILABLE")
    else:
        prefix = evidence.frequency.upper()
        is_first = point_type in {"1buy", "1sell"}
        is_second = point_type in {"2buy", "2sell"}
        is_third = point_type in {"3buy", "3sell"}
        if is_first:
            reasons.append(f"{prefix}_MA_CONTEXT_ONLY_FOR_FIRST_POINT")
        if is_second:
            ma_facts: list[int] = []
            if evidence.close_vs_ma5 in {"above", "below"}:
                above = evidence.close_vs_ma5 == "above"
                ma_facts.append(1 if _desired(side, above) else -1)
                reasons.append(
                    f"{prefix}_MA5_{'SUPPORTS' if _desired(side, above) else 'OPPOSES'}_{side.upper()}"
                )
            if evidence.ma_cross in {"golden", "death"}:
                positive = evidence.ma_cross == "golden"
                ma_facts.append(1 if _desired(side, positive) else -1)
                reasons.append(f"{prefix}_MA_CROSS_{evidence.ma_cross.upper()}")
            if ma_facts:
                votes.append(1 if sum(ma_facts) > 0 else -1 if sum(ma_facts) < 0 else 0)
                reasons.append(
                    f"{prefix}_MA5_STREAK_{evidence.consecutive_closes_vs_ma5}"
                )
        if is_third:
            alignment: list[int] = []
            if evidence.close_vs_ma5 in {"above", "below"}:
                value = evidence.close_vs_ma5 == "above"
                alignment.append(1 if _desired(side, value) else -1)
            if evidence.ma5_vs_ma10 in {"ma5_above_ma10", "ma5_below_ma10"}:
                value = evidence.ma5_vs_ma10 == "ma5_above_ma10"
                alignment.append(1 if _desired(side, value) else -1)
            streak_supports = (
                evidence.consecutive_closes_vs_ma5 >= 2
                if side == "buy"
                else evidence.consecutive_closes_vs_ma5 <= -2
            )
            if evidence.consecutive_closes_vs_ma5 != 0:
                alignment.append(1 if streak_supports else -1)
            if alignment:
                alignment_score = sum(alignment)
                votes.append(
                    1 if alignment_score > 0 else -1 if alignment_score < 0 else 0
                )
                reasons.append(
                    f"{prefix}_MA_ALIGNMENT_{'SUPPORTS' if alignment_score > 0 else 'OPPOSES' if alignment_score < 0 else 'MIXED'}_{side.upper()}"
                )
        pen_fresh = _fresh_context_event(evidence, evidence.latest_pen_end_at)
        fractal_fresh = _fresh_context_event(
            evidence,
            evidence.fractal_confirmed_at,
        )
        if (
            evidence.latest_pen_direction in {"up", "down"}
            and evidence.latest_pen_locked is True
            and pen_fresh
        ):
            positive = evidence.latest_pen_direction == "up"
            votes.append(1 if _desired(side, positive) else -1)
            reasons.append(
                f"{prefix}_LOCKED_PEN_{evidence.latest_pen_direction.upper()}_{'SUPPORTS' if _desired(side, positive) else 'OPPOSES'}_{side.upper()}"
            )
        elif (
            evidence.fractal_type in {"top", "bottom"}
            and evidence.fractal_state
            in {
                "confirmed",
                "pen_endpoint_pending_lock",
                "pen_locked",
                "continuation",
            }
            and fractal_fresh
        ):
            positive = evidence.fractal_type == "bottom"
            votes.append(1 if _desired(side, positive) else -1)
            reasons.append(
                f"{prefix}_{evidence.fractal_type.upper()}_FRACTAL_{'SUPPORTS' if _desired(side, positive) else 'OPPOSES'}_{side.upper()}"
            )
        elif (
            evidence.latest_pen_end_at is not None
            or evidence.fractal_confirmed_at is not None
        ):
            reasons.append(f"{prefix}_STRUCTURE_CONTEXT_STALE")
    if not votes:
        return "unresolved", tuple(dict.fromkeys(reasons))
    score = sum(votes)
    return (
        "supportive" if score > 0 else "adverse" if score < 0 else "neutral",
        tuple(dict.fromkeys(reasons)),
    )


def assess_signal_context(
    *,
    side: PointSide,
    point_type: PointType,
    daily_evidence: SamePeriodTechnicalContext | None,
    thirty_minute_evidence: SamePeriodTechnicalContext | None,
    daily_structure: TimeframeContext,
    thirty_minute_structure: TimeframeContext,
) -> SignalContextAssessment:
    """给环境分级；返回值永远不会改写 5m/1m 结构事实。"""

    daily_stance, daily_reasons = _frequency_stance(
        daily_evidence,
        daily_structure,
        side=side,
        point_type=point_type,
    )
    thirty_stance, thirty_reasons = _frequency_stance(
        thirty_minute_evidence,
        thirty_minute_structure,
        side=side,
        point_type=point_type,
    )
    stances = (daily_stance, thirty_stance)
    completeness_reasons: list[str] = []
    for frequency, evidence in (
        ("D", daily_evidence),
        ("30M", thirty_minute_evidence),
    ):
        if evidence is None:
            completeness_reasons.append(f"{frequency}_SAME_PERIOD_CONTEXT_UNAVAILABLE")
        elif point_type in {"2buy", "2sell"} and (
            evidence.close_vs_ma5 == "unresolved"
        ):
            completeness_reasons.append(f"{frequency}_MA5_REQUIRED_FOR_SECOND_POINT")
        elif point_type in {"3buy", "3sell"} and (
            evidence.close_vs_ma5 == "unresolved"
            or evidence.ma5_vs_ma10 == "unresolved"
        ):
            completeness_reasons.append(
                f"{frequency}_MA5_MA10_REQUIRED_FOR_THIRD_POINT"
            )
    if stances == ("supportive", "supportive"):
        grade: ContextGrade = "A"
    elif all(value == "unresolved" for value in stances):
        grade = "UNRESOLVED"
    elif (
        stances == ("adverse", "adverse")
        or (thirty_stance == "adverse" and daily_stance != "supportive")
        or (daily_stance == "adverse" and thirty_stance in {"neutral", "unresolved"})
    ):
        grade = "C"
    else:
        grade = "B"
    # A 级必须有两个真实物理周期所需的完整证据。缺少 MA 数据可以继续观察，
    # 但不能仅凭结构方向把不完整上下文包装成“双周期支持”。
    if grade == "A" and completeness_reasons:
        grade = "B"
    return SignalContextAssessment(
        grade=grade,
        daily_stance=daily_stance,
        thirty_minute_stance=thirty_stance,
        reason_codes=tuple(
            dict.fromkeys(
                (
                    f"CONTEXT_GRADE_{grade}",
                    *daily_reasons,
                    *thirty_reasons,
                    *completeness_reasons,
                )
            )
        ),
    )


__all__ = (
    "SamePeriodTechnicalContext",
    "SignalContextAssessment",
    "assess_signal_context",
    "build_same_period_technical_context",
    "signal_context_risk_scale",
)
