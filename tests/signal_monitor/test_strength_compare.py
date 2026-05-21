"""tests/signal_monitor/test_strength_compare.py — 中枢-free 力度对比内核单测。

compare_strength_raw 是纯函数（手搓力度字典即可测，无浮点噪声问题）；
compare_strength / manual_strength_compare 走真实 LINE + cd（合成 K 线，确定性）。
"""
from __future__ import annotations

import pytest

from chanlun.signal_monitor.strength_compare import (
    StrengthCompareResult,
    compare_strength,
    compare_strength_raw,
    find_line_by_locator,
    list_compare_lines,
    manual_strength_compare,
)


def _ld(up_sum: float = 0.0, down_sum: float = 0.0,
        dif_max: float = 0.0, dif_min: float = 0.0) -> dict:
    """构造一个与 query_macd_ld 输出同结构的力度字典。"""
    return {
        "macd": {
            "hist": {
                "sum": up_sum + down_sum,
                "up_sum": up_sum,
                "down_sum": down_sum,
                "max": 0.0,
                "min": 0.0,
                "end": 0.0,
            },
            "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
            "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        }
    }


# --------------------------- 纯函数 raw ---------------------------

def test_raw_down_beichi_when_new_low_and_weaker():
    """下跌段创新低 + 绿柱面积衰减 → 背驰。"""
    ref = _ld(down_sum=100.0, dif_min=-5.0)
    cur = _ld(down_sum=40.0, dif_min=-2.0)
    r = compare_strength_raw(ref, cur, "down", made_new_extreme=True)
    assert isinstance(r, StrengthCompareResult)
    assert r.is_beichi is True
    assert r.made_new_extreme is True
    assert r.direction == "down"
    assert r.macd_area_ratio == pytest.approx(0.4)
    assert r.strength_score == 60  # round((1 - 0.4) * 100)


def test_raw_no_beichi_when_no_new_extreme():
    """没有创新低 → 即便力度衰减也不算背驰。"""
    ref = _ld(down_sum=100.0)
    cur = _ld(down_sum=40.0)
    r = compare_strength_raw(ref, cur, "down", made_new_extreme=False)
    assert r.is_beichi is False
    assert r.strength_score == 0


def test_raw_no_beichi_when_stronger():
    """力度增强（面积变大）→ 不背驰。"""
    ref = _ld(up_sum=50.0)
    cur = _ld(up_sum=80.0)
    r = compare_strength_raw(ref, cur, "up", made_new_extreme=True)
    assert r.is_beichi is False
    assert r.macd_area_ratio == pytest.approx(1.6)


def test_raw_invalid_direction_raises():
    with pytest.raises(ValueError):
        compare_strength_raw(_ld(), _ld(), "sideways", made_new_extreme=True)


# --------------------- 真实 LINE + cd 封装 ---------------------

def test_compare_strength_requires_same_direction(cl_with_synthetic_klines):
    """两段不同向 → 抛 ValueError。"""
    cd = cl_with_synthetic_klines(200, multi_freq=True)
    bis = cd.get_bis()
    up = next(b for b in bis if b.type == "up")
    down = next(b for b in bis if b.type == "down")
    with pytest.raises(ValueError):
        compare_strength(up, down, cd)


def test_compare_strength_on_synthetic_returns_result(cl_with_synthetic_klines):
    """同向两笔能算出结构完整的对比结论。"""
    cd = cl_with_synthetic_klines(200, multi_freq=True)
    downs = [b for b in cd.get_bis() if b.type == "down"]
    assert len(downs) >= 2
    r = compare_strength(downs[-2], downs[-1], cd)
    assert isinstance(r, StrengthCompareResult)
    assert r.direction == "down"
    assert 0 <= r.strength_score <= 100


# ----------------- 手动图上对比 manual_strength_compare -----------------

def _dt(d) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def test_find_line_by_locator(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(200, multi_freq=True)
    xds = cd.get_xds()
    assert len(xds) >= 1
    target = xds[len(xds) // 2]
    found = find_line_by_locator(
        cd, _dt(target.start.k.date), _dt(target.end.k.date), "xd")
    assert found is not None
    assert found.type == target.type


def test_manual_compare_ref_not_found(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(200, multi_freq=True)
    r = manual_strength_compare(cd, "1999-01-01 00:00:00", "1999-01-02 00:00:00")
    assert r["ok"] is False
    assert "未找到" in r["error"]


def test_manual_compare_same_direction_ok(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(240, multi_freq=True)
    xds = cd.get_xds()
    cur = xds[-1]
    ref = next((x for x in xds[:-1] if x.type == cur.type), None)
    if ref is None:
        return  # 合成数据未凑出同向历史线段，跳过
    r = manual_strength_compare(cd, _dt(ref.start.k.date), _dt(ref.end.k.date), "xd")
    assert r["ok"] is True
    assert r["direction"] == cur.type
    assert isinstance(r["is_beichi"], bool)
    assert "verdict" in r


def test_manual_compare_direction_mismatch(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(240, multi_freq=True)
    xds = cd.get_xds()
    cur = xds[-1]
    ref = next((x for x in xds[:-1] if x.type != cur.type), None)
    if ref is None:
        return
    r = manual_strength_compare(cd, _dt(ref.start.k.date), _dt(ref.end.k.date), "xd")
    assert r["ok"] is False
    assert "方向不一致" in r["error"]


def test_list_compare_lines(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(220, multi_freq=True)
    lines = list_compare_lines(cd, "xd")
    assert isinstance(lines, list)
    assert len(lines) >= 1
    for ln in lines:
        assert ln["type"] in ("up", "down")
        assert "start" in ln and "end" in ln and "high" in ln and "low" in ln
    # limit 生效
    assert len(list_compare_lines(cd, "xd", limit=3)) <= 3
    # 列表里最后一项应能被 find_line_by_locator 定位到（与对比接口口径一致）
    last = lines[-1]
    assert find_line_by_locator(cd, last["start"], last["end"], "xd") is not None
