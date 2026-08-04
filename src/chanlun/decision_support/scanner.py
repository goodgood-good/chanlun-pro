"""Public surface for the sole active Chanlun trading system."""

from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
    TradingEngine,
    evaluate_symbol,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)
from chanlun.decision_support.structure_snapshot import SymbolStructureSnapshot


ACTIVE_STRATEGY_ID = STRICT_STRATEGY_ID


__all__ = (
    "ACTIVE_STRATEGY_ID",
    "EvaluatedSignal",
    "SymbolStructureBundle",
    "TradingEngine",
    "SymbolStructureSnapshot",
    "evaluate_symbol",
)
