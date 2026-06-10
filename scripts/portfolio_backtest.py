"""scripts/portfolio_backtest.py — 缠论买点选股 + 组合回测(模拟真实实盘)。

完全基于缠论原文的选股交易:扫描股票池,在每根 5m bar 找出当下处于**买点**(原文一/二/三类
买点,操作级别确认)的标的 → 大小级别结合(30m方向!=down开窗,5m买点进场,我已验证的最优口径)→
组合并发持仓(max_pos 个仓位,等权)→ 卖点/大级别反转退出。含 A股 T+1/印花税/涨跌停。
大盘(上证)30m方向可作择时过滤(原文「大盘不好别乱买」的结构化)。

选股优先级(slot 有限时):一类买点(趋势背驰底,最强)>二类>三类(原文18/20/24课)。
基准:股票池等权买入持有。复用 recursive_backtest 的信号口径(已验 0% repaint)。
运行: PYTHONPATH="src;web/chanlun_chart;." python scripts/portfolio_backtest.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from recursive_backtest import (  # noqa: E402
    load_klines, collect_branch_signals, CL_CFG, SYMBOLS, MTFStrategy,
)
from chanlun.core.cl import CL  # noqa: E402


@dataclass
class PTrade:
    code: str
    entry_date: pd.Timestamp
    entry_px: float
    exit_date: pd.Timestamp
    exit_px: float
    ret: float
    bs_type: str
    reason: str = ""


def prep(name: str) -> dict:
    """跑 CL,得每根 5m bar 的买/卖信号 + 30m 大级别方向(复用已验证口径)。"""
    prefix, code, rules = SYMBOLS[name]
    df5 = load_klines(prefix, "5m")
    df30 = load_klines(prefix, "30m")
    cd5 = CL(code, "5m", dict(CL_CFG))
    cd5.process_klines(df5)
    small = collect_branch_signals(cd5, use_xd=False)
    big: List = []
    if df30 is not None and len(df30) >= 50:
        cd30 = CL(code, "30m", dict(CL_CFG))
        cd30.process_klines(df30)
        big = collect_branch_signals(cd30, use_xd=False)
    dates = list(df5["date"])
    strat = MTFStrategy(small, big, dates, "5m+30m", gate="not_down")
    return {
        "name": name, "code": code, "rules": rules, "dates": dates,
        "open": df5["open"].to_numpy(), "close": df5["close"].to_numpy(),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": strat.small_by_bar, "big_dir_at": strat.big_dir_at,
    }


def _buys_at(s: dict, j: int):
    return [x for x in s["small_by_bar"].get(j, []) if x.is_buy]


def _sells_at(s: dict, j: int):
    return [x for x in s["small_by_bar"].get(j, []) if x.is_sell]


BT_DATA = "D:/chanlun_pro/bt_data"


def load_cached(code: str) -> Optional[dict]:
    """从 qmt_fetch 预计算缓存载入一只标的(含按板块的涨跌停 rules)。"""
    import pickle
    from recursive_backtest import MarketRules
    p = f"{BT_DATA}/{code}.pkl"
    if not os.path.exists(p):
        return None
    d = pickle.load(open(p, "rb"))
    d["name"] = code
    d["rules"] = MarketRules("A股", commission=0.0003, stamp_duty=0.0005,
                             t_plus=1, allow_short=False, lot=100,
                             limit_pct=d.get("limit_pct", 0.10))
    d["d2i"] = {dt: i for i, dt in enumerate(d["dates"])}
    return d


def portfolio_backtest(universe: Optional[List[str]] = None, max_pos: int = 2,
                       market_filter: Optional[str] = None,
                       init_cash: float = 1_000_000,
                       syms: Optional[dict] = None, filt: Optional[dict] = None,
                       label: Optional[str] = None,
                       buy_classes: Optional[set] = None,
                       require: tuple = ("tech",)):
    """组合回测。syms 已构建则直接用(QMT缓存路径);否则按 universe 名走 chart_cache。
    market_filter=大盘标的名(其30m方向==down时禁止开新仓)。
    buy_classes=入场只认的买点类别集合(如{1}=只一类买点选股;None=全部1/2/3类)。
    require=缠论三独立系统门控:('tech',)=只技术面;加'fund'=并需基本面通过(s['fund_ok'][bar]);
    加'value'=并需比价通过(s['rs'][bar]>0 相对强度=资金流入)。三者齐=三系统结合(概率原则)。"""
    if syms is None:
        syms = {n: prep(n) for n in universe}
        filt = prep(market_filter) if market_filter else None
        label = str(universe)
    label = label or f"{len(syms)}只"
    # 过滤重度停牌(bar数 < 0.9×中位数),防个别长停标的把交集主时钟拖垮
    if len(syms) > 5:
        import statistics
        med = statistics.median(len(s["dates"]) for s in syms.values())
        syms = {n: s for n, s in syms.items() if len(s["dates"]) >= 0.9 * med}
    # 主时钟 = 股票池 5m 时间戳交集(A股同时段→覆盖绝大多数bar)
    master = sorted(set.intersection(*[set(s["dates"]) for s in syms.values()]))
    if filt:
        master = [t for t in master if t in filt["d2i"]]

    cash = init_cash
    positions: Dict[str, dict] = {}
    pending: List[tuple] = []
    equity = np.empty(len(master))
    trades: List[PTrade] = []

    def mk(t):  # 市值
        return cash + sum(syms[n]["close"][syms[n]["d2i"][t]] * p["shares"]
                          for n, p in positions.items())

    for m, t in enumerate(master):
        # 1) 执行上一bar挂单(本bar开盘价)
        carry = []
        for name, act in pending:
            s = syms[name]
            j = s["d2i"][t]
            px = s["open"][j]
            r = s["rules"]
            if act == "buy" and name not in positions and cash > 0:
                target = mk(t) / max_pos
                budget = min(target, cash) * 0.99
                size = budget / (px * (1 + r.commission))
                if r.lot > 1:
                    size = (int(size) // r.lot) * r.lot
                if size > 0:
                    cash -= size * px * (1 + r.commission)
                    positions[name] = {"shares": size, "entry_date": t,
                                       "entry_px": px, "bs": act}
            elif act == "sell" and name in positions:
                p = positions[name]
                if r.t_plus == 0 or t.date() > p["entry_date"].date():
                    cash += p["shares"] * px * (1 - r.commission - r.stamp_duty)
                    trades.append(PTrade(s["code"], p["entry_date"], p["entry_px"],
                                         t, px, px / p["entry_px"] - 1,
                                         p.get("bs_type", ""), p.get("reason", "")))
                    del positions[name]
                else:
                    carry.append((name, act))   # T+1 未满足,顺延
        pending = carry

        # 2) 退出信号(持仓中:大级别down 或 小级别卖点)
        for name in list(positions):
            if (name, "sell") in pending:
                continue
            s = syms[name]
            j = s["d2i"][t]
            if s["big_dir_at"][j] == "down" or _sells_at(s, j):
                positions[name]["reason"] = ("大级别转空" if s["big_dir_at"][j] == "down"
                                             else "小级别卖点")
                pending.append((name, "sell"))

        # 3) 选股开仓(大盘过滤 + 空仓位时,买点候选按 1>2>3 类排序填仓)
        block = filt and filt["big_dir_at"][filt["d2i"][t]] == "down"
        free = max_pos - len(positions) - sum(1 for x in pending if x[1] == "buy")
        if free > 0 and not block:
            cands = []
            for name, s in syms.items():
                if name in positions or (name, "buy") in pending:
                    continue
                j = s["d2i"][t]
                buys = _buys_at(s, j)
                if buy_classes is not None:                      # 只认指定类别买点(选股系统)
                    buys = [x for x in buys if int(x.bs_type[0]) in buy_classes]
                if not (s["big_dir_at"][j] != "down" and buys):
                    continue
                if "fund" in require and not s["fund_ok"][j]:    # ①基本面独立系统门控
                    continue
                if "value" in require and not (s["rs"][j] > 0):  # ②比价(相对强度=资金流入)门控
                    continue
                pr = min(int(x.bs_type[0]) for x in buys)        # 1买优先
                cands.append((pr, name))
            cands.sort()
            for _pr, name in cands[:free]:
                pending.append((name, "buy"))

        equity[m] = mk(t)

    # 收尾强平
    t = master[-1]
    for name in list(positions):
        s = syms[name]
        p = positions[name]
        px = s["close"][s["d2i"][t]]
        r = s["rules"]
        cash += p["shares"] * px * (1 - r.commission - r.stamp_duty)
        trades.append(PTrade(s["code"], p["entry_date"], p["entry_px"], t, px,
                             px / p["entry_px"] - 1, "", "收尾"))
    if positions:
        equity[-1] = cash
    flabel = market_filter if market_filter else ("大盘" if filt else None)
    return _report(label, master, equity, trades, syms, flabel)


def _report(label, master, equity, trades, syms, flabel):
    total = equity[-1] / equity[0] - 1
    peak = np.maximum.accumulate(equity)
    max_dd = float(np.max((peak - equity) / peak))
    rets = np.diff(equity) / equity[:-1]
    ann = np.sqrt(244 * 48)   # 5m: ~48bar/日 × 244日
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-12) * ann) if len(rets) else 0.0
    wins = sum(1 for t in trades if t.ret > 0)
    wr = wins / len(trades) if trades else 0.0
    # 基准:等权买入持有
    bh = 0.0
    for s in syms.values():
        i0, i1 = s["d2i"][master[0]], s["d2i"][master[-1]]
        bh += (s["close"][i1] / s["open"][i0] - 1) / len(syms)
    tag = f"+大盘过滤({flabel})" if flabel else ""
    print(f"\n=== 组合回测{tag} | 池={label} 期={master[0].date()}~{master[-1].date()} ===")
    print(f"  组合收益={total:+.1%}  等权基准={bh:+.1%}  超额={total - bh:+.1%}  "
          f"回撤={max_dd:.1%}  夏普={sharpe:.2f}  胜率={wr:.0%}  交易={len(trades)}")
    return {"total": total, "bh": bh, "max_dd": max_dd, "sharpe": sharpe,
            "wr": wr, "n": len(trades), "trades": trades,
            "equity": equity, "master": master}


def generate_portfolio_report(syms, filt, out_png="scripts/portfolio_report.png"):
    """组合权益曲线 vs 等权基准(沪深300选股),多 max_pos 对比。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    runs = {}
    for mp in (5, 10, 20):
        runs[mp] = portfolio_backtest(syms=syms, filt=None, max_pos=mp, label=f"{len(syms)}只")
    master = runs[10]["master"]
    # 等权基准逐bar曲线(算一次);跳过缺 master 日期的标的(停牌→被主时钟过滤)
    nrm = np.zeros(len(master))
    cnt = 0
    for s in syms.values():
        if master[0] not in s["d2i"] or master[-1] not in s["d2i"]:
            continue
        try:
            idx = np.array([s["d2i"][t] for t in master])
        except KeyError:
            continue
        nrm += s["close"][idx] / s["open"][s["d2i"][master[0]]]
        cnt += 1
    bench = nrm / max(cnt, 1)
    x = pd.to_datetime(master)
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = {5: "orange", 10: "crimson", 20: "purple"}
    for mp, r in runs.items():
        eq = r["equity"] / r["equity"][0]
        ax.plot(x, eq, label=f"select max_pos={mp}  {r['total']:+.0%} (DD {r['max_dd']:.0%}, Sharpe {r['sharpe']:.1f})",
                color=colors[mp], lw=1.4)
    ax.plot(x, bench, label=f"HS300 equal-weight buy&hold  {bench[-1] - 1:+.0%}",
            color="gray", lw=1.2, ls="--")
    ax.set_title(f"Chanlun buy-point stock selection on HS300 ({len(syms)} stocks, front-adjusted, "
                 f"{x[0].date()}~{x[-1].date()})", fontsize=11)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    print(f"\n组合报告已保存: {out_png}")
    return out_png


