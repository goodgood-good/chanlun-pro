# -*- coding: utf-8 -*-
"""Walk-forward nested confirmation for 5m signals using 1m local structure.

Inputs:
- stage1 episodes from wf_confirm_scan_tsla.py.  These episodes are produced by
  incremental 5m recalculation and record when a signal is first visible and how
  long it remains visible.
- raw 1m klines.  We compute a no-lookahead local raw-fractal direction stack
  as a fast proxy for the next lower-level turn, and replay without future bars.

The experiment compares structural confirmation against blind time waits:
- struct_already_up: buy when the local 1m direction is up within max_wait.
- struct_flip_up: buy only on a local transition into up within max_wait.
- wait_N: buy after N 1m bars if the 5m episode is still alive.
"""
from __future__ import annotations

import bisect
import glob
import json
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.recursive_bt.engine import US_STOCK, recommended_buy_ratio  # noqa: E402


COMMISSION = US_STOCK.commission
DEFAULT_DIR = "D:/chanlun_pro/chart_cache_us_tsla_1y"
DEFAULT_PREFIX = "us_TSLA_US"
DEFAULT_TAG = "tsla"
MAX_WAIT_1M = 90
WAIT_BARS = (0, 5, 15, 30, 60)


class _LocalFx:
    __slots__ = ("type", "index", "val")

    def __init__(self, _type: str, index: int, val: float):
        self.type = _type
        self.index = index
        self.val = val


def _parse_args(argv: list[str]) -> dict:
    args = {
        "dir": DEFAULT_DIR,
        "prefix": DEFAULT_PREFIX,
        "tag": DEFAULT_TAG,
        "max_wait": MAX_WAIT_1M,
        "stage1": "",
        "out": "",
        "dir_cache": "",
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dir" and i + 1 < len(argv):
            args["dir"] = argv[i + 1]
            i += 2
        elif a == "--prefix" and i + 1 < len(argv):
            args["prefix"] = argv[i + 1]
            i += 2
        elif a == "--tag" and i + 1 < len(argv):
            args["tag"] = argv[i + 1]
            i += 2
        elif a == "--max-wait" and i + 1 < len(argv):
            args["max_wait"] = int(argv[i + 1])
            i += 2
        elif a == "--stage1" and i + 1 < len(argv):
            args["stage1"] = argv[i + 1]
            i += 2
        elif a == "--out" and i + 1 < len(argv):
            args["out"] = argv[i + 1]
            i += 2
        elif a == "--dir-cache" and i + 1 < len(argv):
            args["dir_cache"] = argv[i + 1]
            i += 2
        else:
            i += 1
    tag = args["tag"]
    args["stage1"] = args["stage1"] or f"D:/chanlun_pro/reports/wf_confirm_{tag}_stage1.pkl"
    args["out"] = args["out"] or f"D:/chanlun_pro/reports/wf_nest_confirm_{tag}.json"
    args["dir_cache"] = args["dir_cache"] or f"D:/chanlun_pro/reports/wf_dir_lookup_{tag}_1m.pkl"
    return args


def _code_from_prefix(prefix: str) -> str:
    return prefix.replace("us_", "").replace("_US", "") + ".US"


def _latest_cache_file(cache_dir: str, prefix: str, freq: str) -> Path:
    files = glob.glob(str(Path(cache_dir) / f"v*_{prefix}_{freq}_recursivebt.pkl"))
    if not files:
        raise FileNotFoundError(f"missing {freq} cache for {prefix} under {cache_dir}")

    def version(path: str) -> int:
        name = Path(path).name
        if name.startswith("v"):
            try:
                return int(name.split("_", 1)[0][1:])
            except Exception:
                return 0
        return 0

    return Path(max(files, key=version))


def _load_1m(cache_dir: str, prefix: str) -> pd.DataFrame:
    data = pickle.loads(_latest_cache_file(cache_dir, prefix, "1m").read_bytes())["data"]
    return pd.DataFrame({
        "date": pd.to_datetime(data["t"], unit="s"),
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
    })


def _load_dir_lookup(
    df1: pd.DataFrame,
    code: str,
    cache_path: str,
    needed_indices: set[int],
) -> dict[int, str]:
    path = Path(cache_path)
    if path.exists():
        cached = pickle.loads(path.read_bytes())
        if (
            cached.get("code") == code
            and cached.get("n") == len(df1)
            and str(cached.get("first")) == str(df1["date"].iloc[0])
            and str(cached.get("last")) == str(df1["date"].iloc[-1])
            and set(cached.get("needed_indices", [])) == set(needed_indices)
        ):
            return {int(k): v for k, v in cached["dirs"].items()}
    dirs = _wf_bi_dir_lookup_fast(df1, code, "1m", needed_indices)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps({
        "code": code,
        "n": len(df1),
        "first": str(df1["date"].iloc[0]),
        "last": str(df1["date"].iloc[-1]),
        "needed_indices": sorted(needed_indices),
        "dirs": dirs,
    }))
    return dirs


