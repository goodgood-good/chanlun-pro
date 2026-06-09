"""scripts/recursive_backtest.py — 缠论多级别买卖点结合策略回测引擎。

数据源: chart_cache pkl(t/o/h/l/c/v 真实 K 线)。信号: cd.get_kuozhan_levels() 多级别买卖点
(L1=5m 非同级别扩展/扩张, L2=30m 同级别分解)。策略: 大小级别结合——大级别(L2/30m)定方向,
小级别(L1/5m)精确进场(缠论「大级别看方向、小级别找买卖点」)。市场规则: A股 T+1/无做空/印花税/
涨跌停; 美股 T+0/可做空。输出: 各标的×策略 收益对比。

回测口径说明: 买卖点取全序列计算(anchor_fx.k.date=确认bar),进场=确认bar的下一bar开盘价
(规避当bar lookahead)。右边缘买卖点会 repaint,但历史段买卖点稳定 → 一阶近似可接受;后续可升级
为逐bar增量重算。运行: PYTHONPATH="src;web/chanlun_chart;." python scripts/recursive_backtest.py
"""
from __future__ import annotations

import glob
import re
import pickle
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from chanlun.core.cl import CL

CL_CFG = {
    "chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
    "chart_show_bi_zs": "1", "chart_show_xd_zs": "1", "chart_show_bi_mmd": "1",
    "chart_show_xd_mmd": "1", "chart_show_bi_bc": "1", "chart_show_xd_bc": "1",
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}

CACHE_DIR = "D:/chanlun_pro/chart_cache"
BUYS = ("1buy", "2buy", "3buy")
SELLS = ("1sell", "2sell", "3sell")


# ---------------------------------------------------------------------------
# 市场规则
# ---------------------------------------------------------------------------
@dataclass
class MarketRules:
    name: str
    commission: float = 0.0003       # 双边佣金率
    stamp_duty: float = 0.0          # 印花税(卖出,A股 0.0005)
    t_plus: int = 0                  # T+N(A股=1 当日买不可卖)
    allow_short: bool = True         # 是否可做空
    lot: int = 1                     # 最小交易单位(A股=100股)
    limit_pct: Optional[float] = None  # 涨跌停幅度(None=无限制,如指数)


A_STOCK = MarketRules("A股主板", commission=0.0003, stamp_duty=0.0005,
                      t_plus=1, allow_short=False, lot=100, limit_pct=0.10)
A_INDEX = MarketRules("A股指数", commission=0.0003, stamp_duty=0.0005,
                      t_plus=1, allow_short=False, lot=100, limit_pct=None)
A_GEM = MarketRules("A股创业板", commission=0.0003, stamp_duty=0.0005,
                    t_plus=1, allow_short=False, lot=100, limit_pct=0.20)
US_STOCK = MarketRules("美股", commission=0.0001, stamp_duty=0.0,
                       t_plus=0, allow_short=True, lot=1, limit_pct=None)


# 标的 → (chart_cache 前缀, CL code, 市场规则)
SYMBOLS: Dict[str, Tuple[str, str, MarketRules]] = {
    "上证指数": ("a_SH_000001", "SH.000001", A_INDEX),
    "纳指ETF": ("a_SH_513100", "SH.513100", A_STOCK),
    "德赛西威": ("a_SZ_002920", "SZ.002920", A_STOCK),
    "嘉益股份": ("a_SZ_301004", "SZ.301004", A_GEM),
    "QQQ": ("us_QQQ_US", "QQQ.US", US_STOCK),
}


def _ver(f: str) -> int:
    m = re.search(r"chart_cache[\\/]+v(\d+)_", f)
    return int(m.group(1)) if m else 0


def load_klines(prefix: str, tf: str = "1m") -> Optional[pd.DataFrame]:
    fs = glob.glob(f"{CACHE_DIR}/v*_{prefix}_{tf}_*.pkl")
    if not fs:
        return None
    d = pickle.load(open(max(fs, key=_ver), "rb"))["data"]
    return pd.DataFrame({
        "date": pd.to_datetime(d["t"], unit="s", utc=True),
        "open": np.asarray(d["o"], float), "high": np.asarray(d["h"], float),
        "low": np.asarray(d["l"], float), "close": np.asarray(d["c"], float),
        "volume": np.asarray(d["v"], float),
    })


@dataclass
class Signal:
    date: pd.Timestamp
    level: int
    bs_type: str
    price: float

    @property
    def is_buy(self) -> bool:
        return self.bs_type in BUYS

    @property
    def is_sell(self) -> bool:
        return self.bs_type in SELLS


def collect_signals(cd: CL) -> List[Signal]:
    """升级级买卖点(get_kuozhan_levels, L1+)。"""
    out: List[Signal] = []
    for lv in cd.get_kuozhan_levels():
        for p in lv["bsp"]:
            fx = p.anchor_fx
            if fx is None or fx.k is None:
                continue
            out.append(Signal(fx.k.date, p.level or lv["level"], p.bs_type, fx.val))
    out.sort(key=lambda s: s.date)
    return out


