"""Old stroke spacing from L062/L077, including physical replay."""

from datetime import datetime, timedelta

import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.types import CLKline, FX, Kline


BASE = datetime(2026, 1, 5, 9, 30)


def _bars(prices, mirror=False):
    if mirror:
        prices = [(100 - low, 100 - high) for high, low in prices]
    return [
        Kline(i, BASE + timedelta(minutes=i), high, low, low, high, 1.0)
        for i, (high, low) in enumerate(prices)
    ]


def _calculate(bars):
    merged = CL_Kline_Process()
    merged.process_cl_klines(bars)
    calc = BiCalculator()
    calc.calculate(merged.cl_klines)
    return merged, calc


@pytest.mark.parametrize("mirror", [False, True])
def test_old_stroke_requires_an_independent_merged_bar_even_with_enough_raw_bars(mirror):
    # Seven raw bars become six merged bars; the middle raw pair is included.
    bars = _bars(
        [(12, 8), (10, 6), (11, 7), (10.8, 7.2), (12, 8), (14, 10), (13, 9)],
        mirror,
    )
    merged, calc = _calculate(bars)
    assert len(merged.cl_klines) == 6
    first, second = calc.fxs
    assert second.k.index - first.k.index == 3
    assert second.k.k_index - first.k.k_index == 4
    assert {k.index for k in first.klines}.isdisjoint(k.index for k in second.klines)
    assert calc.bis == []


@pytest.mark.parametrize("mirror", [False, True])
def test_six_unmerged_bars_are_insufficient_for_an_old_stroke(mirror):
    _, calc = _calculate(
        _bars([(12, 8), (10, 6), (11, 7), (12, 8), (14, 10), (13, 9)], mirror)
    )
    assert len(calc.fxs) == 2
    assert calc.bis == []


@pytest.mark.parametrize("mirror", [False, True])
def test_shared_fractal_shoulder_is_forbidden_even_with_enough_raw_bars(mirror):
    _, calc = _calculate(
        _bars([(12, 8), (10, 6), (11, 7), (10.8, 7.2), (10.9, 7.3), (14, 10), (13, 9)], mirror)
    )
    first, second = calc.fxs
    assert second.k.index - first.k.index == 2
    assert second.k.k_index - first.k.k_index >= 4
    assert calc.bis == []


def _fractal(kind, index, high, low):
    direction = 1 if kind == "di" else -1
    values = [(high + direction, low + direction), (high, low), (high + direction, low + direction)]
    klines = [
        CLKline(
            k_index=index + offset - 1,
            index=index + offset - 1,
            date=BASE + timedelta(minutes=index + offset - 1),
            h=h, l=l, o=l, c=h, a=1,
        )
        for offset, (h, l) in enumerate(values)
    ]
    return FX(kind, klines[1], klines, high if kind == "ding" else low)


@pytest.mark.parametrize("bottom_range,top_range", [
    ((10, 2), (8, 4)),  # L077: top's entire middle K is inside the bottom's.
    ((10, 4), (12, 2)),  # The price-reflected containment is equally invalid.
    ((10, 2), (10, 4)),
    ((10, 4), (12, 4)),
    ((10, 6), (8, 4)),
])
@pytest.mark.parametrize("reverse_time", [False, True])
def test_endpoint_value_alone_does_not_prove_top_bottom_price_order(
    bottom_range, top_range, reverse_time,
):
    bottom = _fractal("di", 8 if reverse_time else 1, *bottom_range)
    top = _fractal("ding", 1 if reverse_time else 8, *top_range)
    first, second = (top, bottom) if reverse_time else (bottom, top)
    assert not BiCalculator()._check_endpoint_geometry(first, second)


@pytest.mark.parametrize("mirror", [False, True])
def test_old_stroke_batch_and_incremental_replay_have_same_qualification_time(mirror):
    bars = _bars(
        [(12, 8), (10, 6), (11, 7), (10.8, 7.2), (12, 8), (13, 9), (14, 10), (13, 9),
         (12, 8), (11, 7), (9, 5), (10, 6), (11, 7), (12, 8), (14, 10), (13, 9)],
        mirror,
    )
    merged, live = CL_Kline_Process(), BiCalculator()

    def signature(calc):
        return [
            (bi.start.k.index, bi.end.k.index, bi.start.val, bi.end.val,
             bi.is_done(), bi.end.k.date, bi.locked_at)
            for bi in calc.bis
        ]

    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        merged.process_cl_klines(prefix)
        live.calculate(
            merged.cl_klines,
            source_revision=merged.structure_revision,
            validated_incremental_prefix=True,
        )
        _, batch = _calculate(prefix)
        assert signature(live) == signature(batch)
    assert len(live.bis) == 3
    assert live.bis[0].is_done()
    assert live.bis[0].end.k.index - live.bis[0].start.k.index == 4
    assert live.bis[0].locked_at == bars[15].date
    assert live.bis[0].fractal_visible_at == bars[7].date
    assert live.qualification_evidence == batch.qualification_evidence
    assert live.completion_evidence == batch.completion_evidence
    assert live.audit_endpoint_ranges() == []


@pytest.mark.parametrize("mirror", [False, True])
def test_wider_endpoint_fails_price_geometry_and_missing_interval_cannot_pass(mirror):
    start = _fractal("di", 1, 10, 6)
    prior = _fractal("ding", 8, 14, 10)
    wider = _fractal("ding", 13, 15, 5)
    if mirror:
        for fx in (start, prior, wider):
            fx.type = "ding" if fx.type == "di" else "di"
            for k in fx.klines:
                k.h, k.l = 100 - k.l, 100 - k.h
            fx.val = fx.k.h if fx.type == "ding" else fx.k.l
    calc = BiCalculator()
    assert calc._check_endpoint_geometry(start, prior)
    assert calc._is_more_extreme(wider, prior)
    assert not calc._check_endpoint_geometry(start, wider)
    assert not calc._check_stroke_validity(start, prior)
