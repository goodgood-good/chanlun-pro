"""tests/core/test_bs_point_incremental.py — 买卖点重算的幂等性(防跨轮累积)。

``BsPointCalculator.calculate`` 是无状态纯重算引擎:每次 ``process_mmd`` 都会
调用它,把买卖点(mmd)/背驰(bc)写到持久的 ``LINE`` 对象上。

历史 bug:唯一去重 ``_mmd_already_attached`` 用 ``mmd.zs is zs`` 对象身份判重,
而 ``ZS`` 对象每轮增量都会重建,身份比较失效 → 同一线段上的买卖点随重算
轮数成倍累积(修复前实测真实行情下增量买卖点暴涨数十倍)。

修复(``_clear_previous_results``)要求:对同一批 ``LINE`` 对象重复调用
``calculate`` 必须幂等,即使每轮喂入的是"重建过的、不同对象的" zss。

本测试直接对 ``BsPointCalculator.calculate`` 做白盒验证,不经 ``process_klines``,
因此不受 ``xd_calculator`` 是否每轮重建 xds 的影响 —— 后者会掩盖本 bug。
"""

from __future__ import annotations

import copy
import pathlib

import pandas as pd
import pytest

from chanlun.core.bs_point_calculator import BsPointCalculator
from chanlun.core.cl import CL
from tests.core.conftest import DEFAULT_CL_CONFIG

_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines"


def _mmd_bc_counts(lines) -> tuple[int, int]:
    """统计 lines 上挂载的买卖点与背驰总数。"""
    mmd_total = bc_total = 0
    for line in lines:
        for mmds in getattr(line, "zs_type_mmds", {}).values():
            mmd_total += len(mmds)
        for bcs in getattr(line, "zs_type_bcs", {}).values():
            bc_total += len(bcs)
    return mmd_total, bc_total


def _load(fixture: str) -> pd.DataFrame:
    csv = _FIXTURES_DIR / fixture
    if not csv.exists():
        pytest.skip(f"缺少 fixture: {fixture}")
    return pd.read_csv(csv, parse_dates=["date"])


@pytest.mark.parametrize("fixture", ["a_SZ_301004_30m.csv", "us_TSLA_US_30m.csv"])
def test_calculate_idempotent_across_rebuilt_zss(fixture: str):
    """对同一批 LINE 重复调用 calculate(喂入重建过的 zss)必须幂等。

    第二轮喂入 ``ZS`` 的浅拷贝(逻辑相同但对象不同),复现"中枢对象每轮
    重建导致 ``mmd.zs is zs`` 身份去重失效"的真实增量场景。修复后
    ``_clear_previous_results`` 保证幂等;若回退去重逻辑,第二轮会重复挂载、
    买卖点与背驰翻倍。
    """
    df = _load(fixture)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)

    lines = cd.get_xds()
    zss = cd.get_xd_zss()
    # 浅拷贝:得到不同的 ZS 对象,但其内部 .lines/.start/.end 仍指向同一批
    # 线段对象 —— 既触发身份去重失效,又不扰动检测器里基于 line 身份的判定。
    zss_rebuilt = [copy.copy(z) for z in zss]

    BsPointCalculator(cd, zs_type="xd").calculate(lines, zss)
    round1 = _mmd_bc_counts(lines)

    BsPointCalculator(cd, zs_type="xd").calculate(lines, zss_rebuilt)
    round2 = _mmd_bc_counts(lines)

    assert round2 == round1, (
        f"{fixture}: 重复 calculate 后买卖点/背驰累积 round1={round1} round2={round2}"
    )
    assert round1[0] > 0, f"{fixture}: 测试数据未识别出任何买卖点,无法验证幂等性"


@pytest.mark.parametrize("fixture", ["a_SZ_301004_30m.csv", "us_TSLA_US_30m.csv"])
def test_no_duplicate_mmd_per_line(fixture: str):
    """重复 calculate 后,单条线段在同一 zs_type 下不应出现重名买卖点。"""
    df = _load(fixture)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)

    lines = cd.get_xds()
    zss = cd.get_xd_zss()
    zss_rebuilt = [copy.copy(z) for z in zss]

    BsPointCalculator(cd, zs_type="xd").calculate(lines, zss)
    BsPointCalculator(cd, zs_type="xd").calculate(lines, zss_rebuilt)

    for line in lines:
        for zs_type, mmds in getattr(line, "zs_type_mmds", {}).items():
            names = [m.name for m in mmds]
            assert len(names) == len(set(names)), (
                f"{fixture}: line.index={line.index} zs_type={zs_type} "
                f"出现重复买卖点: {names}"
            )
