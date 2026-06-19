# -*- coding: utf-8 -*-
"""Task 4 验收测：002299 1m「出三买仍延伸」矛盾归零。

矛盾定义（R4 口径）：
  L0 done 中枢的 segs 序列里存在三类点——即中枢贪婪延伸、
  在自身产生三类信号之后仍继续吸收后续段。
  用 _first_third_class(segs, zd, zg) != None 检测。

RED  → 开关 off：3 个中枢跨三类点延伸
GREEN → 开关 on ：0 个（R4 切分后清零）

字段路径（核实于 2026-06-19）：
  z.lines[0].start.k.k_index  — 中枢第一段起始 k
  z.end.end.k.k_index         — 离开段末端 k（z.end 存在时）
  z.lines[-1].end.k.k_index  — 右边缘无离开段时
  m.anchor_fx.k.k_index       — 三类点锚点 k（= retest.end.k）
"""
from pathlib import Path

import pandas as pd
import pytest

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "SZ.002299_1m.parquet"


def _load_df() -> pd.DataFrame:
    return pd.read_parquet(FIX)


def _contradictions(cd) -> int:
    """统计 recursive L0 done_zss 中「出三买仍延伸」的中枢数（R4 口径）。

    口径：中枢自身的 segs 序列里存在三类点模式（内部有离开段+回试），
    即 _first_third_class(segs, zd, zg) 不为 None。
    这对应「贪婪中枢在三类信号之后仍延伸」的原文矛盾（L038:215）。
    """
    from chanlun.core import zs_diversity as _zsd

    levels = cd.get_recursive_branch_levels()
    if not levels:
        return 0
    l0 = levels[0]
    return sum(
        1 for z in l0.zss
        if _zsd._first_third_class(_zsd._zs_segs(z), z.zd, z.zg, core=3) is not None
    )


@pytest.mark.skipif(not FIX.exists(), reason="002299 fixture 不存在")
def test_002299_off_has_contradictions():
    """开关 off 时应存在矛盾（现状 3 个），确认 RED 基线。"""
    from chanlun.core.cl import CL
    from chanlun.recursive_bt.engine.engine import CL_CFG

    cd = CL("SZ.002299", "1m", dict(CL_CFG))
    cd.process_klines(_load_df())
    assert _contradictions(cd) > 0, "开关 off 应存在「出三买仍延伸」矛盾"


@pytest.mark.skipif(not FIX.exists(), reason="002299 fixture 不存在")
def test_002299_on_no_contradictions():
    """开关 on 后「出三买仍延伸」矛盾必须归零（Task 4 GREEN 验收）。"""
    from chanlun.core.cl import CL
    from chanlun.recursive_bt.engine.engine import CL_CFG

    cfg = dict(CL_CFG)
    cfg["recursive_zs_diversity"] = True
    cd = CL("SZ.002299", "1m", cfg)
    cd.process_klines(_load_df())
    assert _contradictions(cd) == 0, "开关 on 后不应有「出三买仍延伸」矛盾"
