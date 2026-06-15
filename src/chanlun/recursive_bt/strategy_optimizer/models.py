"""strategy_optimizer 数据类型 —— 从 strategy_optimizer.py 拆出(facade 包化首批)。

6 个不可变数据类:选股系统/策略候选/评分权重/策略评分/运行时摘要源/已评分摘要。
自包含(仅依赖 dataclasses/typing),被 candidates/scoring/reports 各层引用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class SelectionSystem:
    """One independent stock-selection confirmation system."""

    key: str
    name: str
    role: str


@dataclass(frozen=True)
class StrategyCandidate:
    """A concrete live strategy profile plus its latest evidence snapshot."""

    id: str
    market: str
    name: str
    description: str
    max_pos: int
    op_level: str
    mid_level: str
    big_level: str
    mid_gate: str
    require: tuple[str, ...] = ("tech",)
    regime_mode: str = "off"
    nest_mode: str = "off"
    trend_3boost: bool = False
    buy_priority: str = "3first"
    selection_systems: tuple[str, ...] = ()
    selection_max_codes: int = 0
    universe: str = ""
    evidence_summary: Mapping[str, float | int | str] | None = None

    def monitor_config(self) -> dict:
        out = {
            "max_pos": self.max_pos,
            "op_level": self.op_level,
            "mid_level": self.mid_level,
            "big_level": self.big_level,
            "mid_gate": self.mid_gate,
            "regime_mode": self.regime_mode,
            "nest_mode": self.nest_mode,
            "trend_3boost": self.trend_3boost,
            "sell_scope": "all",
        }
        if self.market == "a":
            out.update(
                {
                    "enable_selection_pool": True,
                    "selection_require_three_systems": (
                        set(self.selection_systems)
                        >= {"technical", "fundamental", "comparison"}
                    ),
                    "selection_max_codes": self.selection_max_codes
                    or max(self.max_pos * 3, self.max_pos),
                }
            )
        return out

    def as_dict(self) -> dict:
        data = asdict(self)
        data["require"] = list(self.require)
        data["selection_systems"] = list(self.selection_systems)
        data["evidence_summary"] = dict(self.evidence_summary or {})
        return data


@dataclass(frozen=True)
class ScoreWeights:
    return_weight: float = 1.0
    drawdown_penalty: float = 2.0
    sharpe_weight: float = 0.01
    trade_floor: int = 10
    low_trade_penalty: float = 0.05


@dataclass(frozen=True)
class StrategyScore:
    candidate_id: str
    market: str
    score: float
    total_return: float
    max_drawdown: float
    sharpe: float
    trade_count: int
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeSummarySource:
    """A backtest summary or paper-ledger summary that can be scored."""

    id: str
    market: str
    kind: str
    path: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ScoredRuntimeSummary:
    source: RuntimeSummarySource
    score: StrategyScore
    summary: Mapping[str, object]

    def as_dict(self) -> dict:
        return {
            "source": self.source.as_dict(),
            "score": self.score.as_dict(),
            "summary": dict(self.summary),
        }
