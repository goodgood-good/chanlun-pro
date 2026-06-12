"""Render a static Chanlun multi-level visual audit page from chart-cache data.

The page is intentionally independent of the live Flask app/session.  It loads
raw 1m/5m/30m cache files, recomputes Chanlun chart JSON, then draws K-lines,
BI strokes, current-level centers, recursive 5m/30m centers, buy/sell points,
and divergences into SVG panels.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = ROOT / "web" / "chanlun_chart"
for p in (ROOT, SRC, WEB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from chanlun.cl_utils import cl_data_to_tv_chart, query_cl_chart_config  # noqa: E402
from chanlun.core.cl import CL  # noqa: E402
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines  # noqa: E402


COLORS = {
    "candle_up": "#cf3f3f",
    "candle_down": "#2f9d67",
    "wick": "#64748b",
    "bi": "#111827",
    "bi_zs": "#f97316",
    "L0": "#8b5cf6",
    "L1": "#0ea5e9",
    "L2": "#ef4444",
    "mmd_buy": "#16a34a",
    "mmd_sell": "#dc2626",
    "bc": "#7c3aed",
    "signal_visible": "#2563eb",
    "signal_fill": "#0f766e",
    "trade_entry": "#0891b2",
    "trade_exit": "#be123c",
}


def _ts(value: str) -> int:
    return int(pd_timestamp(value).timestamp())


def pd_timestamp(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def fmt_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def iso_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).isoformat()


def _maybe_ts(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(pd_timestamp(str(value)).timestamp())
    except Exception:
        return None


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def load_json(path: str) -> dict[str, Any]:
    p = Path(path)
    if not path or not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def load_csv_rows(path: str) -> list[dict[str, str]]:
    p = Path(path)
    if not path or not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as fp:
        return [dict(row) for row in csv.DictReader(fp)]


def resolve_window(chart: dict[str, Any], freq: str, start_s: str, end_s: str) -> tuple[str, str]:
    ts = [int(t) for t in chart.get("t", []) or []]
    if not ts:
        return start_s, end_s
    start, end = _ts(start_s), _ts(end_s)
    if any(start <= t <= end for t in ts):
        return start_s, end_s
    target = {"1m": 1800, "5m": 900, "30m": 360}.get(freq, 500)
    lo = ts[max(0, len(ts) - target)]
    hi = ts[-1]
    return iso_ts(lo), iso_ts(hi)


def chart_for(
    cache_dir: str,
    prefix: str,
    freq: str,
    code: str,
    *,
    recursive_l0_min_zs_lines: int,
) -> dict[str, Any]:
    cfg = query_cl_chart_config("us", code)
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.update(
        {
            "chart_use_branch_core": "1",
            "chart_show_recursive_levels": "1",
            "chart_show_bi": "1",
            "chart_show_bi_mmd": "1",
            "chart_show_bi_bc": "1",
            "chart_show_xd_mmd": "1",
            "chart_show_xd_bc": "1",
            "recursive_l0_min_zs_lines": int(recursive_l0_min_zs_lines),
        }
    )
    df = load_chart_cache_klines("us", code, freq, cache_dir)
    if df is None or df.empty:
        raise FileNotFoundError(f"missing {freq} chart-cache data for {code} under {cache_dir}")
    df = df.sort_values("date").reset_index(drop=True)
    cd = CL(code, freq, dict(cfg), market="us")
    cd.process_klines(df)
    return cl_data_to_tv_chart(cd, cfg)


def _point_time(point: dict[str, Any]) -> int | None:
    t = point.get("time") if isinstance(point, dict) else None
    if t is None:
        return None
    try:
        return int(t)
    except Exception:
        return None


def _shape_points(item: dict[str, Any]) -> list[dict[str, Any]]:
    pts = item.get("points")
    if isinstance(pts, list):
        return [p for p in pts if isinstance(p, dict)]
    if isinstance(pts, dict):
        return [pts]
    return []


def _shape_intersects(item: dict[str, Any], start: int, end: int) -> bool:
    times = [_point_time(p) for p in _shape_points(item)]
    times = [t for t in times if t is not None]
    if not times:
        return False
    return min(times) <= end and max(times) >= start


def _shape_in_window(item: dict[str, Any], start: int, end: int) -> bool:
    times = [_point_time(p) for p in _shape_points(item)]
    times = [t for t in times if t is not None]
    return bool(times) and any(start <= t <= end for t in times)


def _price_values_from_shapes(shapes: list[dict[str, Any]], start: int, end: int) -> list[float]:
    vals: list[float] = []
    for item in shapes:
        if not _shape_intersects(item, start, end):
            continue
        for p in _shape_points(item):
            if "price" in p:
                try:
                    vals.append(float(p["price"]))
                except Exception:
                    pass
    return vals


def _line(points: list[dict[str, Any]], x_for, y_for, color: str, width: float = 1.5) -> str:
    coords = []
    for p in points:
        t = _point_time(p)
        if t is None or "price" not in p:
            continue
        coords.append(f"{x_for(t):.1f},{y_for(float(p['price'])):.1f}")
    if len(coords) < 2:
        return ""
    return (
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" />'
    )


def _rect(item: dict[str, Any], x_for, y_for, color: str, start: int, end: int, width: float) -> str:
    pts = _shape_points(item)
    if len(pts) < 2:
        return ""
    t0 = max(_point_time(pts[0]) or start, start)
    t1 = min(_point_time(pts[-1]) or end, end)
    prices = [float(p["price"]) for p in pts if "price" in p]
    if len(prices) < 2 or t1 <= t0:
        return ""
    y0 = y_for(max(prices))
    y1 = y_for(min(prices))
    dash = ' stroke-dasharray="6 4"' if str(item.get("linestyle", "0")) != "0" else ""
    return (
        f'<rect x="{x_for(t0):.1f}" y="{y0:.1f}" width="{max(1, x_for(t1)-x_for(t0)):.1f}" '
        f'height="{max(1, y1-y0):.1f}" fill="{color}" fill-opacity="0.08" '
        f'stroke="{color}" stroke-width="{width}"{dash}/>'
    )


def _overlay_prices(signals: list[dict[str, str]], trades: list[dict[str, str]], start: int, end: int) -> list[float]:
    vals: list[float] = []
    for row in signals:
        t = _maybe_ts(row.get("next_fill_time") or row.get("visible_time"))
        px = _maybe_float(row.get("next_fill_open") or row.get("signal_price"))
        if t is not None and px is not None and start <= t <= end:
            vals.append(px)
    for row in trades:
        for tk, pk in (("entry_date", "entry_px"), ("exit_date", "exit_px")):
            t = _maybe_ts(row.get(tk))
            px = _maybe_float(row.get(pk))
            if t is not None and px is not None and start <= t <= end:
                vals.append(px)
    return vals


def render_panel(
    chart: dict[str, Any],
    title: str,
    start_s: str,
    end_s: str,
    *,
    signals: list[dict[str, str]] | None = None,
    trades: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, int]]:
    signals = signals or []
    trades = trades or []
    start = _ts(start_s)
    end = _ts(end_s)
    ts = [int(t) for t in chart.get("t", [])]
    idx = [i for i, t in enumerate(ts) if start <= t <= end]
    if not idx:
        return f"<h2>{html.escape(title)}</h2><p>No bars in requested window.</p>", {}

    bars = [
        (
            ts[i],
            float(chart["o"][i]),
            float(chart["h"][i]),
            float(chart["l"][i]),
            float(chart["c"][i]),
        )
        for i in idx
    ]
    visible_shapes: list[dict[str, Any]] = []
    visible_shapes.extend([z for z in chart.get("bi_zss", []) if _shape_intersects(z, start, end)])
    for lv in chart.get("recursive_levels", []) or []:
        visible_shapes.extend([z for z in lv.get("zss", []) if _shape_intersects(z, start, end)])
        visible_shapes.extend([m for m in lv.get("mmds", []) if _shape_in_window(m, start, end)])
        visible_shapes.extend([b for b in lv.get("bcs", []) if _shape_in_window(b, start, end)])
    visible_shapes.extend([m for m in chart.get("bi_mmds", []) if _shape_in_window(m, start, end)])
    visible_shapes.extend([m for m in chart.get("xd_mmds", []) if _shape_in_window(m, start, end)])
    visible_shapes.extend([b for b in chart.get("bi_bcs", []) if _shape_in_window(b, start, end)])
    visible_shapes.extend([b for b in chart.get("xd_bcs", []) if _shape_in_window(b, start, end)])

    lows = [b[3] for b in bars]
    highs = [b[2] for b in bars]
    extra = _price_values_from_shapes(visible_shapes, start, end)
    extra.extend(_overlay_prices(signals, trades, start, end))
    lo = min(lows + extra)
    hi = max(highs + extra)
    pad = max((hi - lo) * 0.08, 1e-9)
    lo -= pad
    hi += pad

    width, height = 1280, 430
    left, right, top, bottom = 64, 18, 24, 44
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_for(t: int) -> float:
        return left + (t - start) / max(1, end - start) * plot_w

    def y_for(v: float) -> float:
        return top + (hi - v) / max(1e-9, hi - lo) * plot_h

    parts: list[str] = []
    parts.append(f'<section class="panel"><h2>{html.escape(title)}</h2>')
    parts.append(
        f'<div class="meta">{fmt_ts(start)} UTC to {fmt_ts(end)} UTC, bars={len(bars)}</div>'
    )
    parts.append(f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">')
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>')
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = top + frac * plot_h
        price = hi - frac * (hi - lo)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="8" y="{y+4:.1f}" class="axis">{price:.2f}</text>')

    # Centers first, so candles and strokes remain readable.
    for z in chart.get("bi_zss", []) or []:
        if _shape_intersects(z, start, end):
            parts.append(_rect(z, x_for, y_for, COLORS["bi_zs"], start, end, 1.0))
    for lv in chart.get("recursive_levels", []) or []:
        lvl = int(lv.get("level", 0) or 0)
        color = COLORS.get(f"L{lvl}", COLORS["L2"])
        for z in lv.get("zss", []) or []:
            if _shape_intersects(z, start, end):
                parts.append(_rect(z, x_for, y_for, color, start, end, 1.4 if lvl else 1.0))

    step = max(1.0, plot_w / max(1, len(bars)))
    candle_w = min(5.0, max(1.0, step * 0.55))
    for t, o, h, l, c in bars:
        x = x_for(t)
        color = COLORS["candle_up"] if c >= o else COLORS["candle_down"]
        parts.append(
            f'<line x1="{x:.1f}" y1="{y_for(h):.1f}" x2="{x:.1f}" y2="{y_for(l):.1f}" '
            f'stroke="{COLORS["wick"]}" stroke-width="0.8"/>'
        )
        y = min(y_for(o), y_for(c))
        rh = max(1.0, abs(y_for(c) - y_for(o)))
        parts.append(
            f'<rect x="{x-candle_w/2:.1f}" y="{y:.1f}" width="{candle_w:.1f}" height="{rh:.1f}" '
            f'fill="{color}" fill-opacity="0.72"/>'
        )

    for bi in chart.get("bis", []) or []:
        if _shape_intersects(bi, start, end):
            pts = [p for p in _shape_points(bi) if start <= (_point_time(p) or 0) <= end]
            if len(pts) < 2:
                pts = _shape_points(bi)
            parts.append(_line(pts, x_for, y_for, COLORS["bi"], 1.2))

    def marker(item: dict[str, Any], color: str, label: str) -> str:
        pts = _shape_points(item)
        if not pts:
            return ""
        p = pts[0]
        t = _point_time(p)
        if t is None or not (start <= t <= end) or "price" not in p:
            return ""
        x, y = x_for(t), y_for(float(p["price"]))
        safe = html.escape(label)
        return (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="#fff" stroke-width="1.5"/>'
            f'<text x="{x+7:.1f}" y="{y-7:.1f}" class="label" fill="{color}">{safe}</text>'
        )

    for key in ("bi_mmds", "xd_mmds"):
        for m in chart.get(key, []) or []:
            txt = str(m.get("text", "M"))
            color = COLORS["mmd_buy"] if "B" in txt.upper() or "BUY" in txt.upper() else COLORS["mmd_sell"]
            parts.append(marker(m, color, txt))
    for key in ("bi_bcs", "xd_bcs"):
        for b in chart.get(key, []) or []:
            parts.append(marker(b, COLORS["bc"], str(b.get("text", "BC"))))
    for lv in chart.get("recursive_levels", []) or []:
        for m in lv.get("mmds", []) or []:
            txt = f"{m.get('level', '')}:{m.get('text', 'M')}"
            color = COLORS["mmd_buy"] if "BUY" in txt.upper() else COLORS["mmd_sell"]
            parts.append(marker(m, color, txt))
        for b in lv.get("bcs", []) or []:
            txt = f"{b.get('level', '')}:{b.get('text', 'BC')}"
            parts.append(marker(b, COLORS["bc"], txt))

    def event_marker(
        t: int | None,
        px: float | None,
        color: str,
        label: str,
        *,
        dashed: bool = False,
        shape: str = "circle",
    ) -> str:
        if t is None or px is None or not (start <= t <= end):
            return ""
        x, y = x_for(t), y_for(px)
        safe = html.escape(label)
        dash = ' stroke-dasharray="4 4"' if dashed else ""
        items = [
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" '
            f'stroke="{color}" stroke-width="1"{dash} opacity="0.7"/>'
        ]
        if shape == "triangle_up":
            pts = f"{x:.1f},{y-7:.1f} {x-7:.1f},{y+7:.1f} {x+7:.1f},{y+7:.1f}"
            items.append(f'<polygon points="{pts}" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        elif shape == "triangle_down":
            pts = f"{x:.1f},{y+7:.1f} {x-7:.1f},{y-7:.1f} {x+7:.1f},{y-7:.1f}"
            items.append(f'<polygon points="{pts}" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        else:
            items.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        items.append(f'<text x="{x+7:.1f}" y="{y-9:.1f}" class="label" fill="{color}">{safe}</text>')
        return "".join(items)

    for row in signals:
        visible_t = _maybe_ts(row.get("visible_time"))
        fill_t = _maybe_ts(row.get("next_fill_time"))
        signal_px = _maybe_float(row.get("signal_price"))
        fill_px = _maybe_float(row.get("next_fill_open")) or signal_px
        bs_type = row.get("bs_type", "")
        level = row.get("level", "")
        parts.append(event_marker(visible_t, signal_px, COLORS["signal_visible"], f"L{level} {bs_type} visible", dashed=True))
        parts.append(event_marker(fill_t, fill_px, COLORS["signal_fill"], f"L{level} fill", shape="triangle_up" if "buy" in bs_type.lower() else "triangle_down"))
    for row in trades:
        entry_t = _maybe_ts(row.get("entry_date"))
        exit_t = _maybe_ts(row.get("exit_date"))
        entry_px = _maybe_float(row.get("entry_px"))
        exit_px = _maybe_float(row.get("exit_px"))
        layer = row.get("entry_layer", "")
        parts.append(event_marker(entry_t, entry_px, COLORS["trade_entry"], f"{layer} entry", shape="triangle_up"))
        parts.append(event_marker(exit_t, exit_px, COLORS["trade_exit"], f"{row.get('exit_layer', '') or 'exit'}", shape="triangle_down"))

    parts.append("</svg>")
    counts = {
        "bars": len(bars),
        "bis": sum(1 for bi in chart.get("bis", []) if _shape_intersects(bi, start, end)),
        "bi_zss": sum(1 for z in chart.get("bi_zss", []) if _shape_intersects(z, start, end)),
        "bi_mmds": sum(1 for m in chart.get("bi_mmds", []) if _shape_in_window(m, start, end)),
        "xd_mmds": sum(1 for m in chart.get("xd_mmds", []) if _shape_in_window(m, start, end)),
        "bi_bcs": sum(1 for b in chart.get("bi_bcs", []) if _shape_in_window(b, start, end)),
        "xd_bcs": sum(1 for b in chart.get("xd_bcs", []) if _shape_in_window(b, start, end)),
        "signals": sum(1 for s in signals if start <= (_maybe_ts(s.get("visible_time")) or -1) <= end),
        "trades": sum(1 for t in trades if start <= (_maybe_ts(t.get("entry_date")) or -1) <= end),
    }
    legend = [
        ("BI", COLORS["bi"]),
        ("BI center", COLORS["bi_zs"]),
        ("L0 current center", COLORS["L0"]),
        ("L1 5m/30m center", COLORS["L1"]),
        ("L2 30m center", COLORS["L2"]),
        ("Buy/Sell", COLORS["mmd_buy"]),
        ("Divergence", COLORS["bc"]),
        ("Signal visible", COLORS["signal_visible"]),
        ("Trade layer", COLORS["trade_entry"]),
    ]
    parts.append('<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{html.escape(name)}</span>' for name, c in legend
    ) + "</div>")
    parts.append("</section>")
    return "\n".join(p for p in parts if p), counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="D:/chanlun_pro/chart_cache_us_tsla_1y")
    parser.add_argument("--prefix", default="us_TSLA_US")
    parser.add_argument("--out", default="D:/chanlun_pro/reports/chanlun_visual_audit_tsla_v8_registry.html")
    parser.add_argument("--recursive-l0-min-zs-lines", type=int, choices=(3, 4), default=3)
    parser.add_argument(
        "--summary",
        default="D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v8_registry_layered_summary.json",
    )
    parser.add_argument(
        "--trades",
        default="D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v8_registry_layered_trades.csv",
    )
    parser.add_argument(
        "--signals",
        default="D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v8_registry_layered_signals.csv",
    )
    args = parser.parse_args(argv)

    code = args.prefix.replace("us_", "").replace("_US", "") + ".US"
    summary = load_json(args.summary)
    trade_rows = load_csv_rows(args.trades)
    signal_rows = load_csv_rows(args.signals)
    specs = [
        ("1m Kline: BI + 1m/5m/30m centers, buy/sell, divergence", "1m", "2026-06-08T13:30:00+00:00", "2026-06-10T20:00:00+00:00"),
        ("5m Kline: BI + 5m/30m centers, buy/sell, divergence", "5m", "2025-11-01T13:30:00+00:00", "2026-04-20T21:00:00+00:00"),
        ("30m Kline: BI + 30m centers, buy/sell, divergence", "30m", "2026-02-15T13:30:00+00:00", "2026-05-20T21:00:00+00:00"),
    ]
    panels = []
    rows = []
    for title, freq, start, end in specs:
        chart = chart_for(
            args.cache_dir,
            args.prefix,
            freq,
            code,
            recursive_l0_min_zs_lines=args.recursive_l0_min_zs_lines,
        )
        start, end = resolve_window(chart, freq, start, end)
        panel, counts = render_panel(
            chart,
            title,
            start,
            end,
            signals=signal_rows if freq == "1m" else [],
            trades=trade_rows if freq == "1m" else [],
        )
        panels.append(panel)
        levels = {
            int(lv.get("level", 0) or 0): {
                "zss": len(lv.get("zss") or []),
                "mmds": len(lv.get("mmds") or []),
                "bcs": len(lv.get("bcs") or []),
            }
            for lv in chart.get("recursive_levels", []) or []
        }
        rows.append((freq, counts, levels))

    summary_rows = []
    for freq, counts, levels in rows:
        summary_rows.append(
            "<tr>"
            f"<td>{html.escape(freq)}</td>"
            f"<td>{counts.get('bars', 0)}</td>"
            f"<td>{counts.get('bis', 0)}</td>"
            f"<td>{counts.get('bi_zss', 0)}</td>"
            f"<td>{html.escape(str(levels))}</td>"
            f"<td>{counts.get('bi_mmds', 0) + counts.get('xd_mmds', 0)}</td>"
            f"<td>{counts.get('bi_bcs', 0) + counts.get('xd_bcs', 0)}</td>"
            f"<td>{counts.get('signals', 0)}</td>"
            f"<td>{counts.get('trades', 0)}</td>"
            "</tr>"
        )

    trade_table_rows = []
    for row in trade_rows:
        trade_table_rows.append(
            "<tr>"
            f"<td>{html.escape(row.get('entry_date', ''))}</td>"
            f"<td>{html.escape(row.get('entry_layer', ''))}</td>"
            f"<td>{html.escape(row.get('entry_level', ''))}</td>"
            f"<td>{html.escape(row.get('buy_ratio', ''))}</td>"
            f"<td>{html.escape(row.get('core_shares_before', ''))}</td>"
            f"<td>{html.escape(row.get('swing_shares_before', ''))}</td>"
            f"<td>{html.escape(row.get('scalp_shares_before', ''))}</td>"
            f"<td>{html.escape(row.get('exit_date', ''))}</td>"
            f"<td>{html.escape(row.get('reason', ''))}</td>"
            "</tr>"
        )
    signal_table_rows = []
    for row in signal_rows:
        signal_table_rows.append(
            "<tr>"
            f"<td>{html.escape(row.get('level', ''))}</td>"
            f"<td>{html.escape(row.get('bs_type', ''))}</td>"
            f"<td>{html.escape(row.get('anchor_time', ''))}</td>"
            f"<td>{html.escape(row.get('visible_time', ''))}</td>"
            f"<td>{html.escape(row.get('next_fill_time', ''))}</td>"
            f"<td>{html.escape(row.get('anchor_to_visible_bars', ''))}</td>"
            "</tr>"
        )
    metrics = {
        "return": summary.get("total_return"),
        "buy_hold": summary.get("buy_hold"),
        "max_dd": summary.get("max_drawdown"),
        "trade_count": summary.get("trade_count"),
        "signal_event_count": summary.get("signal_event_count"),
        "core_signal_level": summary.get("core_signal_level"),
        "swing_signal_level": summary.get("swing_signal_level"),
        "recursive_l0_min_zs_lines": summary.get("recursive_l0_min_zs_lines"),
        "signal_seen_registry_complete": summary.get("signal_seen_registry_complete"),
        "stale_reappearing_signal_risk": (
            summary.get("no_future_policy", {}) or {}
        ).get("stale_reappearing_signal_risk"),
    }

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<link rel="icon" href="data:,">
<title>Chanlun Visual Audit - {html.escape(code)}</title>
<style>
body {{ margin: 0; padding: 24px; background: #f8fafc; color: #111827; font: 14px/1.45 Arial, sans-serif; }}
h1 {{ margin: 0 0 8px; font-size: 24px; }}
h2 {{ margin: 0 0 6px; font-size: 18px; }}
.note {{ margin: 0 0 18px; color: #475569; max-width: 1100px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin: 14px 0; }}
.card {{ background: #fff; border: 1px solid #d1d5db; border-radius: 6px; padding: 12px; }}
.panel {{ background: #fff; border: 1px solid #d1d5db; border-radius: 6px; padding: 14px; margin: 16px 0; box-shadow: 0 1px 2px rgba(15,23,42,.06); }}
.meta {{ color: #64748b; margin-bottom: 8px; font-size: 12px; }}
svg {{ width: 100%; height: auto; border: 1px solid #e5e7eb; background: #fff; }}
.axis {{ font-size: 11px; fill: #64748b; }}
.label {{ font-size: 11px; font-weight: 700; paint-order: stroke; stroke: white; stroke-width: 3px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; color: #334155; font-size: 12px; }}
.legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
.legend i {{ width: 14px; height: 4px; display: inline-block; border-radius: 2px; }}
table {{ border-collapse: collapse; background: #fff; margin: 14px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; }}
</style>
</head>
<body>
<h1>Chanlun Visual Audit - {html.escape(code)}</h1>
<p class="note">Generated from raw cache data with the same chart JSON serializer used by the web app. Panels intentionally use different windows so each requested level is visible instead of compressed into a full-year blur.</p>
<div class="grid">
<div class="card">
<h2>Strict Replay Metrics</h2>
<table>
<tbody>
<tr><th>Total Return</th><td>{float(metrics.get('return') or 0):.2%}</td></tr>
<tr><th>Buy Hold</th><td>{float(metrics.get('buy_hold') or 0):.2%}</td></tr>
<tr><th>Max Drawdown</th><td>{float(metrics.get('max_dd') or 0):.2%}</td></tr>
<tr><th>Trades</th><td>{html.escape(str(metrics.get('trade_count', '')))}</td></tr>
<tr><th>Signal Events</th><td>{html.escape(str(metrics.get('signal_event_count', '')))}</td></tr>
<tr><th>Core Level</th><td>{html.escape(str(metrics.get('core_signal_level', '')))}</td></tr>
<tr><th>Swing Level</th><td>{html.escape(str(metrics.get('swing_signal_level', '')))}</td></tr>
<tr><th>L0 Center Lines</th><td>{html.escape(str(metrics.get('recursive_l0_min_zs_lines', '')))}</td></tr>
<tr><th>Registry Complete</th><td>{html.escape(str(metrics.get('signal_seen_registry_complete', '')))}</td></tr>
<tr><th>Stale Signal Risk</th><td>{html.escape(str(metrics.get('stale_reappearing_signal_risk', '')))}</td></tr>
</tbody>
</table>
</div>
<div class="card">
<h2>Layer Trades</h2>
<table>
<thead><tr><th>Entry</th><th>Layer</th><th>Level</th><th>Ratio</th><th>Core</th><th>Swing</th><th>Scalp</th><th>Exit</th><th>Reason</th></tr></thead>
<tbody>{''.join(trade_table_rows) or '<tr><td colspan="9">No trades</td></tr>'}</tbody>
</table>
</div>
</div>
<h2>Signal Visibility Audit</h2>
<table>
<thead><tr><th>Level</th><th>Type</th><th>Anchor</th><th>Visible</th><th>Next Fill</th><th>Anchor→Visible Bars</th></tr></thead>
<tbody>{''.join(signal_table_rows) or '<tr><td colspan="6">No signal rows</td></tr>'}</tbody>
</table>
<table>
<thead><tr><th>Freq</th><th>Bars In Panel</th><th>BI Strokes</th><th>BI Centers</th><th>Recursive Levels Total</th><th>Current MMDs</th><th>Current Divergences</th><th>Overlay Signals</th><th>Overlay Trades</th></tr></thead>
<tbody>{''.join(summary_rows)}</tbody>
</table>
{''.join(panels)}
</body>
</html>
"""
    html_doc = html_doc.replace("Anchor\u2192Visible Bars", "Anchor-&gt;Visible Bars")
    html_doc = html_doc.replace("Anchor\u922b\u6278isible Bars", "Anchor-&gt;Visible Bars")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
