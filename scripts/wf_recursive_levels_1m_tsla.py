# -*- coding: utf-8 -*-
"""Walk-forward replay from 1m recursive Chanlun L1/L2 signals.

This is the strict "cascade" experiment:

- feed only raw 1m klines into one CL("*.US", "1m") object;
- after each visible 1m close, read cd.get_kuozhan_levels() through
  collect_signals();
- L1 is the derived 5m level, L2 is the derived 30m level;
- a signal can trigger only when it first appears in the current live snapshot;
- orders are decided after the 1m close and filled at the next 1m open.

It is deliberately separate from tsla_live_walk_forward_replay.py, whose
multi-timeframe mode computes each frequency branch independently.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import pickle
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.core.cl import CL  # noqa: E402
from chanlun.recursive_bt.engine import (  # noqa: E402
    CL_CFG,
    US_STOCK,
    buy_class,
    collect_signals,
    recommended_buy_ratio,
)


DEFAULT_DIR = "D:/chanlun_pro/chart_cache_us_tsla_1y"
DEFAULT_PREFIX = "us_TSLA_US"
DEFAULT_TAG = "tsla"
DEFAULT_REPORT_DIR = "D:/chanlun_pro/reports"
COMMISSION = US_STOCK.commission
LEVEL_FREQ = {1: "5m", 2: "30m"}
EVENT_CACHE_VERSION = 1


def _code_from_prefix(prefix: str) -> str:
    return prefix.replace("us_", "").replace("_US", "") + ".US"


def _latest_cache_file(cache_dir: str, prefix: str, freq: str) -> Path:
    files = glob.glob(str(Path(cache_dir) / f"v*_{prefix}_{freq}_recursivebt.pkl"))
    if not files:
        files = glob.glob(str(Path(cache_dir) / f"v*_{prefix}_{freq}_recursivebt*.pkl"))
    if not files:
        raise FileNotFoundError(f"missing {freq} cache for {prefix} under {cache_dir}")

    def version(path: str) -> int:
        name = Path(path).name
        if not name.startswith("v"):
            return 0
        try:
            return int(name.split("_", 1)[0][1:])
        except Exception:
            return 0

    return Path(max(files, key=version))


def _load_1m(cache_dir: str, prefix: str) -> pd.DataFrame:
    data = pickle.loads(_latest_cache_file(cache_dir, prefix, "1m").read_bytes())["data"]
    cols: dict[str, Any] = {
        "date": pd.to_datetime(data["t"], unit="s"),
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
    }
    if "v" in data:
        cols["volume"] = data["v"]
    return pd.DataFrame(cols).sort_values("date").reset_index(drop=True)


def _coerce_ts(value: str | None, ref: pd.Timestamp) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.Timestamp(value)
    if ref.tzinfo is None:
        return ts.tz_convert(None) if ts.tzinfo is not None else ts
    return ts.tz_localize(ref.tzinfo) if ts.tzinfo is None else ts.tz_convert(ref.tzinfo)


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)


def _signal_id(sig) -> tuple:
    return (
        int(sig.level or 0),
        str(sig.bs_type),
        str(pd.Timestamp(sig.date)),
        round(float(sig.price), 6),
    )


def _event_from_signal(sig, bar: int, visible_time: pd.Timestamp, trade_start_idx: int) -> dict[str, Any]:
    side = "buy" if sig.is_buy else "sell" if sig.is_sell else "other"
    return {
        "bar": int(bar),
        "visible_time": str(visible_time),
        "anchor_time": str(pd.Timestamp(sig.date)),
        "level": int(sig.level or 0),
        "freq": LEVEL_FREQ.get(int(sig.level or 0), f"L{sig.level}"),
        "bs_type": str(sig.bs_type),
        "side": side,
        "price": float(sig.price),
        "in_trade_window": bool(bar >= trade_start_idx),
    }


def _infer_big_dir(signals) -> tuple[str, dict[str, Any] | None]:
    l2 = [s for s in signals if int(s.level or 0) == 2 and (s.is_buy or s.is_sell)]
    if not l2:
        return "neutral", None
    last = max(l2, key=lambda s: pd.Timestamp(s.date))
    direction = "up" if last.is_buy else "down"
    return direction, {
        "anchor_time": str(pd.Timestamp(last.date)),
        "bs_type": str(last.bs_type),
        "price": float(last.price),
    }


def _scan_recursive_events(
    df: pd.DataFrame,
    code: str,
    trade_start_idx: int,
    signal_warmup_bars: int,
    progress_every: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Incrementally scan 1m CL snapshots and emit fresh recursive L1/L2 signals."""
    cd = CL(code, "1m", dict(CL_CFG))
    n = len(df)
    initial_bars = max(0, min(int(signal_warmup_bars), trade_start_idx, n))
    alive_prev: set[tuple] = set()
    initial_big_dir = "neutral"
    initial_big_signal = None

    if initial_bars:
        cd.process_klines(df.iloc[:initial_bars].reset_index(drop=True))
        initial_snapshot = collect_signals(cd)
        alive_prev = {_signal_id(s) for s in initial_snapshot if int(s.level or 0) in (1, 2)}
        initial_big_dir, initial_big_signal = _infer_big_dir(initial_snapshot)

    events: list[dict[str, Any]] = []
    start_clock = time.time()
    for i in range(initial_bars, n):
        cd.process_klines(df.iloc[i:i + 1].reset_index(drop=True))
        visible_time = pd.Timestamp(df["date"].iloc[i])
        current_ids: set[tuple] = set()
        for sig in collect_signals(cd):
            level = int(sig.level or 0)
            if level not in (1, 2) or (not sig.is_buy and not sig.is_sell):
                continue
            sid = _signal_id(sig)
            if sid in current_ids:
                continue
            current_ids.add(sid)
            if sid not in alive_prev:
                events.append(_event_from_signal(sig, i, visible_time, trade_start_idx))
        alive_prev = current_ids
        if progress_every and (i + 1) % progress_every == 0:
            elapsed = time.time() - start_clock
            print(f"scanned {i + 1}/{n} bars, events={len(events)}, elapsed={elapsed:.1f}s", flush=True)

    stats = {
        "bars": n,
        "initial_bars": initial_bars,
        "warmup_requested_bars": int(signal_warmup_bars),
        "warmup_satisfied": bool(initial_bars >= int(signal_warmup_bars)),
        "initial_big_dir": initial_big_dir,
        "initial_big_signal": initial_big_signal,
        "scan_seconds": round(time.time() - start_clock, 3),
    }
    return events, stats


