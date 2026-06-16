"""strategy_optimizer 策略候选目录 —— 从 _impl 拆出。

A/US 实盘策略候选 + 选股系统定义。evidence_summary 为内联快照(硬编码),
只依赖 models 数据类,不依赖常量/helper。
"""
from __future__ import annotations

from typing import Optional

from chanlun.recursive_bt.strategy_optimizer.models import (
    SelectionSystem,
    StrategyCandidate,
)


def a_selection_systems() -> tuple[SelectionSystem, ...]:
    return (
        SelectionSystem(
            "fundamental",
            "quality_growth",
            "Point-in-time ROE/growth filter; avoids pure chart-only bets.",
        ),
        SelectionSystem(
            "comparison",
            "relative_value",
            "ROE/PB style valuation comparison across the current universe.",
        ),
        SelectionSystem(
            "technical",
            "chanlun_buy_point",
            "1/2/3 class buy points with 30m not-down and 5m soft gate.",
        ),
    )


def default_strategy_candidates(market: Optional[str] = None) -> tuple[StrategyCandidate, ...]:
    candidates = _a_candidates() + _us_candidates()
    if market is None:
        return candidates
    market = str(market).strip().lower()
    return tuple(candidate for candidate in candidates if candidate.market == market)


def _a_candidates() -> tuple[StrategyCandidate, ...]:
    common = {
        "market": "a",
        "op_level": "1m",
        "mid_level": "5m",
        "big_level": "30m",
        "mid_gate": "soft",
        "require": ("tech", "fund", "value"),
        "selection_systems": ("technical", "fundamental", "comparison"),
        "selection_max_codes": 90,
        "universe": "true full A walk-forward, 5143 loaded symbols",
    }
    return (
        StrategyCandidate(
            id="a_full_market_balanced",
            name="A full-market balanced default",
            description="Current A-share default: full-market three-system pool, 30 slots, 1m execution.",
            max_pos=30,
            evidence_summary={
                "total_return": 1.281,
                "max_drawdown": 0.070,
                "sharpe": 5.10,
                "trade_count": 1451,
            },
            **common,
        ),
        StrategyCandidate(
            id="a_full_market_low_dd",
            name="A full-market low drawdown",
            description="Lower-volatility A-share profile; gives up return for lower drawdown.",
            max_pos=50,
            evidence_summary={
                "total_return": 1.097,
                "max_drawdown": 0.067,
                "sharpe": 5.05,
                "trade_count": 2409,
            },
            **common,
        ),
        StrategyCandidate(
            id="a_concentrated_trend3_research",
            name="A concentrated trend-3 research",
            description="Research-only concentrated profile; not the current A default after GEM refill.",
            max_pos=10,
            nest_mode="soft",
            trend_3boost=True,
            evidence_summary={
                "total_return": 0.903,
                "max_drawdown": 0.090,
                "sharpe": 2.91,
                "trade_count": 457,
            },
            **common,
        ),
    )


def _us_candidates() -> tuple[StrategyCandidate, ...]:
    common = {
        "market": "us",
        "op_level": "1m",
        "mid_level": "5m",
        "big_level": "30m",
        "selection_systems": ("technical",),
        "universe": "online core 9: SPY/QQQ/AAPL/MSFT/NVDA/AMZN/META/GOOGL/TSLA",
    }
    return (
        StrategyCandidate(
            id="us_core9_default",
            name="US core-9 default",
            description="Current US default: soft 5m gate, trend-3 boost, soft interval nesting.",
            max_pos=9,
            mid_gate="soft",
            nest_mode="soft",
            trend_3boost=True,
            evidence_summary={
                "total_return": 0.196,
                "max_drawdown": 0.015,
                "sharpe": 9.31,
                "trade_count": 450,
            },
            **common,
        ),
        StrategyCandidate(
            id="us_core9_tech_soft",
            name="US core-9 technical soft",
            description="Technical-only soft-gate baseline for US core names.",
            max_pos=9,
            mid_gate="soft",
            evidence_summary={
                "total_return": 0.162,
                "max_drawdown": 0.013,
                "sharpe": 9.10,
                "trade_count": 450,
            },
            **common,
        ),
        StrategyCandidate(
            id="us_core9_strict_control",
            name="US core-9 strict control",
            description="Strict 5m gate control; useful to monitor soft-gate drift.",
            max_pos=9,
            mid_gate="strict",
            nest_mode="soft",
            trend_3boost=True,
            evidence_summary={
                "total_return": 0.149,
                "max_drawdown": 0.016,
                "sharpe": 8.15,
                "trade_count": 244,
            },
            **common,
        ),
    )
