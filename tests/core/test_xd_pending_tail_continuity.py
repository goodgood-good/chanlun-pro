from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chanlun.core import xd_calculator
from chanlun.core.xd_calculator import XdCalculator


def test_pending_tail_is_connected_and_opposes_previous_segment(monkeypatch) -> None:
    calculator = XdCalculator()
    emitted: list[tuple[int, str]] = []
    monkeypatch.setattr(calculator, "_make_xd", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        calculator,
        "_emit_pending",
        lambda _all_bis, start, direction: emitted.append((start, direction)),
    )
    segments = [
        (0, 2, "down", None),
        (3, 5, "up", None),
        (6, 8, "down", None),
    ]

    calculator._emit_segments_deferred(
        [object()] * 14,
        segments,
        (10, "down"),
        0,
        {},
    )

    assert emitted == [(9, "up")]


def test_gap_invalidation_keeps_the_later_causal_witness(monkeypatch) -> None:
    """A later BI may invalidate a pending gap but may not be forgotten.

    This is the exact branch that once made the SZ.301171 1m third-buy first
    appear on 2025-09-15 while claiming it had existed on 2025-09-12.
    """

    base = datetime(2025, 9, 12, 14, 0, tzinfo=timezone.utc)

    def bi(index: int, direction: str, high: int, low: int):
        return SimpleNamespace(
            index=index,
            type=direction,
            high=high,
            low=low,
            locked_at=base + timedelta(minutes=index),
        )

    values = [
        bi(0, "up", 9, 8),
        bi(1, "down", 9, 8),
        bi(2, "up", 9, 8),
        bi(3, "down", 12, 10),
        bi(4, "up", 9, 8),
        bi(5, "down", 11, 9),
        bi(6, "up", 11, 9),
    ]
    calculator = XdCalculator()
    monkeypatch.setattr(
        calculator,
        "_check_type2",
        lambda *_args: (xd_calculator._TYPE2_INVALIDATED, 5),
    )

    result = calculator._try_end(
        values,
        seg_start=0,
        seg_end=2,
        seg_type="up",
        seg_high=9,
        seg_low=8,
        check_pos=3,
        seg_cs_bis_cache=[values[0]],
    )

    assert isinstance(result, xd_calculator._GapConfirmationInvalidated)
    assert result.witnessed_at == values[5].locked_at


def test_segment_lock_waits_for_the_full_cascade_horizon() -> None:
    """候选段至少有四个后继段后，才允许进入不可改写的 locked 前缀。"""

    calculator = XdCalculator()
    assert calculator._DEFER_DONE >= 4
    base = datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc)
    segments = [
        (
            index * 3,
            index * 3 + 2,
            "up" if index % 2 == 0 else "down",
            base + timedelta(minutes=index),
        )
        for index in range(calculator._DEFER_DONE + 1)
    ]
    locked: dict[tuple[int, int, str], datetime] = {}

    calculator._freeze_confirmed_candidate(
        segments[: calculator._DEFER_DONE],
        locked,
    )
    assert locked == {}

    calculator._freeze_confirmed_candidate(segments, locked)
    first = segments[0]
    boundary = segments[calculator._DEFER_DONE]
    assert locked[(first[0], first[1], first[2])] == max(first[3], boundary[3])