def _pick_buy(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    buys = [e for e in events if e["side"] == "buy"]
    if not buys:
        return None
    return max(buys, key=lambda e: (buy_class(e["bs_type"]), e["price"]))


def _pick_sell(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    sells = [e for e in events if e["side"] == "sell"]
    if not sells:
        return None
    return max(sells, key=lambda e: buy_class(e["bs_type"]))


def _event_counts(events: list[dict[str, Any]], trade_only: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        if trade_only and not e["in_trade_window"]:
            continue
        key = f"L{e['level']}_{e['side']}_{e['bs_type']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _event_cache_meta(
    code: str,
    df: pd.DataFrame,
    trade_start_idx: int,
    trade_start: pd.Timestamp,
    signal_warmup_bars: int,
) -> dict[str, Any]:
    return {
        "version": EVENT_CACHE_VERSION,
        "code": code,
        "freq": "1m",
        "bars": int(len(df)),
        "first": str(pd.Timestamp(df["date"].iloc[0])),
        "last": str(pd.Timestamp(df["date"].iloc[-1])),
        "trade_start_requested": str(pd.Timestamp(trade_start)),
        "trade_start_idx": int(trade_start_idx),
        "trade_start_bar": str(pd.Timestamp(df["date"].iloc[trade_start_idx])),
        "signal_warmup_bars": int(signal_warmup_bars),
    }


def _load_event_cache(path: str, meta: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    cached = pickle.loads(p.read_bytes())
    if cached.get("meta") != meta:
        return None
    return cached["events"], cached["scan"]


def _write_event_cache(
    path: str,
    meta: dict[str, Any],
    events: list[dict[str, Any]],
    scan_stats: dict[str, Any],
) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(pickle.dumps({
        "meta": meta,
        "events": events,
        "scan": scan_stats,
    }))


def _run_variant(
    name: str,
    df: pd.DataFrame,
    events: list[dict[str, Any]],
    trade_start_idx: int,
    initial_big_dir: str,
    size_mode: str,
) -> dict[str, Any]:
    by_bar: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        by_bar.setdefault(int(e["bar"]), []).append(e)

    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    dates = list(df["date"])
    cash, shares, entry_px = 1.0, 0.0, 0.0
    entry_time = None
    entry_bs = ""
    peak, max_dd = 1.0, 0.0
    pending: tuple[str, str, str] | None = None
    big_dir = initial_big_dir
    trades: list[dict[str, Any]] = []
    l2_dir_changes = 0
    l1_buy_seen = 0
    l1_sell_seen = 0

    for i in range(len(df)):
        if pending is not None:
            act, bs_type, reason = pending
            px = float(opens[i])
            if act == "buy" and shares == 0.0 and px > 0:
                if size_mode == "full":
                    ratio = 1.0
                else:
                    ratio = recommended_buy_ratio(
                        bs_type,
                        1,
                        big_dir=big_dir,
                        trend_boost=True,
                    )
                budget = cash * min(float(ratio), 1.0) * 0.99
                size = budget / (px * (1 + COMMISSION))
                if size > 0:
                    cash -= size * px * (1 + COMMISSION)
                    shares = size
                    entry_px = px
                    entry_time = str(dates[i])
                    entry_bs = bs_type
            elif act == "sell" and shares > 0.0 and px > 0:
                cash += shares * px * (1 - COMMISSION)
                ret = px / entry_px - 1 if entry_px > 0 else 0.0
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": str(dates[i]),
                    "entry_bs": entry_bs,
                    "exit_reason": reason,
                    "entry_price": entry_px,
                    "exit_price": px,
                    "ret": ret,
                })
                shares = 0.0
                entry_px = 0.0
                entry_time = None
                entry_bs = ""
            pending = None

        bar_events = by_bar.get(i, [])
        l2_events = [e for e in bar_events if e["level"] == 2]
        for e in l2_events:
            new_dir = "up" if e["side"] == "buy" else "down"
            if new_dir != big_dir:
                l2_dir_changes += 1
            big_dir = new_dir

        l1_events = [e for e in bar_events if e["level"] == 1 and e["in_trade_window"]]
        l1_buy = _pick_buy(l1_events)
        l1_sell = _pick_sell(l1_events)
        l1_buy_seen += 1 if l1_buy is not None else 0
        l1_sell_seen += 1 if l1_sell is not None else 0

        l2_down_now = any(e["side"] == "sell" for e in l2_events)
        gate_down = big_dir == "down"
        gate_up = big_dir == "up"

        if i >= trade_start_idx:
            if shares > 0.0:
                sell_reason = None
                if l1_sell is not None:
                    sell_reason = f"L1_{l1_sell['bs_type']}"
                if name in {"l1_l2_not_down", "l1_l2_up"} and (l2_down_now or gate_down):
                    sell_reason = "L2_down"
                if sell_reason is not None:
                    pending = ("sell", sell_reason, sell_reason)
            elif shares == 0.0 and l1_buy is not None:
                gate_ok = True
                if name == "l1_l2_not_down":
                    gate_ok = not gate_down
                elif name == "l1_l2_up":
                    gate_ok = gate_up
                if gate_ok:
                    pending = ("buy", l1_buy["bs_type"], f"L1_{l1_buy['bs_type']}")

        eq = cash + shares * float(closes[i])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)

    if shares > 0.0:
        px = float(closes[-1])
        cash += shares * px * (1 - COMMISSION)
        ret = px / entry_px - 1 if entry_px > 0 else 0.0
        trades.append({
            "entry_time": entry_time,
            "exit_time": str(dates[-1]),
            "entry_bs": entry_bs,
            "exit_reason": "final",
            "entry_price": entry_px,
            "exit_price": px,
            "ret": ret,
        })

    wins = sum(1 for tr in trades if tr["ret"] > 0)
    return {
        "name": name,
        "ret": cash - 1.0,
        "max_dd": max_dd,
        "trades": len(trades),
        "win_rate": wins / len(trades) if trades else 0.0,
        "avg_trade_ret": sum(tr["ret"] for tr in trades) / len(trades) if trades else 0.0,
        "score_ret_minus_2dd": cash - 1.0 - 2 * max_dd,
        "l1_buy_signal_bars": l1_buy_seen,
        "l1_sell_signal_bars": l1_sell_seen,
        "l2_dir_changes": l2_dir_changes,
        "sample_trades": trades[:20],
    }


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1m recursive L1/L2 walk-forward replay")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="chart-cache directory")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="chart-cache symbol prefix")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="report tag")
    parser.add_argument("--out-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--window-days", type=int, default=5)
    parser.add_argument("--warmup-days", type=int, default=20)
    parser.add_argument("--signal-warmup-bars", type=int, default=1000)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--size-mode", choices=("ratio", "full"), default="ratio")
    parser.add_argument("--event-cache", help="optional pickle cache for recursive L1/L2 visible events")
    parser.add_argument("--force-rescan", action="store_true", help="ignore event cache and rebuild from 1m bars")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    code = _code_from_prefix(args.prefix)
    df_all = _load_1m(args.dir, args.prefix)
    if df_all.empty:
        raise SystemExit("empty 1m cache")

    ref = pd.Timestamp(df_all["date"].iloc[-1])
    end = _coerce_ts(args.end, ref) or ref
    trade_start = _coerce_ts(args.start, ref) or (end - pd.Timedelta(days=args.window_days))
    warm_start = trade_start - pd.Timedelta(days=args.warmup_days)
    df = _slice(df_all, warm_start, end)
    if df.empty:
        raise SystemExit("selected window has no 1m bars")

    dates = list(df["date"])
    trade_start_idx = bisect.bisect_left(dates, trade_start)
    if trade_start_idx >= len(df):
        raise SystemExit("trade_start is after selected data")
    if args.event_cache is None:
        args.event_cache = str(Path(args.out_dir) / f"wf_recursive_levels_{args.tag}_1m_events.pkl")

    print(
        f"{code} 1m recursive WF bars={len(df)} "
        f"warm={df['date'].iloc[0]} trade_start={df['date'].iloc[trade_start_idx]} end={df['date'].iloc[-1]}",
        flush=True,
    )
    event_cache_meta = _event_cache_meta(
        code,
        df,
        trade_start_idx,
        trade_start,
        args.signal_warmup_bars,
    )
    cached = None if args.force_rescan else _load_event_cache(args.event_cache, event_cache_meta)
    if cached is not None:
        events, scan_stats = cached
        scan_stats = dict(scan_stats)
        scan_stats["cache_hit"] = True
        scan_stats["event_cache"] = args.event_cache
        print(f"loaded recursive events from cache: {args.event_cache}", flush=True)
    else:
        events, scan_stats = _scan_recursive_events(
            df,
            code,
            trade_start_idx,
            args.signal_warmup_bars,
            args.progress_every,
        )
        scan_stats = dict(scan_stats)
        scan_stats["cache_hit"] = False
        scan_stats["event_cache"] = args.event_cache
        _write_event_cache(args.event_cache, event_cache_meta, events, scan_stats)

    variants = ["l1_only", "l1_l2_not_down", "l1_l2_up"]
    rows = [
        _run_variant(v, df, events, trade_start_idx, scan_stats["initial_big_dir"], args.size_mode)
        for v in variants
    ]
    rows_sorted = sorted(rows, key=lambda r: (r["ret"], -r["max_dd"]), reverse=True)
    bh = float(df["close"].iloc[-1]) / float(df["open"].iloc[trade_start_idx]) - 1

    print(f"events all={len(events)} trade={sum(1 for e in events if e['in_trade_window'])} "
          f"initial_L2={scan_stats['initial_big_dir']} buy_hold={bh:+.1%}")
    print(f"{'variant':>18} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5} {'l1b':>5} {'l1s':>5} {'l2chg':>6}")
    for r in rows_sorted:
        print(
            f"{r['name']:>18} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
            f"{r['win_rate']:>5.0%} {r['l1_buy_signal_bars']:>5} {r['l1_sell_signal_bars']:>5} "
            f"{r['l2_dir_changes']:>6}"
        )

    report = {
        "code": code,
        "span": f"{df['date'].iloc[0]}~{df['date'].iloc[-1]}",
        "window": {
            "warm_start": str(warm_start),
            "trade_start_requested": str(trade_start),
            "trade_start_bar": str(df["date"].iloc[trade_start_idx]),
            "trade_start_idx": int(trade_start_idx),
            "end": str(end),
            "bars": int(len(df)),
        },
        "no_future_policy": {
            "source": "raw 1m chart-cache klines",
            "signal_source": "CL(1m).get_kuozhan_levels() via collect_signals",
            "level_chain": "1m -> L1(5m, kuozhan) -> L2(30m, tongjibie)",
            "visibility": "fresh signal appears only after the current 1m bar is processed",
            "execution": "decision at 1m close, fill at next 1m open",
            "warmup": "pre-trade bars update CL state and L2 direction but cannot trade",
        },
        "buy_hold": bh,
        "scan": scan_stats,
        "event_cache_meta": event_cache_meta,
        "event_counts_all": _event_counts(events),
        "event_counts_trade": _event_counts(events, trade_only=True),
        "events": events,
        "rows": rows_sorted,
        "sample_events": events[:30],
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"wf_recursive_levels_{args.tag}_1m.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
