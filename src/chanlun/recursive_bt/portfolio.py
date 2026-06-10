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

from chanlun.recursive_bt.engine import (
    load_klines, collect_branch_signals, CL_CFG, SYMBOLS, MTFStrategy,
)
from chanlun.core.cl import CL


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


BT_DATA = os.environ.get("BT_DATA_DIR", "D:/chanlun_pro/bt_data")


def load_cached(code: str) -> Optional[dict]:
    """从 qmt_fetch 预计算缓存载入一只标的(含按板块的涨跌停 rules)。"""
    import pickle
    from chanlun.recursive_bt.engine import MarketRules
    p = f"{BT_DATA}/{code}.pkl"
    if not os.path.exists(p):
        return None
    d = pickle.load(open(p, "rb"))
    d["name"] = code
    d["rules"] = MarketRules("A股", commission=0.0003, stamp_duty=0.0005,
                             t_plus=1, allow_short=False, lot=100,
                             limit_pct=d.get("limit_pct", 0.10))
    d["d2i"] = {dt: i for i, dt in enumerate(d["dates"])}
    # 大级别走势方向(walk-forward 当下笔方向,fetch *_trend 补):周线无买卖点时门控的替代。
    # wf 口径事件=「该bar收盘时可见」→ 次日(下一bar)生效即可;TREND_DELAY 默认 1 天。
    ev = d.get("big_trend_events")
    if ev:
        delay = pd.Timedelta(os.environ.get("TREND_DELAY", "1D"))
        dirs = ["neutral"] * len(d["dates"])
        bi = 0
        cur = "neutral"
        for i, t in enumerate(d["dates"]):
            while bi < len(ev) and ev[bi][0] + delay <= t:
                cur = ev[bi][1]
                bi += 1
            dirs[i] = cur
        d["trend_dir_at"] = dirs
    return d


def _daily_closes(s: dict):
    """5m/日线 bar 序列 → [(日期, 当日收盘, 当日最后bar索引)](按 bar date 的自然日分组)。"""
    out = []
    cur_day = None
    for i, t in enumerate(s["dates"]):
        d = t.date()
        if d != cur_day:
            out.append([d, s["close"][i], i])
            cur_day = d
        else:
            out[-1][1] = s["close"][i]
            out[-1][2] = i
    return out


def attach_pool_filters(syms: dict, market: dict, ma_win: int = 70, rs_win: int = 20):
    """海选(第8课)+比价资金流向(第9课)逐bar过滤数组,全部 point-in-time(用截至**前一完整
    交易日**收盘的序列,当日盘中不用未完成日线,无lookahead)。

    - s['ma_ok'][i]: 前日收盘 > 前日 ma_win 日均线(第8课「250天线突破…资金量不大可改70天线、
      35天线」→ 取70,数据仅1年,250日不可得)。均线未满窗口 → False(不在「能搞的」分类)。
    - s['rs_ok'][i]: 个股 rs_win 日收益 > 大盘同窗收益(第9课「比价关系的变动…和市场资金的
      流向相关」=资金流入)。窗口不足 → False。
    """
    mkt_daily = _daily_closes(market)
    mkt_idx = {d: k for k, (d, _c, _i) in enumerate(mkt_daily)}
    mkt_close = [c for _d, c, _i in mkt_daily]
    for s in syms.values():
        dly = _daily_closes(s)
        closes = [c for _d, c, _i in dly]
        nd = len(dly)
        # 每日截至收盘的 ma / rs(对齐大盘用日期 map,缺日用大盘最近≤该日的值)
        csum = np.cumsum(np.asarray(closes, dtype=float))
        day_ma_ok = [False] * nd
        day_rs_ok = [False] * nd
        mk = -1
        for k in range(nd):
            d = dly[k][0]
            if d in mkt_idx:
                mk = mkt_idx[d]
            if k + 1 >= ma_win:
                ma = (csum[k] - (csum[k - ma_win] if k >= ma_win else 0.0)) / ma_win
                day_ma_ok[k] = closes[k] > ma
            if k >= rs_win and mk >= rs_win:
                sret = closes[k] / closes[k - rs_win] - 1
                mret = mkt_close[mk] / mkt_close[mk - rs_win] - 1
                day_rs_ok[k] = sret > mret
        n = len(s["dates"])
        ma_ok = np.zeros(n, bool)
        rs_ok = np.zeros(n, bool)
        k = -1   # 已收盘的最后完整日(当日盘中只用截至前一日)
        di = 0
        for i, t in enumerate(s["dates"]):
            d = t.date()
            while di < nd and dly[di][0] < d:
                k = di
                di += 1
            if k >= 0:
                ma_ok[i] = day_ma_ok[k]
                rs_ok[i] = day_rs_ok[k]
        s["ma_ok"] = ma_ok
        s["rs_ok"] = rs_ok


