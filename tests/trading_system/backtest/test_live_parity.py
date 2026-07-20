from __future__ import annotations

from datetime import timedelta

from chanlun.decision_support.trading_system.backtest.portfolio import (
    CausalStructureReplay,
    replay_engine_decisions,
)
from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
    TradingEngine,
)
from tests.trading_system.backtest.helpers import dataset, minute_bar, normal_status
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    eligible_sector,
)


def deterministic_bundle() -> SymbolStructureBundle:
    return SymbolStructureBundle(
        code="SZ.000001",
        as_of=AS_OF,
        sector=eligible_sector(),
        thirty_direction="neutral",
        thirty_points=(),
        five_points=(confirmed_point("2buy"),),
        one_points=(
            confirmed_point("1buy", frequency="1m", minutes_after=1),
        ),
        opposite_points=(),
    )


class StaticBuilder:
    def __init__(self, bundle: SymbolStructureBundle) -> None:
        self.bundle = bundle
        self.seen_closed_at = ()

    def build_bundle(self, *, code, closed_at, bars):
        assert code == self.bundle.code
        assert closed_at == self.bundle.as_of
        self.seen_closed_at = tuple(bar.closed_at for bar in bars)
        return self.bundle


def test_backtest_and_live_adapter_emit_identical_engine_decision() -> None:
    bundle = deterministic_bundle()
    bar = minute_bar(
        opened_at=AS_OF - timedelta(minutes=1),
        closed_at=AS_OF,
        adjustment_known_at=AS_OF,
    )
    source = dataset(
        bars=(bar,),
        statuses=(normal_status(session=AS_OF.date()),),
    )
    engine = TradingEngine()
    builder = StaticBuilder(bundle)

    live = engine.evaluate_symbol(bundle)
    replayed = replay_engine_decisions(
        source,
        engine=engine,
        structure_replay=CausalStructureReplay(builder),
        closed_at=AS_OF,
    )

    assert replayed == ((bundle.code, live),)


def test_causal_replay_never_passes_future_bars_to_builder() -> None:
    bundle = deterministic_bundle()
    current = minute_bar(
        opened_at=AS_OF - timedelta(minutes=1),
        closed_at=AS_OF,
        adjustment_known_at=AS_OF,
    )
    future_at = AS_OF + timedelta(minutes=1)
    future = minute_bar(
        opened_at=AS_OF,
        closed_at=future_at,
        adjustment_known_at=future_at,
    )
    source = dataset(
        bars=(future, current),
        statuses=(normal_status(session=AS_OF.date()),),
    )
    builder = StaticBuilder(bundle)

    CausalStructureReplay(builder).bundle_at(
        dataset=source,
        closed_at=AS_OF,
        code=bundle.code,
    )

    assert builder.seen_closed_at == (AS_OF,)
