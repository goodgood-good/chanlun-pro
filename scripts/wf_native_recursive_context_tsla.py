# -*- coding: utf-8 -*-
"""Replay 5m course-38 trades with 1m recursive L1 context.

The execution clock is 1m:
- native course-38 5m events are visible after their 5m bar closes and fill next 1m;
- recursive L1 events are visible at their recorded 1m visible_time and fill next 1m;
- no precomputed future signals are introduced.  The recursive report must come
  from wf_recursive_levels_1m_tsla.py, which records visible-time episodes.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.recursive_bt.engine import US_STOCK, wf_seg38_series  # noqa: E402


DEFAULT_DIR = "D:/chanlun_pro/chart_cache_us_tsla_1y"
DEFAULT_PREFIX = "us_TSLA_US"
DEFAULT_RECURSIVE_REPORT = "D:/chanlun_pro/reports/wf_recursive_levels_tsla_15d_1m.json"
DEFAULT_OUT = "D:/chanlun_pro/reports/wf_native_recursive_context_tsla_15d.json"
DEFAULT_REPORT_DIR = "D:/chanlun_pro/reports"
COMMISSION = US_STOCK.commission


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


def _load_klines(cache_dir: str, prefix: str, freq: str) -> pd.DataFrame:
    data = pickle.loads(_latest_cache_file(cache_dir, prefix, freq).read_bytes())["data"]
    return pd.DataFrame({
        "date": pd.to_datetime(data["t"], unit="s"),
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
    }).sort_values("date").reset_index(drop=True)


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)


def _bar_index(dates: list[pd.Timestamp], t: pd.Timestamp) -> int | None:
    i = bisect.bisect_left(dates, t)
    if i >= len(dates):
        return None
    return i


def _freq_delta(freq: str) -> pd.Timedelta:
    value = str(freq).strip().lower()
    if value.endswith("m"):
        return pd.Timedelta(minutes=int(value[:-1]))
    if value == "d":
        return pd.Timedelta(days=1)
    raise ValueError(f"unsupported frequency: {freq!r}")


def _native_events_to_1m(
    events,
    dates1: list[pd.Timestamp],
    event_freq: str = "5m",
    main_freq: str = "1m",
) -> list[dict[str, Any]]:
    visible_offset = max(_freq_delta(event_freq) - _freq_delta(main_freq), pd.Timedelta(0))
    out = []
    for t, side in events:
        visible_time = pd.Timestamp(t) + visible_offset
        i = _bar_index(dates1, visible_time)
        if i is None:
            continue
        out.append({
            "bar": i,
            "side": side,
            "source": "seg38_5m",
            "reason": f"seg38_{side}",
            "visible_time": str(visible_time),
            "anchor_time": str(pd.Timestamp(t)),
        })
    return out


def _recursive_events_to_1m(report: dict[str, Any], dates1: list[pd.Timestamp]) -> list[dict[str, Any]]:
    events = report.get("events")
    if events is None:
        cache_path = (report.get("scan") or {}).get("event_cache")
        if cache_path and Path(cache_path).exists():
            events = pickle.loads(Path(cache_path).read_bytes()).get("events")
    if events is None:
        events = report.get("sample_events") or []
    out = []
    for e in events:
        if int(e.get("level") or 0) != 1:
            continue
        side = str(e.get("side"))
        if side not in {"buy", "sell"}:
            continue
        i = _bar_index(dates1, pd.Timestamp(e["visible_time"]))
        if i is None:
            continue
        out.append({
            "bar": i,
            "side": side,
            "source": "recursive_L1",
            "reason": f"L1_{e.get('bs_type')}",
            "bs_type": e.get("bs_type"),
            "visible_time": e.get("visible_time"),
            "anchor_time": e.get("anchor_time"),
            "in_trade_window": bool(e.get("in_trade_window")),
        })
    return out


def _native_cache_path(code: str) -> str:
    tag = code.split(".", 1)[0].lower()
    return str(Path(DEFAULT_REPORT_DIR) / f"wf_native_{tag}_events.pkl")


def _load_or_build_native_events(
    df5: pd.DataFrame,
    code: str,
    cache_path: str,
    force: bool = False,
):
    p = Path(cache_path)
    meta = {
        "code": code,
        "freq": "5m",
        "n": int(len(df5)),
        "first": str(pd.Timestamp(df5["date"].iloc[0])),
        "last": str(pd.Timestamp(df5["date"].iloc[-1])),
    }
    if p.exists() and not force:
        cached = pickle.loads(p.read_bytes())
        if cached.get("meta") == meta and "seg38_5m" in cached:
            return cached["seg38_5m"], True, str(p)
        # Backward-compatible cache created by wf_chanlun_native_tsla.py.
        if "seg38_5m" in cached and "meta" not in cached:
            return cached["seg38_5m"], True, str(p)
    ev5 = wf_seg38_series(df5, code, "5m")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(pickle.dumps({
        "meta": meta,
        "seg38_5m": ev5,
    }))
    return ev5, False, str(p)


def _run_variant(
    name: str,
    df1: pd.DataFrame,
    native_events: list[dict[str, Any]],
    recursive_events: list[dict[str, Any]],
    trade_start_idx: int,
) -> dict[str, Any]:
    by_bar: dict[int, list[dict[str, Any]]] = {}
    for e in native_events:
        by_bar.setdefault(int(e["bar"]), []).append(e)
    for e in recursive_events:
        by_bar.setdefault(int(e["bar"]), []).append(e)

    opens = df1["open"].to_numpy()
    closes = df1["close"].to_numpy()
    dates = list(df1["date"])
    cash, shares, entry_px = 1.0, 0.0, 0.0
    entry_time = ""
    peak, max_dd = 1.0, 0.0
    pending: tuple[str, str] | None = None
    trades: list[dict[str, Any]] = []
    ctx = "neutral"
    ctx_changes = 0
    recursive_exits = 0
    blocked_buys = 0

    for i in range(len(df1)):
        if pending is not None:
            act, reason = pending
            px = float(opens[i])
            if act == "buy" and shares == 0.0 and px > 0:
                shares = cash * 0.99 / (px * (1 + COMMISSION))
                cash -= shares * px * (1 + COMMISSION)
                entry_px = px
                entry_time = str(dates[i])
            elif act == "sell" and shares > 0.0 and px > 0:
                cash += shares * px * (1 - COMMISSION)
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": str(dates[i]),
                    "entry_price": entry_px,
                    "exit_price": px,
                    "ret": px / entry_px - 1 if entry_px > 0 else 0.0,
                    "reason": reason,
                })
                shares = 0.0
                entry_px = 0.0
                entry_time = ""
            pending = None

        bar_events = by_bar.get(i, [])
        rec = [e for e in bar_events if e["source"] == "recursive_L1"]
        rec_buy = any(e["side"] == "buy" for e in rec)
        rec_sell = any(e["side"] == "sell" for e in rec)
        if rec_buy and rec_sell:
            new_ctx = "neutral"
        elif rec_buy:
            new_ctx = "up"
        elif rec_sell:
            new_ctx = "down"
        else:
            new_ctx = ctx
        if new_ctx != ctx:
            ctx_changes += 1
            ctx = new_ctx

        if i >= trade_start_idx:
            native_buy = any(e["source"] == "seg38_5m" and e["side"] == "buy" for e in bar_events)
            native_sell = any(e["source"] == "seg38_5m" and e["side"] == "sell" for e in bar_events)
            rec_exit = rec_sell and name in {
                "seg38_plus_l1_sell",
                "seg38_l1_not_down",
                "seg38_l1_up",
            }

            if shares > 0.0:
                if native_sell:
                    pending = ("sell", "seg38_sell")
                elif rec_exit:
                    recursive_exits += 1
                    pending = ("sell", "recursive_L1_sell")
            elif shares == 0.0 and native_buy:
                can_buy = True
                if name == "seg38_l1_not_down":
                    can_buy = ctx != "down"
                elif name == "seg38_l1_up":
                    can_buy = ctx == "up"
                if can_buy:
                    pending = ("buy", "seg38_buy")
                else:
                    blocked_buys += 1

        eq = cash + shares * float(closes[i])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)

    if shares > 0.0:
        px = float(closes[-1])
        cash += shares * px * (1 - COMMISSION)
        trades.append({
            "entry_time": entry_time,
            "exit_time": str(dates[-1]),
            "entry_price": entry_px,
            "exit_price": px,
            "ret": px / entry_px - 1 if entry_px > 0 else 0.0,
            "reason": "final",
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
        "ctx_changes": ctx_changes,
        "recursive_exits": recursive_exits,
        "blocked_buys": blocked_buys,
        "sample_trades": trades[:20],
    }


def _sleeve_grid() -> list[tuple[float, float]]:
    core_fracs = (0.0, 0.25, 0.33, 0.50, 0.67, 0.75, 1.0)
    active_fracs = (0.0, 0.25, 0.33, 0.50, 0.67, 0.75, 1.0)
    pairs: list[tuple[float, float]] = []
    for core in core_fracs:
        for active in active_fracs:
            if core == 0.0 and active == 0.0:
                continue
            if core + active <= 1.0000001:
                pairs.append((core, active))
    return pairs


def _run_sleeve_variant(
    name: str,
    df1: pd.DataFrame,
    native_events: list[dict[str, Any]],
    recursive_events: list[dict[str, Any]],
    trade_start_idx: int,
    core_fraction: float,
    active_fraction: float,
) -> dict[str, Any]:
    if core_fraction < 0 or active_fraction < 0 or core_fraction + active_fraction > 1.0000001:
        raise ValueError("core_fraction + active_fraction must be in [0, 1]")

    by_bar: dict[int, list[dict[str, Any]]] = {}
    for e in native_events:
        by_bar.setdefault(int(e["bar"]), []).append(e)
    for e in recursive_events:
        by_bar.setdefault(int(e["bar"]), []).append(e)

    opens = df1["open"].to_numpy()
    closes = df1["close"].to_numpy()
    dates = list(df1["date"])

    idle_cash = 1.0 - core_fraction - active_fraction
    if abs(idle_cash) < 1e-9:
        idle_cash = 0.0
    core_cash, core_sh = core_fraction, 0.0
    active_cash, active_sh, active_entry = active_fraction, 0.0, 0.0
    entry_time = ""
    peak, max_dd = 1.0, 0.0
    pending: tuple[str, str] | None = None
    trades: list[dict[str, Any]] = []
    ctx = "neutral"
    ctx_changes = 0
    recursive_exits = 0
    blocked_buys = 0

    for i in range(len(df1)):
        px_open = float(opens[i])
        if i == trade_start_idx and core_cash > 0.0 and px_open > 0:
            core_sh = core_cash / (px_open * (1 + COMMISSION))
            core_cash = 0.0

        if pending is not None:
            act, reason = pending
            if act == "buy" and active_sh == 0.0 and active_cash > 0.0 and px_open > 0:
                active_sh = active_cash * 0.99 / (px_open * (1 + COMMISSION))
                active_cash -= active_sh * px_open * (1 + COMMISSION)
                active_entry = px_open
                entry_time = str(dates[i])
            elif act == "sell" and active_sh > 0.0 and px_open > 0:
                active_cash += active_sh * px_open * (1 - COMMISSION)
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": str(dates[i]),
                    "entry_price": active_entry,
                    "exit_price": px_open,
                    "ret": px_open / active_entry - 1 if active_entry > 0 else 0.0,
                    "reason": reason,
                })
                active_sh = 0.0
                active_entry = 0.0
                entry_time = ""
            pending = None

        bar_events = by_bar.get(i, [])
        rec = [e for e in bar_events if e["source"] == "recursive_L1"]
        rec_buy = any(e["side"] == "buy" for e in rec)
        rec_sell = any(e["side"] == "sell" for e in rec)
        if rec_buy and rec_sell:
            new_ctx = "neutral"
        elif rec_buy:
            new_ctx = "up"
        elif rec_sell:
            new_ctx = "down"
        else:
            new_ctx = ctx
        if new_ctx != ctx:
            ctx_changes += 1
            ctx = new_ctx

        if i >= trade_start_idx:
            native_buy = any(e["source"] == "seg38_5m" and e["side"] == "buy" for e in bar_events)
            native_sell = any(e["source"] == "seg38_5m" and e["side"] == "sell" for e in bar_events)
            rec_exit = rec_sell and name in {
                "seg38_plus_l1_sell",
                "seg38_l1_not_down",
                "seg38_l1_up",
            }

            if active_sh > 0.0:
                if native_sell:
                    pending = ("sell", "seg38_sell")
                elif rec_exit:
                    recursive_exits += 1
                    pending = ("sell", "recursive_L1_sell")
            elif active_sh == 0.0 and native_buy:
                can_buy = active_cash > 0.0
                if name == "seg38_l1_not_down":
                    can_buy = can_buy and ctx != "down"
                elif name == "seg38_l1_up":
                    can_buy = can_buy and ctx == "up"
                if can_buy:
                    pending = ("buy", "seg38_buy")
                else:
                    blocked_buys += 1

        eq = idle_cash + core_cash + core_sh * float(closes[i]) + active_cash + active_sh * float(closes[i])
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)

    final_px = float(closes[-1])
    final_eq = idle_cash + core_cash + active_cash
    if core_sh > 0.0:
        final_eq += core_sh * final_px * (1 - COMMISSION)
    if active_sh > 0.0:
        final_eq += active_sh * final_px * (1 - COMMISSION)
        trades.append({
            "entry_time": entry_time,
            "exit_time": str(dates[-1]),
            "entry_price": active_entry,
            "exit_price": final_px,
            "ret": final_px / active_entry - 1 if active_entry > 0 else 0.0,
            "reason": "final",
        })

    wins = sum(1 for tr in trades if tr["ret"] > 0)
    return {
        "variant": name,
        "core_fraction": round(core_fraction, 4),
        "active_fraction": round(active_fraction, 4),
        "idle_fraction": round(idle_cash, 4),
        "ret": final_eq - 1.0,
        "max_dd": max_dd,
        "trades": len(trades),
        "win_rate": wins / len(trades) if trades else 0.0,
        "score_ret_minus_2dd": final_eq - 1.0 - 2 * max_dd,
        "ctx_changes": ctx_changes,
        "recursive_exits": recursive_exits,
        "blocked_buys": blocked_buys,
    }


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="5m course-38 + 1m recursive context replay")
    parser.add_argument("--dir", default=DEFAULT_DIR)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--recursive-report", default=DEFAULT_RECURSIVE_REPORT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--allow-partial-recursive", action="store_true")
    parser.add_argument("--native-event-cache")
    parser.add_argument("--force-native-rescan", action="store_true")
    parser.add_argument("--sleeve-grid", action="store_true")
    parser.add_argument("--sleeve-variant", default="seg38_l1_up")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    code = _code_from_prefix(args.prefix)
    report = json.loads(Path(args.recursive_report).read_text(encoding="utf-8"))
    scan = report.get("scan") or {}
    if scan.get("is_partial") and not args.allow_partial_recursive:
        missing = scan.get("missing_chunks")
        raise SystemExit(
            f"recursive report is partial; missing chunks={missing}. "
            "Pass --allow-partial-recursive only for debugging partial coverage."
        )
    if scan.get("is_partial"):
        print(
            f"WARNING: recursive report is partial; missing chunks={scan.get('missing_chunks')}. "
            "Results are debugging-only.",
            flush=True,
        )
    warm_start = pd.Timestamp(report["window"]["warm_start"])
    trade_start = pd.Timestamp(report["window"]["trade_start_bar"])
    end = pd.Timestamp(report["window"]["end"])

    df1 = _slice(_load_klines(args.dir, args.prefix, "1m"), warm_start, end)
    df5 = _slice(_load_klines(args.dir, args.prefix, "5m"), warm_start, end)
    if df1.empty or df5.empty:
        raise SystemExit("selected window has no 1m/5m bars")
    dates1 = list(df1["date"])
    trade_start_idx = _bar_index(dates1, trade_start)
    if trade_start_idx is None:
        raise SystemExit("trade_start is outside 1m data")

    native_cache = args.native_event_cache or _native_cache_path(code)
    native_raw, native_cache_hit, native_cache_path = _load_or_build_native_events(
        df5,
        code,
        native_cache,
        force=args.force_native_rescan,
    )
    native_events = _native_events_to_1m(native_raw, dates1)
    recursive_events = _recursive_events_to_1m(report, dates1)
    variants = [
        "seg38_base",
        "seg38_plus_l1_sell",
        "seg38_l1_not_down",
        "seg38_l1_up",
    ]
    rows = [_run_variant(v, df1, native_events, recursive_events, trade_start_idx) for v in variants]
    rows_sorted = sorted(rows, key=lambda r: (r["ret"], -r["max_dd"]), reverse=True)
    buy_hold = float(df1["close"].iloc[-1]) / float(df1["open"].iloc[trade_start_idx]) - 1

    print(f"{code} {trade_start}~{end} buy_hold={buy_hold:+.1%}")
    print(f"{'variant':>20} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5} {'rex':>4} {'blk':>4}")
    for r in rows_sorted:
        print(
            f"{r['name']:>20} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
            f"{r['win_rate']:>5.0%} {r['recursive_exits']:>4} {r['blocked_buys']:>4}"
        )

    sleeve_rows = []
    if args.sleeve_grid:
        valid_variants = set(variants)
        if args.sleeve_variant not in valid_variants:
            raise SystemExit(f"--sleeve-variant must be one of {sorted(valid_variants)}")
        sleeve_rows = [
            _run_sleeve_variant(
                args.sleeve_variant,
                df1,
                native_events,
                recursive_events,
                trade_start_idx,
                core,
                active,
            )
            for core, active in _sleeve_grid()
        ]
        sleeve_rows.sort(key=lambda r: (r["score_ret_minus_2dd"], r["ret"]), reverse=True)
        print(f"\nsleeve grid for {args.sleeve_variant}, sorted by ret-2*dd:")
        print(f"{'core':>5} {'act':>5} {'idle':>5} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5} {'score':>8}")
        for r in sleeve_rows[:12]:
            print(
                f"{r['core_fraction']:>5.0%} {r['active_fraction']:>5.0%} "
                f"{r['idle_fraction']:>5.0%} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} "
                f"{r['trades']:>4} {r['win_rate']:>5.0%} {r['score_ret_minus_2dd']:>+8.1%}"
            )

    out = {
        "code": code,
        "recursive_report": args.recursive_report,
        "span": f"{df1['date'].iloc[0]}~{df1['date'].iloc[-1]}",
        "trade_start": str(trade_start),
        "buy_hold": buy_hold,
        "native_events": len(native_events),
        "native_event_cache": native_cache_path,
        "native_event_cache_hit": native_cache_hit,
        "recursive_events": len(recursive_events),
        "recursive_trade_events": sum(1 for e in recursive_events if e.get("in_trade_window")),
        "recursive_is_partial": bool(scan.get("is_partial")),
        "recursive_missing_chunks": scan.get("missing_chunks", []),
        "no_future_policy": {
            "native": "wf_seg38_series runs incrementally on 5m bars; 5m start timestamps are shifted to the closing 1m bar, then filled next 1m open",
            "recursive": "wf_recursive_levels_1m visible_time events; event fills next 1m open",
            "execution": "single 1m clock, decisions after bar close",
        },
        "rows": rows_sorted,
        "sleeve_variant": args.sleeve_variant if args.sleeve_grid else None,
        "sleeve_grid": sleeve_rows,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
