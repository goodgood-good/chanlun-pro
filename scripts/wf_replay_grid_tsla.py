# -*- coding: utf-8 -*-
"""TSLA 真·wf 大网格离线 replay:确认层 × 买点类 × 卖点类 × 门控严格度。

读 wf_confirm_scan_tsla 的 stage1 pkl(信号生命周期,无未来函数),离线扫策略变体:
- n_buy/n_sell: 确认层(信号持稳 N 根才动作)
- buy_classes: 只接受的买点类(3=突破回试,1=趋势背驰反转,2=次级别回试)
- sell_classes: 触发卖出的卖点类({1,2}=只背驰反转卖,3 卖不卖靠门控兜底)
- gate_mode: not_down(30m 非下跌可买) / up(仅 30m 上涨窗口可买);down 一律强平
执行模型同 wf_backtest_tsla(下一根开盘成交/比例/T+0/费用)。
运行: PYTHONPATH=src python scripts/wf_replay_grid_tsla.py [stage1.pkl]
"""
import json
import pickle
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import buy_class, recommended_buy_ratio, US_STOCK  # noqa: E402

STAGE1_PKL = sys.argv[1] if len(sys.argv) > 1 else "D:/chanlun_pro/reports/wf_confirm_tsla_stage1.pkl"
OUT_JSON = "D:/chanlun_pro/reports/wf_replay_grid_tsla.json"
COMMISSION = US_STOCK.commission

N_BUYS = (0, 4, 8, 12, 16, 24, 36)
N_SELLS = (0, 1)
BUY_CLASSES = (None, frozenset({3}), frozenset({1, 3}), frozenset({1}))
SELL_CLASSES = (None, frozenset({1, 2}))
GATE_MODES = ("not_down", "up")


def run_exec(buy_events, sell_bars, gate, opens, closes, *, gate_mode="not_down"):
    n = len(opens)
    cash, shares, entry_px = 1.0, 0.0, 0.0
    trades = []
    pending = None
    peak, max_dd = 1.0, 0.0
    wins = 0
    for i in range(n):
        if pending is not None:
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
                r = px / entry_px - 1
                trades.append(r)
                wins += r > 0
                shares = 0.0
            pending = None
        eq = cash + shares * closes[i]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        gate_down = gate[i] == "down"
        can_buy = (gate[i] == "up") if gate_mode == "up" else (not gate_down)
        if shares > 0.0 and (i in sell_bars or gate_down):
            pending = ("sell", "exit")
        elif shares == 0.0 and i in buy_events and can_buy:
            pending = ("buy", buy_events[i])
    if shares > 0.0:
        cash += shares * closes[-1] * (1 - COMMISSION)
        r = closes[-1] / entry_px - 1
        trades.append(r)
        wins += r > 0
    nt = len(trades)
    return {"ret": cash - 1.0, "max_dd": max_dd, "trades": nt,
            "win": (wins / nt) if nt else 0.0}


def build_events(eps, n, n_buy, n_sell, buy_cls, sell_cls):
    buy_events, sell_bars = {}, set()
    for ep in eps:
        c_ = buy_class(ep["bs_type"])
        if ep["side"] == "buy":
            if buy_cls is not None and c_ not in buy_cls:
                continue
            c = ep["first_seen"] + n_buy
            if c >= n or ep["alive_until"] < c:
                continue
            prev = buy_events.get(c)
            if prev is None or buy_class(ep["bs_type"]) > buy_class(prev):
                buy_events[c] = ep["bs_type"]
        else:
            if sell_cls is not None and c_ not in sell_cls:
                continue
            c = ep["first_seen"] + n_sell
            if c >= n or ep["alive_until"] < c:
                continue
            sell_bars.add(c)
    return buy_events, sell_bars


def main() -> int:
    st = pickle.loads(Path(STAGE1_PKL).read_bytes())
    eps, n, gate = st["episodes"], st["n"], st["gate"]
    opens, closes = st["opens"], st["closes"]
    bh = closes[-1] / opens[st["warmup"]] - 1
    print(f"TSLA {st['span']} bars={n} episodes={len(eps)} 裸持={bh:+.1%}")
    rows = []
    for nb in N_BUYS:
        for ns in N_SELLS:
            for bc in BUY_CLASSES:
                for sc in SELL_CLASSES:
                    be, sb = build_events(eps, n, nb, ns, bc, sc)
                    for gm in GATE_MODES:
                        r = run_exec(be, sb, gate, opens, closes, gate_mode=gm)
                        rows.append({
                            "n_buy": nb, "n_sell": ns,
                            "buy_cls": "all" if bc is None else "".join(map(str, sorted(bc))),
                            "sell_cls": "all" if sc is None else "".join(map(str, sorted(sc))),
                            "gate": gm, "ret": round(r["ret"], 4),
                            "max_dd": round(r["max_dd"], 4), "trades": r["trades"],
                            "win": round(r["win"], 3),
                        })
    # 评分=收益-2×回撤(项目统一口径)
    for r in rows:
        r["score"] = round(r["ret"] - 2 * r["max_dd"], 4)
    rows.sort(key=lambda x: -x["score"])
    hdr = f"{'Nb':>3} {'Ns':>3} {'买类':>4} {'卖类':>4} {'门控':>8} {'收益':>8} {'回撤':>7} {'笔':>4} {'胜率':>5} {'评分':>7}"
    print(hdr)
    for r in rows[:25]:
        print(f"{r['n_buy']:>3} {r['n_sell']:>3} {r['buy_cls']:>4} {r['sell_cls']:>4} "
              f"{r['gate']:>8} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
              f"{r['win']:>5.0%} {r['score']:>+7.3f}")
    print("  ...")
    for r in rows[-5:]:
        print(f"{r['n_buy']:>3} {r['n_sell']:>3} {r['buy_cls']:>4} {r['sell_cls']:>4} "
              f"{r['gate']:>8} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
              f"{r['win']:>5.0%} {r['score']:>+7.3f}")
    Path(OUT_JSON).write_text(json.dumps({"buy_hold": round(bh, 4), "span": st["span"],
                                          "rows": rows}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"-> {OUT_JSON} ({len(rows)} 配置)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
