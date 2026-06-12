"""Audit structural invalidation of strict TSLA Chanlun replay trades.

The live backtest CSV proves when a signal became visible and when it filled.
This script adds the structural context that is required by the original
Chanlun rules: the associated center boundary for a buy/sell point, especially
the ``ZG``/``ZD`` boundary behind a third-class buy/sell.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from chanlun.core.cl import CL
from chanlun.recursive_bt.live_backtest import _cl_config
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines


DEFAULT_SUMMARY = "D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_summary.json"
DEFAULT_TRADES = "D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_trades.csv"
DEFAULT_SIGNALS = "D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_signals.csv"


def _ts(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value)


def _date(obj) -> str:
    k = getattr(obj, "k", None)
    date = getattr(k, "date", None)
    return "" if date is None else str(pd.Timestamp(date))


def _seg_row(seg) -> dict:
    if seg is None:
        return {}
    return {
        "dir": str(getattr(seg, "type", getattr(seg, "_type", "")) or ""),
        "start_time": _date(getattr(seg, "start", None)),
        "end_time": _date(getattr(seg, "end", None)),
        "low": _float_or_none(getattr(seg, "low", getattr(seg, "zs_low", None))),
        "high": _float_or_none(getattr(seg, "high", getattr(seg, "zs_high", None))),
    }


def _float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if np.isfinite(out) else None
    except Exception:
        return None


def _bsp_row(p, level: int) -> dict:
    z = getattr(p, "zs", None)
    return {
        "level": int(level),
        "bs_type": str(getattr(p, "bs_type", "")),
        "anchor_time": _date(getattr(p, "anchor_fx", None)),
        "anchor_price": _float_or_none(getattr(getattr(p, "anchor_fx", None), "val", None)),
        "structural_stop_below": _float_or_none(getattr(p, "structural_stop_below", None)),
        "structural_stop_above": _float_or_none(getattr(p, "structural_stop_above", None)),
        "zs_zd": _float_or_none(getattr(z, "zd", None)),
        "zs_zg": _float_or_none(getattr(z, "zg", None)),
        "zs_dd": _float_or_none(getattr(z, "dd", None)),
        "zs_gg": _float_or_none(getattr(z, "gg", None)),
        "zs_done": bool(getattr(z, "done", False)),
        "signal_seg": _seg_row(getattr(p, "signal_seg", None)),
    }


def _iter_snapshot_bspoints(cd: CL, include_l0: bool = True):
    if include_l0:
        for p in cd.get_branch_bspoints(use_xd=True):
            level = int(getattr(p, "level", 0) or 0)
            if level == 0:
                yield level, p
    for lv in cd.get_kuozhan_levels() or []:
        level = int(lv.get("level", 0) or 0)
        for p in lv.get("bsp", []) or []:
            yield level, p


def _find_bspoint(
    df: pd.DataFrame,
    *,
    code: str,
    op_level: str,
    recursive_l0_min_zs_lines: int,
    visible_time: pd.Timestamp,
    anchor_time: pd.Timestamp,
    level: int,
    bs_type: str,
) -> dict | None:
    sub = df[pd.to_datetime(df["date"]) <= visible_time].reset_index(drop=True)
    if sub.empty:
        return None
    cd = CL(
        code,
        op_level,
        _cl_config(recursive_l0_min_zs_lines, skip_legacy_mmd=True),
    )
    cd.process_klines(sub)
    for p_level, p in _iter_snapshot_bspoints(cd):
        p_anchor = pd.Timestamp(_date(getattr(p, "anchor_fx", None)) or pd.NaT)
        if (
            p_level == int(level)
            and str(getattr(p, "bs_type", "")) == str(bs_type)
            and p_anchor == anchor_time
        ):
            return _bsp_row(p, p_level)
    return None


def _invalidation_rule(bs_type: str, bsp: dict | None, signal: dict | None = None) -> dict:
    if not bsp:
        return {"rule": "missing_bsp", "boundary": None, "side": ""}
    bs_type = str(bs_type)
    signal = signal or {}
    sig_stop_below = _float_or_none(signal.get("structural_stop_below"))
    sig_stop_above = _float_or_none(signal.get("structural_stop_above"))
    bsp_stop_below = _float_or_none(bsp.get("structural_stop_below"))
    bsp_stop_above = _float_or_none(bsp.get("structural_stop_above"))
    if bs_type.endswith("buy"):
        boundary = sig_stop_below if sig_stop_below is not None else bsp_stop_below
        if boundary is not None:
            return {
                "rule": (
                    "third_buy_breaks_zg"
                    if bs_type == "3buy"
                    else "buy_breaks_structural_stop_below"
                ),
                "boundary": boundary,
                "side": "low_below",
                "source": (
                    "signal structural_stop_below"
                    if sig_stop_below is not None
                    else "snapshot structural_stop_below"
                ),
            }
    if bs_type.endswith("sell"):
        boundary = sig_stop_above if sig_stop_above is not None else bsp_stop_above
        if boundary is not None:
            return {
                "rule": (
                    "third_sell_breaks_zd"
                    if bs_type == "3sell"
                    else "sell_breaks_structural_stop_above"
                ),
                "boundary": boundary,
                "side": "high_above",
                "source": (
                    "signal structural_stop_above"
                    if sig_stop_above is not None
                    else "snapshot structural_stop_above"
                ),
            }
    if bs_type == "3buy":
        return {
            "rule": "third_buy_breaks_zg",
            "boundary": bsp.get("zs_zg"),
            "side": "low_below",
            "source": "associated center ZG",
        }
    if bs_type == "3sell":
        return {
            "rule": "third_sell_breaks_zd",
            "boundary": bsp.get("zs_zd"),
            "side": "high_above",
            "source": "associated center ZD",
        }
    if bs_type.endswith("buy"):
        return {
            "rule": "buy_breaks_anchor_extreme",
            "boundary": bsp.get("anchor_price"),
            "side": "low_below",
            "source": "legacy fallback anchor extreme",
        }
    if bs_type.endswith("sell"):
        return {
            "rule": "sell_breaks_anchor_extreme",
            "boundary": bsp.get("anchor_price"),
            "side": "high_above",
            "source": "legacy fallback anchor extreme",
        }
    return {"rule": "unknown", "boundary": None, "side": ""}


def _path_stats(
    df: pd.DataFrame,
    *,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    entry_px: float,
    rule: dict,
) -> dict:
    dates = pd.to_datetime(df["date"])
    path = df[(dates >= entry_time) & (dates <= exit_time)].reset_index(drop=True)
    if path.empty:
        return {"bars": 0}
    close = path["close"].astype(float).to_numpy()
    high = path["high"].astype(float).to_numpy()
    low = path["low"].astype(float).to_numpy()
    boundary = rule.get("boundary")
    side = str(rule.get("side") or "")
    first_break = None
    if boundary is not None:
        b = float(boundary)
        if side == "low_below":
            idx = np.where(low < b)[0]
        elif side == "high_above":
            idx = np.where(high > b)[0]
        else:
            idx = np.array([], dtype=int)
        if len(idx):
            i = int(idx[0])
            first_break = {
                "time": str(pd.Timestamp(path["date"].iloc[i])),
                "bar_offset": i,
                "low": float(low[i]),
                "high": float(high[i]),
                "close": float(close[i]),
                "ret_at_close": float(close[i] / entry_px - 1.0),
            }
    return {
        "bars": int(len(path)),
        "min_low": float(np.min(low)),
        "max_high": float(np.max(high)),
        "mfe": float(np.max(high / entry_px - 1.0)),
        "mae": float(np.min(low / entry_px - 1.0)),
        "first_invalidation": first_break,
    }


def _matching_entry_signal(trade: pd.Series, signals: pd.DataFrame) -> dict | None:
    fill_time = _ts(trade["entry_date"])
    level = int(float(trade.get("entry_level", 0) or 0))
    bs_type = f"{int(float(trade.get('bs_type', 0) or 0))}buy"
    rows = signals[
        (pd.to_datetime(signals["next_fill_time"]) == fill_time)
        & (signals["bs_type"].astype(str) == bs_type)
        & (signals["level"].astype(int) == level)
    ]
    if rows.empty:
        return None
    row = rows.iloc[0].to_dict()
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}


def build_report(args: argparse.Namespace) -> dict:
    df = load_chart_cache_klines(args.market, args.code, args.op_level, args.chart_cache)
    df = df.sort_values("date").reset_index(drop=True)
    trades = pd.read_csv(args.trades)
    signals = pd.read_csv(args.signals)
    summary = {}
    summary_path = Path(args.summary)
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    rows = []
    for idx, trade in trades.iterrows():
        entry_signal = _matching_entry_signal(trade, signals)
        bsp = None
        rule = {"rule": "missing_entry_signal", "boundary": None, "side": ""}
        if entry_signal is not None:
            bsp = _find_bspoint(
                df,
                code=args.code,
                op_level=args.op_level,
                recursive_l0_min_zs_lines=args.recursive_l0_min_zs_lines,
                visible_time=_ts(entry_signal["visible_time"]),
                anchor_time=_ts(entry_signal["anchor_time"]),
                level=int(entry_signal["level"]),
                bs_type=str(entry_signal["bs_type"]),
            )
            rule = _invalidation_rule(str(entry_signal["bs_type"]), bsp, entry_signal)
        entry_px = float(trade["entry_px"])
        path = _path_stats(
            df,
            entry_time=_ts(trade["entry_date"]),
            exit_time=_ts(trade["exit_date"]),
            entry_px=entry_px,
            rule=rule,
        )
        rows.append({
            "trade_index": int(idx),
            "code": str(trade.get("code", args.code)),
            "entry_date": str(trade["entry_date"]),
            "exit_date": str(trade["exit_date"]),
            "entry_px": entry_px,
            "exit_px": float(trade["exit_px"]),
            "ret": float(trade["ret"]),
            "entry_layer": str(trade.get("entry_layer", "")),
            "entry_level": int(float(trade.get("entry_level", 0) or 0)),
            "bs_type": str(trade.get("bs_type", "")),
            "reason": str(trade.get("reason", "")),
            "entry_signal": entry_signal,
            "bsp": bsp,
            "invalidation_rule": rule,
            "path_stats": path,
        })

    return {
        "version": 1,
        "code": args.code,
        "market": args.market,
        "op_level": args.op_level,
        "summary_path": args.summary,
        "trades_path": args.trades,
        "signals_path": args.signals,
        "summary": {
            "total_return": float(summary.get("total_return", summary.get("total", 0.0)) or 0.0),
            "buy_hold": float(summary.get("buy_hold", 0.0) or 0.0),
            "max_drawdown": float(summary.get("max_drawdown", summary.get("max_dd", 0.0)) or 0.0),
            "trade_count": int(summary.get("trade_count", len(rows)) or 0),
            "signal_event_count": int(summary.get("signal_event_count", 0) or 0),
        },
        "trades": rows,
    }


def render_markdown(report: dict) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# TSLA Trade Structural Invalidation Audit",
        "",
        f"Code: `{report.get('code')}`",
        f"Operation Level: `{report.get('op_level')}`",
        f"Summary: `{report.get('summary_path')}`",
        f"Trades: `{report.get('trades_path')}`",
        f"Signals: `{report.get('signals_path')}`",
        "",
        "## Replay Summary",
        "",
        "| Total Return | Buy Hold | Max DD | Trades | Signals |",
        "| ---: | ---: | ---: | ---: | ---: |",
        "| {ret:.2%} | {bh:.2%} | {dd:.2%} | {trades} | {signals} |".format(
            ret=float(summary.get("total_return") or 0.0),
            bh=float(summary.get("buy_hold") or 0.0),
            dd=float(summary.get("max_drawdown") or 0.0),
            trades=int(summary.get("trade_count") or 0),
            signals=int(summary.get("signal_event_count") or 0),
        ),
        "",
        "## Trade Diagnostics",
        "",
        "| # | Entry | Exit | Layer | Signal | Boundary | First Break | MAE | MFE | Ret |",
        "| ---: | --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for row in report.get("trades", []) or []:
        sig = row.get("entry_signal") or {}
        rule = row.get("invalidation_rule") or {}
        path = row.get("path_stats") or {}
        first = path.get("first_invalidation")
        first_text = "none"
        if first:
            first_text = "{time} ({ret:.2%})".format(
                time=first.get("time"),
                ret=float(first.get("ret_at_close") or 0.0),
            )
        boundary = rule.get("boundary")
        lines.append(
            "| {idx} | `{entry}` | `{exit}` | `{layer}` | L{level} `{bs}` | {boundary} | {first} | {mae:.2%} | {mfe:.2%} | {ret:.2%} |".format(
                idx=int(row.get("trade_index", 0)),
                entry=row.get("entry_date", ""),
                exit=row.get("exit_date", ""),
                layer=row.get("entry_layer", ""),
                level=int(row.get("entry_level", 0)),
                bs=sig.get("bs_type", f"{row.get('bs_type')}buy"),
                boundary="" if boundary is None else f"{float(boundary):.3f}",
                first=first_text,
                mae=float(path.get("mae") or 0.0),
                mfe=float(path.get("mfe") or 0.0),
                ret=float(row.get("ret") or 0.0),
            )
        )
    lines.extend(["", "## Notes", ""])
    lines.append("- `3buy` invalidation is audited as a later low breaking the associated center `ZG`.")
    lines.append("- `1buy`/`2buy` use the exported signal `structural_stop_below`; for `2buy` this is the first-class low that must not be broken.")
    lines.append("- Legacy reports without exported structural stops fall back to the reconstructed anchor extreme.")
    lines.append("- This report is diagnostic; it does not change the strict walk-forward replay.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="us")
    parser.add_argument("--code", default="TSLA.US")
    parser.add_argument("--op-level", default="1m")
    parser.add_argument("--chart-cache", default="D:/chanlun_pro/chart_cache")
    parser.add_argument("--recursive-l0-min-zs-lines", type=int, default=3)
    parser.add_argument("--summary", default=DEFAULT_SUMMARY)
    parser.add_argument("--trades", default=DEFAULT_TRADES)
    parser.add_argument("--signals", default=DEFAULT_SIGNALS)
    parser.add_argument("--out", default="D:/chanlun_pro/reports/tsla_trade_invalidation_audit.json")
    parser.add_argument("--md", default="D:/chanlun_pro/reports/tsla_trade_invalidation_audit.md")
    args = parser.parse_args()

    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        md = Path(args.md)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote={out}")
    if args.md:
        print(f"markdown={args.md}")
    print(
        "trades={n} invalidated={bad}".format(
            n=len(report["trades"]),
            bad=sum(
                1
                for row in report["trades"]
                if (row.get("path_stats") or {}).get("first_invalidation")
            ),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
