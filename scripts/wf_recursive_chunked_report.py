# -*- coding: utf-8 -*-
"""Build a merged recursive L1/L2 walk-forward event report from chunks.

Full-year 1m recursive scanning is expensive.  This script keeps the strict
no-future semantics of wf_recursive_levels_1m_tsla.py but splits the trade
window into resumable chunks:

- each chunk has its own warmup history before the chunk start;
- only events whose visible_time falls inside that chunk trade range are merged;
- each chunk writes/reads a pickle cache keyed by its exact metadata;
- the final merged report contains full events and can be consumed by
  wf_native_recursive_context_tsla.py.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf_recursive_levels_1m_tsla import (  # noqa: E402
    DEFAULT_REPORT_DIR,
    _code_from_prefix,
    _coerce_ts,
    _event_cache_meta,
    _event_counts,
    _load_1m,
    _load_event_cache,
    _run_variant,
    _scan_recursive_events,
    _slice,
    _write_event_cache,
)


DEFAULT_DIR = "D:/chanlun_pro/chart_cache_us_tsla_1y"
DEFAULT_PREFIX = "us_TSLA_US"
DEFAULT_TAG = "tsla_year"


def _chunk_ranges(start: pd.Timestamp, end: pd.Timestamp, chunk_days: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    cur = start
    step = pd.Timedelta(days=int(chunk_days))
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out


def _event_identity(event: dict[str, Any]) -> tuple:
    return (
        str(event.get("visible_time")),
        int(event.get("level") or 0),
        str(event.get("bs_type")),
        str(event.get("anchor_time")),
        round(float(event.get("price") or 0.0), 6),
    )


def _build_one_chunk(
    df_all: pd.DataFrame,
    code: str,
    chunk_start: pd.Timestamp,
    chunk_end: pd.Timestamp,
    warmup_days: int,
    signal_warmup_bars: int,
    progress_every: int,
    cache_path: Path,
    force_rescan: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    warm_start = chunk_start - pd.Timedelta(days=int(warmup_days))
    df = _slice(df_all, warm_start, chunk_end)
    if df.empty:
        return [], {"empty": True}
    dates = list(df["date"])
    trade_start_idx = bisect.bisect_left(dates, chunk_start)
    if trade_start_idx >= len(df):
        return [], {"empty": True}
    meta = _event_cache_meta(code, df, trade_start_idx, chunk_start, signal_warmup_bars)
    cached = None if force_rescan else _load_event_cache(str(cache_path), meta)
    if cached is not None:
        events, scan = cached
        scan = dict(scan)
        scan["cache_hit"] = True
    else:
        events, scan = _scan_recursive_events(
            df,
            code,
            trade_start_idx,
            signal_warmup_bars,
            progress_every,
        )
        scan = dict(scan)
        scan["cache_hit"] = False
        _write_event_cache(str(cache_path), meta, events, scan)
    scan["cache_path"] = str(cache_path)
    scan["chunk_start"] = str(chunk_start)
    scan["chunk_end"] = str(chunk_end)
    scan["warm_start"] = str(warm_start)
    return events, scan


def _try_load_chunk(
    df_all: pd.DataFrame,
    code: str,
    chunk_start: pd.Timestamp,
    chunk_end: pd.Timestamp,
    warmup_days: int,
    signal_warmup_bars: int,
    cache_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    warm_start = chunk_start - pd.Timedelta(days=int(warmup_days))
    df = _slice(df_all, warm_start, chunk_end)
    if df.empty:
        return None
    dates = list(df["date"])
    trade_start_idx = bisect.bisect_left(dates, chunk_start)
    if trade_start_idx >= len(df):
        return None
    meta = _event_cache_meta(code, df, trade_start_idx, chunk_start, signal_warmup_bars)
    cached = _load_event_cache(str(cache_path), meta)
    if cached is None:
        return None
    events, scan = cached
    scan = dict(scan)
    scan["cache_hit"] = True
    scan["cache_path"] = str(cache_path)
    scan["chunk_start"] = str(chunk_start)
    scan["chunk_end"] = str(chunk_end)
    scan["warm_start"] = str(warm_start)
    return events, scan


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="chunked 1m recursive L1/L2 event report")
    parser.add_argument("--dir", default=DEFAULT_DIR)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--out-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--start", help="trade window start, e.g. 2025-06-10 16:00:00")
    parser.add_argument("--end")
    parser.add_argument("--window-days", type=int, default=365)
    parser.add_argument("--warmup-days", type=int, default=30)
    parser.add_argument("--chunk-days", type=int, default=15)
    parser.add_argument("--signal-warmup-bars", type=int, default=1200)
    parser.add_argument("--progress-every", type=int, default=2000)
    parser.add_argument("--max-chunks", type=int, default=0, help="debug limit; 0 means all chunks")
    parser.add_argument("--chunk-start-index", type=int, default=1, help="1-based first chunk to scan")
    parser.add_argument("--chunk-end-index", type=int, default=0, help="1-based last chunk to scan; 0 means through end")
    parser.add_argument("--merge-only", action="store_true", help="do not scan missing chunks; merge available caches")
    parser.add_argument("--status-only", action="store_true", help="only report chunk-cache progress; no scan or merged report")
    parser.add_argument("--force-rescan", action="store_true")
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
    df_global = _slice(df_all, warm_start, end)
    if df_global.empty:
        raise SystemExit("selected window has no 1m bars")
    global_dates = list(df_global["date"])
    global_trade_start_idx = bisect.bisect_left(global_dates, trade_start)
    if global_trade_start_idx >= len(df_global):
        raise SystemExit("trade_start is after selected data")

    out_dir = Path(args.out_dir)
    chunk_dir = out_dir / f"wf_recursive_chunks_{args.tag}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    all_chunks = _chunk_ranges(trade_start, end, args.chunk_days)
    selected_start = max(int(args.chunk_start_index or 1), 1)
    selected_end = int(args.chunk_end_index or 0) or len(all_chunks)
    selected_end = min(selected_end, len(all_chunks))
    selected_indices = set(range(selected_start, selected_end + 1))
    chunks = list(all_chunks)
    if args.max_chunks and args.max_chunks > 0:
        selected_indices = set(sorted(selected_indices)[:args.max_chunks])

    if args.status_only:
        cached, missing, selected_cached, selected_missing = [], [], [], []
        for idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            cache_path = chunk_dir / f"chunk_{idx:03d}_{chunk_start:%Y%m%d%H%M}_{chunk_end:%Y%m%d%H%M}.pkl"
            loaded = _try_load_chunk(
                df_all,
                code,
                chunk_start,
                chunk_end,
                args.warmup_days,
                args.signal_warmup_bars,
                cache_path,
            )
            if loaded is None:
                missing.append(idx)
                if idx in selected_indices:
                    selected_missing.append(idx)
            else:
                cached.append(idx)
                if idx in selected_indices:
                    selected_cached.append(idx)
        status = {
            "code": code,
            "tag": args.tag,
            "chunk_days": int(args.chunk_days),
            "warmup_days": int(args.warmup_days),
            "signal_warmup_bars": int(args.signal_warmup_bars),
            "trade_start": str(trade_start),
            "end": str(end),
            "chunks": len(chunks),
            "cached": cached,
            "missing": missing,
            "selected_chunk_start_index": selected_start,
            "selected_chunk_end_index": selected_end,
            "selected_cached": selected_cached,
            "selected_missing": selected_missing,
            "chunk_dir": str(chunk_dir),
        }
        print(
            f"{code} {args.tag}: cached {len(cached)}/{len(chunks)} chunks; "
            f"selected cached {len(selected_cached)}/{len(selected_indices)}",
            flush=True,
        )
        if selected_missing:
            print(f"selected missing chunks: {selected_missing}", flush=True)
        out_path = out_dir / f"wf_recursive_status_{args.tag}_chunked.json"
        out_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"-> {out_path}")
        return 0

    merged: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    chunk_stats: list[dict[str, Any]] = []
    print(
        f"{code} chunked recursive WF chunks={len(chunks)} "
        f"selected={min(selected_indices) if selected_indices else '-'}..{max(selected_indices) if selected_indices else '-'} "
        f"trade={trade_start}~{end} warm_start={warm_start}",
        flush=True,
    )
    missing_chunks: list[int] = []
    scanned_chunks: list[int] = []
    cached_chunks: list[int] = []
    skipped_chunks: list[int] = []
    for idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        cache_path = chunk_dir / f"chunk_{idx:03d}_{chunk_start:%Y%m%d%H%M}_{chunk_end:%Y%m%d%H%M}.pkl"
        should_scan = idx in selected_indices and not args.merge_only
        loaded = None if (should_scan and args.force_rescan) else _try_load_chunk(
            df_all,
            code,
            chunk_start,
            chunk_end,
            args.warmup_days,
            args.signal_warmup_bars,
            cache_path,
        )
        if loaded is not None:
            print(f"chunk {idx}/{len(chunks)} {chunk_start}~{chunk_end}", flush=True)
            events, scan = loaded
            cached_chunks.append(idx)
        elif should_scan:
            print(f"chunk {idx}/{len(chunks)} {chunk_start}~{chunk_end}", flush=True)
            events, scan = _build_one_chunk(
                df_all,
                code,
                chunk_start,
                chunk_end,
                args.warmup_days,
                args.signal_warmup_bars,
                args.progress_every,
                cache_path,
                args.force_rescan,
            )
            scanned_chunks.append(idx)
        else:
            missing_chunks.append(idx)
            skipped_chunks.append(idx)
            if idx in selected_indices:
                print("  missing cache; skipped", flush=True)
            continue
        kept = 0
        for e in events:
            vt = pd.Timestamp(e["visible_time"])
            in_chunk = chunk_start <= vt < chunk_end
            if idx == len(chunks):
                in_chunk = chunk_start <= vt <= chunk_end
            if not in_chunk:
                continue
            gi = bisect.bisect_left(global_dates, vt)
            if gi >= len(global_dates):
                continue
            item = dict(e)
            item["bar"] = int(gi)
            item["in_trade_window"] = bool(gi >= global_trade_start_idx)
            ident = _event_identity(item)
            if ident in seen:
                continue
            seen.add(ident)
            merged.append(item)
            kept += 1
        scan["events_seen"] = len(events)
        scan["events_kept"] = kept
        chunk_stats.append(scan)
        print(f"  kept={kept} cache_hit={scan.get('cache_hit')} scan_seconds={scan.get('scan_seconds')}", flush=True)

    merged.sort(key=lambda e: (pd.Timestamp(e["visible_time"]), e.get("level", 0), str(e.get("bs_type"))))
    rows = [
        _run_variant(v, df_global, merged, global_trade_start_idx, "neutral", "ratio")
        for v in ["l1_only", "l1_l2_not_down", "l1_l2_up"]
    ]
    rows_sorted = sorted(rows, key=lambda r: (r["ret"], -r["max_dd"]), reverse=True)
    bh = float(df_global["close"].iloc[-1]) / float(df_global["open"].iloc[global_trade_start_idx]) - 1
    total_scan_seconds = sum(float(s.get("scan_seconds") or 0.0) for s in chunk_stats if not s.get("cache_hit"))
    cache_hits = sum(1 for s in chunk_stats if s.get("cache_hit"))

    print(f"events all={len(merged)} trade={sum(1 for e in merged if e['in_trade_window'])} buy_hold={bh:+.1%}")
    print(f"{'variant':>18} {'ret':>8} {'dd':>7} {'tr':>4} {'win':>5}")
    for r in rows_sorted:
        print(f"{r['name']:>18} {r['ret']:>+8.1%} {r['max_dd']:>7.1%} {r['trades']:>4} {r['win_rate']:>5.0%}")

    report = {
        "code": code,
        "span": f"{df_global['date'].iloc[0]}~{df_global['date'].iloc[-1]}",
        "window": {
            "warm_start": str(warm_start),
            "trade_start_requested": str(trade_start),
            "trade_start_bar": str(df_global["date"].iloc[global_trade_start_idx]),
            "trade_start_idx": int(global_trade_start_idx),
            "end": str(end),
            "bars": int(len(df_global)),
        },
        "no_future_policy": {
            "source": "raw 1m chart-cache klines",
            "chunking": "each chunk scans with pre-chunk warmup; only visible_time inside the chunk is merged",
            "level_chain": "1m -> L1(5m, kuozhan) -> L2(30m, tongjibie)",
            "execution": "decision at 1m close, fill at next 1m open",
        },
        "buy_hold": bh,
        "scan": {
            "chunk_days": int(args.chunk_days),
            "warmup_days": int(args.warmup_days),
            "signal_warmup_bars": int(args.signal_warmup_bars),
            "chunks": len(chunks),
            "cache_hits": cache_hits,
            "cache_misses": len(scanned_chunks),
            "selected_chunk_start_index": selected_start,
            "selected_chunk_end_index": selected_end,
            "scanned_chunks": scanned_chunks,
            "cached_chunks": cached_chunks,
            "missing_chunks": missing_chunks,
            "skipped_chunks": skipped_chunks,
            "is_partial": bool(missing_chunks),
            "new_scan_seconds": round(total_scan_seconds, 3),
            "chunk_dir": str(chunk_dir),
        },
        "chunks": chunk_stats,
        "event_counts_all": _event_counts(merged),
        "event_counts_trade": _event_counts(merged, trade_only=True),
        "events": merged,
        "sample_events": merged[:30],
        "rows": rows_sorted,
    }
    out_path = out_dir / f"wf_recursive_levels_{args.tag}_chunked_1m.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
