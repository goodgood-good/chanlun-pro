# -*- coding: utf-8 -*-
"""
缠论信号对象。

``ClSignal`` 是中枢-free 监控的统一信号载体：背驰类 / 结构类信号都用它表示。
``identity`` 用于跨扫描轮次去重（自动监控按它判定"是否已报过"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from chanlun.core.types import LINE
from chanlun.signal_monitor.strength_compare import StrengthCompareResult

# ---------------------- 信号种类 ----------------------
SIGNAL_KIND_BI_BEICHI = "bi_beichi"   # 笔背驰
SIGNAL_KIND_XD_BEICHI = "xd_beichi"   # 线段背驰
SIGNAL_KIND_BI_BREAK = "bi_break"     # 笔破/不破前同向极值
SIGNAL_KIND_BI_PAUSE = "bi_pause"     # 笔停顿
# 注：原 new_xd（新线段成立）信号已移除 —— 实测该信号为滞后/衰竭事实，
# 不构成有效方向信号。

SIGNAL_KINDS = (
    SIGNAL_KIND_BI_BEICHI,
    SIGNAL_KIND_XD_BEICHI,
    SIGNAL_KIND_BI_BREAK,
    SIGNAL_KIND_BI_PAUSE,
)

# ---------------------- 信号方向 ----------------------
DIRECTION_BULLISH = "bullish"  # 偏多（下跌段背驰 / 向上结构）
DIRECTION_BEARISH = "bearish"  # 偏空（上涨段背驰 / 向下结构）
DIRECTIONS = (DIRECTION_BULLISH, DIRECTION_BEARISH)

# ---------------------- 信号分级 ----------------------
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"


def direction_of_line(line_type: str) -> str:
    """笔/线段方向 → 信号方向：down 段（背驰）偏多，up 段（背驰）偏空。"""
    return DIRECTION_BULLISH if line_type == "down" else DIRECTION_BEARISH


def _fmt_date(date: Any) -> str:
    return date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(date, "strftime") else str(date)


def build_line_stable_key(line: LINE) -> str:
    """线段的稳定标识：起点分型 K 线时间 + 线方向 + 完成状态。

    增量计算会重排 ``line.index``，起点分型的 K 线日期 + 方向**仅对已完成
    (done)段稳定**；当下正在形成(forming)的最后一笔/线段，其起点分型在新
    bar 到来时可能被改写(成笔前左右横跳/末段被重划)。S1(done-gating): 把
    ``done``/``forming`` 纳入身份——「形成中」与「已确认」是两个 identity，
    forming 段起点漂移时仍可能多发 forming 信号，但一旦该段 done，identity
    切到稳定的 done 版，不再随后续 K 漂移重发。配合 S4(只喂收盘 bar)后
    forming 段已大幅减少，二者叠加根治重复推送。

    ``is_done()`` 经 getattr 兜底:实际调用方(evaluator)传入的是 BI/XD
    对象(均有真实 ``is_done()``);异常对象退化为 ``forming``，不致崩溃。
    """
    start = getattr(line, "start", None)
    k = getattr(start, "k", None)
    date = getattr(k, "date", None)
    is_done_fn = getattr(line, "is_done", None)
    done = bool(is_done_fn()) if callable(is_done_fn) else False
    return f"{_fmt_date(date)}|{line.type}|{'done' if done else 'forming'}"


def make_identity(
    code: str,
    operation_level: str,
    signal_kind: str,
    direction: str,
    line_stable_key: str,
) -> str:
    """去重身份键：同 identity 视为同一个信号。"""
    return f"{code}|{operation_level}|{signal_kind}|{direction}|{line_stable_key}"


@dataclass
class ClSignal:
    """中枢-free 缠论监控信号。"""

    market: str
    code: str
    name: str
    operation_level: str             # 操作级别周期，如 "30m"
    signal_kind: str                 # SIGNAL_KINDS 之一
    direction: str                   # bullish / bearish
    line_stable_key: str             # 信号所在段的稳定标识
    strength: Optional[StrengthCompareResult] = None
    resonance: dict = field(default_factory=dict)
    interval_nesting: dict = field(default_factory=dict)
    zs_context: dict = field(default_factory=dict)
    score: int = 0
    grade: str = GRADE_C
    msg: str = ""
    k_date: Any = None               # 触发 K 线时间

    @property
    def identity(self) -> str:
        return make_identity(
            self.code, self.operation_level, self.signal_kind,
            self.direction, self.line_stable_key,
        )

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "operation_level": self.operation_level,
            "signal_kind": self.signal_kind,
            "direction": self.direction,
            "line_stable_key": self.line_stable_key,
            "score": self.score,
            "grade": self.grade,
            "resonance": self.resonance,
            "interval_nesting": self.interval_nesting,
            "zs_context": self.zs_context,
            "strength": (self.strength.__dict__ if self.strength is not None else None),
            "msg": self.msg,
            "identity": self.identity,
            "k_date": _fmt_date(self.k_date) if self.k_date is not None else None,
        }
