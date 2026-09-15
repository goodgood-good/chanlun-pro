"""Physical source changes must invalidate geometrically equal cached fractals."""

import copy
import pickle

import pandas as pd
import pytest

from chanlun.core.cl import CL


def _frame(prices, mirror):
    if mirror:
        prices = [(100 - low, 100 - high) for high, low in prices]
    return pd.DataFrame([
        dict(date=pd.Timestamp("2026-01-05 09:30") + pd.Timedelta(minutes=i),
             high=high, low=low, open=low, close=high, volume=1.0)
        for i, (high, low) in enumerate(prices)
    ])


def _signature(cd):
    def evidence(fx):
        return (fx.type, fx.val, fx.k.to_dict(), [k.to_dict() for k in fx.klines])

    return (
        tuple(evidence(fx) for fx in cd.get_fxs()),
        tuple((evidence(bi.start), evidence(bi.end),
               bi.high, bi.low, bi.is_done(), bi.forming, bi.locked_at)
              for bi in cd.get_bis()),
    )


@pytest.mark.parametrize("mirror", [False, True])
def test_last_bar_update_and_append_refresh_equal_price_endpoint_sources(mirror):
    initial = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10), (13, 9)]
    # Updating raw 6 merges it with raw 5. The merged center's index/high/low
    # remain unchanged, but the extreme's physical source moves from 5 to 6.
    updated = initial[:-1] + [(14, 9), (13, 9)]
    # A lower new bottom also removes the still-viable observation-origin path.
    extended = updated + [(12, 8), (11, 7), (9, 5), (10, 6)]
    live = CL("TST", "1m", market="a")
    for stage, prices in enumerate((initial, updated, extended)):
        frame = _frame(prices, mirror)
        live.process_klines(frame)
        cold = CL("TST", "1m", market="a")
        cold.process_klines(frame)
        assert _signature(live) == _signature(cold), f"stage {stage}"
        assert live.get_bis()[0].end.k.index == 5
        assert live.get_bis()[0].end.k.k_index == (5 if stage == 0 else 6)
        assert live.get_bis()[0].end.k.date == frame.iloc[5 if stage == 0 else 6].date
    assert len(live.get_bis()) == 2
    assert live.get_bis()[0].has_qualified_successor()
    assert not live.get_bis()[0].is_done()
    assert live.get_bis()[0].locked_at is None
    assert live.get_bis()[0].fractal_visible_at == _frame(updated, mirror).iloc[-1].date
    assert live.bi_calculator.qualification_evidence == cold.bi_calculator.qualification_evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_included_right_shoulder_refreshes_without_center_or_price_change(mirror):
    initial = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10), (13, 9)]
    live = CL("TST", "1m", market="a")
    live.process_klines(_frame(initial, mirror))
    updated = initial + [(14, 9)]
    live.process_klines(_frame(updated, mirror))
    cold = CL("TST", "1m", market="a")
    cold.process_klines(_frame(updated, mirror))
    assert _signature(live) == _signature(cold)
    bi = live.get_bis()[0]
    assert bi.end.k.k_index == 5
    assert [k.index for k in bi.end.klines[-1].klines] == [6, 7]


def test_old_stroke_profile_cannot_restore_stale_endpoint_cache():
    current = CL("TST", "1m", market="a")
    state = copy.deepcopy(current.__getstate__())
    state["config"]["stroke_rule"] = "source-fractal-nonshared-raw-distance-v2"
    restored = object.__new__(CL)
    with pytest.raises(ValueError, match="production base structure configuration is fixed"):
        restored.__setstate__(state)


def test_current_endpoint_cache_round_trip_continues_with_fresh_source_evidence():
    initial = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10), (13, 9)]
    current = CL("TST", "1m", market="a")
    current.process_klines(_frame(initial, False))
    restored = pickle.loads(pickle.dumps(current))
    updated = initial[:-1] + [(14, 9), (13, 9), (12, 8), (11, 7), (10, 6), (11, 7)]
    restored.process_klines(_frame(updated, False))
    cold = CL("TST", "1m", market="a")
    cold.process_klines(_frame(updated, False))
    assert _signature(restored) == _signature(cold)
