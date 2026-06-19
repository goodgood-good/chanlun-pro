"""Task 1: recursive_zs_diversity 开关骨架——默认 off 与不传开关完全等价。"""
import sys

import pandas as pd

sys.path.insert(0, "src")

from chanlun.core.cl import CL
from chanlun.recursive_bt.engine.engine import CL_CFG


def _levels(cfg_extra):
    cd = CL("SH.600519", "5m", {**CL_CFG, **cfg_extra})
    cd.process_klines(
        pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")
    )
    return [
        (lv.level, [(z.zd, z.zg, len(z.lines)) for z in lv.zss])
        for lv in cd.get_recursive_branch_levels()
    ]


def test_flag_off_equals_current():
    """开关 off → 与不传开关完全一致（纯加法，不改行为）。"""
    assert _levels({}) == _levels({"recursive_zs_diversity": False})


# ── Task 2: _first_third_class ──────────────────────────────────────────────
from types import SimpleNamespace as NS  # noqa: E402

from chanlun.core.zs_diversity import _first_third_class  # noqa: E402


def _seg(t, sv, ev):
    return NS(
        _type=t,
        start=NS(val=float(sv)),
        end=NS(val=float(ev)),
        zs_low=min(sv, ev),
        zs_high=max(sv, ev),
    )


def test_first_third_class_3buy():
    """核心区[10,11]; 段3向上离开到11.5, 段4回试到11.2(≥ZG=11 不破回) → 三买在下标3。"""
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),   # 0,1,2 核心
        _seg("up", 10.8, 11.5),
        _seg("down", 11.5, 11.2),  # 3 离开, 4 回试不破
    ]
    assert _first_third_class(segs, 10.0, 11.0) == 3


def test_oscillation_not_third_class():
    """段3冲到11.5但段4回试破回10.5(<ZG) → 震荡, 非三类。"""
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),
        _seg("up", 10.8, 11.5),
        _seg("down", 11.5, 10.5),
    ]
    assert _first_third_class(segs, 10.0, 11.0) is None


def test_first_third_class_3sell():
    """离开下 end<zd, 回试 end≤zd → 三卖在下标3。"""
    segs = [
        _seg("down", 11, 10),
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("down", 10.2, 9.5),
        _seg("up", 9.5, 9.8),  # 离开下, 回试9.8≤ZD=10
    ]
    assert _first_third_class(segs, 10.0, 11.0) == 3
