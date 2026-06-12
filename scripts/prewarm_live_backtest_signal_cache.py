"""Prewarm strict live-backtest signal caches one symbol at a time.

The live-parity backtest already writes a cache for each exact walk-forward
signal scan.  A multi-symbol strict replay can still be painful, because a
long first symbol must finish before any cache file exists.  This helper keeps
the same no-future semantics but isolates the work per symbol and writes a
manifest after every completed symbol, so the run can be resumed safely.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chanlun.recursive_bt import live_backtest  # noqa: E402
from chanlun.recursive_bt.market_runtime import (  # noqa: E402
    CHART_CACHE_DIR,
    list_chart_cache_codes,
    normalize_code,
    normalize_market,
    parse_codes,
)


REPORT_DIR = Path("D:/chanlun_pro/reports")
DEFAULT_SIGNAL_CACHE_DIR = (
    "D:/chanlun_pro/reports/live_backtest_signal_cache_v7_l0min3_registry"
)
DEFAULT_OUT = REPORT_DIR / "live_backtest_signal_cache_prewarm_manifest.json"
SCRIPT_VERSION = 1


def _stable_hash(obj: Mapping[str, Any]) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _run_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "version": SCRIPT_VERSION,
        "market": normalize_market(args.market),
        "chart_cache": str(args.chart_cache),
        "op_level": str(args.op_level),
        "mid_level": str(args.mid_level or ""),
        "big_level": str(args.big_level),
        "start": str(args.start or ""),
        "end": str(args.end or ""),
        "signal_mode": str(args.signal_mode),
        "signal_source": str(args.signal_source),
        "signal_unit": str(args.signal_unit),
        "include_l0_upgrade_signals": bool(args.include_l0_upgrade_signals),
        "recursive_l0_min_zs_lines": int(args.recursive_l0_min_zs_lines),
        "signal_warmup_bars": int(args.signal_warmup_bars),
        "signal_cache_dir": str(args.signal_cache_dir or ""),
        "signal_scan_chunk_bars": int(args.signal_scan_chunk_bars),
        "trend_dir_warmup_bars": int(args.trend_dir_warmup_bars),
        "annotate_nest": bool(args.annotate_nest),
        "checkpoint_every": int(getattr(args, "checkpoint_every", 0) or 0),
        "checkpoint_dir": str(getattr(args, "checkpoint_dir", "") or ""),
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _codes_for_args(args: argparse.Namespace) -> list[str]:
    market = normalize_market(args.market)
    codes = parse_codes(market, args.codes)
    if not codes:
        codes = list_chart_cache_codes(market, args.chart_cache, frequency=args.op_level)
    codes = [normalize_code(market, code) for code in (codes or [])]
    if args.pool_size and args.pool_size > 0:
        codes = codes[: int(args.pool_size)]
    return list(dict.fromkeys(codes))


def _cache_stats_total(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    total = {"hits": 0, "misses": 0, "writes": 0, "entries": 0}
    for row in rows:
        stats = row.get("signal_cache_stats")
        if not isinstance(stats, Mapping):
            continue
        for key in total:
            total[key] += int(stats.get(key, 0) or 0)
    return total


def _symbol_row(
    code: str,
    sym: Mapping[str, Any],
    *,
    config_hash: str,
    seconds: float,
) -> dict[str, Any]:
    dates = list(sym.get("dates") or [])
    events = list(sym.get("signal_events") or [])
    return {
        "code": code,
        "status": "ok",
        "config_hash": config_hash,
        "seconds": round(float(seconds), 3),
        "bars": int(len(dates)),
        "first_bar": str(dates[0]) if dates else "",
        "last_bar": str(dates[-1]) if dates else "",
        "signal_seen_registry_complete": bool(
            sym.get("signal_seen_registry_complete")
        ),
        "signal_seen_registry_first_date": str(
            sym.get("signal_seen_registry_first_date") or ""
        ),
        "signal_seen_registry_emit_start_idx": int(
            sym.get("signal_seen_registry_emit_start_idx") or 0
        ),
        "signal_cache_stats": dict(sym.get("signal_cache_stats") or {}),
        "signal_event_count": int(len(events)),
        "levels": dict(sym.get("levels") or {}),
    }


def _error_row(code: str, exc: BaseException, *, config_hash: str, seconds: float) -> dict[str, Any]:
    return {
        "code": code,
        "status": "error",
        "config_hash": config_hash,
        "seconds": round(float(seconds), 3),
        "error": f"{type(exc).__name__}: {exc}",
    }


def _manifest(
    *,
    config: Mapping[str, Any],
    config_hash: str,
    codes: list[str],
    rows_by_code: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows = [dict(rows_by_code[code]) for code in codes if code in rows_by_code]
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    error_rows = [row for row in rows if row.get("status") == "error"]
    skipped = sum(1 for row in rows if row.get("skipped"))
    complete_ok = [
        row for row in ok_rows if bool(row.get("signal_seen_registry_complete"))
    ]
    return {
        "version": SCRIPT_VERSION,
        "config_hash": config_hash,
        "config": dict(config),
        "codes": codes,
        "rows": rows,
        "summary": {
            "symbols": len(codes),
            "ok": len(ok_rows),
            "errors": len(error_rows),
            "skipped": skipped,
            "registry_complete": len(complete_ok),
            "registry_complete_all": len(complete_ok) == len(codes) and not error_rows,
            "signal_cache_stats": _cache_stats_total(rows),
            "signal_event_count": sum(int(row.get("signal_event_count") or 0) for row in rows),
        },
    }


def build_prewarm(args: argparse.Namespace) -> dict[str, Any]:
    market = normalize_market(args.market)
    codes = _codes_for_args(args)
    config = _run_config(args)
    config_hash = _stable_hash(config)
    out = Path(args.out)
    old = _read_manifest(out) if args.resume else {}
    old_rows = {
        str(row.get("code")): dict(row)
        for row in old.get("rows", [])
        if isinstance(row, dict) and row.get("code")
    }
    rows_by_code: dict[str, dict[str, Any]] = {}

    if not codes:
        payload = _manifest(config=config, config_hash=config_hash, codes=[], rows_by_code={})
        _write_manifest(out, payload)
        return payload

    for idx, code in enumerate(codes, start=1):
        prior = old_rows.get(code)
        if (
            args.resume
            and not args.force
            and prior
            and prior.get("status") == "ok"
            and prior.get("config_hash") == config_hash
        ):
            row = dict(prior)
            row["skipped"] = True
            rows_by_code[code] = row
            print(f"[{idx}/{len(codes)}] skip {code}: manifest ok", flush=True)
            payload = _manifest(
                config=config,
                config_hash=config_hash,
                codes=codes,
                rows_by_code=rows_by_code,
            )
            _write_manifest(out, payload)
            continue

        print(f"[{idx}/{len(codes)}] prewarm {code}", flush=True)
        t0 = time.time()
        try:
            syms = live_backtest.load_chart_cache_syms(
                market,
                codes=[code],
                cache_dir=args.chart_cache,
                pool_size=0,
                op_level=args.op_level,
                big_level=args.big_level,
                mid_level=args.mid_level or None,
                signal_mode=args.signal_mode,
                signal_warmup_bars=args.signal_warmup_bars,
                annotate_nest=args.annotate_nest,
                start=args.start,
                end=args.end,
                signal_cache_dir=args.signal_cache_dir,
                signal_scan_chunk_bars=args.signal_scan_chunk_bars,
                trend_dir_warmup_bars=args.trend_dir_warmup_bars,
                signal_unit=args.signal_unit,
                signal_source=args.signal_source,
                recursive_l0_min_zs_lines=args.recursive_l0_min_zs_lines,
                include_l0_upgrade_signals=args.include_l0_upgrade_signals,
            )
            sym = syms.get(code)
            if sym is None:
                raise RuntimeError(f"symbol was not loaded from chart cache: {code}")
            row = _symbol_row(
                code,
                sym,
                config_hash=config_hash,
                seconds=time.time() - t0,
            )
        except Exception as exc:
            row = _error_row(code, exc, config_hash=config_hash, seconds=time.time() - t0)
            rows_by_code[code] = row
            payload = _manifest(
                config=config,
                config_hash=config_hash,
                codes=codes,
                rows_by_code=rows_by_code,
            )
            _write_manifest(out, payload)
            print(f"  error: {row['error']}", flush=True)
            if args.fail_fast:
                raise
            continue

        rows_by_code[code] = row
        payload = _manifest(
            config=config,
            config_hash=config_hash,
            codes=codes,
            rows_by_code=rows_by_code,
        )
        _write_manifest(out, payload)
        stats = row.get("signal_cache_stats") or {}
        print(
            "  ok "
            f"registry={row.get('signal_seen_registry_complete')} "
            f"events={row.get('signal_event_count')} "
            f"cache(h/m/w)={stats.get('hits', 0)}/{stats.get('misses', 0)}/{stats.get('writes', 0)} "
            f"seconds={row.get('seconds')}",
            flush=True,
        )

    # Preserve rows that were already completed for codes later in the list if a
    # resumed run was limited externally before reaching them this time.
    for code in codes:
        if code not in rows_by_code and code in old_rows:
            prior = dict(old_rows[code])
            if prior.get("config_hash") == config_hash:
                prior["skipped"] = True
                rows_by_code[code] = prior
    payload = _manifest(
        config=config,
        config_hash=config_hash,
        codes=codes,
        rows_by_code=rows_by_code,
    )
    _write_manifest(out, payload)
    return payload


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prewarm strict live-backtest signal caches per symbol"
    )
    parser.add_argument("--market", choices=("a", "us"), default="us")
    parser.add_argument("--codes", help="Comma-separated codes or a text file path")
    parser.add_argument("--pool-size", type=int, default=0)
    parser.add_argument("--chart-cache", default=CHART_CACHE_DIR)
    parser.add_argument("--op-level", default="1m")
    parser.add_argument("--mid-level", default="5m")
    parser.add_argument("--big-level", default="30m")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--signal-mode", choices=("walk_forward",), default="walk_forward")
    parser.add_argument("--signal-source", choices=("branch", "upgrade"), default="upgrade")
    parser.add_argument("--signal-unit", choices=("bi", "xd"), default="bi")
    parser.add_argument(
        "--include-l0-upgrade-signals",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--recursive-l0-min-zs-lines",
        type=int,
        choices=(3, 4),
        default=live_backtest.DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    )
    parser.add_argument("--signal-warmup-bars", type=int, default=-1)
    parser.add_argument("--signal-cache-dir", default=DEFAULT_SIGNAL_CACHE_DIR)
    parser.add_argument("--signal-scan-chunk-bars", type=int, default=0)
    parser.add_argument(
        "--trend-dir-warmup-bars",
        type=int,
        default=live_backtest.DEFAULT_TREND_DIR_WARMUP_BARS,
    )
    parser.add_argument("--annotate-nest", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="Write strict scan checkpoint every N main-clock bars; 0 disables",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default="",
        help="Optional checkpoint directory; defaults beside signal cache files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    old_every = os.environ.get("CHANLUN_WF_CHECKPOINT_EVERY")
    old_dir = os.environ.get("CHANLUN_WF_CHECKPOINT_DIR")
    if int(args.checkpoint_every or 0) > 0:
        os.environ["CHANLUN_WF_CHECKPOINT_EVERY"] = str(int(args.checkpoint_every))
    if args.checkpoint_dir:
        os.environ["CHANLUN_WF_CHECKPOINT_DIR"] = str(args.checkpoint_dir)
    try:
        payload = build_prewarm(args)
    finally:
        if old_every is None:
            os.environ.pop("CHANLUN_WF_CHECKPOINT_EVERY", None)
        else:
            os.environ["CHANLUN_WF_CHECKPOINT_EVERY"] = old_every
        if old_dir is None:
            os.environ.pop("CHANLUN_WF_CHECKPOINT_DIR", None)
        else:
            os.environ["CHANLUN_WF_CHECKPOINT_DIR"] = old_dir
    summary = payload.get("summary") or {}
    print(f"manifest={args.out}")
    print(
        "ok={ok}/{symbols} errors={errors} registry_complete={registry_complete} "
        "events={events}".format(
            ok=summary.get("ok", 0),
            symbols=summary.get("symbols", 0),
            errors=summary.get("errors", 0),
            registry_complete=summary.get("registry_complete", 0),
            events=summary.get("signal_event_count", 0),
        )
    )
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