def attach_daily_bsp_window(syms: dict, win_days: int = 10, bs_class: int = 3):
    """三级共振选股锚(原文line13507缠亲答:日线3买→30m回抽→5m背驰,「必须三个级别共同来」):
    s['d3_ok'][i] = 第 i 根 bar 是否处于「日线 bs_class 类买点窗口」内——日线买点确认bar
    收盘**次日**起 win_days 个自然日(无lookahead)。需 pkl 含 daily_bsp(fetch daily_bsp 补)。"""
    key = f"{bs_class}buy" if bs_class else None     # None=日线任意类买点(宽口径)
    for s in syms.values():
        ev = [d for d, bt in (s.get("daily_bsp") or [])
              if (bt == key if key else bt.endswith("buy"))]
        n = len(s["dates"])
        ok = np.zeros(n, bool)
        if ev:
            ei = 0
            active_until = None
            for i, t in enumerate(s["dates"]):
                while ei < len(ev) and ev[ei] + pd.Timedelta("1D") <= t:
                    active_until = ev[ei] + pd.Timedelta(days=1 + win_days)
                    ei += 1
                ok[i] = active_until is not None and t <= active_until
        s["d3_ok"] = ok


def portfolio_backtest(universe: Optional[List[str]] = None, max_pos: int = 2,
                       market_filter: Optional[str] = None,
                       init_cash: float = 1_000_000,
                       syms: Optional[dict] = None, filt: Optional[dict] = None,
                       label: Optional[str] = None,
                       buy_classes: Optional[set] = None,
                       require: tuple = ("tech",),
                       big_gate: str = "bsp",
                       buy_priority: str = "1first",
                       pool_schedule: Optional[list] = None):
    """组合回测。syms 已构建则直接用(QMT缓存路径);否则按 universe 名走 chart_cache。
    market_filter=大盘标的名(其30m方向==down时禁止开新仓)。
    buy_classes=入场只认的买点类别集合(如{1}=只一类买点选股;None=全部1/2/3类)。
    require=缠论三独立系统门控:('tech',)=只技术面;加'fund'=并需①基本面通过(s['fund_ok'][bar]质量+成长);
    加'value'=并需②比价低估(s['value_ok'][bar]=ROE年化/PB高于全市场中位=优质却便宜)。三者齐=三系统结合(概率原则)。
    big_gate='bsp'=大级别方向用买卖点事件(big_dir_at,现状);'trend'=用走势方向(trend_dir_at,
    周线笔方向——周线图无买卖点时 bsp 门控恒 neutral 失效,走势方向是结构化替代)。
    require 另支持(须先 attach_pool_filters):'ma'=海选门槛(第8课,收盘>70日线=「能搞的」分类);
    'rs'=比价资金流向(第9课,个股20日收益>大盘=资金流入)。
    buy_priority:'1first'=1买>2买>3买(反转抄底口径);'3first'=3买>2买>1买(line23172
    「牛市里第三类买点的爆发力是最强的」,突破延续口径)。
    pool_schedule=原文三层架构(line38515-38544)的①基本面**结构层**:[(生效时刻,{code:权重})]
    季度池调度(industry.build_pool_schedule:行业龙头70%+成长30%,季度重算=②比价换股语义)。
    传入后:只买池内标的、买入预算=组合市值×该标权重(非等权slot)、max_pos 失效;
    技术面(③执行层)仍管时机——池内标的出现买点才进场,卖点/大级别down退出;被剔池持仓
    不强平(原文「技术面把握好,在较大级别卖点卖掉被超越者」),但不再开新仓。"""
    _dir_key = "trend_dir_at" if big_gate == "trend" else "big_dir_at"

    def _bdir(s, j):
        return s.get(_dir_key, s["big_dir_at"])[j]
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
        # 剔除中途上市/复牌晚的标的:起点晚于全池中位起点+30天会把交集窗口起点拉后,
        # 砍掉窗口前段行情(熊市验证曾被截掉2022年1~4月主跌段) → 按起点对齐而非缩窗口
        med_start = statistics.median(s["dates"][0].value for s in syms.values())
        cutoff = pd.Timestamp(med_start, tz="Asia/Shanghai") + pd.Timedelta("30D")
        syms = {n: s for n, s in syms.items() if s["dates"][0] <= cutoff}
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

    pool_idx = -1
    reentry: Dict[str, str] = {}      # 池模式短差状态:卖点减仓后 'wait_buy'=等买点回补
    for m, t in enumerate(master):
        # 1) 执行上一bar挂单(本bar开盘价)
        carry = []
        for o in pending:
            name, act = o[0], o[1]
            w = o[2] if len(o) > 2 else None        # 池模式:目标权重
            s = syms[name]
            j = s["d2i"][t]
            px = s["open"][j]
            r = s["rules"]
            if act == "buy" and name not in positions and cash > 0:
                target = mk(t) * w if w else mk(t) / max_pos
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
        pend_sell = {o[0] for o in pending if o[1] == "sell"}
        for name in list(positions):
            if name in pend_sell:
                continue
            s = syms[name]
            j = s["d2i"][t]
            if _bdir(s, j) == "down" or _sells_at(s, j):
                is_down = _bdir(s, j) == "down"
                positions[name]["reason"] = "大级别转空" if is_down else "小级别卖点"
                pending.append((name, "sell"))
                if pool_schedule is not None and not is_down:
                    reentry[name] = "wait_buy"   # 卖点减仓→等买点回补(短差);down→非down即回补

        # 3) 选股开仓
        block = filt and _bdir(filt, filt["d2i"][t]) == "down"
        pend_buy = {o[0] for o in pending if o[1] == "buy"}
        if pool_schedule is not None:
            # 三层架构:①结构层季度池=**持有为本**(原文38536:70/30配置一直持着,技术面只管
            # 中枢震荡短差降成本)——非「买点才进场」(那是全池猎手口径,小池会饿死)。
            # 大级别 not_down 即按权重持有;③技术面短差循环:小级别卖点减仓→**买点回补**,
            # 大级别 down 退出→非 down 回补。
            while (pool_idx + 1 < len(pool_schedule)
                   and pool_schedule[pool_idx + 1][0] <= t):
                pool_idx += 1
            cur_pool = pool_schedule[pool_idx][1] if pool_idx >= 0 else {}
            if not block:
                for name, w in cur_pool.items():
                    s = syms.get(name)
                    if s is None or name in positions or name in pend_buy:
                        continue
                    j = s["d2i"].get(t)
                    if j is None or _bdir(s, j) == "down":
                        continue
                    if reentry.get(name) == "wait_buy":      # 卖点减仓后,等买点回补(短差)
                        buys = _buys_at(s, j)
                        if buy_classes is not None:
                            buys = [x for x in buys if int(x.bs_type[0]) in buy_classes]
                        if not buys:
                            continue
                    pending.append((name, "buy", w))
                    reentry.pop(name, None)
            equity[m] = mk(t)
            continue
        free = max_pos - len(positions) - len(pend_buy)
        if free > 0 and not block:
            cands = []
            for name, s in syms.items():
                if name in positions or name in pend_buy:
                    continue
                j = s["d2i"][t]
                buys = _buys_at(s, j)
                if buy_classes is not None:                      # 只认指定类别买点(选股系统)
                    buys = [x for x in buys if int(x.bs_type[0]) in buy_classes]
                if not (_bdir(s, j) != "down" and buys):
                    continue
                if "mid_dir_at" in s and s["mid_dir_at"][j] == "down":  # 三级联立:中级别(5m)也不空
                    continue
                if "fund" in require and not s["fund_ok"][j]:    # ①基本面独立系统门控
                    continue
                if "value" in require and not s["value_ok"][j]:  # ②比价(低估:ROE年化/PB高)门控
                    continue
                if "ma" in require and not s["ma_ok"][j]:        # 海选门槛(第8课,70日线上=能搞的)
                    continue
                if "rs" in require and not s["rs_ok"][j]:        # ②比价资金流向(第9课,强于大盘)
                    continue
                if "d3" in require and not s["d3_ok"][j]:        # 三级共振:日线3买窗口(line13507)
                    continue
                cls = min(int(x.bs_type[0]) for x in buys)
                pr = -cls if buy_priority == "3first" else cls   # 3买优先(line23172牛市)或1买优先
                # 三级共振**排序融合**(非硬门控):日线3买窗口内的候选排前(line13507 单笔质量
                # 实证胜率57%→71%),slot 充足时不砍机会、竞争时优先共振标的
                d3 = s.get("d3_ok")
                cands.append((0 if (d3 is not None and d3[j]) else 1, pr, name))
            cands.sort()
            for c in cands[:free]:
                pending.append((c[-1], "buy"))

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
    # 年化系数按 bar 频率自适应(总 bar 数 / 跨度年数),支持 1m/5m/日/周线
    years = max((master[-1] - master[0]).days / 365.0, 1e-9)
    ann = np.sqrt(max(len(master) / years, 1.0))
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-12) * ann) if len(rets) else 0.0
    wins = sum(1 for t in trades if t.ret > 0)
    wr = wins / len(trades) if trades else 0.0
    # 基准:等权买入持有(逐bar曲线,可算基准回撤对比风险)
    nrm = np.zeros(len(master))
    cnt = 0
    for s in syms.values():
        try:
            idx = np.array([s["d2i"][t] for t in master])
        except KeyError:
            continue
        nrm += s["close"][idx] / s["open"][s["d2i"][master[0]]]
        cnt += 1
    bench = nrm / max(cnt, 1)
    bh = float(bench[-1] - 1)
    bpeak = np.maximum.accumulate(bench)
    bench_dd = float(np.max((bpeak - bench) / bpeak)) if len(bench) else 0.0
    tag = f"+大盘过滤({flabel})" if flabel else ""
    print(f"\n=== 组合回测{tag} | 池={label} 期={master[0].date()}~{master[-1].date()} ===")
    print(f"  组合收益={total:+.1%}  等权基准={bh:+.1%}  超额={total - bh:+.1%}  "
          f"回撤={max_dd:.1%}(基准{bench_dd:.1%})  夏普={sharpe:.2f}  胜率={wr:.0%}  交易={len(trades)}")
    return {"total": total, "bh": bh, "max_dd": max_dd, "sharpe": sharpe,
            "wr": wr, "n": len(trades), "trades": trades,
            "equity": equity, "master": master}