def main():
    # 旧 chart_cache 小池(3只)——保留对照
    universe = ["纳指ETF", "德赛西威", "嘉益股份"]
    print("# 缠论买点选股 + 组合回测(chart_cache 3只对照)")
    for mp in (1, 2, 3):
        portfolio_backtest(universe, max_pos=mp)
        print(f"    ↑ max_pos={mp}")
    portfolio_backtest(universe, max_pos=2, market_filter="上证指数")
    print("    ↑ max_pos=2 + 大盘过滤")


def main_qmt():
    """QMT 全市场缓存(bt_data)选股组合回测。"""
    import glob
    INDEX = "SH.000001"                # 上证指数:只作大盘过滤器,不进可交易池
    syms = {}
    for f in glob.glob(f"{BT_DATA}/*.pkl"):
        code = os.path.basename(f)[:-4]
        if code == INDEX:
            continue
        d = load_cached(code)
        if d and len(d["dates"]) > 500:
            syms[code] = d
    label = f"{len(syms)}只"
    filt = load_cached(INDEX)          # 大盘择时过滤器(若已缓存)
    print("#" * 64)
    print(f"# 缠论全市场买点选股 + 组合回测(QMT前复权) universe={len(syms)}只")
    print("#" * 64)
    for mp in (3, 5, 10, 20):
        portfolio_backtest(syms=syms, filt=None, max_pos=mp, label=label)
        print(f"    ↑ max_pos={mp}(同时最多持{mp}只)")
    if filt:
        portfolio_backtest(syms=syms, filt=filt, max_pos=10, label=label)
        print("    ↑ max_pos=10 + 大盘(上证)择时过滤")
    generate_portfolio_report(syms, filt)


