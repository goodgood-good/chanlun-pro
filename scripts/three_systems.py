"""scripts/three_systems.py — 缠论「三个独立的系统」组合选股回测(原文line38542)。

①基本面(quality+growth,point-in-time按公告日) + ②比价(相对大盘强度=资金流入) + ③技术面(缠论买卖点)。
概率原则(line8245):三独立系统同时确认→数学胜率保证。对比:技术面only / +基本面 / +比价 / 三结合。
运行: PYTHONPATH="src;web/chanlun_chart;." python scripts/three_systems.py
"""
from __future__ import annotations

import os
import pickle

import numpy as np
import pandas as pd

from portfolio_backtest import _load_bt_universe, load_cached, portfolio_backtest

FUND_DIR = "D:/chanlun_pro/bt_data_fund"
RS_WIN = 960          # 相对强度窗口(~20交易日×48根5m)
ROE_ANN_MIN = 8.0     # 基本面:年化ROE阈值(%)


def attach_scores(syms: dict, market: dict, rs_win: int = RS_WIN):
    """给每只股票按 bar 计算 fund_ok(基本面 point-in-time) + rs(比价相对强度)。"""
    mkt_close = market["close"]
    mkt_d2i = market["d2i"]
    passed_fund = passed_any = 0
    for code, s in syms.items():
        reports = []
        total_share = None
        fp = f"{FUND_DIR}/{code}.pkl"
        if os.path.exists(fp):
            fd = pickle.load(open(fp, "rb"))
            reports = fd.get("reports", [])
            total_share = fd.get("total_share")
        # 公告日 → 可用时点(公告次日,防当日盘中 lookahead)
        rep_av = [pd.Timestamp(r["anntime"], tz="Asia/Shanghai") + pd.Timedelta("1D")
                  for r in reports]
        dates = s["dates"]
        close = s["close"]
        n = len(dates)
        fund_ok = np.zeros(n, bool)
        rs = np.zeros(n)
        ri = -1
        for i in range(n):
            di = dates[i]
            while ri + 1 < len(reports) and rep_av[ri + 1] <= di:
                ri += 1
            if ri >= 0:
                r = reports[ri]
                roe_ann = (r["roe"] or 0.0) * 4.0                    # 单季ROE×4≈年化
                grow = max(r["rev_inc"] if r["rev_inc"] is not None else -999,
                           r["np_inc"] if r["np_inc"] is not None else -999)
                fund_ok[i] = (roe_ann > ROE_ANN_MIN) and (grow > 0)
            mi = mkt_d2i.get(di)
            if i >= rs_win and mi is not None and mi >= rs_win:
                sret = close[i] / close[i - rs_win] - 1
                mret = mkt_close[mi] / mkt_close[mi - rs_win] - 1
                rs[i] = sret - mret
        s["fund_ok"] = fund_ok
        s["rs"] = rs
        s["total_share"] = total_share
        passed_fund += int(fund_ok.any())
        passed_any += 1
    print(f"基本面有数据/通过过(任一bar): {passed_fund}/{passed_any} 只")


def main():
    syms = _load_bt_universe()
    market = load_cached("SH.000001")
    if market is None:
        print("缺上证指数缓存(比价基准),先 qmt_fetch 上证")
        return
    print(f"载入 {len(syms)} 只 + 上证基准,计算三系统分数...")
    attach_scores(syms, market)
    label = f"{len(syms)}只"
    print("#" * 64)
    print("# 缠论三个独立系统组合选股(基本面+比价+技术面,原文line38542/概率原则)")
    print("#" * 64)
    configs = [
        ("③技术面 only(基线=之前的纯缠论买点)", ("tech",)),
        ("①基本面 + ③技术面", ("tech", "fund")),
        ("②比价 + ③技术面", ("tech", "value")),
        ("①+②+③ 三系统结合(概率原则)", ("tech", "fund", "value")),
    ]
    for name, req in configs:
        portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label, require=req)
        print(f"    ↑ {name}")


if __name__ == "__main__":
    main()
