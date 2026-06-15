# -*- coding: utf-8 -*-
"""
中枢-free 缠论信号评估器。

在「力度对比内核」之上叠加：

* 信号识别 —— 背驰类（笔背驰 / 线段背驰）+ 结构类（新线段 / 笔破位 / 停顿）
* 四支柱 —— 操作级别、多级别共振、区间套、信号分级

只用 分型 / 笔 / 线段 + MACD，不以中枢做硬触发；中枢仅作 ``zs_context`` 参考层。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from chanlun.core.types import ICL, LINE
from chanlun.signal_monitor.cl_signal import (
    DIRECTION_BEARISH,
    DIRECTION_BULLISH,
    GRADE_A,
    GRADE_B,
    GRADE_C,
    SIGNAL_KIND_BI_BEICHI,
    SIGNAL_KIND_BI_BREAK,
    SIGNAL_KIND_BI_PAUSE,
    SIGNAL_KIND_XD_BEICHI,
    SIGNAL_KINDS,
    ClSignal,
    build_line_stable_key,
    direction_of_line,
)
from chanlun.signal_monitor.strength_compare import compare_strength

# 各市场默认级别梯队（由高到低）：[高一级别, 操作级别候选, 次级别]
DEFAULT_LEVEL_LADDERS: Dict[str, List[str]] = {
    "a": ["d", "30m", "5m"],
    "futures": ["d", "30m", "5m"],
    "hk": ["d", "30m", "5m"],
    "us": ["d", "30m", "5m"],
    "currency": ["d", "30m", "5m"],
}

_GRADE_ORDER = {GRADE_C: 0, GRADE_B: 1, GRADE_A: 2}

_BEICHI_KINDS = (SIGNAL_KIND_BI_BEICHI, SIGNAL_KIND_XD_BEICHI)


@dataclass
class EvaluatorConfig:
    """信号评估配置。"""

    operation_level: str             # 操作级别周期
    level_ladder: List[str]          # 级别梯队（由高到低），operation_level 必须在其中
    signal_kinds: tuple = SIGNAL_KINDS
    min_grade: str = GRADE_C         # 低于此分级的信号被过滤

    def __post_init__(self) -> None:
        if not self.level_ladder:
            raise ValueError("level_ladder 不能为空")
        if self.operation_level not in self.level_ladder:
            raise ValueError(
                f"operation_level={self.operation_level!r} 必须在 "
                f"level_ladder={self.level_ladder} 中"
            )
        if self.min_grade not in _GRADE_ORDER:
            raise ValueError(f"min_grade 非法: {self.min_grade!r}")

    @property
    def _op_idx(self) -> int:
        return self.level_ladder.index(self.operation_level)

    @property
    def high_level(self):
        """操作级别的高一级别（梯队里更靠前的一个），无则 None。"""
        i = self._op_idx
        return self.level_ladder[i - 1] if i > 0 else None

    @property
    def sub_level(self):
        """操作级别的次级别（梯队里更靠后的一个），无则 None。"""
        i = self._op_idx
        return self.level_ladder[i + 1] if i + 1 < len(self.level_ladder) else None


# 信号类型基础分 —— 由验证闭环在 9 标的 307 信号上实测校准（详见验证报告）：
#   xd_beichi 胜率 75% / 前瞻 +1.04%；bi_break 66% / +0.80%；bi_beichi 56% / -0.13%
# 旧评分（背驰强度 + 共振 + 区间套加成）经验证为非单调、A 不优于 C，已废弃。
_KIND_BASE_SCORE = {
    SIGNAL_KIND_XD_BEICHI: 78,   # 线段背驰 —— 实测最强
    SIGNAL_KIND_BI_BREAK: 62,    # 笔不创新高/低 —— 实测稳健微正
    SIGNAL_KIND_BI_PAUSE: 45,    # 笔停顿 —— 触发极少，暂取中性
    SIGNAL_KIND_BI_BEICHI: 40,   # 笔背驰 —— 实测无边际优势（根因：缺趋势前提）
}


def grade_signal(signal: ClSignal) -> ClSignal:
    """计算 score 与 grade（原地写回）。

    评分以**信号类型**为锚 —— 验证闭环实测信号类型是预测力的主轴；背驰类再按
    力度强度做小幅微调（±5 内，不喧宾夺主）。共振 / 区间套经验证未带来正向区分度，
    **不计入评分**（仍作为 ``resonance`` / ``zs_context`` 等参考信息保留）。
    """
    score = _KIND_BASE_SCORE.get(signal.signal_kind, 45)
    if signal.strength is not None:
        # strength_score 0-100 → 微调 -5..+5
        score += int(round((signal.strength.strength_score - 50) / 10))
    score = max(0, min(100, score))
    signal.score = score
    if score >= 70:
        signal.grade = GRADE_A
    elif score >= 55:
        signal.grade = GRADE_B
    else:
        signal.grade = GRADE_C
    return signal


class ClSignalEvaluator:
    """缠论信号评估器。一个实例对应一个标的。"""

    def __init__(self, market: str, code: str, name: str = ""):
        self.market = market
        self.code = code
        self.name = name

    # ------------------------------------------------------------------
    # 背驰类信号
    # ------------------------------------------------------------------
    def detect_beichi_signals(self, cd: ICL, level: str) -> List[ClSignal]:
        """识别本级别最后一笔 / 最后一线段的背驰信号。"""
        signals: List[ClSignal] = []
        signals += self._beichi_on_lines(
            cd, cd.get_bis(), level, SIGNAL_KIND_BI_BEICHI, "笔"
        )
        signals += self._beichi_on_lines(
            cd, cd.get_xds(), level, SIGNAL_KIND_XD_BEICHI, "线段"
        )
        return signals

    def _beichi_on_lines(
        self, cd: ICL, lines: List[LINE], level: str, kind: str, label: str
    ) -> List[ClSignal]:
        # 笔/线段严格上下交替，前一同向段是 lines[-3]
        if len(lines) < 3:
            return []
        cur = lines[-1]
        prev_same = lines[-3]
        if prev_same.type != cur.type:
            return []
        try:
            res = compare_strength(prev_same, cur, cd)
        except Exception:
            # 力度计算依赖 MACD，数据不足时跳过该信号，不阻断整批
            return []
        if not res.is_beichi:
            return []
        sig = self._make_signal(
            cd=cd, line=cur, level=level, kind=kind,
            direction=direction_of_line(cur.type),
            msg=(f"{level} {label}背驰：{cur.type} 段力度衰减"
                 f"（面积比 {res.macd_area_ratio:.2f}）"),
        )
        sig.strength = res
        sig.interval_nesting["line_done"] = bool(cur.is_done())
        return [sig]

    # ------------------------------------------------------------------
    # 结构类信号
    # ------------------------------------------------------------------
    def detect_structure_signals(self, cd: ICL, level: str) -> List[ClSignal]:
        """识别笔破位 / 笔停顿。

        注：原"新线段成立(new_xd)"信号已移除 —— 验证闭环实测其在真实行情上
        反向（胜率 ~20%、平均前瞻收益 ~-1.9%），属滞后/衰竭事实、非方向信号。
        """
        signals: List[ClSignal] = []
        bis = cd.get_bis()

        # 笔破位：最后一笔相对前一同向笔「未创新高/新低」→ 转折提示
        if len(bis) >= 3 and bis[-1].type == bis[-3].type:
            cur, prev_same = bis[-1], bis[-3]
            if cur.type == "down" and cur.low >= prev_same.low:
                signals.append(self._make_signal(
                    cd=cd, line=cur, level=level, kind=SIGNAL_KIND_BI_BREAK,
                    direction=DIRECTION_BULLISH,
                    msg=f"{level} 笔不创新低（前低 {prev_same.low} 未破）",
                ))
            elif cur.type == "up" and cur.high <= prev_same.high:
                signals.append(self._make_signal(
                    cd=cd, line=cur, level=level, kind=SIGNAL_KIND_BI_BREAK,
                    direction=DIRECTION_BEARISH,
                    msg=f"{level} 笔不创新高（前高 {prev_same.high} 未破）",
                ))

        # 笔停顿
        if bis and self._bi_paused(bis[-1], cd):
            cur = bis[-1]
            signals.append(self._make_signal(
                cd=cd, line=cur, level=level, kind=SIGNAL_KIND_BI_PAUSE,
                direction=direction_of_line(cur.type),
                msg=f"{level} 笔停顿（{cur.type} 段）",
            ))
        return signals

    @staticmethod
    def _bi_paused(bi: LINE, cd: ICL) -> bool:
        """笔是否停顿。lazy import，避免 signal_monitor 包硬依赖 cl_utils。"""
        try:
            from chanlun.cl_utils import bi_td
            return bool(bi_td(bi, cd))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def evaluate(
        self, cds_by_level: Dict[str, ICL], config: EvaluatorConfig
    ) -> List[ClSignal]:
        """对一个标的的级别梯队数据做评估，返回分级后的信号列表。"""
        op_cd = cds_by_level.get(config.operation_level)
        if op_cd is None:
            return []

        raw: List[ClSignal] = []
        raw += self.detect_beichi_signals(op_cd, config.operation_level)
        raw += self.detect_structure_signals(op_cd, config.operation_level)
        raw = [s for s in raw if s.signal_kind in config.signal_kinds]

        out: List[ClSignal] = []
        for sig in raw:
            self._apply_resonance(sig, cds_by_level, config)
            self._apply_interval_nesting(sig, cds_by_level, config)
            grade_signal(sig)
            if _GRADE_ORDER[sig.grade] >= _GRADE_ORDER[config.min_grade]:
                out.append(sig)
        return out

    # ------------------------------------------------------------------
    # 四支柱辅助
    # ------------------------------------------------------------------
    def _make_signal(
        self, cd: ICL, line: LINE, level: str, kind: str,
        direction: str, msg: str,
    ) -> ClSignal:
        """构造 ClSignal 并就地附上中枢参考上下文。"""
        sig = ClSignal(
            market=self.market, code=self.code, name=self.name,
            operation_level=level, signal_kind=kind, direction=direction,
            line_stable_key=build_line_stable_key(line),
            k_date=getattr(getattr(line.end, "k", None), "date", None),
            msg=msg,
        )
        sig.zs_context = self._zs_context(cd, line)
        return sig

    @staticmethod
    def _zs_context(cd: ICL, line: LINE) -> dict:
        """中枢参考层：标注信号触发价相对最近线段中枢区间的位置。"""
        try:
            zss = cd.get_xd_zss()
        except Exception:
            zss = []
        price = getattr(line.end, "val", None)
        if not zss or price is None:
            return {"has_zs": False}
        last = zss[-1]
        zg = getattr(last, "zg", None)
        zd = getattr(last, "zd", None)
        if zg is None or zd is None:
            return {"has_zs": False}
        if price > zg:
            pos = "above"
        elif price < zd:
            pos = "below"
        else:
            pos = "inside"
        return {"has_zs": True, "zg": zg, "zd": zd, "price": price, "price_vs_zs": pos}

    def _apply_resonance(
        self, sig: ClSignal, cds_by_level: Dict[str, ICL], config: EvaluatorConfig
    ) -> None:
        """多级别共振：高级别方向佐证 + 次级别同步背驰。"""
        high = config.high_level
        sub = config.sub_level
        if high and cds_by_level.get(high) is not None:
            xds = cds_by_level[high].get_xds()
            if xds:
                high_type = xds[-1].type
                sig.resonance["high_level"] = high
                sig.resonance["high_xd_type"] = high_type
                sig.resonance["high_aligned"] = (
                    (sig.direction == DIRECTION_BULLISH and high_type == "up")
                    or (sig.direction == DIRECTION_BEARISH and high_type == "down")
                )
        if sub and cds_by_level.get(sub) is not None:
            sub_sigs = self.detect_beichi_signals(cds_by_level[sub], sub)
            sig.resonance["sub_beichi"] = any(
                s.direction == sig.direction for s in sub_sigs
            )

    def _apply_interval_nesting(
        self, sig: ClSignal, cds_by_level: Dict[str, ICL], config: EvaluatorConfig
    ) -> None:
        """区间套：仅背驰类信号 —— 次级别在同方向上也出现背驰则视为嵌套确认。"""
        if sig.signal_kind not in _BEICHI_KINDS:
            return
        sub = config.sub_level
        if not sub or cds_by_level.get(sub) is None:
            return
        sub_sigs = self.detect_beichi_signals(cds_by_level[sub], sub)
        matched = [s for s in sub_sigs if s.direction == sig.direction]
        sig.interval_nesting["nested"] = bool(matched)
        if matched:
            sig.interval_nesting["sub_point"] = matched[0].line_stable_key