def generate_portfolio_report(syms, filt, out_png="D:/chanlun_pro/reports/portfolio.png"):
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
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


def main_mtf3():
    """30m+5m+1m 三级联立 vs 1m+30m 两级对照(去5m中门控)。BT_DATA_DIR 指 bt_data_mtf3。"""
    syms = _load_bt_universe()
    filt = load_cached("SH.000001")
    label = f"{len(syms)}只"
    print("#" * 64)
    print(f"# 30m+5m+1m 三级联立 vs 1m+30m 两级 | universe={len(syms)}只(1m bar)")
    print("#" * 64)
    for mp in (5, 10):
        portfolio_backtest(syms=syms, filt=None, max_pos=mp, label=label)
        print(f"    ↑ 三级联立(1m买点+5m不空+30m不空) max_pos={mp}")
    if filt:
        portfolio_backtest(syms=syms, filt=filt, max_pos=10, label=label)
        print("    ↑ 三级联立 + 大盘过滤 max_pos=10")
    for s in syms.values():           # 对照:去掉中级别门控 → 退化为两级
        s.pop("mid_dir_at", None)
    for mp in (5, 10):
        portfolio_backtest(syms=syms, filt=None, max_pos=mp, label=label)
        print(f"    ↑ 两级对照(1m买点+30m不空,无5m中门控) max_pos={mp}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "chart_cache":
        main()
    elif arg == "systems":
        main_systems()
    elif arg == "mtf3":
        main_mtf3()
    else:
        main_qmt()
