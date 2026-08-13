from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.core.strict_structure.current_events import current_strict_events
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
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


def test_delayed_first_buy_survives_its_confirmation_frontier() -> None:
    """一类点由下一锁定段确认时，仍须在首次可见批次进入选股。"""

    frame = _frame()
    target_time = pd.Timestamp("2025-12-15 06:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)

    first_buy = next(
        point
        for point in evidence.confirmed_points
        if point.point_type == "1buy"
        and point.anchor_at == pd.Timestamp("2025-12-05 02:30:00+00:00").to_pydatetime()
    )
    level = evidence.structure.levels[first_buy.structural_level]
    locked = tuple(unit for unit in level.units if unit.locked)

    assert locked[-1].unit_id != first_buy.anchor_unit_id
    assert locked[-1].available_at == first_buy.available_at
    assert first_buy in current_strict_events(evidence).points


def test_new_locked_frontier_expires_old_confirmation_batch() -> None:
    frame = _frame()
    target_time = pd.Timestamp("2025-12-16 01:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)
    current = current_strict_events(evidence)

    assert any(point.point_type == "2buy" for point in current.points)
    assert all(
        not (
            point.point_type == "1buy"
            and point.anchor_at
            == pd.Timestamp("2025-12-05 02:30:00+00:00").to_pydatetime()
        )
        for point in current.points
    )


def test_current_divergence_tracks_delayed_first_point_batch() -> None:
    frame = _frame()
    target_time = pd.Timestamp("2025-12-15 06:45:00+00:00")
    prefix = frame.loc[frame["date"] <= target_time].copy()
    prefix.attrs.update(frame.attrs)
    evidence = _evidence(prefix)
    current = current_strict_events(evidence)

    first_buy = next(point for point in current.points if point.point_type == "1buy")
    assert first_buy.divergence is not None
    assert first_buy.divergence in current.divergences


def test_current_frontier_uses_latest_available_locked_evidence() -> None:
    """递归单元按市场时间排列时，确认可用时间仍可能相同或延后。"""

    early_market_late_confirmation = SimpleNamespace(
        locked=True,
        available_at=pd.Timestamp("2026-08-10 10:05:00+08:00").to_pydatetime(),
    )
    later_market_earlier_confirmation = SimpleNamespace(
        locked=True,
        available_at=pd.Timestamp("2026-08-10 10:04:00+08:00").to_pydatetime(),
    )
    current_point = SimpleNamespace(
        structural_level=0,
        available_at=early_market_late_confirmation.available_at,
        divergence=None,
    )
    evidence = SimpleNamespace(
        structure=SimpleNamespace(
            levels=(
                SimpleNamespace(
                    structural_level=0,
                    units=(
                        early_market_late_confirmation,
                        later_market_earlier_confirmation,
                    ),
                ),
            )
        ),
        confirmed_points=(current_point,),
        divergences=(),
    )

    assert current_strict_events(evidence).points == (current_point,)
