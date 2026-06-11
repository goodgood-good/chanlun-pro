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
import os
import re
import sys
import pickle
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from chanlun.core.cl import CL

# 向后兼容:旧 bt_data 缓存里的 Signal 以模块名 'recursive_backtest' 序列化(本模块原名)。
# 注册别名,让旧 pkl 仍可反序列化;新缓存以 chanlun.recursive_bt.engine 路径序列化。
sys.modules.setdefault("recursive_backtest", sys.modules[__name__])

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
A_BJ = MarketRules("A股北交所", commission=0.0003, stamp_duty=0.0005,
                   t_plus=1, allow_short=False, lot=100, limit_pct=0.30)
US_STOCK = MarketRules("美股", commission=0.0001, stamp_duty=0.0,
                       t_plus=0, allow_short=True, lot=1, limit_pct=None)


def buy_class(bs_type: str) -> int:
    try:
        return int(str(bs_type)[0])
    except Exception:
        return 0


def recommended_buy_ratio(
    bs_type: str,
    max_pos: int,
    big_dir: str = "neutral",
    daily_resonance: bool = False,
    regime_mode: str = "off",
    mid_dir: str = "",
    nest_mode: str = "off",
    nest_operable: Optional[bool] = None,
    nest_depth: int = 0,
    trend_boost: bool = False,
) -> float:
    """Portfolio-level target weight for a Chanlun buy signal."""
    base = 1.0 / max(int(max_pos or 1), 1)
    cls = buy_class(bs_type)
    multiplier = {3: 1.0, 2: 0.75, 1: 0.5}.get(cls, 0.5)
    if big_dir == "up":
        multiplier += 0.15
    if daily_resonance:
        multiplier += 0.15
    ratio = min(base * min(multiplier, 1.0), 1.0)
    if regime_mode == "adaptive":
        if big_dir == "down":
            ratio = 0.0
        elif big_dir == "neutral":
            ratio *= 0.75 if cls == 3 else 0.9
        if mid_dir == "down":
            ratio *= 0.5
    if trend_boost and cls == 3 and big_dir == "up":
        ratio = min(ratio * 1.25, base * 1.25, 1.0)
    if nest_mode == "soft" and cls in (1, 2):
        if nest_operable is True:
            ratio *= 1.0
        elif int(nest_depth or 0) > 0:
            ratio *= 0.75
        else:
            ratio *= 0.5
    return round(ratio, 4)


def recommended_sell_ratio(bs_type: str, big_dir: str = "neutral") -> float:
    """Position share to sell for a Chanlun sell/exit signal."""
    if big_dir == "down":
        return 1.0
    cls = buy_class(bs_type)
    if cls in (1, 2, 3):
        return 1.0
    return 1.0


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
    nest_operable: Optional[bool] = None
    nest_depth: int = 0

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


def collect_dir_events(cd: CL, use_xd: bool = False) -> List[Tuple[pd.Timestamp, str]]:
    """[已废弃于门控,仅 validate 滞后实证用] 最终序列笔方向事件流 [(笔 start, dir)]。

    ⚠ 截断实证(validate trend):笔事件「首次稳定出现」滞后中位9bar/p90=20bar——固定 delay
    小了=lookahead(曾虚出+205%),大了=钝化 → 门控改用 wf_dir_series(真walk-forward)。"""
    lines = list(cd.get_xds()) if use_xd else list(cd.get_bis())
    out = []
    for ln in lines:
        if ln.start is None or ln.start.k is None:
            continue
        out.append((ln.start.k.date, "up" if ln.type == "up" else "down"))
    return out


