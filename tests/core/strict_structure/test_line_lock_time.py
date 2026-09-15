"""BI/XD 因果锁定时间与逐 K 当下性账本。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "SH.600519_5m.parquet"


@pytest.fixture(scope="module")
def sample_frame() -> pd.DataFrame:
    return (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(500)
        .reset_index(drop=True)
    )


@pytest.fixture(scope="module")
def segment_frame() -> pd.DataFrame:
    """Conservative BI confirmation needs enough history for locked XDs."""

    return (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(2400)
        .reset_index(drop=True)
    )


def _line_identity(kind, line):
    return (
        kind,
        line.type,
        line.start.k.k_index,
        line.end.k.k_index,
        line.start.val,
        line.end.val,
    )


def _locked_lines(cd):
    for kind, lines in (("bi", cd.get_bis()), ("xd", cd.get_xds())):
        for line in lines:
            if line.is_done():
                yield kind, line


def _line_lock_fingerprint(cd):
    return tuple(
        (*_line_identity(kind, line), line.is_done(), line.locked_at)
        for kind, line in _locked_lines(cd)
    )


def _incremental_update(cd, row):
    cd.process_kline_values(
        row.date, row.open, row.high, row.low, row.close, row.volume
    )


def _first_sufficient_fractal_witness(fx):
    left, middle, right = [k for k in fx.klines if k is not None][-3:]
    for end in range(1, len(right.klines) + 1):
        prefix = right.klines[:end]
        if right.up_qs == "up":
            right_high = max(source.h for source in prefix)
            right_low = max(source.l for source in prefix)
        elif right.up_qs == "down":
            right_high = min(source.h for source in prefix)
            right_low = min(source.l for source in prefix)
        else:
            right_high, right_low = prefix[-1].h, prefix[-1].l
        if fx.type == "ding":
            confirmed = (
                middle.h > left.h
                and middle.h > right_high
                and middle.l > left.l
                and middle.l > right_low
            )
        else:
            confirmed = (
                middle.l < left.l
                and middle.l < right_low
                and middle.h < left.h
                and middle.h < right_high
            )
        if confirmed:
            return prefix[-1].date
    raise AssertionError("fixture fractal has no sufficient physical witness")


def test_done_and_locked_at_are_bijective(sample_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    cd.process_klines(sample_frame)

    assert any(line.is_done() for line in (*cd.get_bis(), *cd.get_xds()))
    for line in (*cd.get_bis(), *cd.get_xds()):
        assert (line.locked_at is not None) is bool(line.is_done())
        if line.locked_at is not None:
            assert line.locked_at >= line.end.k.date
            assert line.locked_at <= sample_frame.iloc[-1]["date"]

    bis = cd.get_bis()
    assert bis
    calc = cd.bi_calculator
    assert bis == calc.confirmed_bis + calc.pending_bis
    assert all(not bi.forming and bi.is_done() for bi in calc.confirmed_bis)
    assert 1 <= len(calc.pending_bis) <= 3
    assert all(bi.forming and not bi.is_done() for bi in calc.pending_bis)
    assert bis[-1].forming is True
    assert bis[-1].is_done() is False


def test_bi_lock_time_matches_its_first_confirmed_physical_prefix(sample_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    cd.process_klines(sample_frame)

    all_bis = cd.get_bis()
    locked_bis = [line for line in all_bis if line.is_done()]
    assert locked_bis
    first_seen = {}
    live = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    for row in sample_frame.itertuples(index=False):
        _incremental_update(live, row)
        for bi in live.bi_calculator.confirmed_bis:
            first_seen.setdefault(_line_identity("bi", bi), row.date)
    physical_witnesses = {_first_sufficient_fractal_witness(fx) for fx in cd.get_fxs()}
    completion_by_edge = {(c.start, c.end): c for c in cd.bi_calculator.completion_evidence}
    for bi in locked_bis:
        assert bi.locked_at == first_seen[_line_identity("bi", bi)]
        event = next(c for c in completion_by_edge.values()
                     if cd.bi_calculator._resolver.nodes[c.start].fx.k.index == bi.start.k.index
                     and cd.bi_calculator._resolver.nodes[c.end].fx.k.index == bi.end.k.index)
        assert event.physical_witness_at in physical_witnesses
        assert bi.locked_at == cd._stroke_close_times[event.physical_witness_at]
        assert bi.locked_at >= _first_sufficient_fractal_witness(bi.end)


def test_xd_lock_time_comes_from_a_later_locked_bi_witness(segment_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    cd.process_klines(segment_frame)

    bi_witness_times = {bi.locked_at for bi in cd.get_bis() if bi.is_done()}
    locked_xds = [line for line in cd.get_xds() if line.is_done()]
    assert locked_xds
    for xd in locked_xds:
        assert xd.formed_at is not None
        assert xd.formed_at <= xd.locked_at
        assert xd.locked_at in bi_witness_times
        assert xd.locked_at >= xd.end.k.date


def test_xd_lock_times_follow_causal_segment_order():
    frame = (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(2400)
        .reset_index(drop=True)
    )
    cd = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    cd.process_klines(frame)

    locked_at = [xd.locked_at for xd in cd.get_xds() if xd.is_done()]
    assert len(locked_at) >= 2
    assert locked_at == sorted(locked_at)


def test_batch_and_bar_by_bar_have_identical_line_lock_times(sample_frame):
    batch = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    batch.process_klines(sample_frame)

    incremental = CL(
        "SH.600519",
        "5m",
        dict(strict_base_config()),
        market="a",
    )
    for row in sample_frame.itertuples(index=False):
        _incremental_update(incremental, row)

    assert _line_lock_fingerprint(batch) == _line_lock_fingerprint(incremental)


def test_locked_line_time_never_moves_on_longer_prefix(sample_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()), market="a")
    frozen = {}
    for row in sample_frame.itertuples(index=False):
        _incremental_update(cd, row)
        current = {_line_identity(kind, line): line for kind, line in _locked_lines(cd)}
        assert frozen.keys() <= current.keys()
        for kind, line in _locked_lines(cd):
            key = _line_identity(kind, line)
            record = (line.locked_at, line.start.k.k_index, line.end.k.k_index)
            assert frozen.setdefault(key, record) == record