def collect_branch_signals(cd: CL, use_xd: bool = False) -> List[Signal]:
    """原生图全量买卖点(get_branch_bspoints, L0 一二三类 + 升级级)。操作级买卖点取此。"""
    out: List[Signal] = []
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        fx = p.anchor_fx
        if fx is None or fx.k is None:
            continue
        out.append(Signal(fx.k.date, p.level or 0, p.bs_type, fx.val))
    out.sort(key=lambda s: s.date)
    return out


# ---------------------------------------------------------------------------
# 模拟撮合
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    side: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    size: float = 0.0
    ret: float = 0.0
    reason: str = ""


@dataclass
class Result:
    name: str
    strategy: str
    total_return: float
    buy_hold: float
    max_dd: float
    sharpe: float
    win_rate: float
    n_trades: int
    equity: np.ndarray = field(repr=False, default=None)


class Simulator:
    """单标的、单方向(暂只多头)逐bar撮合。进场在 pending bar 的开盘价成交。"""

    def __init__(self, df: pd.DataFrame, rules: MarketRules, init_cash: float = 1_000_000):
        self.df = df.reset_index(drop=True)
        self.rules = rules
        self.init_cash = init_cash
        self.opens = self.df["open"].to_numpy()
        self.closes = self.df["close"].to_numpy()
        self.dates = self.df["date"].to_list()

    def _limit_locked(self, i: int, side: str) -> bool:
        """涨跌停锁死无法成交(简化:开盘价较前收触及限幅)。"""
        lp = self.rules.limit_pct
        if lp is None or i == 0:
            return False
        prev_close = self.closes[i - 1]
        chg = self.opens[i] / prev_close - 1
        if side == "buy" and chg >= lp * 0.995:    # 涨停买不进
            return True
        if side == "sell" and chg <= -lp * 0.995:  # 跌停卖不出
            return True
        return False

    def run(self, decide) -> Result:
        """decide(i, date, holding, entry_idx) → 'buy'|'sell'|None(在 bar i 收盘后决定,bar i+1 开盘执行)。"""
        cash = self.init_cash
        shares = 0.0
        entry_idx = -1
        entry_price = 0.0
        n = len(self.df)
        equity = np.empty(n)
        trades: List[Trade] = []
        pending: Optional[str] = None
        r = self.rules

        for i in range(n):
            # 1) 执行上一bar挂的单(本bar开盘价)
            if pending == "buy" and shares == 0 and not self._limit_locked(i, "buy"):
                px = self.opens[i]
                budget = cash * 0.98
                size = budget / (px * (1 + r.commission))
                if r.lot > 1:
                    size = (int(size) // r.lot) * r.lot
                if size > 0:
                    cost = size * px * (1 + r.commission)
                    cash -= cost
                    shares = size
                    entry_idx = i
                    entry_price = px
            elif pending == "sell" and shares > 0 and not self._limit_locked(i, "sell"):
                if r.t_plus == 0 or self.dates[i].date() > self.dates[entry_idx].date():
                    px = self.opens[i]
                    proceeds = shares * px * (1 - r.commission - r.stamp_duty)
                    cash += proceeds
                    tr = Trade("long", self.dates[entry_idx], entry_price,
                               self.dates[i], px, shares)
                    tr.ret = px / entry_price - 1
                    trades.append(tr)
                    shares = 0.0
                    entry_idx = -1
            pending = None

            # 2) 盯市权益
            equity[i] = cash + shares * self.closes[i]

            # 3) 决策(下一bar执行)
            act = decide(i, self.dates[i], shares > 0, entry_idx)
            if act in ("buy", "sell"):
                pending = act

        # 收尾:强平
        if shares > 0:
            px = self.closes[-1]
            cash += shares * px * (1 - r.commission - r.stamp_duty)
            tr = Trade("long", self.dates[entry_idx], entry_price, self.dates[-1], px, shares)
            tr.ret = px / entry_price - 1
            tr.reason = "收尾强平"
            trades.append(tr)
            equity[-1] = cash

        return self._metrics(equity, trades)

    def _metrics(self, equity: np.ndarray, trades: List[Trade]) -> Result:
        total = equity[-1] / equity[0] - 1
        bh = self.closes[-1] / self.closes[0] - 1
        peak = np.maximum.accumulate(equity)
        max_dd = float(np.max((peak - equity) / peak)) if len(equity) else 0.0
        rets = np.diff(equity) / equity[:-1]
        # 1m bar → 年化: A股 ~240bar/日 × 244日; 美股 ~390×252。粗用 sqrt(年bar数)。
        ann = np.sqrt(244 * 240)
        sharpe = float(np.mean(rets) / (np.std(rets) + 1e-12) * ann) if len(rets) else 0.0
        wins = sum(1 for t in trades if t.ret > 0)
        wr = wins / len(trades) if trades else 0.0
        return Result("", "", total, bh, max_dd, sharpe, wr, len(trades), equity)


# ---------------------------------------------------------------------------
# 策略(大小级别结合)
# ---------------------------------------------------------------------------
class MTFStrategy:
    """多周期大小级别结合(原文同级别分解操作:大级别30m定方向开窗,小级别5m窗口内进场)。

    decide 在 5m 执行bar 上工作。big_dir_at[i] = 截至第 i 根 5m bar 的 30m 大级别方向
    (30m 信号 +30min 延迟生效,规避未完成 30m bar 的 lookahead)。
    """

    def __init__(self, sig_small: List[Signal], sig_big: List[Signal],
                 dates5: List[pd.Timestamp], mode: str):
        self.mode = mode
        idx = {d: k for k, d in enumerate(dates5)}
        # 小级别信号 → 5m bar
        self.small_by_bar: Dict[int, List[Signal]] = {}
        for s in sig_small:
            k = idx.get(s.date)
            if k is not None:
                self.small_by_bar.setdefault(k, []).append(s)
        # 大级别方向逐 5m bar 预计算(30m 信号确认后 +30min 生效)
        n = len(dates5)
        self.big_dir_at = ["neutral"] * n
        big = sorted(sig_big, key=lambda s: s.date)
        bi = 0
        cur = "neutral"
        delay = pd.Timedelta("30min")
        for i in range(n):
            while bi < len(big) and big[bi].date + delay <= dates5[i]:
                cur = "up" if big[bi].is_buy else "down"
                bi += 1
            self.big_dir_at[i] = cur

    def decide(self, i, date, holding, entry_idx) -> Optional[str]:
        small = self.small_by_bar.get(i, [])
        big_dir = self.big_dir_at[i]
        if self.mode == "5m_only":
            if not holding and any(s.is_buy for s in small):
                return "buy"
            if holding and any(s.is_sell for s in small):
                return "sell"
        elif self.mode == "30m_only":
            # 仅大级别:方向翻转即进出
            if not holding and big_dir == "up" and (i == 0 or self.big_dir_at[i - 1] != "up"):
                return "buy"
            if holding and big_dir == "down":
                return "sell"
        elif self.mode == "5m+30m":   # 共振:大级别up窗口内,小级别买点进场;大级别down或小级别卖点出
            if not holding and big_dir == "up" and any(s.is_buy for s in small):
                return "buy"
            if holding and (big_dir == "down" or any(s.is_sell for s in small)):
                return "sell"
        return None


def backtest_symbol(name: str, prefix: str, code: str, rules: MarketRules,
                    small_use_xd: bool = False) -> List[Result]:
    df5 = load_klines(prefix, "5m")
    df30 = load_klines(prefix, "30m")
    if df5 is None or len(df5) < 100:
        return []
    cd5 = CL(code, "5m", dict(CL_CFG))
    cd5.process_klines(df5)
    sig_small = collect_branch_signals(cd5, use_xd=small_use_xd)
    sig_big: List[Signal] = []
    if df30 is not None and len(df30) >= 50:
        cd30 = CL(code, "30m", dict(CL_CFG))
        cd30.process_klines(df30)
        sig_big = collect_branch_signals(cd30, use_xd=False)
    sim = Simulator(df5, rules)
    results = []
    for mode in ("5m_only", "30m_only", "5m+30m"):
        strat = MTFStrategy(sig_small, sig_big, sim.dates, mode)
        res = sim.run(strat.decide)
        res.name, res.strategy = name, mode
        results.append(res)
    print(f"\n=== {name}({code}) {rules.name} | 5mbars={len(df5)} "
          f"小级别信号={len(sig_small)} 大级别(30m)信号={len(sig_big)} ===")
    return results


def main():
    rows = []
    for name, (prefix, code, rules) in SYMBOLS.items():
        for res in backtest_symbol(name, prefix, code, rules):
            print(f"  {res.strategy:10s} 收益={res.total_return:+7.1%} "
                  f"基准={res.buy_hold:+7.1%} 超额={res.total_return - res.buy_hold:+7.1%} "
                  f"回撤={res.max_dd:5.1%} 夏普={res.sharpe:5.2f} "
                  f"胜率={res.win_rate:4.0%} 交易={res.n_trades}")
            rows.append(res)
    # 汇总
    print("\n" + "=" * 70)
    print("策略汇总(各模式平均):")
    for mode in ("5m_only", "30m_only", "5m+30m"):
        rs = [r for r in rows if r.strategy == mode]
        if rs:
            avg_ex = np.mean([r.total_return - r.buy_hold for r in rs])
            avg_ret = np.mean([r.total_return for r in rs])
            avg_dd = np.mean([r.max_dd for r in rs])
            avg_sh = np.mean([r.sharpe for r in rs])
            print(f"  {mode:9s} 平均收益={avg_ret:+7.1%} 平均超额={avg_ex:+7.1%} "
                  f"平均回撤={avg_dd:5.1%} 平均夏普={avg_sh:5.2f}")


if __name__ == "__main__":
    main()