def wf_dir_series(df: pd.DataFrame, code: str, tf: str,
                  warmup: int = 30) -> List[Tuple[pd.Timestamp, str]]:
    """真 walk-forward「当下笔方向」序列:逐根增量重算,每根 bar 收盘时取
    当时可见的最后一笔方向(含右边缘未完成笔=实盘真实状态)。

    100% 实盘可复制:无确认延迟参数、无 lookahead;右边缘 repaint 的代价(假方向期)
    原样体现在回测里。返回压缩事件流 [(bar收盘时刻, 'up'|'down'|'neutral')],仅方向
    变化时发事件;回测端用「事件时刻 + 下一bar」生效(bar 收盘才知道方向)。"""
    cd = CL(code, tf, dict(CL_CFG))
    events: List[Tuple[pd.Timestamp, str]] = []
    cur = None
    n = len(df)
    w = min(warmup, n)
    # 增量尾喂(已验:与全量逐根重算 123/123 一致)——避免 iloc[:t] 的 O(n²) 复制
    for t in range(w, n + 1):
        chunk = df.iloc[:w] if t == w else df.iloc[t - 1:t]
        cd.process_klines(chunk.reset_index(drop=True))
        bis = list(cd.get_bis())
        d = "neutral" if not bis else ("up" if bis[-1].type == "up" else "down")
        if d != cur:
            events.append((df["date"].iloc[t - 1], d))
            cur = d
    return events


def wf_seg38_series(df: pd.DataFrame, code: str, tf: str,
                    warmup: int = 30) -> List[Tuple[pd.Timestamp, str]]:
    """38课同级别分解**机械化操作程式**(原文line24751)的 walk-forward 工程版。

    原文程式(30m同级别分解):向上段背驰点先卖;向下第二段不跌破前低→重新买入;
    向上第三段不创新高→一定先卖出(创新高+盘整背驰也卖,不背驰持有);循环。
    工程化(实盘可复制,无lookahead):逐根增量喂,当「当下笔方向」翻转时,比较刚完成笔
    与前一同向笔的极值——down→up 翻转:刚完成 down 笔低点 ≥ 前 down 笔低点(不破低)→buy;
    up→down 翻转:刚完成 up 笔高点 ≤ 前 up 笔高点(不创新高)→sell。
    背驰点提前卖在右边缘不可知 → 用翻转确认近似(滞后换严谨);盘整背驰卖出条件留简版(不卖)。
    返回事件流 [(bar收盘时刻, 'buy'|'sell')]。"""
    cd = CL(code, tf, dict(CL_CFG))
    events: List[Tuple[pd.Timestamp, str]] = []
    cur_dir = None
    n = len(df)
    w = min(warmup, n)
    for t in range(w, n + 1):
        chunk = df.iloc[:w] if t == w else df.iloc[t - 1:t]
        cd.process_klines(chunk.reset_index(drop=True))
        bis = list(cd.get_bis())
        if not bis:
            continue
        d = bis[-1].type
        if cur_dir is not None and d != cur_dir and len(bis) >= 4:
            done, prev_same = bis[-2], bis[-4]     # 刚完成笔 vs 前一同向笔
            if done.end is not None and prev_same.end is not None:
                date = df["date"].iloc[t - 1]
                if d == "up" and done.end.val >= prev_same.end.val:
                    events.append((date, "buy"))     # 向下段不破前低 → 重新买入
                elif d == "down" and done.end.val <= prev_same.end.val:
                    events.append((date, "sell"))    # 向上段不创新高 → 先卖出
        cur_dir = d
    return events


