from __future__ import annotations

import pickle

import pandas as pd

from chanlun.recursive_bt.chanlun_selector import (
    ASelectionConfig,
    OriginalChanlunASelector,
)
from chanlun.recursive_bt.engine import Signal


def _write_symbol(
    bt_data,
    code: str,
    signals,
    *,
    big_dir="up",
    mid_dir=None,
    d3_idx=None,
):
    dates = list(pd.date_range("2026-06-10 09:30:00", periods=120, freq="5min"))
    small_by_bar = {}
    for idx, bs_type, price in signals:
        small_by_bar.setdefault(idx, []).append(Signal(dates[idx], 0, bs_type, price))
    data = {
        "dates": dates,
        "close": [10.0 + i * 0.01 for i in range(len(dates))],
        "small_by_bar": small_by_bar,
        "big_dir_at": [big_dir] * len(dates),
    }
    if mid_dir is not None:
        data["mid_dir_at"] = [mid_dir] * len(dates)
    if d3_idx is not None:
        d3 = [False] * len(dates)
        d3[d3_idx] = True
        data["d3_ok"] = d3
    with open(bt_data / f"{code}.pkl", "wb") as fp:
        pickle.dump(data, fp)


def _write_fund(fund_data, code: str, *, roe=3.0, rev_inc=10.0, np_inc=10.0, bps=10.0):
    fund_data.mkdir(exist_ok=True)
    data = {
        "code": code,
        "reports": [
            {
                "anntime": "20260601",
                "period": "20260331",
                "roe": roe,
                "rev_inc": rev_inc,
                "np_inc": np_inc,
                "bps": bps,
            }
        ],
    }
    with open(fund_data / f"{code}.pkl", "wb") as fp:
        pickle.dump(data, fp)


def test_original_selector_filters_by_recent_buy_and_big_level(tmp_path):
    bt_data = tmp_path / "bt_data"
    bt_data.mkdir()
    _write_symbol(bt_data, "SH.600000", [(119, "3buy", 10.5)], big_dir="up")
    _write_symbol(bt_data, "SZ.000001", [(119, "1buy", 11.0)], big_dir="down")
    _write_symbol(bt_data, "SZ.300001", [(100, "2buy", 12.0)], big_dir="up")
    _write_symbol(bt_data, "SH.600001", [(119, "2sell", 9.0)], big_dir="up")
    _write_symbol(bt_data, "SH.600002", [(119, "1buy", 10.0)], big_dir="up", mid_dir="down")

    selector = OriginalChanlunASelector(
        ASelectionConfig(
            bt_data=str(bt_data),
            lookback_bars=3,
            min_bars=100,
            require_three_systems=False,
        )
    )

    candidates = selector.select()

    assert [candidate.code for candidate in candidates] == ["SH.600000"]
    assert candidates[0].bs_type == "3buy"


def test_original_selector_prioritizes_configured_buy_classes(tmp_path):
    bt_data = tmp_path / "bt_data"
    bt_data.mkdir()
    _write_symbol(bt_data, "SH.600000", [(119, "1buy", 10.5)], big_dir="up")
    _write_symbol(bt_data, "SZ.000001", [(118, "3buy", 11.0)], big_dir="up")
    _write_symbol(bt_data, "SZ.300001", [(119, "2buy", 12.0)], big_dir="up")

    selector = OriginalChanlunASelector(
        ASelectionConfig(
            bt_data=str(bt_data),
            lookback_bars=3,
            max_codes=2,
            buy_classes=(3, 2, 1),
            min_bars=100,
            require_three_systems=False,
        )
    )

    candidates = selector.select()

    assert [(candidate.code, candidate.bs_type) for candidate in candidates] == [
        ("SZ.000001", "3buy"),
        ("SZ.300001", "2buy"),
    ]


def test_original_selector_requires_three_independent_systems(tmp_path):
    bt_data = tmp_path / "bt_data"
    fund_data = tmp_path / "fund"
    bt_data.mkdir()
    _write_symbol(bt_data, "SH.600000", [(119, "3buy", 10.0)], big_dir="up")
    _write_symbol(bt_data, "SZ.000001", [(119, "3buy", 10.0)], big_dir="up")
    _write_symbol(bt_data, "SZ.300001", [(119, "3buy", 10.0)], big_dir="up")
    _write_fund(fund_data, "SH.600000", roe=3.0, rev_inc=12.0, np_inc=9.0, bps=4.0)
    _write_fund(fund_data, "SZ.000001", roe=1.0, rev_inc=20.0, np_inc=15.0, bps=1.0)
    _write_fund(fund_data, "SZ.300001", roe=3.0, rev_inc=-5.0, np_inc=-8.0, bps=0.0)

    selector = OriginalChanlunASelector(
        ASelectionConfig(
            bt_data=str(bt_data),
            fund_data=str(fund_data),
            lookback_bars=3,
            buy_classes=(3, 2, 1),
            require_three_systems=True,
            fundamental_roe_ann_min=8.0,
            min_bars=100,
        )
    )

    candidates = selector.select()

    assert [candidate.code for candidate in candidates] == ["SH.600000"]
    assert candidates[0].fund_ok is True
    assert candidates[0].comparison_ok is True
