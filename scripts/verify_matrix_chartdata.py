# -*- coding: utf-8 -*-
"""端到端验证三周期矩阵的 chart_data（web 同路径 cl_data_to_tv_chart）。

断言(goal 三周期矩阵):
  1m 图: 笔/线段 + recursive_levels 含 L0(1m) + L1(5m,中枢/买卖点/背驰) + L2(30m,行存在)
  5m 图: 笔/线段 + L0(5m) + L1(30m)
  30m图: 笔/线段 + L0(30m)（无升级链）
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.exchange.exchange_qmt import ExchangeQMT
from chanlun.core.cl import CL as CoreCL
from chanlun.cl_utils import cl_data_to_tv_chart

CFG = {
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
    "chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
    "chart_show_bi_zs": "1", "chart_show_xd_zs": "1",
    "chart_show_bi_mmd": "1", "chart_show_xd_mmd": "1",
    "chart_show_bi_bc": "1", "chart_show_xd_bc": "1",
    "chart_use_branch_core": "1",            # 新核心(图表默认)
    "chart_show_recursive_levels": "1",
    "chart_show_interval_nest": "1",
}
EXPECT_LEVELS = {"1m": [0, 1, 2], "5m": [0, 1], "30m": [0]}


def main():
    ex = ExchangeQMT()
    ok = True
    # 窗口=web 真实口径(QMT_LOOKBACK_OVERRIDE_DAYS: 1m/5m=365天;30m 默认)
    for freq, sd in [("1m", "2025-06-12"), ("5m", "2025-06-12"), ("30m", "2025-06-03")]:
        df = ex.klines("SH.000001", freq, start_date=sd, end_date="2026-06-11")
        cd = CoreCL("SH.000001", freq, dict(CFG))
        cd.process_klines(df)
        chart = cl_data_to_tv_chart(cd, dict(CFG))
        rl = chart.get("recursive_levels") or []
        lv_map = {e["level"]: e for e in rl}
        got_levels = sorted(lv_map)
        exp = EXPECT_LEVELS[freq]
        line = (f"{freq}: bis={len(chart['bis'])} xds={len(chart['xds'])} "
                f"levels={got_levels}(期望{exp})")
        for lvl in got_levels:
            e = lv_map[lvl]
            line += (f" | L{lvl}: zss={len(e['zss'])} mmds={len(e.get('mmds') or [])} "
                     f"bcs={len(e.get('bcs') or [])}")
        print(line)
        if got_levels != exp:
            ok = False
            print(f"  !! {freq} 级别集不符")
        if not chart["bis"] or not chart["xds"]:
            ok = False
            print(f"  !! {freq} 笔/线段为空")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
