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