def collect_branch_signals(
    cd: CL,
    use_xd: bool = False,
    annotate_nest: bool = False,
) -> List[Signal]:
    """原生图全量买卖点(get_branch_bspoints, L0 一二三类 + 升级级)。操作级买卖点取此。"""
    def _fx_key(fx):
        if fx is None:
            return None
        k = getattr(fx, "k", None)
        return (
            getattr(k, "k_index", None),
            str(getattr(k, "date", "")),
            round(float(getattr(fx, "val", 0.0) or 0.0), 8),
        )

    def _div_key(level: int, dv) -> tuple:
        if dv is None:
            return ()
        leave = getattr(dv, "leave_seg", None)
        return (
            int(level or 0),
            str(getattr(dv, "kind", "")),
            str(getattr(leave, "_type", getattr(leave, "type", ""))),
            _fx_key(getattr(leave, "start", None)),
            _fx_key(getattr(leave, "end", None)),
        )

    def _interval_nest_reads():
        if not hasattr(cd, "get_bis") or not hasattr(cd, "config"):
            return cd.get_branch_interval_nest()
        from chanlun.core.beichi_nest import BeichiNestCalculator
        from chanlun.core.cl_interface import Config, query_macd_ld
        from chanlun.core.interval_nest import IntervalNestCalculator
        from chanlun.core.recursive_branch import RecursiveBranchCalculator

        ld = lambda s, e: query_macd_ld(cd, s, e)
        wzgx = cd.config.get("zs_wzgx", Config.ZS_WZGX_ZGD.value)
        units = list(cd.get_xds()) if use_xd else list(cd.get_bis())
        levels = RecursiveBranchCalculator().calculate(
            units,
            ld,
            wzgx,
            cd.frequency,
        )
        forest = BeichiNestCalculator().calculate(levels)
        return IntervalNestCalculator().calculate(forest)

    out: List[Signal] = []
    nest_by_divergence: dict[int, object] = {}
    nest_by_key: dict[tuple, object] = {}
    if annotate_nest:
        try:
            for read in _interval_nest_reads():
                node = getattr(read, "node", None)
                dv = getattr(node, "divergence", None)
                if dv is not None:
                    nest_by_divergence[id(dv)] = read
                    nest_by_key[_div_key(getattr(node, "level", 0), dv)] = read
        except Exception:
            nest_by_divergence = {}
            nest_by_key = {}
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        fx = p.anchor_fx
        if fx is None or fx.k is None:
            continue
        level = p.level or 0
        nest_operable = None
        nest_depth = 0
        if annotate_nest and getattr(p, "divergence", None) is not None:
            read = nest_by_divergence.get(id(p.divergence))
            if read is None:
                read = nest_by_key.get(_div_key(level, p.divergence))
            nest_operable = bool(getattr(read, "operable", False)) if read else False
            nest_depth = int(getattr(read, "depth", 0) or 0) if read else 0
        out.append(Signal(
            fx.k.date, level, p.bs_type, fx.val,
            nest_operable=nest_operable,
            nest_depth=nest_depth,
        ))
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
    trades: List["Trade"] = field(repr=False, default_factory=list)


def prev_day_close_series(dates, closes) -> np.ndarray:
    """逐 bar 的前一交易日收盘价(A股涨跌停判定基准)。

    日线 bar=前一根收盘;分钟 bar=前一交易日最后一根收盘;首日无昨收为 NaN
    (调用方视为不判)。涨跌停是「相对昨日收盘」的日内累计约束,分钟级回测
    若用前一根 bar 收盘判定会恒失效(单根分钟 bar 涨不到 10%),涨停板上
    照常成交,与实盘不一致。"""
    closes_arr = np.asarray(closes, dtype=float)
    n = len(closes_arr)
    if n == 0:
        return np.empty(0)
    days = pd.DatetimeIndex(dates).normalize()
    first_of_day = np.r_[True, (days[1:] != days[:-1])]
    day_id = np.cumsum(first_of_day) - 1
    last_close_per_day = closes_arr[np.r_[first_of_day[1:], np.array([True])]]
    prev_per_day = np.r_[np.nan, last_close_per_day[:-1]]
    return prev_per_day[day_id]


def latest_prev_day_close(df: pd.DataFrame) -> float:
    """实时 K 线窗口里最后 bar 所在交易日的前一交易日收盘;窗口内没有
    昨日数据时返回 0.0(调用方视为不判涨跌停)。"""
    if df is None or len(df) == 0:
        return 0.0
    dates = pd.to_datetime(df["date"])
    last_day = dates.iloc[-1].normalize()
    mask = dates.dt.normalize() < last_day
    if not mask.any():
        return 0.0
    return float(df["close"][mask].iloc[-1])