def _load_bt_universe(index="SH.000001"):
    import glob
    syms = {}
    for f in glob.glob(f"{BT_DATA}/*.pkl"):
        code = os.path.basename(f)[:-4]
        if code == index:
            continue
        d = load_cached(code)
        if d and len(d["dates"]) > 500:
            syms[code] = d
    return syms


def main_systems():
    """缠论三类买点选股系统(一/二/三类)各自 + 三类结合,对比。"""
    syms = _load_bt_universe()
    filt = load_cached("SH.000001")
    label = f"{len(syms)}只"
    print("#" * 64)
    print(f"# 缠论三类买点选股系统 + 结合 | universe={len(syms)}只(沪深300前复权)")
    print("#" * 64)
    systems = [
        ("①一类买点系统(趋势背驰底·抄底反转)", {1}),
        ("②二类买点系统(1买后回调不破·确认)", {2}),
        ("③三类买点系统(突破中枢回试不破·延续)", {3}),
        ("①+②+③ 三类结合(1买优先)", {1, 2, 3}),
    ]
    res = {}
    for name, bc in systems:
        r = portfolio_backtest(syms=syms, filt=None, max_pos=10, label=label, buy_classes=bc)
        res[name] = r
        print(f"    ↑ {name}")
    # 结合 + 大盘择时过滤
    if filt:
        portfolio_backtest(syms=syms, filt=filt, max_pos=10, label=label, buy_classes={1, 2, 3})
        print("    ↑ ①+②+③ 结合 + 大盘(上证)择时过滤")
    return res


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "chart_cache":
        main()
    elif arg == "systems":
        main_systems()
    else:
        main_qmt()
