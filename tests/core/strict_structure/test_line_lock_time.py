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
    """Four-successor XD locking needs a longer non-vacuous real sample."""

    return (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(800)
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
    cd = CL("SH.600519", "5m", dict(strict_base_config()))
    cd.process_klines(sample_frame)

    assert any(line.is_done() for line in (*cd.get_bis(), *cd.get_xds()))
    for line in (*cd.get_bis(), *cd.get_xds()):
        assert (line.locked_at is not None) is bool(line.is_done())
        if line.locked_at is not None:
            assert line.locked_at >= line.end.k.date
            assert line.locked_at <= sample_frame.iloc[-1]["date"]


def test_bi_lock_time_is_first_sufficient_following_endpoint_witness(sample_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()))
    cd.process_klines(sample_frame)

    all_bis = cd.get_bis()
    locked_bis = [line for line in all_bis if line.is_done()]
    assert locked_bis
    for bi in locked_bis:
        following = next(
            fx
            for fx in cd.get_fxs()
            if fx.k.index > bi.end.k.index
            and fx.type != bi.end.type
            and cd.bi_calculator._check_stroke_validity(bi.end, fx)
        )
        assert bi.locked_at == _first_sufficient_fractal_witness(following)
        assert bi.locked_at >= _first_sufficient_fractal_witness(bi.end)


def test_xd_lock_time_comes_from_a_later_locked_bi_witness(segment_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()))
    cd.process_klines(segment_frame)

    bi_witness_times = {bi.locked_at for bi in cd.get_bis() if bi.is_done()}
    locked_xds = [line for line in cd.get_xds() if line.is_done()]
    assert locked_xds
    for xd in locked_xds:
        assert xd.locked_at in bi_witness_times
        assert xd.locked_at >= xd.end.k.date


def test_xd_lock_times_follow_causal_segment_order():
    frame = (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(800)
        .reset_index(drop=True)
    )
    cd = CL("SH.600519", "5m", dict(strict_base_config()))
    cd.process_klines(frame)

    locked_at = [xd.locked_at for xd in cd.get_xds() if xd.is_done()]
    assert len(locked_at) >= 2
    assert locked_at == sorted(locked_at)


def test_batch_and_bar_by_bar_have_identical_line_lock_times(sample_frame):
    batch = CL("SH.600519", "5m", dict(strict_base_config()))
    batch.process_klines(sample_frame)

    incremental = CL("SH.600519", "5m", dict(strict_base_config()))
    for row in sample_frame.itertuples(index=False):
        _incremental_update(incremental, row)

    assert _line_lock_fingerprint(batch) == _line_lock_fingerprint(incremental)


def test_locked_line_time_never_moves_on_longer_prefix(sample_frame):
    cd = CL("SH.600519", "5m", dict(strict_base_config()))
    frozen = {}
    for row in sample_frame.itertuples(index=False):
        _incremental_update(cd, row)
        for kind, line in _locked_lines(cd):
            key = _line_identity(kind, line)
            record = (line.locked_at, line.start.k.k_index, line.end.k.k_index)
            assert frozen.setdefault(key, record) == record