class Simulator:
    """单标的、单方向(暂只多头)逐bar撮合。进场在 pending bar 的开盘价成交。"""

    def __init__(self, df: pd.DataFrame, rules: MarketRules, init_cash: float = 1_000_000):
        self.df = df.reset_index(drop=True)
        self.rules = rules
        self.init_cash = init_cash
        self.opens = self.df["open"].to_numpy()
        self.highs = self.df["high"].to_numpy()
        self.lows = self.df["low"].to_numpy()
        self.closes = self.df["close"].to_numpy()
        self.dates = self.df["date"].to_list()
        self.prev_day_closes = prev_day_close_series(self.dates, self.closes)

    def _limit_locked(self, i: int, side: str) -> bool:
        """涨跌停锁死无法成交(开盘价较**前一交易日收盘**触及限幅)。"""
        lp = self.rules.limit_pct
        if lp is None or i == 0:
            return False
        prev_close = self.prev_day_closes[i]
        if not np.isfinite(prev_close) or prev_close <= 0:
            return False
        chg = self.opens[i] / prev_close - 1
        if side == "buy" and chg >= lp * 0.995:    # 涨停买不进
            return True
        if side == "sell" and chg <= -lp * 0.995:  # 跌停卖不出
            return True
        return False

    def run(self, decide, stop_pct: Optional[float] = None) -> Result:
        """decide(i, date, holding, entry_idx) → 'buy'|'sell'|None(在 bar i 收盘后决定,bar i+1 开盘执行)。
        stop_pct: 硬止损(入场价回撤幅度,盘中 low 触及即平,缺口跳空按 min(open,止损价)成交)。"""
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
            # 0) 硬止损(盘中,T+1/涨跌停约束下);缺口跳空按 min(开盘,止损价)成交
            if (shares > 0 and stop_pct and not self._limit_locked(i, "sell")
                    and (r.t_plus == 0 or self.dates[i].date() > self.dates[entry_idx].date())):
                sp = entry_price * (1 - stop_pct)
                if self.lows[i] <= sp:
                    fill = min(self.opens[i], sp)
                    cash += shares * fill * (1 - r.commission - r.stamp_duty)
                    tr = Trade("long", self.dates[entry_idx], entry_price,
                               self.dates[i], fill, shares)
                    tr.ret = fill / entry_price - 1
                    tr.reason = "止损"
                    trades.append(tr)
                    shares = 0.0
                    entry_idx = -1
                    pending = None
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
        return Result("", "", total, bh, max_dd, sharpe, wr, len(trades), equity, trades)


# ---------------------------------------------------------------------------
# 策略(大小级别结合)
# ---------------------------------------------------------------------------
class MTFStrategy:
    """多周期大小级别结合(原文同级别分解操作:大级别30m定方向开窗,小级别5m窗口内进场)。

    decide 在 5m 执行bar 上工作。big_dir_at[i] = 截至第 i 根 5m bar 的 30m 大级别方向
    (30m 信号 +30min 延迟生效,规避未完成 30m bar 的 lookahead)。
    """

    def __init__(self, sig_small: List[Signal], sig_big: List[Signal],
                 dates5: List[pd.Timestamp], mode: str, gate: str = "up",
                 exit_mode: str = "any_sell", big_delay=None):
        self.mode = mode
        self.gate = gate          # "up"=严格(大级别必须up);"not_down"=放松(neutral也允许进场)
        self.exit_mode = exit_mode  # "any_sell"=任何小级别卖点出;"reversal"=只1/2卖(背驰反转)出,扛过3卖延续
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
        delay = big_delay if big_delay is not None else pd.Timedelta("30min")
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
        elif self.mode == "5m+30m":   # 共振:大级别窗口内,小级别买点进场;大级别down或小级别卖点出
            ok = (big_dir == "up") if self.gate == "up" else (big_dir != "down")
            if not holding and ok and any(s.is_buy for s in small):
                return "buy"
            if self.exit_mode == "reversal":
                sell_sig = any(s.bs_type in ("1sell", "2sell") for s in small)
            else:
                sell_sig = any(s.is_sell for s in small)
            if holding and (big_dir == "down" or sell_sig):
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
    # (策略名, mode, gate, exit_mode, stop_pct)
    grid = [
        ("5m_only", "5m_only", "up", "any_sell", None),
        ("30m_only", "30m_only", "up", "any_sell", None),
        ("5m+30m", "5m+30m", "up", "any_sell", None),               # 严格门控
        ("5m+30m_relax", "5m+30m", "not_down", "any_sell", None),     # 放松门控
        ("5m+30m_trend", "5m+30m", "not_down", "reversal", None),     # 放松+扛过3卖(吃趋势)
        ("5m+30m_trend_st", "5m+30m", "not_down", "reversal", 0.05),  # +5%止损兜底
    ]
    for sname, mode, gate, exit_mode, stop in grid:
        strat = MTFStrategy(sig_small, sig_big, sim.dates, mode, gate=gate, exit_mode=exit_mode)
        res = sim.run(strat.decide, stop_pct=stop)
        res.name, res.strategy = name, sname
        results.append(res)
    print(f"\n=== {name}({code}) {rules.name} | 5mbars={len(df5)} "
          f"小级别信号={len(sig_small)} 大级别(30m)信号={len(sig_big)} ===")
    return results


