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

from chanlun.recursive_bt.portfolio import (
    _load_bt_universe, load_cached, portfolio_backtest,
)

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
        vscore = np.zeros(n)        # ②比价低估度 = ROE年化/PB(高=优质却便宜,point-in-time)
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
                bps = r["bps"]
                if bps and bps > 0 and close[i] > 0:
                    vscore[i] = roe_ann * bps / close[i]            # ROE年化 ÷ PB(=价/BPS)
            mi = mkt_d2i.get(di)                                     # rs 仅留作参考(非比价口径)
            if i >= rs_win and mi is not None and mi >= rs_win:
                sret = close[i] / close[i - rs_win] - 1
                mret = mkt_close[mi] / mkt_close[mi - rs_win] - 1
                rs[i] = sret - mret
        s["fund_ok"] = fund_ok
        s["rs"] = rs
        s["_vscore"] = vscore
        s["total_share"] = total_share
        passed_fund += int(fund_ok.any())
        passed_any += 1
    # ②比价低估:全市场自校准——ROE年化/PB 高于中位=优质却便宜=低估=通过(原文line38539市值与行业地位)
    allv = np.concatenate([s["_vscore"][s["_vscore"] > 0] for s in syms.values()
                           if (s["_vscore"] > 0).any()] or [np.zeros(1)])
    vmed = float(np.median(allv)) if len(allv) else 0.0
    for s in syms.values():
        s["value_ok"] = s["_vscore"] > vmed
    passed_val = sum(int(s["value_ok"].any()) for s in syms.values())
    print(f"①基本面通过过: {passed_fund}/{passed_any}; ②比价低估阈值(ROE年化/PB)中位={vmed:.2f}, "
          f"低估过: {passed_val}/{passed_any}")


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
    # 大级别门控对照:走势方向(周线笔方向)替代离散买卖点(周线图无买卖点→bsp门控恒neutral失效)
    if any("trend_dir_at" in s for s in syms.values()):
        for name, req in [("③技术面 + 走势方向门控", ("tech",)),
                          ("①+③ + 走势方向门控", ("tech", "fund"))]:
            portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label,
                               require=req, big_gate="trend")
            print(f"    ↑ {name}")


def main_v2():
    """原文选股系统 v2 对比(2026-06-10 深挖第8/9课+line23172):
    海选门槛(第8课:收盘>70日线=「能搞的」分类,250天线因数据仅1年降级为原文明示的70天线)
    + ②比价资金流向(第9课原味:「比价关系的变动…和市场资金的流向相关」,20日RS>大盘)
    + 3买优先(line23172「牛市里第三类买点的爆发力是最强的」)。全部 point-in-time。"""
    from chanlun.recursive_bt.portfolio import attach_pool_filters
    syms = _load_bt_universe()
    market = load_cached("SH.000001")
    if market is None:
        print("缺上证指数缓存")
        return
    attach_scores(syms, market)
    attach_pool_filters(syms, market)
    n_ma = sum(int(s["ma_ok"].any()) for s in syms.values())
    n_rs = sum(int(s["rs_ok"].any()) for s in syms.values())
    label = f"{len(syms)}只"
    print(f"海选(70日线)曾通过: {n_ma}/{len(syms)}; RS(20日强于大盘)曾通过: {n_rs}/{len(syms)}")
    print("#" * 64)
    print("# 原文选股系统 v2(第8课海选+第9课比价资金流向+line23172三买优先) wf门控")
    print("#" * 64)
    configs = [
        ("基线: ③技术面买点(1买优先)", ("tech",), "1first"),
        ("3买优先(line23172 牛市口径)", ("tech",), "3first"),
        ("+海选70日线(第8课「能搞的」)", ("tech", "ma"), "1first"),
        ("+RS资金流向(第9课比价)", ("tech", "rs"), "1first"),
        ("v2: 海选+RS+3买优先", ("tech", "ma", "rs"), "3first"),
        ("v2+①基本面(乘法原则四过滤)", ("tech", "ma", "rs", "fund"), "3first"),
    ]
    for name, req, bp in configs:
        portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label,
                           require=req, big_gate="trend", buy_priority=bp)
        print(f"    ↑ {name}")


def main_v3():
    """原文三层架构(line38515-38544「三个独立系统完美的组合」):
    ①基本面结构层=行业龙头70%+成长30%季度池(industry.build_pool_schedule)
    ②比价再平衡层=季度重算池(市值/行业地位变化→换股)
    ③技术面执行层=缠论买点(3买优先)+wf走势方向门控定时机。
    对比:三层架构 vs 基线(全池猎手) vs 池子买入持有(无技术面=剥离③的贡献)。"""
    from chanlun.recursive_bt.industry import build_pool_schedule
    syms = _load_bt_universe()
    sched = build_pool_schedule(syms)
    label = f"{len(syms)}只"
    print(f"季度池 {len(sched)} 期, 首期 {len(sched[0][1])} 只" if sched else "池空")
    print("#" * 64)
    print("# 原文三层架构(①行业池70/30 ②季度换股 ③缠论时机) vs 基线")
    print("#" * 64)
    portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label,
                       big_gate="trend", buy_priority="3first")
    print("    ↑ 基线: 全池买点选股(3买优先)+wf门控")
    portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label,
                       big_gate="trend", buy_priority="3first", pool_schedule=sched)
    print("    ↑ 三层架构: ①池70/30 + ②季度换股 + ③买点时机+wf门控")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "v2":
        main_v2()
    elif len(sys.argv) > 1 and sys.argv[1] == "v3":
        main_v3()
    else:
        main()
