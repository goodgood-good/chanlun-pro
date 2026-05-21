"""tests/core/test_cl_data_to_tv_chart_zs.py — cl_data_to_tv_chart 对「无进入段中枢」的容错回归。

中枢重做后，开头中枢（核心段从线段序列 index 0 起、没有进入段）的
``ZS.start`` 合法地为 ``None``（见 zs_calculator._create_zs_full 文档：
``entry_idx == -1`` 时中枢没有进入段）。

历史 bug：``cl_data_to_tv_chart`` 在 cl_utils.py 绘制线段中枢时直接取
``zs.start.end.k.date``，遇到 ``ZS.start is None`` 抛
``AttributeError: 'NoneType' object has no attribute 'end'``，被 tv_history
外层 except 吞掉 → 返回 ``{"s": "no_data"}``，前端图表整体无数据。

本测试锁定：线段中枢 start 为 None 时，图表数据转换不得崩溃，且该中枢左
边界回退到首个核心线段的起点。
"""

from __future__ import annotations

from chanlun import fun
from chanlun.cl_utils import cl_data_to_tv_chart


def test_cl_data_to_tv_chart_handles_xd_zs_without_entry_segment(
    cl_with_synthetic_klines, cl_config
):
    """线段中枢无进入段（ZS.start is None）时，图表转换不得崩溃。"""
    cd = cl_with_synthetic_klines(500, seed=42, trend="up", multi_freq=True)
    xd_zss = cd.get_xd_zss()
    assert xd_zss, "合成上涨数据应至少产生 1 个线段中枢，否则本测试无意义"

    # 模拟「开头中枢无进入段」这一合法状态：start 置 None，核心 lines 保留。
    target = xd_zss[0]
    target.start = None

    # 只看线段中枢路径，关掉笔中枢避免与其它绘制分支耦合。
    config = dict(cl_config)
    config["chart_show_bi_zs"] = "0"
    config["chart_show_xd_zs"] = "1"

    chart_data = cl_data_to_tv_chart(cd, config)

    assert isinstance(chart_data, dict)
    # start 为 None 时，中枢左边界回退到首个核心线段的起点。
    expected_time = fun.datetime_to_int(target.lines[0].start.k.date)
    assert chart_data["xd_zss"][0]["points"][0]["time"] == expected_time


def test_cl_data_to_tv_chart_includes_recursive_l1plus(
    cl_with_synthetic_klines, cl_config
):
    """回归保护: 足量数据下图表数据的 recursive_levels 必须含 L1+。

    这是前端 charts.js 渲染多级中枢/走势的数据源(cl_utils.py:1168)。配合
    test_recursive_levels_not_starved(测数据层 get_recursive_levels), 这条测
    图表数据层 cl_data_to_tv_chart 把 L1+ 透传到前端——L0 口径回归会让此处
    恒空、图上多级中枢消失。
    """
    cd = cl_with_synthetic_klines(1500, seed=42, trend="up", multi_freq=True)
    config = dict(cl_config)
    config["chart_show_recursive_levels"] = "1"

    chart_data = cl_data_to_tv_chart(cd, config)

    rl = chart_data["recursive_levels"]
    levels_present = sorted({item["level"] for item in rl})
    assert any(lv >= 1 for lv in levels_present), (
        f"图表数据 recursive_levels 无 L1+, 前端将看不到多级中枢。"
        f"levels={levels_present}"
    )
