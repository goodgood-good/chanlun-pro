"""tests/core/test_zslx_fixture_regression.py — SH.000001 5m 真实数据走势类型回归。

fix/zhongshu-l0：盘整段 _type 继承摆动腿方向导致「净下跌的 up 段」，结合运算
(_jiehe_segments)误并「真上涨+横盘暴跌收尾」→ 30m tongjibie 段语义失真。
本回归锚定原文判据（docs/yuanwen_study/topic3 C3.2）：同级别分解交替段严格按
涨跌交替，段方向必须与净位移一致。

fixture: a_SH_000001_5m.parquet（2025-12-01~2026-06-11，QMT 5m，parquet 保位元精度）。
"""
from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from chanlun.core.cl import CL
from tests.core.conftest import DEFAULT_CL_CONFIG

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "fixtures" / "klines" / "a_SH_000001_5m.parquet"
)


def _load_000001() -> CL:
    if not _FIXTURE.exists():
        pytest.skip(f"缺少 fixture: {_FIXTURE}")
    df = pd.read_parquet(_FIXTURE)
    cd = CL("SH.000001", "5m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    return cd


def test_000001_5m_zslx_direction_matches_net_displacement():
    """每个 L0 走势类型：_type 与净位移（**转折点口径**）一致。

    转折点口径（L25128 段起点=前段结束点 / L8131 a1=b1 共享端点）：段方向的
    终点 = 离开段**起点**（离开段跨越转折、属下一段）；无离开段时用末段终点。
    修复前两类病：①盘整段继承摆动腿方向（高位横盘+暴跌收尾标 up，000001）；
    ②净位移用离开段终点（600519 down 腿翻成 up）。原文 L25179：Ai 按涨跌交替。"""
    cd = _load_000001()
    levels = cd.get_recursive_branch_levels()
    lv0 = next(lv for lv in levels if lv.level == 0)
    assert len(lv0.zslxs) >= 3, f"走势类型数异常: {len(lv0.zslxs)}"
    for k, z in enumerate(lv0.zslxs):
        if z.start is None or z.end is None:
            continue                          # 右边缘未完成段
        s_val = z.start.val
        has_leave = z.zss and z.zss[-1].end is not None
        e_val = z.end_line.start.val if has_leave else z.end.val
        if z._type == "up":
            assert e_val > s_val, (
                f"zslx[{k}] _type=up 但转折点净位移向下 {s_val}->{e_val}")
        elif z._type == "down":
            assert e_val < s_val, (
                f"zslx[{k}] _type=down 但转折点净位移向上 {s_val}->{e_val}")


def test_000001_5m_tongjibie_segs_alternate_and_seamless():
    """同级别分解交替段（摆动腿版）：严格方向交替（L25179/C3.2）+ 端点无缝
    （L25128 段起点=前段结束点）+ 三段重合产出 30m 中枢。"""
    cd = _load_000001()
    levels = cd.get_recursive_branch_levels()
    lv0 = next(lv for lv in levels if lv.level == 0)
    from chanlun.core.zs_upgrade import _swing_alternating_segs, tongjibie_zhongshu_ex
    segs = _swing_alternating_segs(lv0.zss)
    assert len(segs) >= 3
    for i in range(1, len(segs)):
        assert segs[i].dir != segs[i - 1].dir, (
            f"seg[{i-1}]→[{i}] 同向 {segs[i].dir}，违反 Ai 严格交替")
        assert segs[i].start is segs[i - 1].end or (
            abs(segs[i].start.val - segs[i - 1].end.val) < 1e-9), (
            f"seg[{i-1}].end 与 seg[{i}].start 不共享转折点")
    zss30, _meta = tongjibie_zhongshu_ex(lv0.zss, list(cd.get_xds()))
    assert len(zss30) >= 1, "三段上下上重合应产出 30m 同级别中枢"
    z = zss30[0]
    assert 3850 < z.zd < 3900 and 4090 < z.zg < 4150, f"30m 中枢区间漂移: [{z.zd},{z.zg}]"


def test_600519_5m_v_shape_leg_and_tongjibie():
    """600519 V 型 expand 链（1428→1322→1565）：摆动腿正确切为 down/up/down，
    三段重合产出 30m 中枢（zd≈1322）。

    修复前两类病在此标的的表现：净位移（离开段终点口径）把 down 腿标 up →
    与后段合并 → 仅 2 段无三段重合 → 30m 中枢丢失。

    口径说明：v34 摆动腿语义在 legacy 4 段确认口径下钉死（显式声明，不再依赖
    config 缺键 fallback——第77轮 F8 把缺键 fallback 统一为生产口径 3，本测试
    曾隐式跑在 4 上）。3 段口径下同窗口的退化是已知开放问题，见下一个测试。"""
    fixture = _FIXTURE.parent / "a_SH_600519_5m.parquet"
    if not fixture.exists():
        pytest.skip(f"缺少 fixture: {fixture}")
    df = pd.read_parquet(fixture)
    cfg = dict(DEFAULT_CL_CONFIG)
    cfg["recursive_l0_min_zs_lines"] = 4
    cd = CL("SH.600519", "5m", cfg)
    cd.process_klines(df)
    lv0 = next(lv for lv in cd.get_recursive_branch_levels() if lv.level == 0)
    from chanlun.core.zs_upgrade import _swing_alternating_segs, tongjibie_zhongshu_ex
    segs = _swing_alternating_segs(lv0.zss)
    assert [s.dir for s in segs[:3]] == ["down", "up", "down"]
    zss30, _meta = tongjibie_zhongshu_ex(lv0.zss, list(cd.get_xds()))
    assert len(zss30) >= 1, "下上下三段重合应产出 30m 中枢"
    assert abs(zss30[0].zd - 1322.01) < 1.0, f"30m 中枢 zd 漂移: {zss30[0].zd}"


def test_600519_5m_l0min3_swing_blindness_known_issue():
    """【现状记录·已知开放问题】生产 3 段口径下 600519 同窗口的摆动腿反转失明。

    机理（2026-06-13 第77轮 F8 修复暴露）：3 段成枢的 V 型转折点中枢 z2
    （dd=1322, gg=1565）——第三段暴力拉升使中枢包络吃进整个离开段；
    _swing_segments 的反转确认要求后续中枢 dd > 谷中枢 gg=1565（全窗口最高）
    → 永假 → 全程单条 down 腿 → 30m tongjibie 中枢丢失。
    与 G 清单 1.23 core_envelope 张力同源（买卖点链路已用本体包络防远摆撑爆，
    _swing_segments 仍用原始 gg/dd）；疑为 §102「30m 同级别信号稀疏」系统性根因。
    修复候选（矩阵主轴六）：脱离判定改用核心区/本体包络，或第三段暴力离开的
    成枢语义裁决（38/20课）。修复落地时本测试应反转为正向断言。"""
    fixture = _FIXTURE.parent / "a_SH_600519_5m.parquet"
    if not fixture.exists():
        pytest.skip(f"缺少 fixture: {fixture}")
    df = pd.read_parquet(fixture)
    cfg = dict(DEFAULT_CL_CONFIG)
    cfg["recursive_l0_min_zs_lines"] = 3
    cd = CL("SH.600519", "5m", cfg)
    cd.process_klines(df)
    lv0 = next(lv for lv in cd.get_recursive_branch_levels() if lv.level == 0)
    from chanlun.core.zs_upgrade import _swing_alternating_segs, tongjibie_zhongshu_ex
    segs = _swing_alternating_segs(lv0.zss)
    zss30, _meta = tongjibie_zhongshu_ex(lv0.zss, list(cd.get_xds()))
    # 现状钉死（防无声漂移）：单腿 + 零 30m 中枢；修复后此断言应失败并被反转
    assert [s.dir for s in segs[:3]] == ["down"], (
        f"3段口径摆动腿现状已变化: {[s.dir for s in segs[:3]]} — 若已修复反转失明, "
        "请把本测试反转为 down/up/down + 30m 中枢存在的正向断言"
    )
    assert len(zss30) == 0
