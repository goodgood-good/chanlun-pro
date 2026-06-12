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

from types import SimpleNamespace

import pytest

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


@pytest.mark.skip(reason="P9 止血: L1+ 渲染暂关(cl_utils 只画L0), 中枢升级按 line4898 重做后恢复")
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
    assert all("zslx_lines" in item for item in rl), "递归层级应暴露走势类型线段"


def test_cl_data_to_tv_chart_exposes_current_level_zslx_lines(
    cl_with_synthetic_klines, cl_config
):
    """当前周期走势类型应同时暴露为线段,供前端作为下一级中枢构件着色。"""
    cd = cl_with_synthetic_klines(1500, seed=42, trend="up", multi_freq=True)
    zslxs = [z for z in (cd.get_xd_zslx() or []) if z.zss]
    assert zslxs, "合成数据应产生当前周期走势类型"

    config = dict(cl_config)
    config["chart_show_xd_zslx"] = "1"
    chart_data = cl_data_to_tv_chart(cd, config)

    lines = chart_data["xd_zslx_lines"]
    assert len(lines) == len(zslxs)
    first = lines[0]
    assert first["points"][0]["time"] == fun.datetime_to_int(zslxs[0].start.k.date)
    assert first["points"][-1]["time"] == fun.datetime_to_int(zslxs[0].end.k.date)
    assert first["level"] == 0


