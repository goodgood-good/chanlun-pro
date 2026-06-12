"""Run a reproducible TSLA live-style Chanlun backtest.

This script is intentionally built on raw chart-cache klines, not precomputed
signal pickles.  The default signal mode is ``walk_forward``: at each main-clock
bar the Chanlun object only sees klines that would have been closed and visible
at that time.  ``--compare-batch`` runs the old full-series signal mapping on the
same sliced window so the future-signal gap is measurable.

Example:
  PYTHONPATH=src;web/chanlun_chart python scripts/tsla_live_walk_forward_replay.py --compare-batch
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.recursive_bt.live_backtest import (
    DEFAULT_SIGNAL_MODE,
    build_symbol_from_klines,
    write_outputs,
)
from chanlun.recursive_bt.market_runtime import (
    CHART_CACHE_DIR,
    load_chart_cache_klines,
    normalize_code,
    normalize_market,
)
from chanlun.recursive_bt.portfolio import portfolio_backtest


def _freq_delta(level: str) -> pd.Timedelta:
    if level.endswith("m"):
        return pd.Timedelta(minutes=int(level[:-1]))
    if level == "d":
        return pd.Timedelta(days=1)
    if level == "w":
        return pd.Timedelta(days=7)
    return pd.Timedelta(0)


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)


def _coerce_ts(value: str | None, ref: pd.Timestamp) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.Timestamp(value)
    if ref.tzinfo is None:
        return ts.tz_convert(None) if ts.tzinfo is not None else ts
    return ts.tz_localize(ref.tzinfo) if ts.tzinfo is None else ts.tz_convert(ref.tzinfo)


def _summary_args(args, mode: str, summary_path: str, trades_path: str, trade_start, end):
    return SimpleNamespace(
        market=args.market,
        source="chart_cache",
        codes=args.code,
        chart_cache=args.chart_cache,
        requested_max_pos=args.max_pos,
        max_pos=args.max_pos,
        pool_size=1,
        bt_pool_mode="raw_klines",
        selection_scan_limit=0,
        selection_sample_mode="single",
        selection_board_filter="",
        selection_lookback_bars=0,
        selection_buy_classes=(3, 2, 1),
        selection_require_three_systems=None,
        walk_forward_scores=False,
        signal_mode=mode,
        signal_warmup_bars=args.signal_warmup_bars,
        require=tuple(x for x in args.require.split(",") if x),
        big_gate=args.big_gate,
        regime_mode="off",
        mid_gate=args.mid_gate,
        op_level=args.op_level,
        big_level=args.big_level,
        mid_level=args.mid_level,
        init_cash=args.init_cash,
        start=str(trade_start),
        end=str(end),
        buy_priority=args.buy_priority,
        bs_point_ratio_overrides_enabled=False,
        bs_point_ratio_overrides_json="",
        bs_point_ratio_multipliers={},
        regime_bs_ratio_multipliers={},
        regime_lookback_days=20,
        regime_source_code="",
        output_summary=summary_path,
        output_trades=trades_path,
    )


def run_one(args, mode: str, df_op: pd.DataFrame, df_big: pd.DataFrame,
            df_mid: pd.DataFrame | None, trade_start, end):
    code = normalize_code(args.market, args.code)
    sym = build_symbol_from_klines(
        args.market,
        code,
        df_op,
        df_big,
        op_level=args.op_level,
        big_level=args.big_level,
        df_mid=df_mid,
        mid_level=args.mid_level,
        signal_mode=mode,
        signal_warmup_bars=args.signal_warmup_bars,
        annotate_nest="nest" in args.require.split(","),
    )
    result = portfolio_backtest(
        syms={code: sym},
        max_pos=args.max_pos,
        label=f"{code}-{args.op_level}+{args.big_level}-{mode}",
        buy_priority=args.buy_priority,
        require=tuple(x for x in args.require.split(",") if x),
        big_gate=args.big_gate,
        mid_gate=args.mid_gate,
        init_cash=args.init_cash,
        t_start=trade_start,
        t_end=end,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"tsla_{args.op_level}_{args.big_level}_{mode}"
    summary_path = str(out_dir / f"{tag}_summary.json")
    trades_path = str(out_dir / f"{tag}_trades.csv")
    summary_args = _summary_args(args, mode, summary_path, trades_path, trade_start, end)
    write_outputs(result, summary_args, {code: sym})
    return result, summary_path, trades_path, sym


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TSLA raw-kline walk-forward replay")
    parser.add_argument("--market", default="us", choices=("us", "a"))
    parser.add_argument("--code", default="TSLA.US")
    parser.add_argument("--chart-cache", default=CHART_CACHE_DIR)
    parser.add_argument("--out-dir", default="D:/chanlun_pro/reports")
    parser.add_argument("--op-level", default="5m")
    parser.add_argument("--big-level", default="30m")
    parser.add_argument("--mid-level")
    parser.add_argument("--window-days", type=int, default=45)
    parser.add_argument("--warmup-days", type=int, default=30)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--signal-warmup-bars", type=int, default=200)
    parser.add_argument("--max-pos", type=int, default=1)
    parser.add_argument("--init-cash", type=float, default=1_000_000)
    parser.add_argument("--buy-priority", choices=("3first", "1first"), default="3first")
    parser.add_argument("--require", default="tech")
    parser.add_argument("--big-gate", choices=("bsp", "trend"), default="bsp")
    parser.add_argument("--mid-gate", choices=("strict", "soft", "bull_relaxed"), default="strict")
    parser.add_argument("--compare-batch", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    args.market = normalize_market(args.market)
    args.code = normalize_code(args.market, args.code)

    df_op_all = load_chart_cache_klines(args.market, args.code, args.op_level, args.chart_cache)
    df_big_all = load_chart_cache_klines(args.market, args.code, args.big_level, args.chart_cache)
    df_mid_all = (
        load_chart_cache_klines(args.market, args.code, args.mid_level, args.chart_cache)
        if args.mid_level
        else None
    )
    if df_op_all is None or df_big_all is None:
        raise SystemExit(f"missing chart cache for {args.code} {args.op_level}/{args.big_level}")

    ref = pd.Timestamp(df_op_all["date"].iloc[-1])
    end = _coerce_ts(args.end, ref) or ref
    trade_start = _coerce_ts(args.start, ref) or (end - pd.Timedelta(days=args.window_days))
    warm_start = trade_start - pd.Timedelta(days=args.warmup_days)
    higher_start = warm_start - max(_freq_delta(args.big_level), _freq_delta(args.mid_level or "0m")) * 2

    df_op = _slice(df_op_all, warm_start, end)
    df_big = _slice(df_big_all, higher_start, end)
    df_mid = _slice(df_mid_all, higher_start, end) if df_mid_all is not None else None
    if len(df_op) == 0 or len(df_big) == 0:
        raise SystemExit("selected window has no klines")

    wf, wf_summary, wf_trades, wf_sym = run_one(
        args, DEFAULT_SIGNAL_MODE, df_op, df_big, df_mid, trade_start, end,
    )
    report = {
        "code": args.code,
        "no_future_policy": {
            "source": "raw chart-cache klines",
            "signal_mode": DEFAULT_SIGNAL_MODE,
            "main_clock": args.op_level,
            "higher_level_delay": f"{args.big_level} bar close before use",
            "warmup_only_before": str(trade_start),
            "execution": "signals at bar close, orders filled at next bar open by portfolio_backtest",
        },
        "window": {
            "warm_start": str(warm_start),
            "trade_start": str(trade_start),
            "end": str(end),
            "op_bars": int(len(df_op)),
            "big_bars": int(len(df_big)),
            "mid_bars": int(len(df_mid)) if df_mid is not None else 0,
        },
        "walk_forward": {
            "summary": wf_summary,
            "trades": wf_trades,
            "total_return": float(wf["total"]),
            "buy_hold": float(wf["bh"]),
            "max_drawdown": float(wf["max_dd"]),
            "trade_count": int(wf["n"]),
            "small_signal_bars": int(len(wf_sym.get("small_by_bar", {}))),
        },
    }

    if args.compare_batch:
        batch, batch_summary, batch_trades, batch_sym = run_one(
            args, "batch", df_op, df_big, df_mid, trade_start, end,
        )
        report["batch_future_signal_baseline"] = {
            "summary": batch_summary,
            "trades": batch_trades,
            "total_return": float(batch["total"]),
            "buy_hold": float(batch["bh"]),
            "max_drawdown": float(batch["max_dd"]),
            "trade_count": int(batch["n"]),
            "small_signal_bars": int(len(batch_sym.get("small_by_bar", {}))),
            "return_gap_walk_forward_minus_batch": float(wf["total"] - batch["total"]),
        }

    out_path = Path(args.out_dir) / f"tsla_{args.op_level}_{args.big_level}_walk_forward_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
