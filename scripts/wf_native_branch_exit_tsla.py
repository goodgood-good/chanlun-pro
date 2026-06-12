# -*- coding: utf-8 -*-
"""Walk-forward replay: 5m native course-38 entries plus branch-core sell exits.

The entry stream stays the no-lookahead 5m course-38 program.  Extra exits come
from wf_confirm_scan stage1 episodes, i.e. sell signals that were first visible
on a specific 5m close during the incremental replay.
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.recursive_bt.engine import US_STOCK, buy_class, wf_seg38_series  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wf_backtest_tsla as _wbt  # noqa: E402
from wf_backtest_tsla import _load  # noqa: E402


COMMISSION = US_STOCK.commission
WARMUP = 400
CODE = "TSLA.US"
EVENT_PKL = "D:/chanlun_pro/reports/wf_native_tsla_events.pkl"
STAGE1_PKL = "D:/chanlun_pro/reports/wf_confirm_tsla_stage1.pkl"
OUT_JSON = "D:/chanlun_pro/reports/wf_native_tsla_branch_exit.json"

for _i, _a in enumerate(list(sys.argv)):
    if _a == "--dir" and _i + 1 < len(sys.argv):
        _wbt.DIR = sys.argv[_i + 1]
    elif _a == "--prefix" and _i + 1 < len(sys.argv):
        _wbt.PREFIX = sys.argv[_i + 1]
        CODE = sys.argv[_i + 1].replace("us_", "").replace("_US", "") + ".US"
    elif _a == "--tag" and _i + 1 < len(sys.argv):
        _t = sys.argv[_i + 1]
        EVENT_PKL = f"D:/chanlun_pro/reports/wf_native_{_t}_events.pkl"
        STAGE1_PKL = f"D:/chanlun_pro/reports/wf_confirm_{_t}_stage1.pkl"
        OUT_JSON = f"D:/chanlun_pro/reports/wf_native_{_t}_branch_exit.json"


def _native_5m_events(df5: pd.DataFrame) -> list[tuple[pd.Timestamp, str, str]]:
    ev_pkl = Path(EVENT_PKL)
    if ev_pkl.exists():
        cached = pickle.loads(ev_pkl.read_bytes())
        ev5 = cached.get("seg38_5m")
        if ev5 is None:
            ev5 = wf_seg38_series(df5, CODE, "5m")
    else:
        ev5 = wf_seg38_series(df5, CODE, "5m")
    return [(t, act, "seg38") for t, act in ev5]


def _stage1_sell_events(st: dict, sell_classes: set[int] | None, n_sell: int) -> list[tuple[pd.Timestamp, str, str]]:
    dates = pd.to_datetime(st["dates"])
    n = int(st["n"])
    out = []
    for ep in st["episodes"]:
        if ep["side"] != "sell":
            continue
        cls = buy_class(ep["bs_type"])
        if sell_classes is not None and cls not in sell_classes:
            continue
        i = int(ep["first_seen"]) + int(n_sell)
        if i >= n or int(ep["alive_until"]) < i:
            continue
        out.append((dates[i], "sell", f"branch_{ep['bs_type']}_n{n_sell}"))
    return out


def _map_events_to_bars(events, dates):
    d2i = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    acts = []
    import bisect
    for event in events:
        t, act, reason = event
        i = d2i.get(t)
        if i is None:
            i = bisect.bisect_left(dates, t)
            if i >= n:
                continue
        acts.append((i, act, reason))
    acts.sort(key=lambda x: (x[0], 0 if x[1] == "sell" else 1))
    return acts


def run_events(events, df5: pd.DataFrame) -> dict:
    dates = list(df5["date"])
    opens = df5["open"].to_numpy()
    closes = df5["close"].to_numpy()
    acts = _map_events_to_bars(events, dates)
    cash, shares, entry_px = 1.0, 0.0, 0.0
    peak, mdd = 1.0, 0.0
    pending = None
    ai = 0
    trades = []

    for i in range(len(dates)):
        if pending is not None:
            act, reason = pending
            px = opens[i]
            if act == "buy" and shares == 0.0 and px > 0:
                shares = cash * 0.99 / (px * (1 + COMMISSION))
                cash -= shares * px * (1 + COMMISSION)
                entry_px = px
            elif act == "sell" and shares > 0.0 and px > 0:
                cash += shares * px * (1 - COMMISSION)
                trades.append({
                    "ret": px / entry_px - 1,
                    "reason": reason,
                })
                shares = 0.0
            pending = None

        bar_events = []
        while ai < len(acts) and acts[ai][0] == i:
            bar_events.append(acts[ai])
            ai += 1
        if shares > 0.0:
            sell_reason = next((reason for _, act, reason in bar_events if act == "sell"), None)
            if sell_reason is not None:
                pending = ("sell", sell_reason)
        elif any(act == "buy" for _, act, _ in bar_events):
            pending = ("buy", "seg38")

        eq = cash + shares * closes[i]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)

    if shares > 0.0:
        cash += shares * closes[-1] * (1 - COMMISSION)
        trades.append({"ret": closes[-1] / entry_px - 1, "reason": "final"})

    wins = sum(1 for tr in trades if tr["ret"] > 0)
    branch_exits = sum(1 for tr in trades if str(tr["reason"]).startswith("branch_"))
    return {
        "ret": cash - 1.0,
        "max_dd": mdd,
        "trades": len(trades),
        "win_rate": wins / len(trades) if trades else 0.0,
        "branch_exits": branch_exits,
        "avg_trade_ret": sum(tr["ret"] for tr in trades) / len(trades) if trades else 0.0,
    }


def _validate_stage1(st: dict, df5: pd.DataFrame) -> None:
    st_dates = pd.to_datetime(st["dates"])
    if len(st_dates) != len(df5):
        raise ValueError(f"stage1 length mismatch: {len(st_dates)} != {len(df5)}")
    if str(st_dates[0]) != str(df5["date"].iloc[0]) or str(st_dates[-1]) != str(df5["date"].iloc[-1]):
        raise ValueError("stage1 date span does not match 5m data")


def main() -> int:
    df5 = _load("5m")
    st_path = Path(STAGE1_PKL)
    if not st_path.exists():
        raise FileNotFoundError(f"missing stage1 episodes: {st_path}")
    st = pickle.loads(st_path.read_bytes())
    _validate_stage1(st, df5)

    native_events = _native_5m_events(df5)
    variants = [
        ("seg38_only", None, None),
        ("seg38_plus_12sell_n0", {1, 2}, 0),
        ("seg38_plus_12sell_n1", {1, 2}, 1),
        ("seg38_plus_all_sell_n0", None, 0),
        ("seg38_plus_all_sell_n1", None, 1),
        ("seg38_plus_3sell_n0", {3}, 0),
    ]
    rows = []
    for name, sell_classes, n_sell in variants:
        events = list(native_events)
        extra = []
        if n_sell is not None:
            extra = _stage1_sell_events(st, sell_classes, n_sell)
            events.extend(extra)
        r = run_events(events, df5)
        r.update({
            "name": name,
            "extra_sell_events": len(extra),
            "sell_classes": "native" if sell_classes is None and n_sell is None else (
                "all" if sell_classes is None else "".join(str(x) for x in sorted(sell_classes))
            ),
            "n_sell": n_sell,
            "score_ret_minus_2dd": r["ret"] - 2 * r["max_dd"],
        })
        rows.append(r)

    bh = df5["close"].iloc[-1] / df5["open"].iloc[WARMUP] - 1
    rows_sorted = sorted(rows, key=lambda r: (r["ret"], -r["max_dd"]), reverse=True)
    print(f"{CODE} {df5['date'].iloc[0].date()}~{df5['date'].iloc[-1].date()} buy_hold={bh:+.1%}")
    print(f"{'variant':>24} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5} {'br_exit':>7} {'extra':>6}")
    for r in rows_sorted:
        print(f"{r['name']:>24} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
              f"{r['win_rate']:>5.0%} {r['branch_exits']:>7} {r['extra_sell_events']:>6}")

    out = {
        "code": CODE,
        "span": f"{df5['date'].iloc[0].date()}~{df5['date'].iloc[-1].date()}",
        "buy_hold": bh,
        "stage1": str(st_path),
        "rows": rows_sorted,
    }
    Path(OUT_JSON).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
