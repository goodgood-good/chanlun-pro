from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    final_confirmed_structure_events,
    strict_state,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_current_confirmed_points,
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
    assert prefix.point_visibility
    assert prefix.completed_trends
    assert prefix.points == tuple(
        point for point in full.points if point.available_at <= cutoff
    )
    assert prefix.completed_trends == tuple(
        trend for trend in full.completed_trends if trend.available_at <= cutoff
    )
    assert prefix.point_visibility == tuple(
        replace(
            interval,
            visible_until=(
                None
                if interval.visible_until is None
                or interval.visible_until > cutoff
                else interval.visible_until
            ),
        )
        for interval in full.point_visibility
        if interval.visible_from <= cutoff
    )


def test_causal_structure_uses_the_single_strict_profile() -> None:
    state = strict_state("SZ.002299", "1m", frame(900))
    config = state.get_config()

    assert config["stroke_rule"] == "strict-cl-k-distance"
    assert config["strict_macd_source"] == "native_l0_causal_recursive"
    assert "recursive_structure_scope" not in config
    assert "screening_structure_scope" not in config


def test_causal_current_point_state_matches_production_cold_projection() -> None:
    source = frame(900)
    cutoff = source.iloc[-1]["date"]
    ledger = final_confirmed_structure_events("SZ.002299", "1m", source)
    evidence = screening_evidence_from_frame(
        code="SZ.002299",
        frequency="1m",
        frame=source,
        as_of=cutoff,
    )
    production = extract_current_confirmed_points(
        evidence,
        code="SZ.002299",
        source_frequency="1m",
        as_of=cutoff,
    )
    active_ids = {
        interval.point_id
        for interval in ledger.point_visibility
        if interval.contains(cutoff)
    }

    assert active_ids == {point.point_id for point in production}
    stored = {point.point_id: point for point in ledger.points}
    assert {
        point_id: (stored[point_id].point_type, stored[point_id].anchor_at)
        for point_id in active_ids
    } == {
        point.point_id: (point.point_type, point.anchor_at)
        for point in production
    }
