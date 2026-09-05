from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.core.strict_structure.current_events import (
    current_strict_events,
    current_strict_point_evidence,
    terminal_segment_windows,
)
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind
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


def test_third_buy_exits_current_events_after_successor_segment_completes() -> None:
    frame = _frame()
    confirmation_time = pd.Timestamp("2025-12-16 01:45:00+00:00")
    confirmation_prefix = frame.loc[frame["date"] <= confirmation_time].copy()
    confirmation_prefix.attrs.update(frame.attrs)
    confirmation = _evidence(confirmation_prefix)
    confirmed_window = terminal_segment_windows(confirmation.structure)[0]
    third_buy = next(
        point
        for point in current_strict_point_evidence(
            confirmation.structure,
            (*confirmation.confirmed_points, *confirmation.approaching_points),
        )
        if point.point_type == "3buy"
    )
    assert confirmed_window.latest_completed is not None
    assert third_buy.anchor_unit_id == confirmed_window.latest_completed.unit_id

    advanced = _evidence(frame)
    advanced_window = terminal_segment_windows(advanced.structure)[0]
    assert advanced_window.latest_completed is not None
    assert (
        advanced_window.latest_completed.market_end
        > confirmed_window.latest_completed.market_end
    )
    assert current_strict_point_evidence(advanced.structure, (third_buy,)) == ()


def test_geometrically_completed_successor_retires_prior_point_before_late_lock() -> (
    None
):
    """A solid successor segment is current even before ``formed_at`` arrives."""

    started_at = datetime(2026, 9, 1, 9, 35, tzinfo=timezone.utc)

    def unit(
        unit_id: str,
        *,
        direction: str,
        start_tick: int,
        end_tick: int,
        market_start: datetime,
        market_end: datetime,
        available_at: datetime,
        forming: bool = False,
        formed_at: datetime | None = None,
    ) -> ConstituentUnit:
        return ConstituentUnit(
            unit_id=unit_id,
            structural_level=0,
            source_kind=SourceKind.SEGMENT,
            price_basis_revision="test-current-geometric-successor",
            direction=direction,  # type: ignore[arg-type]
            start_tick=start_tick,
            end_tick=end_tick,
            low_tick=min(start_tick, end_tick) - 1,
            high_tick=max(start_tick, end_tick) + 1,
            market_start=market_start,
            market_end=market_end,
            confirmed_at=None,
            available_at=available_at,
            locked=False,
            child_ids=(f"{unit_id}-child",),
            forming=forming,
            formed_at=formed_at,
        )

    prior = unit(
        "prior-third-buy-anchor",
        direction="down",
        start_tick=110,
        end_tick=100,
        market_start=started_at,
        market_end=started_at + timedelta(hours=1),
        available_at=started_at + timedelta(hours=2),
        formed_at=started_at + timedelta(hours=2),
    )
    successor = unit(
        "solid-successor-without-late-lock",
        direction="up",
        start_tick=100,
        end_tick=120,
        market_start=prior.market_end,
        market_end=started_at + timedelta(hours=3),
        available_at=started_at + timedelta(hours=4),
    )
    forming_tail = unit(
        "forming-tail",
        direction="down",
        start_tick=120,
        end_tick=115,
        market_start=successor.market_end,
        market_end=started_at + timedelta(hours=4),
        available_at=started_at + timedelta(hours=4),
        forming=True,
    )
    structure = SimpleNamespace(
        levels=(
            SimpleNamespace(
                structural_level=0,
                units=(prior, successor, forming_tail),
            ),
        )
    )
    old_third_buy = SimpleNamespace(
        structural_level=0,
        anchor_unit_id=prior.unit_id,
    )

    window = terminal_segment_windows(structure)[0]
    assert window.latest_completed is not None
    assert window.latest_completed.unit_id == successor.unit_id
    assert window.latest_completed.state == "formed"
    assert successor.formed_at is None
    assert current_strict_point_evidence(structure, (old_third_buy,)) == ()


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