def _wf_bi_dir_lookup_fast(
    df: pd.DataFrame,
    code: str,
    freq: str,
    needed_indices: set[int],
    warmup: int = 30,
) -> dict[int, str]:
    """Local raw-1m fractal direction proxy at selected bar indices only."""
    del code, freq
    dirs: dict[int, str] = {}
    n = len(df)
    w = min(warmup, n)
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    def more_extreme(new: _LocalFx, old: _LocalFx) -> bool:
        if new.type != old.type:
            return False
        return new.val > old.val if new.type == "ding" else new.val < old.val

    def valid_bi(a: _LocalFx, b: _LocalFx) -> bool:
        if a.type == b.type:
            return False
        if b.index - a.index < 4:
            return False
        if a.type == "ding":
            return b.val < a.val
        return b.val > a.val

    def push_fx(stack: list[_LocalFx], fx: _LocalFx) -> None:
        while True:
            if not stack:
                stack.append(fx)
                return
            last = stack[-1]
            if fx.type == last.type:
                if more_extreme(fx, last):
                    stack.pop()
                    continue
                return
            if valid_bi(last, fx):
                stack.append(fx)
                return
            if len(stack) < 3:
                return
            prev = stack[-2]
            last_peer = stack[-3]
            if more_extreme(last, last_peer):
                return
            if not more_extreme(fx, prev):
                return
            stack.pop()
            stack.pop()

    stack: list[_LocalFx] = []
    for idx in range(n):
        mid = idx - 1
        if mid >= 1:
            if highs[mid] > highs[mid - 1] and highs[mid] > highs[idx] and lows[mid] > lows[mid - 1] and lows[mid] > lows[idx]:
                push_fx(stack, _LocalFx("ding", mid, float(highs[mid])))
            elif lows[mid] < lows[mid - 1] and lows[mid] < lows[idx] and highs[mid] < highs[mid - 1] and highs[mid] < highs[idx]:
                push_fx(stack, _LocalFx("di", mid, float(lows[mid])))
        if idx < w or idx not in needed_indices:
            continue
        if len(stack) < 2:
            d = "neutral"
        else:
            d = "up" if stack[-2].type == "di" else "down"
        dirs[idx] = d
    return dirs


def _map_gate_to_1m(st: dict, dates1: list[pd.Timestamp], dates5) -> list[str]:
    gate5 = st["gate"]
    out = ["neutral"] * len(dates1)
    for i, t in enumerate(dates1):
        j = bisect.bisect_right(dates5, t) - 1
        out[i] = gate5[j] if j >= 0 else "neutral"
    return out


def _t5_to_i1(dates1: list[pd.Timestamp], dates5, bar5: int) -> int:
    return bisect.bisect_right(dates1, dates5[bar5]) - 1


def _needed_dir_indices(
    st: dict,
    dates1: list[pd.Timestamp],
    dates5,
    max_wait: int,
) -> set[int]:
    needed: set[int] = set()
    n1 = len(dates1)
    for ep in st["episodes"]:
        if ep["side"] != "buy":
            continue
        i0 = _t5_to_i1(dates1, dates5, int(ep["first_seen"]))
        if i0 < 0:
            continue
        alive_idx = min(int(ep["alive_until"]), len(dates5) - 1)
        alive_t = dates5[alive_idx]
        end_i = min(i0 + max_wait, n1 - 1)
        for i in range(i0 + 1, end_i + 1):
            if dates1[i] > alive_t:
                break
            needed.add(i)
            if i > 0:
                needed.add(i - 1)
    return needed


def run_1m_exec(orders, gate1, opens, closes) -> dict:
    n = len(opens)
    cash, shares, entry = 1.0, 0.0, 0.0
    peak, max_dd = 1.0, 0.0
    trades, wins = [], 0
    pending = None
    oi = 0
    orders = sorted(orders, key=lambda x: (x[0], 0 if x[1] == "sell" else 1))
    for i in range(n):
        if pending is not None:
            act, bs = pending
            px = opens[i]
            if act == "buy" and shares == 0.0 and px > 0:
                ratio = recommended_buy_ratio(
                    bs,
                    1,
                    big_dir=gate1[i - 1] if i else "neutral",
                    trend_boost=True,
                )
                budget = cash * ratio * 0.99 if ratio < 1 else cash * 0.99
                size = budget / (px * (1 + COMMISSION))
                if size > 0:
                    cash -= size * px * (1 + COMMISSION)
                    shares, entry = size, px
            elif act == "sell" and shares > 0.0 and px > 0:
                cash += shares * px * (1 - COMMISSION)
                ret = px / entry - 1
                trades.append(ret)
                wins += ret > 0
                shares = 0.0
            pending = None

        gate_down = gate1[i] == "down"
        if shares > 0.0 and gate_down:
            pending = ("sell", "gate_down")
        while oi < len(orders) and orders[oi][0] == i:
            _, act, bs = orders[oi]
            oi += 1
            if act == "sell" and shares > 0.0:
                pending = ("sell", bs)
            elif act == "buy" and shares == 0.0 and not gate_down and pending is None:
                pending = ("buy", bs)

        eq = cash + shares * closes[i]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    if shares > 0.0:
        cash += shares * closes[-1] * (1 - COMMISSION)
        ret = closes[-1] / entry - 1
        trades.append(ret)
        wins += ret > 0

    return {
        "ret": cash - 1.0,
        "max_dd": max_dd,
        "trades": len(trades),
        "win_rate": wins / len(trades) if trades else 0.0,
        "avg_trade_ret": sum(trades) / len(trades) if trades else 0.0,
    }


