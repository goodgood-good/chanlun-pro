from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
)
from chanlun.xuangu import strict_xuangu
from cl_app import xuangu_tasks


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _point(
    point_type: str,
    *,
    anchor: str = "terminal",
    at: datetime = NOW,
    divergence=None,
    level: int = 0,
):
    return SimpleNamespace(
        structural_level=level,
        anchor_unit_id=anchor,
        side="buy" if point_type.endswith("buy") else "sell",
        point_type=point_type,
        available_at=at,
        anchor_at=at - timedelta(minutes=5),
        point_id=f"{point_type}:{anchor}:{at.isoformat()}",
        variant=SimpleNamespace(value="standard"),
        divergence=divergence,
    )


def _divergence(
    direction: str,
    *,
    signal: str = "terminal",
    at: datetime = NOW,
    level: int = 0,
):
    return SimpleNamespace(
        structural_level=level,
        signal_unit_id=signal,
        direction=direction,
        anchor_at=at - timedelta(minutes=5),
        available_at=at,
        kind="trend",
        divergence_id=f"{direction}:{signal}:{at.isoformat()}",
    )


def _evidence(
    frequency: str,
    *,
    points=(),
    divergences=(),
    boundaries=(),
    levels=1,
    frontier_at: datetime = NOW,
):
    terminal_at = frontier_at
    structure_levels = tuple(
        SimpleNamespace(
            structural_level=level,
            units=(
                SimpleNamespace(
                    unit_id=f"old-l{level}",
                    structural_level=level,
                    source_kind=(
                        SourceKind.SEGMENT
                        if level == 0
                        else SourceKind.TREND_TYPE
                    ),
                    direction="down",
                    locked=True,
                    forming=False,
                    market_start=NOW - timedelta(days=1, minutes=10),
                    market_end=NOW - timedelta(days=1, minutes=5),
                    available_at=NOW - timedelta(days=1),
                ),
                SimpleNamespace(
                    unit_id="terminal" if level == 0 else f"terminal-l{level}",
                    structural_level=level,
                    source_kind=(
                        SourceKind.SEGMENT
                        if level == 0
                        else SourceKind.TREND_TYPE
                    ),
                    direction="up",
                    locked=True,
                    forming=False,
                    market_start=terminal_at - timedelta(minutes=10),
                    market_end=terminal_at - timedelta(minutes=5),
                    available_at=terminal_at,
                ),
            ),
            decomposition_boundaries=(tuple(boundaries) if level == 0 else ()),
        )
        for level in range(levels)
    )
    return SimpleNamespace(
        source_frequency=frequency,
        structure=SimpleNamespace(levels=structure_levels),
        confirmed_points=tuple(points),
        divergences=tuple(divergences),
    )


def _market_datas(*frequencies: str):
    return SimpleNamespace(frequencys=list(frequencies), market="a")


def test_registered_structure_tasks_use_only_strict_implementation() -> None:
    structural_ids = set(xuangu_tasks.xuangu_task_configs) - {"closed_ma250"}

    assert structural_ids
    assert all(
        xuangu_tasks.xuangu_task_configs[task_id]["task_fun"].__module__
        == "chanlun.xuangu.strict_xuangu"
        for task_id in structural_ids
    )


def test_single_class_selection_uses_latest_confirmation_batch(monkeypatch) -> None:
    evidence = _evidence(
        "5m",
        points=(
            _point("1buy", anchor="old-l0", at=NOW - timedelta(days=1)),
            _point("1sell"),
        ),
    )
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)
    data = _market_datas("5m")

    assert strict_xuangu.select_strict_class1_point("SH.600000", data, ["long"]) is None
    result = strict_xuangu.select_strict_class1_point("SH.600000", data, ["short"])
    assert result is not None
    assert "1sell" in result["msg"]


def test_delayed_first_point_is_not_current_after_terminal_unit_advances(
    monkeypatch,
) -> None:
    evidence = _evidence(
        "5m",
        points=(_point("1buy", anchor="old-l0", at=NOW),),
    )
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)

    result = strict_xuangu.select_strict_class1_point(
        "SH.600000",
        _market_datas("5m"),
        ["long"],
    )

    assert result is None


def test_delayed_first_sell_is_not_current_after_terminal_unit_advances(
    monkeypatch,
) -> None:
    evidence = _evidence(
        "5m",
        points=(_point("1sell", anchor="old-l0", at=NOW),),
    )
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)

    result = strict_xuangu.select_strict_class1_point(
        "SH.600000",
        _market_datas("5m"),
        ["short"],
    )

    assert result is None


def test_third_after_first_uses_same_strict_point_ledger(monkeypatch) -> None:
    divergence = _divergence(
        "down",
        signal="old",
        at=NOW - timedelta(hours=1),
    )
    first = _point(
        "1buy",
        anchor="old",
        at=NOW - timedelta(hours=1),
        divergence=divergence,
    )
    third = _point("3buy")
    boundary = SimpleNamespace(
        anchor_unit_id="old",
        anchor_at=first.anchor_at,
        available_at=first.available_at,
        boundary_id="boundary-current-partition",
        divergence=divergence,
    )
    evidence = _evidence(
        "5m",
        points=(first, third),
        boundaries=(boundary,),
    )
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)

    result = strict_xuangu.select_strict_class3_after_class1(
        "SH.600000",
        _market_datas("5m"),
        ["long"],
    )

    assert result is not None
    assert "严格一类点" in result["msg"]


