from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.core.strict_structure.current_events import (
    current_strict_events,
    current_strict_point_evidence,
    terminal_segment_windows,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_current_confirmed_points,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _frame() -> pd.DataFrame:
    frame = pd.read_parquet(FIXTURES / "SZ.002299_1m.parquet")[
        ["date", "open", "high", "low", "close", "volume"]
    ].copy()
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-current-strict-events",
    )
    return frame


def _evidence(frame: pd.DataFrame):
    return screening_evidence_from_frame(
        code="SZ.002299",
        frequency="1m",
        frame=frame,
        as_of=pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime(),
        market="a",
    )


def test_delayed_historical_point_is_not_reintroduced_after_terminal_advance() -> None:
    """A late audit timestamp cannot reintroduce an old segment point."""

    frame = _frame()
    target_time = pd.Timestamp("2025-12-15 06:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)

    terminal_ids = {
        (reference.structural_level, reference.unit_id)
        for window in terminal_segment_windows(evidence.structure)
        for reference in window.references
    }
    historical = max(
        (
            point
            for point in evidence.confirmed_points
            if (point.structural_level, point.anchor_unit_id) not in terminal_ids
        ),
        key=lambda point: (point.available_at, point.point_id),
    )
    delayed = replace(
        historical,
        confirmed_at=evidence.source_closed_at,
        available_at=evidence.source_closed_at,
    )
    delayed_snapshot = SimpleNamespace(
        structure=evidence.structure,
        confirmed_points=(delayed,),
        divergences=(),
    )

    assert delayed.available_at == evidence.source_closed_at
    assert (delayed.structural_level, delayed.anchor_unit_id) not in terminal_ids
    assert current_strict_events(delayed_snapshot).points == ()


def test_candidate_stays_current_when_unfinished_segment_becomes_latest_completed() -> None:
    frame = _frame()
    target_time = pd.Timestamp("2025-12-16 01:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)
    current_candidates = current_strict_point_evidence(
        evidence.structure,
        evidence.approaching_points,
    )

    third_buy = next(point for point in current_candidates if point.point_type == "3buy")
    window = terminal_segment_windows(evidence.structure)[third_buy.structural_level]
    assert window.latest_completed is not None
    assert third_buy.anchor_unit_id == window.latest_completed.unit_id
    assert current_strict_events(evidence).points == ()


def test_current_divergence_tracks_a_terminal_segment_point() -> None:
    frame = _frame()
    target_time = pd.Timestamp("2025-12-15 06:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)
    window = terminal_segment_windows(evidence.structure)[0]
    assert window.latest_completed is not None
    divergence = SimpleNamespace(
        divergence_id="terminal-divergence",
        structural_level=0,
        signal_unit_id=window.latest_completed.unit_id,
    )
    point = SimpleNamespace(
        structural_level=0,
        anchor_unit_id=window.latest_completed.unit_id,
        divergence=divergence,
    )
    synthetic = SimpleNamespace(
        structure=evidence.structure,
        confirmed_points=(point,),
        divergences=(divergence,),
    )

    current = current_strict_events(synthetic)
    assert current.points == (point,)
    assert current.divergences == (divergence,)


def test_later_lock_availability_cannot_override_terminal_segment_lineage() -> None:
    evidence = _evidence(_frame())
    level = evidence.structure.levels[0]
    window = terminal_segment_windows(evidence.structure)[0]
    assert window.latest_completed is not None
    historical = level.units[0]
    old_point = SimpleNamespace(
        structural_level=0,
        anchor_unit_id=historical.unit_id,
        divergence=None,
    )
    terminal_point = SimpleNamespace(
        structural_level=0,
        anchor_unit_id=window.latest_completed.unit_id,
        divergence=None,
    )

    assert current_strict_point_evidence(
        evidence.structure,
        (old_point, terminal_point),
    ) == (terminal_point,)


def test_trading_adapter_uses_exact_same_current_confirmation_batch() -> None:
    frame = _frame()
    target_time = pd.Timestamp("2025-12-16 01:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)
    points = extract_current_confirmed_points(
        evidence,
        code=evidence.symbol,
        source_frequency=evidence.source_frequency,
        as_of=evidence.source_closed_at,
    )

    assert points
    assert all(point.status == "confirmed" for point in points)
    assert all(
        "geometry_confirmed_before_audit_lock" in point.evidence_codes
        for point in points
    )
    assert all(point.terminal_segment is not None for point in points)
    assert all(point.terminal_segment.role == "latest_completed" for point in points)
    assert all(point.terminal_segment.state == "formed" for point in points)


def test_real_current_window_is_not_restricted_to_third_class_points() -> None:
    """The live-tail filter selects terminal lineage, not point class."""

    evidence = _evidence(_frame())
    window = terminal_segment_windows(evidence.structure)[0]
    reference = window.latest_completed
    assert reference is not None
    first_class = SimpleNamespace(
        point_type="1buy" if reference.direction == "down" else "1sell",
        structural_level=reference.structural_level,
        anchor_unit_id=reference.unit_id,
    )

    assert current_strict_point_evidence(
        evidence.structure,
        (first_class,),
    ) == (first_class,)
