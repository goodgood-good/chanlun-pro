"""chanlun.recursive_bt.paper — 仿实盘实时交易(paper trading)。

实盘同款决策链(与回测定论一致的策略A):每根 5m bar 收盘后增量更新各标的 CL(尾喂,
与全量重算已验一致),小级别(5m)技术面买点进场 + 大级别(30m)wf 当下笔方向门控
(not_down 开仓 / down 或小级别卖点退出),1买>2买>3买排序填仓,等权 max_pos。
A股规则:T+1/印花税/涨跌停粗判。账本持久化 JSON(重启恢复),逐笔与权益落
D:/chanlun_pro/paper/。挂单下一轮(下一5m bar)以最新开盘价成交,无任何未来信息。

运行(建议 -u 关缓冲,否则重定向时看不到逐轮日志;账本 JSON 始终每轮落盘):
  PYTHONPATH="src;web/chanlun_chart;." python -u -m chanlun.recursive_bt.paper          # 实时循环(交易时段)
  PYTHONPATH=...                      python -u -m chanlun.recursive_bt.paper replay 5  # 回放冒烟/演练
池=bt_data 已缓存标的(默认前50只,POOL_SIZE 环境变量调)。
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import datetime as _dt
from typing import Dict, List, Optional

import pandas as pd

from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import CL_CFG, collect_branch_signals

PAPER_DIR = "D:/chanlun_pro/paper"
BT_DATA = "D:/chanlun_pro/bt_data"
MAX_POS = 10
COMMISSION, STAMP = 0.0003, 0.0005
INIT_CASH = 1_000_000.0


def limit_pct(code: str) -> float:
    num = code.split(".")[1]
    return 0.20 if num[:3] in ("688", "300", "301") else 0.10


class SymbolState:
    """常驻一只标的的 5m/30m/日线 CL 与最新信号状态(增量尾喂)。"""

    def __init__(self, code: str, ex):
        self.code = code
        self.ex = ex
        self.cd5 = CL(code, "5m", dict(CL_CFG))
        self.cd30 = CL(code, "30m", dict(CL_CFG))
        self.cdd = CL(code, "d", dict(CL_CFG))
        self.last5: Optional[pd.Timestamp] = None
        self.last30: Optional[pd.Timestamp] = None
        self.lastd: Optional[pd.Timestamp] = None
        self.d3_until: Optional[pd.Timestamp] = None   # 日线3买窗口截止(三级共振排序,line13507)
        self.last_px: float = 0.0
        self.prev_close: float = 0.0
        self.seen = set()          # 已消费的(信号date,bs_type)

    def refresh(self) -> List:
        """拉最新K线,尾喂新完整bar;返回**新增**的5m买卖点信号。"""
        df5 = self.ex.klines(self.code, "5m")
        df30 = self.ex.klines(self.code, "30m")
        if df5 is None or len(df5) < 100:
            return []
        new5 = df5 if self.last5 is None else df5[df5["date"] > self.last5]
        if len(new5):
            self.cd5.process_klines(new5.reset_index(drop=True))
            self.last5 = df5["date"].iloc[-1]
        if df30 is not None and len(df30) >= 50:
            new30 = df30 if self.last30 is None else df30[df30["date"] > self.last30]
            if len(new30):
                self.cd30.process_klines(new30.reset_index(drop=True))
                self.last30 = df30["date"].iloc[-1]
        # 日线:仅在出现新完整日线 bar 时重算(每日一次),维护日线3买窗口(确认次日起10天,
        # 三级共振排序信息 line13507;回测实证排序融合 +158.9% vs 基线 +147.6%)
        dfd = self.ex.klines(self.code, "d")
        if dfd is not None and len(dfd) >= 100:
            newd = dfd if self.lastd is None else dfd[dfd["date"] > self.lastd]
            if len(newd):
                self.cdd.process_klines(newd.reset_index(drop=True))
                self.lastd = dfd["date"].iloc[-1]
                for s in collect_branch_signals(self.cdd, use_xd=False):
                    if s.bs_type == "3buy":
                        until = s.date + pd.Timedelta(days=11)
                        if self.d3_until is None or until > self.d3_until:
                            self.d3_until = until
        self.last_px = float(df5["close"].iloc[-1])
        self.prev_close = float(df5["close"].iloc[-2]) if len(df5) > 1 else self.last_px
        out = []
        for s in collect_branch_signals(self.cd5, use_xd=False):
            k = (s.date, s.bs_type)
            if k in self.seen:
                continue
            self.seen.add(k)
            # 只消费「最新bar」上的新信号(历史首轮灌入时全部标记已见、不触发交易)
            if self.last5 is not None and s.date == self.last5:
                out.append(s)
        return out

    def big_dir(self) -> str:
        """30m 当下笔方向(wf 口径:当时可见最后一笔,含未完成笔)。"""
        bis = list(self.cd30.get_bis())
        if not bis:
            return "neutral"
        return "up" if bis[-1].type == "up" else "down"

    def in_d3(self) -> bool:
        """当前是否处于日线3买窗口(三级共振排序用)。"""
        if self.d3_until is None or self.last5 is None:
            return False
        return self.last5 <= self.d3_until


class PaperBroker:
    """账本:现金/持仓/挂单/逐笔,JSON 持久化,T+1 与涨跌停粗判。"""

    def __init__(self, path: str):
        self.path = path
        self.cash = INIT_CASH
        self.positions: Dict[str, dict] = {}
        self.pending: List[dict] = []
        self.trades: List[dict] = []
        if os.path.exists(path):
            d = json.load(open(path, encoding="utf-8"))
            self.cash = d["cash"]
            self.positions = d["positions"]
            self.pending = d.get("pending", [])
            self.trades = d.get("trades", [])

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump({"cash": self.cash, "positions": self.positions,
                   "pending": self.pending, "trades": self.trades},
                  open(self.path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1, default=str)

    def equity(self, px: Dict[str, float]) -> float:
        return self.cash + sum(p["shares"] * px.get(c, p["entry_px"])
                               for c, p in self.positions.items())

    def fill_pending(self, states: Dict[str, "SymbolState"], now: str):
        """以各标的最新价成交上一轮挂单(=下一bar开盘近似)。"""
        carry = []
        for o in self.pending:
            c = o["code"]
            st = states.get(c)
            if st is None or st.last_px <= 0:
                continue
            px = st.last_px
            chg = px / st.prev_close - 1 if st.prev_close else 0.0
            lp = limit_pct(c)
            if o["act"] == "buy" and c not in self.positions:
                if chg >= lp * 0.995:      # 涨停买不进
                    continue
                eq = self.equity({k: s.last_px for k, s in states.items()})
                budget = min(eq / MAX_POS, self.cash) * 0.99
                size = int(budget / (px * (1 + COMMISSION)) / 100) * 100
                if size > 0:
                    self.cash -= size * px * (1 + COMMISSION)
                    self.positions[c] = {"shares": size, "entry_px": px,
                                         "entry_date": now, "bs": o.get("bs", "")}
            elif o["act"] == "sell" and c in self.positions:
                p = self.positions[c]
                if str(p["entry_date"])[:10] >= now[:10]:    # T+1
                    carry.append(o)
                    continue
                if chg <= -lp * 0.995:     # 跌停卖不出
                    carry.append(o)
                    continue
                self.cash += p["shares"] * px * (1 - COMMISSION - STAMP)
                self.trades.append({"code": c, "entry_date": p["entry_date"],
                                    "entry_px": p["entry_px"], "exit_date": now,
                                    "exit_px": px, "ret": px / p["entry_px"] - 1,
                                    "reason": o.get("reason", "")})
                del self.positions[c]
        self.pending = carry


def step(broker: PaperBroker, states: Dict[str, SymbolState], now: str):
    """一轮决策:成交挂单 → 退出信号 → 选股开仓(1买优先)。"""
    broker.fill_pending(states, now)
    sigs: Dict[str, List] = {}
    for c, st in states.items():
        try:
            sigs[c] = st.refresh()
        except Exception as e:
            print(f"  {c} refresh 失败 {type(e).__name__}: {e}")
            sigs[c] = []
    # 退出:大级别down 或 当bar小级别卖点
    pend_codes = {o["code"] for o in broker.pending}
    for c in list(broker.positions):
        if c in pend_codes:
            continue
        st = states[c]
        if st.big_dir() == "down":
            broker.pending.append({"code": c, "act": "sell", "reason": "大级别转空"})
        elif any(s.is_sell for s in sigs.get(c, [])):
            broker.pending.append({"code": c, "act": "sell", "reason": "小级别卖点"})
    # 开仓:技术面买点 + 30m not_down,**3买优先**(line23172「牛市里第三类买点的爆发力
    # 是最强的」;回测实证:牛市+10.5pp/熊市+1.4pp 两段皆优于1买优先)
    free = MAX_POS - len(broker.positions) - sum(1 for o in broker.pending if o["act"] == "buy")
    if free > 0:
        cands = []
        for c, ss in sigs.items():
            if c in broker.positions or c in {o["code"] for o in broker.pending}:
                continue
            buys = [s for s in ss if s.is_buy]
            if buys and states[c].big_dir() != "down":
                # 排序:3买优先(line23172)。d3共振排序已移除——审计2(master并集)修复后
                # d3增益翻转为噪声级(交集+11.3pp/并集-7.6pp),按数据定夺中性弃用;
                # in_d3() 观测能力保留(实盘A/B分析备用)。
                cands.append((-min(int(s.bs_type[0]) for s in buys), c))
        cands.sort()
        for item in cands[:free]:
            c = item[-1]
            broker.pending.append({"code": c, "act": "buy",
                                   "bs": str(min(int(s.bs_type[0]) for s in sigs[c] if s.is_buy))})
    eq = broker.equity({k: s.last_px for k, s in states.items()})
    print(f"[{now}] 权益={eq:,.0f} 现金={broker.cash:,.0f} 持仓={len(broker.positions)} "
          f"挂单={len(broker.pending)} 累计成交={len(broker.trades)}")
    broker.save()


def in_session(t: _dt.datetime) -> bool:
    if t.weekday() >= 5:
        return False
    hm = t.hour * 100 + t.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def main():
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    pool_n = int(os.environ.get("POOL_SIZE", "50"))
    codes = sorted(os.path.basename(f)[:-4] for f in glob.glob(f"{BT_DATA}/*.pkl")
                   if "SH.000001" not in f)[:pool_n]
    ex = ExchangeQMT()
    print(f"初始化 {len(codes)} 只(灌历史K线,首轮信号只登记不交易)...")
    states = {c: SymbolState(c, ex) for c in codes}
    for c, st in states.items():
        try:
            st.refresh()
        except Exception as e:
            print(f"  {c} 初始化失败 {e}")
    broker = PaperBroker(f"{PAPER_DIR}/ledger.json")
    print(f"账本: 现金={broker.cash:,.0f} 持仓={len(broker.positions)}")

    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        # 回放冒烟:不真等5m,立即把「当前最新bar」当一轮跑 N 次(增量无新bar=空转,验证链路)
        rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for r in range(rounds):
            step(broker, states, now=str(pd.Timestamp.now()))
        print("回放冒烟完成(链路通)。实时模式: python -m chanlun.recursive_bt.paper")
        return

    print("实时循环:每5m一轮(交易时段),Ctrl+C 退出。")
    while True:
        now = _dt.datetime.now()
        if in_session(now):
            step(broker, states, now=now.strftime("%Y-%m-%d %H:%M:%S"))
        nxt = (now.replace(second=5, microsecond=0)
               + _dt.timedelta(minutes=5 - now.minute % 5))
        time.sleep(max((nxt - _dt.datetime.now()).total_seconds(), 10))


if __name__ == "__main__":
    main()
