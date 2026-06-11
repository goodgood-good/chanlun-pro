# -*- coding: utf-8 -*-
"""TSLA 区间套式确认(方案B,真·wf 无未来函数):5m 买点首见 + 1m 笔方向当下结构确认。

原文依据:61课L33017「观察内部结构…逐次下去…在当下精确定位转折点」/29课L20110
「5分钟背驰段,考察1分钟以下级别精确定位」——本级别信号的确认=次级别结构完成,
而非本级别 bar 数盲等(第69轮 N=12 盲等确认的正统替代)。

流程:
- 5m 信号 episodes(wf_confirm_tsla_stage1.pkl,逐根重算产物,无未来函数);
- 1m 当下笔方向 wf_dir_series(尾喂增量,无 lookahead);
- 买点:5m episode 首见时刻 t0 起,在 1m 轴等**第一个 1m 笔方向翻 up**的 bar(结构确认,
  限 MAX_WAIT_1M 根内,且确认时 5m 信号仍存活[alive_until 映射]),下一根 1m 开盘买入;
- 卖点:首见即动(第69轮 N_sell=0 最优);30m 门控 down 在 1m 轴强平。
- 对照组:同执行轴「盲等 K 根 1m」(K=5/15/30/60),隔离「结构确认 vs 纯时间」增量。
执行轴=1m(下一根 1m 开盘成交,费用 0.0001,全仓比例,T+0)。
运行: PYTHONPATH=src python scripts/wf_nest_confirm_tsla.py
"""
import bisect
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import wf_dir_series, recommended_buy_ratio, US_STOCK

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wf_backtest_tsla as wbt  # noqa: E402

STAGE1_PKL = "D:/chanlun_pro/reports/wf_confirm_tsla_stage1.pkl"
DIR1M_PKL = "D:/chanlun_pro/chart_cache_us_tsla_1y/v33_us_TSLA_US_1m_recursivebt.pkl"
COMMISSION = US_STOCK.commission
MAX_WAIT_1M = 90          # 结构确认最长等待(1m根);超时=放弃该信号


def _load_1m() -> pd.DataFrame:
    e = pickle.loads(Path(DIR1M_PKL).read_bytes())
    d = e["data"]
    return pd.DataFrame({
        "date": pd.to_datetime(d["t"], unit="s"),
        "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"],
    })


def run_1m_exec(orders, gate1, opens, closes):
    """orders: 时间序 [(1m_bar_idx, 'buy'/'sell', bs_type)];下一根开盘成交;gate down 强平。"""
    n = len(opens)
    cash, sh, entry = 1.0, 0.0, 0.0
    peak, mdd = 1.0, 0.0
    trades, wins = 0, 0
    pend = None
    oi = 0
    orders = sorted(orders)
    for i in range(n):
        if pend is not None:
            act, bs = pend
            px = opens[i]
            if act == "buy" and sh == 0.0 and px > 0:
                ratio = recommended_buy_ratio(bs, 1, big_dir=gate1[i - 1] if i else "neutral",
                                              trend_boost=True)
                budget = cash * ratio * 0.99 if ratio < 1 else cash * 0.99
                sz = budget / (px * (1 + COMMISSION))
                if sz > 0:
                    cash -= sz * px * (1 + COMMISSION)
                    sh, entry = sz, px
            elif act == "sell" and sh > 0.0 and px > 0:
                cash += sh * px * (1 - COMMISSION)
                trades += 1
                wins += px > entry
                sh = 0.0
            pend = None
        gate_down = gate1[i] == "down"
        if sh > 0.0 and gate_down:
            pend = ("sell", "exit")
        while oi < len(orders) and orders[oi][0] == i:
            _, act, bs = orders[oi]
            oi += 1
            if act == "sell" and sh > 0.0:
                pend = ("sell", bs)
            elif act == "buy" and sh == 0.0 and not gate_down and pend is None:
                pend = ("buy", bs)
        eq = cash + sh * closes[i]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    if sh > 0.0:
        cash += sh * closes[-1] * (1 - COMMISSION)
        trades += 1
        wins += closes[-1] > entry
    return {"ret": cash - 1, "max_dd": mdd, "trades": trades,
            "win": wins / trades if trades else 0.0}


