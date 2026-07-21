from __future__ import annotations

from datetime import timedelta

import pandas as pd

from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.decision_support.trading_system.backtest.data_source import (
    CausalStructureReplay as AtomicStructureReplay,
)
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
from tests.trading_system.strict_helpers import (
    StrictOnlyCL,
    strict_evidence_result,
    strict_point,
)
from cl_app.services import trading_screening_gateway as gateway_module
from cl_app.services.trading_screening_gateway import analyze_native_frame


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


def test_live_and_replay_share_strict_point_ids_and_availability(monkeypatch) -> None:
    confirmed = strict_point("2buy", available_at=AS_OF)
    approaching = strict_point(
        "3buy",
        status=StrictPointStatus.APPROACHING,
        available_at=AS_OF,
    )
    live_evidence = strict_evidence_result(
        source_frequency="5m",
        source_closed_at=AS_OF,
        confirmed_points=(confirmed,),
        approaching_points=(approaching,),
    )
    live_state = StrictOnlyCL(live_evidence)
    monkeypatch.setattr(
        gateway_module,
        "CL",
        lambda *_args, **_kwargs: live_state,
    )
    frame = pd.DataFrame(
        {
            "date": [AS_OF],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1000.0],
        }
    )
    frame.attrs["structure_price_quantum"] = "0.01"
    frame.attrs["price_basis_revision"] = "test-raw-v1"
    live = analyze_native_frame(
        code="SZ.000001",
        frequency="5m",
        frame=frame,
        as_of=AS_OF,
    )

    replay_states: dict[str, StrictOnlyCL] = {}

    def replay_factory(
        code: str,
        frequency: str,
        _snapshot: pd.DataFrame,
    ) -> StrictOnlyCL:
        evidence = (
            live_evidence
            if frequency == "5m"
            else strict_evidence_result(
                code=code,
                source_frequency=frequency,
                source_closed_at=AS_OF,
            )
        )
        state = StrictOnlyCL(evidence)
        replay_states[frequency] = state
        return state

    replay = AtomicStructureReplay(
        frames={
            ("SZ.000001", frequency): frame.copy()
            for frequency in ("1m", "5m", "30m")
        },
        cl_factory=replay_factory,
    )
    bundle = replay.bundle_at(
        dataset=dataset(),
        closed_at=AS_OF,
        code="SZ.000001",
    )
    [replay_confirmed] = [
        point for point in bundle.five_points if point.status == "confirmed"
    ]
    [replay_approaching] = [
        point for point in bundle.five_points if point.status == "provisional"
    ]

    assert (live.confirmed_points[0].point_id, live.confirmed_points[0].available_at) == (
        replay_confirmed.point_id,
        replay_confirmed.available_at,
    )
    assert (
        live.provisional_points[0].candidate_id,
        live.provisional_points[0].observed_at,
    ) == (replay_approaching.candidate_id, replay_approaching.observed_at)
    assert live_state.evidence_calls == 1
    assert all(state.evidence_calls == 1 for state in replay_states.values())
