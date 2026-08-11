from __future__ import annotations

from pathlib import Path

import pandas as pd

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    final_confirmed_structure_events,
    strict_state,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def frame(rows: int) -> pd.DataFrame:
    value = pd.read_parquet(FIXTURES / "SZ.002299_1m.parquet")[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(rows).copy()
    value.insert(0, "code", "SZ.002299")
    for field in ("open", "high", "low", "close"):
        value[f"raw_{field}"] = value[field]
    value = value.loc[:, list(FRAME_COLUMNS)]
    value.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    return value


def test_point_and_completed_trend_ledgers_are_prefix_invariant() -> None:
    prefix_frame = frame(900)
    full_frame = frame(1100)
    cutoff = prefix_frame.iloc[-1]["date"]

    prefix = final_confirmed_structure_events(
        "SZ.002299", "1m", prefix_frame
    )
    full = final_confirmed_structure_events("SZ.002299", "1m", full_frame)

    assert prefix.points
    assert prefix.completed_trends
    assert prefix.points == tuple(
        point for point in full.points if point.available_at <= cutoff
    )
    assert prefix.completed_trends == tuple(
        trend for trend in full.completed_trends if trend.available_at <= cutoff
    )


def test_causal_structure_uses_original_old_pen_recursive_profile() -> None:
    state = strict_state("SZ.002299", "1m", frame(900))
    config = state.get_config()

    assert config["stroke_rule"] == "strict-cl-k-distance"
    assert config["recursive_structure_scope"] == "same-source-direct-recursion"