def main() -> int:
    st = pickle.loads(Path(STAGE1_PKL).read_bytes())
    df1 = _load_1m()
    if "--smoke" in sys.argv:                      # 冒烟:前 8000 根 1m,仅验证流程
        df1 = df1.iloc[:8000].reset_index(drop=True)
    df5_dates = pd.to_datetime(st["dates"])
    d1 = list(df1["date"])
    opens1, closes1 = df1["open"].to_numpy(), df1["close"].to_numpy()
    n1 = len(d1)
    print(f"TSLA 1m bars={n1} | 5m episodes={len(st['episodes'])} | {st['span']}")

    # 1m 当下笔方向(结构确认源,逐根无 lookahead)
    print("wf_dir_series(1m) ...", flush=True)
    dir1_ev = wf_dir_series(df1, "TSLA.US", "1m")
    dir1 = ["neutral"] * n1
    cur, bi = "neutral", 0
    for i, t in enumerate(d1):
        while bi < len(dir1_ev) and dir1_ev[bi][0] <= t:
            cur = dir1_ev[bi][1]
            bi += 1
        dir1[i] = cur

    # 30m 门控映射到 1m 轴(stage1 的 gate 是 5m 轴;直接由 5m 时刻映射)
    gate1 = ["neutral"] * n1
    g5 = st["gate"]
    for i, t in enumerate(d1):
        j = bisect.bisect_right(df5_dates, t) - 1
        gate1[i] = g5[j] if j >= 0 else "neutral"

    # 5m episode 时刻 → 1m 索引
    def t5_to_i1(bar5: int) -> int:
        t = df5_dates[bar5]
        return bisect.bisect_right(d1, t) - 1      # 该 5m bar 收盘时刻对应的 1m bar

    buys, sells = [], []
    for ep in st["episodes"]:
        if ep["side"] == "sell":
            i1 = t5_to_i1(ep["first_seen"])
            if i1 >= 0:
                sells.append((i1, "sell", ep["bs_type"]))
        else:
            buys.append(ep)

    def orders_struct():
        """结构确认:首见后等第一个 1m 笔方向=up(限 MAX_WAIT;确认时 5m 信号须仍存活)。"""
        out = list(sells)
        for ep in buys:
            i0 = t5_to_i1(ep["first_seen"])
            if i0 < 0:
                continue
            alive_t = df5_dates[min(ep["alive_until"], len(df5_dates) - 1)]
            for i in range(i0 + 1, min(i0 + 1 + MAX_WAIT_1M, n1)):
                if d1[i] > alive_t:
                    break                          # 5m 信号已被重绘掉
                if dir1[i] == "up":
                    out.append((i, "buy", ep["bs_type"]))
                    break
        return out

    def orders_wait(k: int):
        """盲等 K 根 1m(对照):确认时 5m 信号须仍存活(同存活约束,公平)。"""
        out = list(sells)
        for ep in buys:
            i0 = t5_to_i1(ep["first_seen"])
            if i0 < 0 or i0 + k >= n1:
                continue
            alive_t = df5_dates[min(ep["alive_until"], len(df5_dates) - 1)]
            if d1[i0 + k] <= alive_t:
                out.append((i0 + k, "buy", ep["bs_type"]))
        return out

    bh = closes1[-1] / opens1[0] - 1
    print(f"裸持={bh:+.1%}")
    r = run_1m_exec(orders_struct(), gate1, opens1, closes1)
    print(f"结构确认(1m笔翻up,≤{MAX_WAIT_1M}根): 收益={r['ret']:+7.1%} 回撤={r['max_dd']:6.1%} "
          f"笔={r['trades']:3d} 胜率={r['win']:4.0%}")
    for k in (5, 15, 30, 60):
        r = run_1m_exec(orders_wait(k), gate1, opens1, closes1)
        print(f"盲等{k:>3}根1m              : 收益={r['ret']:+7.1%} 回撤={r['max_dd']:6.1%} "
              f"笔={r['trades']:3d} 胜率={r['win']:4.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
