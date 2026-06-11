# -*- coding: utf-8 -*-
"""TSLA 真·walk-forward 回测(绝对无未来函数) vs 同口径预算回测对照。

策略 = 5m 操作级买卖点 + 30m 方向门控(spec 两级体系),US T+0,max_pos=1(满仓单标)。
两臂**执行模型完全相同**(下一根5m开盘成交、commission 0.0001、无滑点、按比例建仓、
卖点/门控down全平),只差信号时点:

- 预算臂:全序列 CL 一次算信号,按 anchor 分型日期触发(=当前主回测口径,含未来函数)。
- WF 臂:5m 逐根尾喂增量,信号 (date,bs_type) **首次出现**即触发(含右边缘幻影/会消失
  的信号);30m 门控用 wf_dir_series(逐根增量,当下可见笔方向,无 lookahead)。
  绝对无未来函数:每个动作只用「该 5m bar 收盘时已知」的信息。

买入比例 recommended_buy_ratio(3买=1.0/2买=0.75/1买=0.5,门控up各+0.15,trend3_boost
对up中3买×1.25);单标 max_pos=1 → base=1.0。运行: PYTHONPATH=src python scripts/wf_backtest_tsla.py
"""
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import (
    CL_CFG, collect_branch_signals, wf_dir_series,
    recommended_buy_ratio, buy_class, US_STOCK,
)
from chanlun.core.cl import CL

DIR = "D:/chanlun_pro/chart_cache_us_tsla_1y"
WARMUP = 400
COMMISSION = US_STOCK.commission   # 0.0001
GATE_DELAY = pd.Timedelta("30min")


def _load(freq: str) -> pd.DataFrame:
    e = pickle.loads(Path(f"{DIR}/v33_us_TSLA_US_{freq}_recursivebt.pkl").read_bytes())
    d = e["data"]
    return pd.DataFrame({
        "date": pd.to_datetime(d["t"], unit="s"),
        "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"],
    })


def _gate_by_bar(df5: pd.DataFrame, df30: pd.DataFrame) -> list:
    """30m 方向 → 逐 5m bar 门控状态(wf_dir_series 无 lookahead;事件+30min 生效)。"""
    events = wf_dir_series(df30, "TSLA.US", "30m")     # [(30m bar收盘时刻, up/down/neutral)]
    dates5 = list(df5["date"])
    out = ["neutral"] * len(dates5)
    bi, cur = 0, "neutral"
    for i, d in enumerate(dates5):
        while bi < len(events) and events[bi][0] + GATE_DELAY <= d:
            cur = events[bi][1]
            bi += 1
        out[i] = cur
    return out


def _run_exec(buy_events: dict, sell_bars: set, gate: list, opens, closes, dates) -> dict:
    """执行模型(两臂共用)。buy_events[i]=该bar触发的最高优先级买点bs_type;
    sell_bars=触发卖出的bar集合;门控down也强平。下一根开盘成交,T+0。"""
    n = len(opens)
    cash, shares, entry_px = 1.0, 0.0, 0.0
    trades = []
    pending = None        # (act, bs_type)
    peak, max_dd = 1.0, 0.0
    for i in range(n):
        # 1) 执行上一bar挂单(本bar开盘)
        if pending is not None and i < n:
            act, bs = pending
            px = opens[i]
            if act == "buy" and shares == 0.0 and cash > 0 and px > 0:
                ratio = recommended_buy_ratio(bs, 1, big_dir=gate[i - 1] if i > 0 else "neutral",
                                              trend_boost=True)
                budget = min(cash, (cash + shares * px) * ratio) * 0.99
                sz = budget / (px * (1 + COMMISSION))
                if sz > 0:
                    cash -= sz * px * (1 + COMMISSION)
                    shares, entry_px = sz, px
            elif act == "sell" and shares > 0.0 and px > 0:
                cash += shares * px * (1 - COMMISSION)
                trades.append((entry_px, px, px / entry_px - 1, bs))
                shares = 0.0
            pending = None
        # 2) 本bar收盘后决策 → 下一bar挂单
        eq = cash + shares * closes[i]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        gate_down = gate[i] == "down"
        if shares > 0.0 and (i in sell_bars or gate_down):
            pending = ("sell", "exit")
        elif shares == 0.0 and i in buy_events and not gate_down:
            pending = ("buy", buy_events[i])
    if shares > 0.0:
        cash += shares * closes[-1] * (1 - COMMISSION)
        trades.append((entry_px, closes[-1], closes[-1] / entry_px - 1, "final"))
    return {"ret": cash - 1.0, "trades": trades, "max_dd": max_dd, "n": len(trades)}


def _pick_buy(sigs) -> str:
    """3买优先(最高 class);返回 bs_type 或 ''。"""
    best, best_cls = "", 0
    for s in sigs:
        c = buy_class(s.bs_type)
        if c in (1, 2, 3) and c > best_cls:
            best, best_cls = s.bs_type, c
    return best


def main() -> int:
    df5, df30 = _load("5m"), _load("30m")
    n = len(df5)
    opens, closes = df5["open"].to_numpy(), df5["close"].to_numpy()
    dates = list(df5["date"])
    d2i = {d: i for i, d in enumerate(dates)}
    gate = _gate_by_bar(df5, df30)
    print(f"TSLA 5m bars={n} ({dates[0].date()}~{dates[-1].date()}) op=5m gate=30m max_pos=1 T+0")

    # ---- 预算臂:全序列信号,anchor 日期触发 ----
    cdf = CL("TSLA.US", "5m", dict(CL_CFG))
    cdf.process_klines(df5)
    pre_buys, pre_sells = {}, set()
    by_bar = {}
    for s in collect_branch_signals(cdf, use_xd=False):
        i = d2i.get(s.date)
        if i is None:
            continue
        by_bar.setdefault(i, []).append(s)
    for i, sigs in by_bar.items():
        b = _pick_buy([s for s in sigs if s.is_buy])
        if b:
            pre_buys[i] = b
        if any(s.is_sell for s in sigs):
            pre_sells.add(i)
    pre = _run_exec(pre_buys, pre_sells, gate, opens, closes, dates)

    # ---- WF 臂:逐根尾喂,信号首次出现即触发(无未来函数) ----
    cd = CL("TSLA.US", "5m", dict(CL_CFG))
    cd.process_klines(df5.iloc[:WARMUP].reset_index(drop=True))
    ever = set()
    wf_buys, wf_sells = {}, set()
    for i in range(WARMUP, n):
        cd.process_klines(df5.iloc[i:i + 1].reset_index(drop=True))
        fresh_buys, fresh_sell = [], False
        for s in collect_branch_signals(cd, use_xd=False):
            k = (s.date, s.bs_type)
            if k in ever:
                continue
            ever.add(k)
            if s.is_buy:
                fresh_buys.append(s)
            elif s.is_sell:
                fresh_sell = True
        b = _pick_buy(fresh_buys)
        if b:
            wf_buys[i] = b
        if fresh_sell:
            wf_sells.add(i)
    wf = _run_exec(wf_buys, wf_sells, gate, opens, closes, dates)

    bh = closes[-1] / opens[WARMUP] - 1
    print(f"裸持TSLA           = {bh:+7.1%}")
    print(f"预算(有未来函数)   = {pre['ret']:+7.1%}  回撤{pre['max_dd']:5.1%}  {pre['n']}笔")
    print(f"真walk-forward     = {wf['ret']:+7.1%}  回撤{wf['max_dd']:5.1%}  {wf['n']}笔")
    print(f"差(WF-预算)        = {wf['ret'] - pre['ret']:+7.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