def test_cl_data_to_tv_chart_includes_recursive_upgrade_mmds(cl_config):
    """中枢升级买卖点应合并进 xd_mmds（文本带「升」前缀），且不冲掉基础买卖点。

    用真实 301004 1m fixture（已知能产出 1 个升级三卖），守护「升级买卖点接入
    图表」这条链路：升级信号可见 + 基础 bi/xd 买卖点完好。
    """
    import pathlib
    import pandas as pd
    import pytest
    from chanlun.core.cl import CL
    from tests.core.conftest import DEFAULT_CL_CONFIG

    csv = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines" / "a_SZ_301004_1m.csv"
    if not csv.exists():
        pytest.skip("缺少 a_SZ_301004_1m fixture")
    cd = CL("301004", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(pd.read_csv(csv, parse_dates=["date"]))

    config = dict(cl_config)
    config["chart_show_recursive_levels"] = "1"
    config["chart_show_xd_mmd"] = "1"
    xdm = cl_data_to_tv_chart(cd, config)["xd_mmds"]

    assert any("升" in m["text"] for m in xdm), "应至少有 1 个升级买卖点(升3S)合并进 xd_mmds"
    assert any("升" not in m["text"] for m in xdm), "基础 xd 买卖点不得被升级买卖点冲掉"


def test_cl_data_to_tv_chart_xd_mmds_are_segment_level_not_bi(
    cl_with_synthetic_klines, cl_config
):
    """新核心(branch core 开)下:**段买卖点 xd_mmds 来自线段、笔买卖点 bi_mmds 来自笔**,各归
    其位。原 bug:`get_branch_bspoints` 恒用笔却全塞 xd_mmds(标 level=xd)=笔买卖点冒充段买卖点。"""
    cd = cl_with_synthetic_klines(1500, seed=42, trend="up", multi_freq=True)
    config = dict(cl_config)
    config["chart_use_branch_core"] = "1"
    config["chart_show_recursive_levels"] = "1"
    config["chart_show_bi_mmd"] = "1"
    config["chart_show_xd_mmd"] = "1"
    chart = cl_data_to_tv_chart(cd, config)
    bi_mmds, xd_mmds = chart["bi_mmds"], chart["xd_mmds"]
    assert bi_mmds, "合成数据应产出笔级买卖点"
    # level 标签各归其位
    assert all(m["level"] == "bi" for m in bi_mmds), "bi_mmds 必须全是笔级"
    assert all(m["level"] == "xd" for m in xd_mmds), "xd_mmds 必须全是段级"
    # 来源正确:bi_mmds==笔级递归、xd_mmds==段级(线段)递归(非笔冒充)
    assert len(bi_mmds) == len(cd.get_branch_bspoints(use_xd=False)), "bi_mmds 来自笔"
    assert len(xd_mmds) == len(cd.get_branch_bspoints(use_xd=True)), "xd_mmds 来自线段"
    # 线段比笔稀疏 → 段买卖点应少于笔买卖点(确实分开、不是同一份笔买卖点)
    assert len(xd_mmds) < len(bi_mmds), "段买卖点应少于笔买卖点(线段稀疏), 证明非笔冒充段"


def test_cl_data_to_tv_chart_bcs_from_branch_core(cl_config):
    """新核心(branch core 开)背驰信号 = get_branch_bcs(笔→bi_bcs、段→xd_bcs),与买卖点同源。
    原图表背驰走 legacy line_bcs(极稀疏)、与新核心一类买卖点不一致(用户:背驰信号没有)。
    用真实 301004(已知有背驰);合成上涨数据无力度衰减、产不出背驰。"""
    import pathlib
    import pandas as pd
    import pytest
    from chanlun.core.cl import CL
    from tests.core.conftest import DEFAULT_CL_CONFIG

    csv = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines" / "a_SZ_301004_1m.csv"
    if not csv.exists():
        pytest.skip("缺少 a_SZ_301004_1m fixture")
    cd = CL("301004", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(pd.read_csv(csv, parse_dates=["date"]))

    config = dict(cl_config)
    config["chart_use_branch_core"] = "1"
    config["chart_show_bi_bc"] = "1"
    config["chart_show_xd_bc"] = "1"
    chart = cl_data_to_tv_chart(cd, config)
    bi_bcs, xd_bcs = chart["bi_bcs"], chart["xd_bcs"]
    assert bi_bcs or xd_bcs, "301004 应产出新核心背驰信号"
    assert all(m["level"] == "bi" for m in bi_bcs), "bi_bcs 必须全是笔级"
    assert all(m["level"] == "xd" for m in xd_bcs), "xd_bcs 必须全是段级"
    assert len(bi_bcs) == len(cd.get_branch_bcs(use_xd=False)), "bi_bcs 来自笔级新核心背驰"
    assert len(xd_bcs) == len(cd.get_branch_bcs(use_xd=True)), "xd_bcs 来自段级新核心背驰"
    assert all(m["text"] in ("QS", "PZ") for m in bi_bcs + xd_bcs), "背驰类型=QS(趋势)/PZ(盘整)"


def test_cl_data_to_tv_chart_renders_forming_l0_zhongshu_dashed(
    cl_with_synthetic_klines, cl_config, monkeypatch
):
    """L0 右边缘正在形成的未完成中枢(LevelResult.live_zss)应序列化进
    recursive_levels[L0].zss 且 linestyle='1'(虚线),让前端画出正在形成的 5min 中枢。

    解耦:不依赖合成数据恰好停在中枢形成途中(脆弱),而是注入一个含 live_zss 的
    LevelResult(素材复用真实 L0 中枢),只验证序列化层是否透传未完成中枢到图表。
    """
    import copy
    from chanlun.core.recursive_branch import LevelResult

    cd = cl_with_synthetic_klines(500, seed=42, trend="up", multi_freq=True)
    levels = cd.get_recursive_branch_levels()
    l0_real = next((lv for lv in levels if lv.level == 0 and lv.zss), None)
    assert l0_real, "合成数据应产出 L0 done 中枢作为构造素材"

    done_zs = l0_real.zss[0]
    forming = copy.copy(done_zs)          # 复用真实中枢做未完成素材, 只翻 done 标志
    forming.done = False
    fake = LevelResult(
        level=0, zss=[done_zs], done_divergence=[None], zslxs=[], live_zss=[forming],
    )
    monkeypatch.setattr(cd, "get_recursive_branch_levels", lambda: [fake])

    config = dict(cl_config)
    config["chart_use_branch_core"] = "1"
    config["chart_show_recursive_levels"] = "1"
    chart_data = cl_data_to_tv_chart(cd, config)

    l0 = next(item for item in chart_data["recursive_levels"] if item["level"] == 0)
    styles = [z["linestyle"] for z in l0["zss"]]
    assert "1" in styles, f"L0 应含正在形成的未完成中枢(虚线 linestyle=1), 实得 {styles}"
    assert "0" in styles, f"L0 也应含已完成中枢(实线 linestyle=0), 实得 {styles}"


def test_cl_data_to_tv_chart_multitimeframe_overlay_contract(cl_config):
    """Chart JSON exposes the requested current/higher-level containers."""
    import pathlib
    import pandas as pd
    from chanlun.core.cl import CL
    from tests.core.conftest import DEFAULT_CL_CONFIG

    base = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines"
    expected = {
        "1m": {0, 1, 2},   # current L0 + 5m + 30m
        "5m": {0, 1},      # current L0 + 30m
        "30m": {0},        # current L0
    }
    for freq, required_levels in expected.items():
        csv = base / f"a_SZ_301004_{freq}.csv"
        if not csv.exists():
            pytest.skip(f"missing fixture: {csv.name}")
        cd = CL("301004", freq, dict(DEFAULT_CL_CONFIG))
        cd.process_klines(pd.read_csv(csv, parse_dates=["date"]))
        config = dict(cl_config)
        config.update({
            "chart_use_branch_core": "1",
            "chart_show_recursive_levels": "1",
            "chart_show_bi": "1",
            "chart_show_bi_mmd": "1",
            "chart_show_bi_bc": "1",
            "chart_show_xd_mmd": "1",
            "chart_show_xd_bc": "1",
        })

        chart = cl_data_to_tv_chart(cd, config)

        assert chart["bis"], f"{freq} chart must expose bi strokes"
        assert "bi_mmds" in chart and "xd_mmds" in chart
        assert "bi_bcs" in chart and "xd_bcs" in chart
        levels = {item["level"] for item in chart["recursive_levels"]}
        assert required_levels <= levels, f"{freq} recursive_levels={levels}"
        for item in chart["recursive_levels"]:
            assert "zss" in item and "zslx_lines" in item
            if item["level"] >= 1:
                assert "mmds" in item and "bcs" in item


def test_cl_data_to_tv_chart_serializes_l1_l2_mmds_and_bcs(
    cl_with_synthetic_klines, cl_config, monkeypatch
):
    """L1/L2 buy-sell points and divergences ride with recursive_levels."""
    import pandas as pd

    cd = cl_with_synthetic_klines(
        500, seed=42, trend="up", multi_freq=True, frequency="1m"
    )
    base_ts = pd.Timestamp("2026-01-01 10:00:00", tz="Asia/Shanghai")

    def fake_point(ts, val, bs_type):
        k = SimpleNamespace(date=ts)
        fx = SimpleNamespace(k=k, val=val)
        return SimpleNamespace(anchor_fx=fx, bs_type=bs_type, level=None)

    monkeypatch.setattr(
        cd,
        "get_kuozhan_levels",
        lambda: [
            {
                "level": 1,
                "zss": [],
                "bsp": [fake_point(base_ts, 10.5, "3buy")],
                "bcs": [(base_ts, 10.8, "qs")],
            },
            {
                "level": 2,
                "zss": [],
                "bsp": [fake_point(base_ts + pd.Timedelta(minutes=5), 11.5, "1sell")],
                "bcs": [(base_ts + pd.Timedelta(minutes=5), 11.8, "pz")],
            },
        ],
    )

    config = dict(cl_config)
    config["chart_use_branch_core"] = "1"
    config["chart_show_recursive_levels"] = "1"
    chart = cl_data_to_tv_chart(cd, config)
    levels = {item["level"]: item for item in chart["recursive_levels"]}

    assert levels[1]["mmds"][0]["level"] == "5m"
    assert levels[1]["mmds"][0]["text"] == "3buy"
    assert levels[1]["bcs"][0]["level"] == "5m"
    assert levels[1]["bcs"][0]["text"] == "QS"
    assert levels[2]["mmds"][0]["level"] == "30m"
    assert levels[2]["mmds"][0]["text"] == "1sell"
    assert levels[2]["bcs"][0]["level"] == "30m"
    assert levels[2]["bcs"][0]["text"] == "PZ"


def test_original_level_ladder_contract_uses_30m_same_level_decomposition():
    """用户指定的原文级别规则：30m 同级别，30m 以下非同级别。

    1m 图展示 1m/5m/30m；5m 图展示 5m/30m；30m 图只展示本级别。
    低于 30m 的升级采用 kuozhan(非同级别分解：延伸/扩展/扩张)，到 30m 时采用
    tongjibie(同级别分解：三段走势类型重合，不延伸)。
    """
    from chanlun.core.cl import CL

    assert CL._UPGRADE_CHAIN == {
        "1m": [("5m", "kuozhan"), ("30m", "tongjibie")],
        "5m": [("30m", "tongjibie")],
    }
    assert "30m" not in CL._UPGRADE_CHAIN
