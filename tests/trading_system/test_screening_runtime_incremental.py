"""分钟选股严格运行时的增量复用与重建边界。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.screening_runtime import (
    ScreeningRuntimeState,
)
from cl_app.services import trading_screening_gateway as gateway_module


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "SZ.002299_1m.parquet"


def _fixture_frame(count: int = 1801) -> pd.DataFrame:
    if not FIXTURE.exists():
        pytest.skip("real market fixture is unavailable")
    frame = pd.read_parquet(FIXTURE)[
        ["date", "open", "high", "low", "close", "volume"]
    ].tail(count).reset_index(drop=True)
    frame.attrs.update(
        structure_price_quantum="0.001",
        price_basis_revision="sz002299-raw",
    )
    return frame


def _as_of(frame: pd.DataFrame):
    return pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime()


def test_warm_runtime_result_equals_cold_production_analysis() -> None:
    frame = _fixture_frame()
    initial = frame.iloc[:-1].copy()
    initial.attrs = dict(frame.attrs)
    states = gateway_module._WarmupRuntimeStates(
        full=ScreeningRuntimeState("SZ.002299", "1m"),
        suffix=ScreeningRuntimeState("SZ.002299", "1m"),
    )
    gateway_module.analyze_native_frame_with_warmup(
        code="SZ.002299",
        frequency="1m",
        frame=initial,
        as_of=_as_of(initial),
        runtime_states=states,
    )

    warm = gateway_module.analyze_native_frame_with_warmup(
        code="SZ.002299",
        frequency="1m",
        frame=frame,
        as_of=_as_of(frame),
        runtime_states=states,
    )
    cold = gateway_module.analyze_native_frame_with_warmup(
        code="SZ.002299",
        frequency="1m",
        frame=frame,
        as_of=_as_of(frame),
    )

    assert warm == cold
    assert states.full.update_count == 2
    assert states.full.incremental_update_count == 1
    assert states.full.rebuild_count == 1
    assert states.full.last_update_incremental is True
    assert states.suffix.update_count == 2
    assert states.suffix.incremental_update_count == 1


@pytest.mark.parametrize("change", ("history", "sliding", "price_basis"))
def test_runtime_rebuilds_when_incremental_proof_is_broken(change: str) -> None:
    frame = _fixture_frame(900)
    state = ScreeningRuntimeState("SZ.002299", "1m")
    state.evidence_from_frame(frame=frame, as_of=_as_of(frame))
    previous_engine = state._state

    if change == "history":
        changed = frame.copy(deep=True)
        for column in ("open", "high", "low", "close"):
            changed.loc[100, column] = float(changed.loc[100, column]) + 0.001
    elif change == "sliding":
        changed = frame.iloc[1:].reset_index(drop=True)
    else:
        changed = frame.copy(deep=True)
        changed.attrs["price_basis_revision"] = "sz002299-adjusted"
    changed.attrs = dict(changed.attrs or frame.attrs)

    update = state.update_from_frame(frame=changed, as_of=_as_of(changed))

    assert state._state is not previous_engine
    assert update.incremental_reused is False
    assert state.rebuild_count == 2
    assert state.incremental_update_count == 0