def generate_report(out_png: str = "D:/chanlun_pro/reports/recursive_single.png"):
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    """最优策略(5m+30m_relax)权益曲线 vs 买入持有,各标的子图 + 交易明细。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = list(SYMBOLS.items())
    fig, axes = plt.subplots(len(items), 1, figsize=(11, 3.0 * len(items)))
    if len(items) == 1:
        axes = [axes]
    for ax, (name, (prefix, code, rules)) in zip(axes, items):
        df5 = load_klines(prefix, "5m")
        df30 = load_klines(prefix, "30m")
        if df5 is None:
            continue
        cd5 = CL(code, "5m", dict(CL_CFG))
        cd5.process_klines(df5)
        sig_small = collect_branch_signals(cd5, use_xd=False)
        sig_big = []
        if df30 is not None and len(df30) >= 50:
            cd30 = CL(code, "30m", dict(CL_CFG))
            cd30.process_klines(df30)
            sig_big = collect_branch_signals(cd30, use_xd=False)
        sim = Simulator(df5, rules)
        strat = MTFStrategy(sig_small, sig_big, sim.dates, "5m+30m", gate="not_down")
        res = sim.run(strat.decide)
        x = pd.to_datetime(sim.dates)
        eq = res.equity / res.equity[0]
        bh = sim.closes / sim.closes[0]
        ax.plot(x, eq, label=f"strategy {res.total_return:+.1%}", color="crimson", lw=1.3)
        ax.plot(x, bh, label=f"buy&hold {res.buy_hold:+.1%}", color="steelblue", lw=1.0, alpha=0.7)
        ax.set_title(f"{code} ({rules.name})  DD={res.max_dd:.1%} Sharpe={res.sharpe:.1f} "
                     f"trades={res.n_trades} win={res.win_rate:.0%}", fontsize=9)
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(alpha=0.3)
    fig.suptitle("Chanlun MTF (5m entry + 30m direction, relaxed gate) vs Buy&Hold",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    print(f"\n报告已保存: {out_png}")
    return out_png


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
    for mode in ("5m_only", "30m_only", "5m+30m", "5m+30m_relax",
                 "5m+30m_trend", "5m+30m_trend_st"):
        rs = [r for r in rows if r.strategy == mode]
        if rs:
            avg_ex = np.mean([r.total_return - r.buy_hold for r in rs])
            avg_ret = np.mean([r.total_return for r in rs])
            avg_dd = np.mean([r.max_dd for r in rs])
            avg_sh = np.mean([r.sharpe for r in rs])
            print(f"  {mode:9s} 平均收益={avg_ret:+7.1%} 平均超额={avg_ex:+7.1%} "
                  f"平均回撤={avg_dd:5.1%} 平均夏普={avg_sh:5.2f}")
    generate_report()


if __name__ == "__main__":
    main()
