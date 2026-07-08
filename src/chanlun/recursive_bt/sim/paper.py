"""chanlun.recursive_bt.sim.paper — 仿实盘实时交易(paper trading)。

每根 5m bar 收盘后增量更新各标的 CL(尾喂),小级别(5m)买点进场 + 大级别(30m)当下笔
方向门控(not_down 开仓 / down 或小级别卖点退出),按买点类别排序填仓,等权 max_pos。
A股规则:T+1/印花税/涨跌停粗判。账本持久化 JSON(重启恢复),逐笔与权益落
D:/chanlun_pro/paper/。挂单下一轮(下一5m bar)以最新开盘价成交,无任何未来信息。

运行(建议 -u 关缓冲,否则重定向时看不到逐轮日志;账本 JSON 始终每轮落盘):
  PYTHONPATH="src;web/chanlun_chart;." python -u -m chanlun.recursive_bt.sim.paper          # 实时循环(交易时段)
  PYTHONPATH=...                      python -u -m chanlun.recursive_bt.sim.paper replay 5  # 回放冒烟/演练
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
from chanlun.recursive_bt.engine.engine import (
    CL_CFG,
    collect_branch_signals,
    latest_prev_day_close,
    recommended_buy_ratio,
    recommended_sell_ratio,
)
from chanlun.recursive_bt.engine.market_runtime import (
    market_rules_for_code,
    normalize_market,
)

PAPER_DIR = "D:/chanlun_pro/paper"
BT_DATA = "D:/chanlun_pro/bt_data"
MAX_POS = 10
COMMISSION, STAMP = 0.0003, 0.0005
INIT_CASH = 1_000_000.0


def limit_pct(code: str, market: str = "a") -> Optional[float]:
    return market_rules_for_code(market, code).limit_pct


def _freq_to_minutes(frequency: str) -> Optional[int]:
    """把级别字符串转成分钟数;非分钟级(如 'd'/'w')返回 None=不裁剪。"""
    f = str(frequency).strip().lower()
    if f.endswith("m"):
        try:
            return max(int(f[:-1]), 1)
        except ValueError:
            return None
    return None


def drop_unclosed_last_bar(df: "pd.DataFrame", frequency: str) -> "pd.DataFrame":
    """丢弃 df 末尾仍在进行(未收盘)的那一根 bar,使 live 口径与回测一致。

    判定方式自包含、不依赖外部 now(tz 不可知):用倒数第二根与最后一根的
    时间差推断 bar 间隔,再用与最后一根**同 tz** 的当前时刻判断「最后一根
    所属周期是否已结束」。无法判定(非分钟级/不足两根/间隔异常)时原样返回,
    绝不误删历史收盘 bar。
    """
    minutes = _freq_to_minutes(frequency)
    if minutes is None or df is None or len(df) < 2:
        return df
    try:
        last_ts = pd.Timestamp(df["date"].iloc[-1])
        prev_ts = pd.Timestamp(df["date"].iloc[-2])
    except Exception:
        return df
    step = pd.Timedelta(minutes=minutes)
    # 数据自身间隔与名义间隔不一致(节假日跳空/缺口)时,只在「正好等于名义间隔」
    # 时才启用裁剪,避免把历史正常 bar 误判为进行中。
    if (last_ts - prev_ts) != step:
        return df
    # 用与 last_ts 同 tz 的当前时刻(naive→naive, aware→对应 tz)
    now = pd.Timestamp.now(tz=last_ts.tz) if last_ts.tz is not None else pd.Timestamp.now()
    # bar 收盘时刻 = bar 起点 + 一个周期。now 还没到收盘时刻=这根还在进行中。
    if now < last_ts + step:
        return df.iloc[:-1]
    return df


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
        self.d3_until: Optional[pd.Timestamp] = None   # 日线3买窗口截止(三级共振排序用)
        self.last_open: float = 0.0
        self.last_px: float = 0.0
        self.prev_close: float = 0.0
        self.seen = set()          # 已消费的(信号date,bs_type)
        self.consecutive_refresh_failures = 0
        # 新鲜窗口=40个5m bar(=200min):买卖点首次可见滞后确认bar中位9/p90=20,
        # 留足右侧深度确认余量;旧150min偏紧偶尔吞真新信号。
        self.signal_freshness = pd.Timedelta(minutes=200)

    def refresh(self) -> List:
        """拉最新K线,尾喂新完整bar;返回**新增**的5m买卖点信号。"""
        df5 = drop_unclosed_last_bar(self.ex.klines(self.code, "5m"), "5m")
        df30 = drop_unclosed_last_bar(self.ex.klines(self.code, "30m"), "30m")
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
        # 日线:仅在出现新完整日线 bar 时重算(每日一次),维护日线3买窗口
        # (买点确认次日起约10天,供三级共振排序使用)
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
        self.last_open = float(df5["open"].iloc[-1])
        self.last_px = float(df5["close"].iloc[-1])
        # 涨跌停基准=前一交易日收盘(窗口内无昨日数据时为 0=不判),
        # 不能用前一根分钟 bar 收盘——日内累计涨停会被漏判。
        self.prev_close = latest_prev_day_close(df5)
        out = []
        for s in collect_branch_signals(self.cd5, use_xd=False):
            k = (s.date, s.bs_type)
            if k in self.seen:
                continue
            self.seen.add(k)
            # 消费「本轮新出现且确认bar在新鲜窗口内」的信号(历史首轮灌入时
            # 全部标记已见、不触发交易);信号首次可见滞后确认bar若干根,
            # 要求恰好等于最新bar会把全部真实新信号吞掉
            if self.last5 is not None and self.last5 - s.date <= self.signal_freshness:
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

    def __init__(self, path: str, market: str = "a", max_pos: int = MAX_POS):
        self.path = path
        self.market = normalize_market(market)
        self.max_pos = max(int(max_pos or MAX_POS), 1)
        self.cash = INIT_CASH
        self.positions: Dict[str, dict] = {}
        self.pending: List[dict] = []
        self.trades: List[dict] = []
        self.equity_curve: List[dict] = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fp:
                    d = json.load(fp)
                if not isinstance(d, dict):
                    raise ValueError("ledger root is not an object")
                # cash/positions 用 .get 兜底:损坏/旧 schema 时以空账本起而非崩溃。
                self.cash = float(d.get("cash", INIT_CASH))
                self.positions = d.get("positions") or {}
                self.pending = d.get("pending") or []
                self.trades = d.get("trades") or []
                self.equity_curve = d.get("equity_curve") or []
            except Exception as exc:
                # 损坏(截断/JSONDecodeError/类型错)时告警并以空账本起,保证监控能启动。
                import shutil
                from chanlun import fun
                fun.get_logger().warning(
                    f"[paper] ledger load failed, starting fresh: path={path} err={exc}"
                )
                try:
                    shutil.copy2(path, path + ".corrupt")   # 保留损坏现场供排查
                except Exception:
                    pass

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            {"cash": self.cash, "positions": self.positions,
             "pending": self.pending, "trades": self.trades,
             "equity_curve": self.equity_curve,
             "summary": self.performance_summary()},
            ensure_ascii=False, indent=1, default=str,
        )
        # 原子写:先写 .tmp 再 os.replace,避免写一半被 kill(崩溃/自愈重启)截断 ledger
        # (与 live_monitor.JsonDeduper.save 同写法)。
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(payload)
        os.replace(tmp, self.path)

    def equity(self, px: Dict[str, float]) -> float:
        return self.cash + sum(p["shares"] * px.get(c, p["entry_px"])
                               for c, p in self.positions.items())

    def _prices_from_states(self, states: Dict[str, object]) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for code, state in states.items():
            if isinstance(state, (int, float)):
                prices[code] = float(state)
                continue
            value = getattr(state, "last_px", None)
            if value is None or float(value or 0.0) <= 0:
                value = getattr(state, "last_open", None)
            try:
                value = float(value or 0.0)
            except Exception:
                value = 0.0
            if value > 0:
                prices[code] = value
        return prices

    def record_snapshot(self, states: Dict[str, object], now: str) -> dict:
        prices = self._prices_from_states(states)
        equity = self.equity(prices)
        snapshot = {
            "time": now,
            "equity": equity,
            "cash": self.cash,
            "positions": len(self.positions),
            "pending": len(self.pending),
            "trades": len(self.trades),
        }
        self.equity_curve.append(snapshot)
        self._trim_equity_curve()
        return snapshot

    EQUITY_CURVE_MAXLEN = 20000   # ~A股一年12000轮,留足两年余量;超过则滚动归档

    def _trim_equity_curve(self) -> None:
        """滚动保留近 EQUITY_CURVE_MAXLEN 条;裁剪时把被丢弃段的起点与峰值折叠成
        一个 anchor 保留在头部,使 performance_summary 的 total_return/max_drawdown
        仍基于完整历史的 start 与 peak,不因裁剪失真。"""
        n = len(self.equity_curve)
        if n <= self.EQUITY_CURVE_MAXLEN:
            return
        drop_to = n - self.EQUITY_CURVE_MAXLEN
        dropped = self.equity_curve[:drop_to]
        eqs = [float(p.get("equity") or 0.0) for p in dropped if float(p.get("equity") or 0.0) > 0]
        if eqs:
            # 折叠段峰值=本段 equity 与「段内旧 anchor 已携带的 peak_equity」之并集的
            # 最大值——逐轮滚动裁剪时旧 anchor 会被再次挤出,必须把它记录的历史峰值
            # 传递给新 anchor,否则早期峰值会在多轮裁剪后丢失致 max_drawdown 失真。
            peak_equity = max(
                eqs + [float(p.get("peak_equity") or 0.0)
                       for p in dropped if p.get("anchor")]
            )
            anchor = {
                "time": dropped[0].get("time"),
                "equity": eqs[0],            # 折叠段起点=总起点,守住 total_return 的分母
                "peak_equity": peak_equity,  # 折叠段峰值(含旧 anchor 链式传递),供 summary 取全局 peak
                "cash": dropped[0].get("cash"),
                "positions": dropped[0].get("positions", 0),
                "pending": dropped[0].get("pending", 0),
                "trades": dropped[0].get("trades", 0),
                "anchor": True,
                "reason": "equity_curve_trim_anchor",
            }
            self.equity_curve = [anchor] + self.equity_curve[drop_to:]
        else:
            self.equity_curve = self.equity_curve[drop_to:]

    def ensure_baseline_snapshot(self, now: str) -> Optional[dict]:
        """Create one neutral equity anchor when a ledger has no curve yet."""
        if self.equity_curve:
            return None
        snapshot = {
            "time": now,
            "equity": self.equity({}),
            "cash": self.cash,
            "positions": len(self.positions),
            "pending": len(self.pending),
            "trades": len(self.trades),
            "baseline": True,
            "reason": "ledger_baseline",
        }
        self.equity_curve.append(snapshot)
        return snapshot

    def performance_summary(self) -> dict:
        curve = [
            float(point.get("equity") or 0.0)
            for point in self.equity_curve
            if float(point.get("equity") or 0.0) > 0
        ]
        if curve:
            start = curve[0]
            latest = curve[-1]
            # 若曾裁剪,头部 anchor 携带折叠段峰值,纳入初始 peak 防回撤被低估
            anchor_peak = 0.0
            for point in self.equity_curve:
                if point.get("anchor"):
                    anchor_peak = max(anchor_peak, float(point.get("peak_equity") or 0.0))
            peak = max(curve[0], anchor_peak)
            max_dd = 0.0
            for value in curve:
                peak = max(peak, value)
                if peak > 0:
                    max_dd = max(max_dd, 1.0 - value / peak)
        else:
            start = INIT_CASH
            latest = self.equity({})
            max_dd = 0.0
        wins = sum(1 for trade in self.trades if float(trade.get("ret") or 0.0) > 0)
        n_trades = len(self.trades)
        return {
            "start_equity": start,
            "latest_equity": latest,
            "total_return": latest / start - 1 if start > 0 else 0.0,
            "max_drawdown": max_dd,
            "positions": len(self.positions),
            "pending": len(self.pending),
            "trades": n_trades,
            "win_rate": wins / n_trades if n_trades else 0.0,
        }

    def queue_events(self, events) -> int:
        """Queue paper orders from live MonitorEvent objects."""
        queued = 0
        pending_buy = {o["code"] for o in self.pending if o.get("act") == "buy"}
        pending_sell = {o["code"] for o in self.pending if o.get("act") == "sell"}
        buy_slots = max(self.max_pos - len(self.positions) - len(pending_buy), 0)
        for event in events:
            code = getattr(event, "code", "")
            side = getattr(event, "side", "")
            if not code:
                continue
            if side == "buy":
                if buy_slots <= 0:
                    continue
                if code in self.positions or code in pending_buy:
                    continue
                ratio = float(getattr(event, "buy_ratio", 0.0) or 0.0)
                if ratio <= 0:
                    continue
                self.pending.append({
                    "code": code,
                    "act": "buy",
                    "bs": getattr(event, "bs_type", ""),
                    "target_weight": ratio,
                    "reason": getattr(event, "reason", ""),
                    "event_id": getattr(event, "identity", ""),
                    "queued_signal_time": getattr(event, "signal_time", ""),
                })
                pending_buy.add(code)
                buy_slots -= 1
                queued += 1
            elif side in {"sell", "exit"}:
                if code not in self.positions or code in pending_sell:
                    continue
                _sr = getattr(event, "sell_ratio", 1.0)
                # 保留显式 0.0(=不卖, 交下方 ratio<=0 guard 跳过), 仅 None 兜底 1.0(不能用 or 1.0 吞 0.0)
                ratio = 1.0 if _sr is None else float(_sr)
                ratio = min(max(ratio, 0.0), 1.0)
                if ratio <= 0:
                    continue
                order = {
                    "code": code,
                    "act": "sell",
                    "sell_ratio": ratio,
                    "reason": getattr(event, "reason", ""),
                    "event_id": getattr(event, "identity", ""),
                    "queued_signal_time": getattr(event, "signal_time", ""),
                }
                bs_type = getattr(event, "bs_type", "")
                if bs_type:
                    order["bs"] = bs_type
                self.pending.append(order)
                pending_sell.add(code)
                queued += 1
        return queued

    def fill_pending(self, states: Dict[str, "SymbolState"], now: str):
        """以各标的最新 bar 开盘价成交上一轮挂单。"""
        carry = []
        for o in self.pending:
            c = o["code"]
            st = states.get(c)
            if st is None or st.last_open <= 0:
                carry.append(o)
                continue
            px = st.last_open
            chg = px / st.prev_close - 1 if st.prev_close else 0.0
            rules = market_rules_for_code(self.market, c)
            lp = rules.limit_pct
            if o["act"] == "buy" and c not in self.positions:
                if lp is not None and chg >= lp * 0.995:      # 涨停买不进
                    continue
                eq = self.equity({k: s.last_px for k, s in states.items()})
                target_weight = float(o.get("target_weight") or 0.0)
                target = eq * target_weight if target_weight > 0 else eq / self.max_pos
                budget = min(target, self.cash) * 0.99
                size = budget / (px * (1 + rules.commission))
                if rules.lot > 1:
                    size = int(size / rules.lot) * rules.lot
                if size > 0:
                    self.cash -= size * px * (1 + rules.commission)
                    self.positions[c] = {"shares": size, "entry_px": px,
                                         "entry_date": now, "bs": o.get("bs", "")}
                    # 漏跑检测:排单锚与当前 bar 差距过大→成交价非「下一根」,仅记录不改价
                    qs = o.get("queued_signal_time")
                    if qs:
                        self.positions[c]["fill_stale"] = bool(
                            st.last5 is not None
                            and str(qs) != ""
                            and (pd.Timestamp(st.last5) - pd.Timestamp(qs))
                            > pd.Timedelta(minutes=10)
                        )
            elif o["act"] == "sell" and c in self.positions:
                p = self.positions[c]
                if rules.t_plus > 0 and str(p["entry_date"])[:10] >= now[:10]:    # T+1
                    carry.append(o)
                    continue
                if lp is not None and chg <= -lp * 0.995:     # 跌停卖不出
                    carry.append(o)
                    continue
                _sr = o.get("sell_ratio")
                # 保留显式 0.0(=不卖, 交下方 size<=0 drop 跳过), 仅缺失/None 兜底 1.0(不能用 or 1.0 吞 0.0)
                sell_ratio = 1.0 if _sr is None else float(_sr)
                sell_ratio = min(max(sell_ratio, 0.0), 1.0)
                size = p["shares"] if sell_ratio >= 0.999 else p["shares"] * sell_ratio
                if rules.lot > 1:
                    size = int(size / rules.lot) * rules.lot
                if size <= 0:
                    # LOW-1(Round12, 同 portfolio HIGH-1 根): sub-lot 部分卖取整为0股无法成交且持仓不
                    # 增长故未来仍0; 原 carry 永久滞留、该标退出被卡。改 drop(all_out 恒不触发, 防未来
                    # 分层退出接入 sell_ratio 时踩)。
                    continue
                self.cash += size * px * (1 - rules.commission - rules.stamp_duty)
                self.trades.append({"code": c, "entry_date": p["entry_date"],
                                    "entry_px": p["entry_px"], "exit_date": now,
                                    "exit_px": px, "ret": px / p["entry_px"] - 1,
                                    "shares": size, "bs_type": p.get("bs", ""),
                                    "exit_bs_type": o.get("bs", ""),
                                    "sell_ratio": sell_ratio,
                                    "reason": o.get("reason", "")})
                remain = p["shares"] - size
                if remain > 1e-9 and sell_ratio < 0.999:
                    p["shares"] = remain
                else:
                    del self.positions[c]
        self.pending = carry


def step(broker: PaperBroker, states: Dict[str, SymbolState], now: str):
    """一轮决策:刷新最新bar → 成交挂单 → 退出信号 → 选股开仓(3买优先)。"""
    sigs: Dict[str, List] = {}
    for c, st in states.items():
        try:
            sigs[c] = st.refresh()
        except Exception as e:
            print(f"  {c} refresh 失败 {type(e).__name__}: {e}")
            sigs[c] = []
    broker.fill_pending(states, now)
    # 退出:大级别down 或 当bar小级别卖点
    pend_codes = {o["code"] for o in broker.pending}
    for c in list(broker.positions):
        if c in pend_codes:
            continue
        st = states.get(c)
        if st is None:
            # 持仓标的不在当前池(ledger 持仓数 > 池 / 未入选)→ 无 CL 状态无法评估退出,
            # 跳过本轮(与上方 fill_pending 的 states.get 兜底一致),避免裸下标 KeyError
            # 整个 step 崩溃(预存 bug,replay 小池+满 ledger 实测命中)。池管理正常时持仓本应在 states 内。
            continue
        if st.big_dir() == "down":
            broker.pending.append({
                "code": c,
                "act": "sell",
                "sell_ratio": recommended_sell_ratio("", big_dir="down"),
                "reason": "big_level_down",
            })
        else:
            sell_sig = next((s for s in sigs.get(c, []) if s.is_sell), None)
            if sell_sig is not None:
                bs_type = str(getattr(sell_sig, "bs_type", ""))
                order = {
                    "code": c,
                    "act": "sell",
                    "sell_ratio": recommended_sell_ratio(bs_type, big_dir=st.big_dir()),
                    "reason": "small_level_sell_point",
                }
                if bs_type:
                    order["bs"] = bs_type
                broker.pending.append(order)
    # 开仓:技术面买点 + 30m not_down,3买优先(突破延续口径)
    free = broker.max_pos - len(broker.positions) - sum(1 for o in broker.pending if o["act"] == "buy")
    if free > 0:
        cands = []
        for c, ss in sigs.items():
            if c in broker.positions or c in {o["code"] for o in broker.pending}:
                continue
            buys = [s for s in ss if s.is_buy]
            if buys and states[c].big_dir() != "down":
                # 排序:3买优先。d3 共振排序已弃用(增益翻转为噪声级);
                # in_d3() 观测能力保留(实盘A/B分析备用)。
                cls = max(int(s.bs_type[0]) for s in buys)
                daily_resonance = bool(states[c].in_d3())
                ratio = recommended_buy_ratio(
                    f"{cls}buy",
                    max_pos=broker.max_pos,
                    big_dir=states[c].big_dir(),
                    daily_resonance=daily_resonance,
                )
                cands.append((-cls, c, cls, ratio))
        cands.sort()
        for item in cands[:free]:
            c = item[1]
            broker.pending.append({"code": c, "act": "buy",
                                   "bs": str(item[2]),
                                   "target_weight": item[3]})
    snapshot = broker.record_snapshot(states, now)
    summary = broker.performance_summary()
    print(f"[{now}] 权益={snapshot['equity']:,.0f} 现金={broker.cash:,.0f} 持仓={len(broker.positions)} "
          f"挂单={len(broker.pending)} 累计成交={len(broker.trades)} "
          f"收益={summary['total_return']:.2%} 回撤={summary['max_drawdown']:.2%}")
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
        print("回放冒烟完成(链路通)。实时模式: python -m chanlun.recursive_bt.sim.paper")
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