def _build_orders(
    st: dict,
    dates1: list[pd.Timestamp],
    dates5,
    dir_lookup: dict[int, str],
    mode: str,
    max_wait: int,
    wait_bars: int = 0,
) -> tuple[list[tuple[int, str, str]], dict]:
    def t5_to_i1(bar5: int) -> int:
        return _t5_to_i1(dates1, dates5, bar5)

    out = []
    buy_candidates = 0
    confirmed = 0
    timed_out = 0
    repainted = 0
    for ep in st["episodes"]:
        i0 = t5_to_i1(int(ep["first_seen"]))
        if i0 < 0:
            continue
        if ep["side"] == "sell":
            out.append((i0, "sell", ep["bs_type"]))
            continue

        buy_candidates += 1
        alive_idx = min(int(ep["alive_until"]), len(dates5) - 1)
        alive_t = dates5[alive_idx]
        if mode.startswith("wait_"):
            i = i0 + wait_bars
            if i >= len(dates1):
                timed_out += 1
                continue
            if dates1[i] <= alive_t:
                out.append((i, "buy", ep["bs_type"]))
                confirmed += 1
            else:
                repainted += 1
            continue

        found = False
        end_i = min(i0 + max_wait, len(dates1) - 1)
        for i in range(i0 + 1, end_i + 1):
            if dates1[i] > alive_t:
                repainted += 1
                break
            cur_dir = dir_lookup.get(i, "neutral")
            prev_dir = dir_lookup.get(i - 1, "neutral")
            if mode == "struct_already_up" and cur_dir == "up":
                out.append((i, "buy", ep["bs_type"]))
                confirmed += 1
                found = True
                break
            if mode == "struct_flip_up" and prev_dir != "up" and cur_dir == "up":
                out.append((i, "buy", ep["bs_type"]))
                confirmed += 1
                found = True
                break
        if not found and dates1[min(end_i, len(dates1) - 1)] <= alive_t:
            timed_out += 1

    stats = {
        "orders": len(out),
        "buy_candidates": buy_candidates,
        "confirmed_buys": confirmed,
        "repainted_before_confirm": repainted,
        "timed_out": timed_out,
    }
    return out, stats


def main() -> int:
    args = _parse_args(sys.argv[1:])
    code = _code_from_prefix(args["prefix"])
    st = pickle.loads(Path(args["stage1"]).read_bytes())
    df1 = _load_1m(args["dir"], args["prefix"])
    dates1 = list(df1["date"])
    dates5 = pd.to_datetime(st["dates"])

    print(f"{code} 1m bars={len(df1)} | 5m episodes={len(st['episodes'])} | {st['span']}")
    needed = _needed_dir_indices(st, dates1, dates5, args["max_wait"])
    print(f"loading/computing local raw-1m direction lookup for {len(needed)} bars ...", flush=True)
    dir_lookup = _load_dir_lookup(df1, code, args["dir_cache"], needed)
    gate1 = _map_gate_to_1m(st, dates1, dates5)

    opens1 = df1["open"].to_numpy()
    closes1 = df1["close"].to_numpy()
    bh = st["closes"][-1] / st["opens"][st["warmup"]] - 1

    variants = [
        ("struct_already_up", 0),
        ("struct_flip_up", 0),
    ] + [(f"wait_{k}", k) for k in WAIT_BARS]

    rows = []
    for mode, wait in variants:
        orders, stats = _build_orders(st, dates1, dates5, dir_lookup, mode, args["max_wait"], wait)
        result = run_1m_exec(orders, gate1, opens1, closes1)
        result.update(stats)
        result.update({
            "mode": mode,
            "wait_bars": wait,
            "max_wait": args["max_wait"],
            "score_ret_minus_2dd": result["ret"] - 2 * result["max_dd"],
        })
        rows.append(result)

    rows_sorted = sorted(rows, key=lambda r: (r["ret"], -r["max_dd"]), reverse=True)
    print(f"buy_hold={bh:+.1%}")
    print(f"{'mode':>18} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5} {'confirmed':>9} {'repaint':>7}")
    for r in rows_sorted:
        print(f"{r['mode']:>18} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} "
              f"{r['win_rate']:>5.0%} {r['confirmed_buys']:>9} {r['repainted_before_confirm']:>7}")

    out = {
        "code": code,
        "span": st["span"],
        "buy_hold": bh,
        "stage1": args["stage1"],
        "dir_cache": args["dir_cache"],
        "confirm_source": "local_raw_1m_fx_stack",
        "rows": rows_sorted,
    }
    Path(args["out"]).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {args['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