def test_third_after_first_rejects_unrelated_historical_first(monkeypatch) -> None:
    stale = _point("1buy", anchor="old", at=NOW - timedelta(days=20))
    third = _point("3buy")
    evidence = _evidence("5m", points=(stale, third))
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)

    assert (
        strict_xuangu.select_strict_class3_after_class1(
            "SH.600000",
            _market_datas("5m"),
            ["long"],
        )
        is None
    )


def test_multi_frequency_selection_matches_point_or_divergence_side(
    monkeypatch,
) -> None:
    evidence_by_frequency = {
        "30m": _evidence("30m", divergences=(_divergence("down"),)),
        "5m": _evidence("5m", points=(_point("2buy"),)),
    }
    monkeypatch.setattr(
        strict_xuangu,
        "_evidence",
        lambda _code, _data, frequency: evidence_by_frequency[frequency],
    )

    result = strict_xuangu.select_strict_two_frequency_confluence(
        "SH.600000",
        _market_datas("30m", "5m"),
        ["long"],
    )

    assert result is not None
    assert "buy" in result["msg"]


def test_multi_frequency_selection_rejects_duplicate_or_low_to_high_order() -> None:
    for frequencies in (("5m", "5m"), ("5m", "30m")):
        with pytest.raises(ValueError):
            strict_xuangu.select_strict_two_frequency_confluence(
                "SH.600000",
                _market_datas(*frequencies),
                ["long"],
            )


def test_single_frequency_selection_rejects_multiple_periods() -> None:
    with pytest.raises(ValueError, match="只提供一个周期"):
        strict_xuangu.select_strict_class1_point(
            "SH.600000",
            _market_datas("30m", "5m"),
            ["long"],
        )


def test_selection_rejects_empty_or_duplicate_directions() -> None:
    for directions in ([], ["long", "long"]):
        with pytest.raises(ValueError, match="非空且不能重复"):
            strict_xuangu.select_strict_class1_point(
                "SH.600000",
                _market_datas("5m"),
                directions,
            )


def test_multi_frequency_confluence_rejects_low_event_before_high_anchor(
    monkeypatch,
) -> None:
    evidence_by_frequency = {
        "30m": _evidence("30m", points=(_point("2buy", at=NOW),)),
        "5m": _evidence(
            "5m",
            points=(_point("1buy", at=NOW - timedelta(hours=1)),),
            frontier_at=NOW - timedelta(hours=1),
        ),
    }
    monkeypatch.setattr(
        strict_xuangu,
        "_evidence",
        lambda _code, _data, frequency: evidence_by_frequency[frequency],
    )

    result = strict_xuangu.select_strict_two_frequency_confluence(
        "SH.600000",
        _market_datas("30m", "5m"),
        ["long"],
    )

    assert result is None


def test_multi_frequency_confluence_waits_for_high_event_confirmation(
    monkeypatch,
) -> None:
    """低周期点晚于高周期锚点、却早于高周期确认时仍不能共振。"""

    high = _point("2buy", at=NOW)
    low = _point("1buy", at=NOW - timedelta(minutes=2))
    assert low.available_at >= high.anchor_at
    assert low.available_at < high.available_at
    evidence_by_frequency = {
        "30m": _evidence("30m", points=(high,)),
        "5m": _evidence(
            "5m",
            points=(low,),
            frontier_at=low.available_at,
        ),
    }
    monkeypatch.setattr(
        strict_xuangu,
        "_evidence",
        lambda _code, _data, frequency: evidence_by_frequency[frequency],
    )

    result = strict_xuangu.select_strict_two_frequency_confluence(
        "SH.600000",
        _market_datas("30m", "5m"),
        ["long"],
    )

    assert result is None


def test_recursive_small_to_large_second_point_is_current_and_selectable(
    monkeypatch,
) -> None:
    evidence = _evidence(
        "5m",
        points=(_point("2buy", anchor="terminal-l1", level=1),),
        levels=2,
    )
    monkeypatch.setattr(strict_xuangu, "_evidence", lambda *_args: evidence)

    result = strict_xuangu.select_strict_class2_point(
        "SH.600000",
        _market_datas("5m"),
        ["long"],
    )

    assert result is not None
    assert "L1:2buy" in result["msg"]


def test_stock_selection_uses_the_same_real_frame_evidence_runtime() -> None:
    frame = (
        pd.read_parquet(FIXTURES / "SZ.002299_1m.parquet")[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(900)
        .copy()
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    as_of = pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime()

    class MarketData:
        market = "a"
        frequencys = ["1m"]

        @staticmethod
        def closed_klines(_code, _frequency):
            return frame

        @staticmethod
        def closed_bar_as_of(_code, _frequency):
            return as_of

    selected = strict_xuangu._evidence("SZ.002299", MarketData(), "1m")
    direct = screening_evidence_from_frame(
        code="SZ.002299",
        frequency="1m",
        frame=frame,
        as_of=as_of,
        market="a",
    )

    assert selected.structure_revision == direct.structure_revision
    assert selected.confirmed_points == direct.confirmed_points
    assert selected.divergences == direct.divergences
