"""把与页面一致的缠论证据渲染为适合通知发送的 PNG 图片。

通知渲染器与 TradingView 页面消费同一个严格结构快照，保证告警图片和当前页面
展示的是同一套结构。
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path
import threading
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.cl_utils.strict_chart import build_strict_structure_snapshot
from chanlun.core.types import ICL


_RENDER_LOCK = threading.RLock()
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_CN = ZoneInfo("Asia/Shanghai")
_POINT_LABELS = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
}


@lru_cache(maxsize=1)
def _chinese_font():
    """运行环境提供字体时，返回随系统安装的中日韩字体。"""

    from matplotlib.font_manager import FontProperties

    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )
    path = next((value for value in candidates if value.is_file()), None)
    return FontProperties(fname=str(path)) if path is not None else FontProperties()


def _epoch(value: datetime) -> int:
    if not isinstance(value, datetime):
        raise TypeError("chart timestamp must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
    # 历史数据源适配器曾允许不带时区的上海时间值。该渲染器按墙上时钟顺序匹配即可；
    # 严格证据本身仍带时区并经过因果校验。
        return int(value.replace(tzinfo=_CN).timestamp())
    return int(value.timestamp())


def _finite(value: object) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("chart price must be finite")
    return result


def _x_for_time(times: Sequence[int], value: object) -> float:
    stamp = int(value)
    index = bisect_left(times, stamp)
    if index <= 0:
        return 0.0
    if index >= len(times):
        return float(len(times) - 1)
    before = times[index - 1]
    after = times[index]
    return float(index - 1 if stamp - before <= after - stamp else index)


def _payload_points(payload: Mapping[str, object]) -> tuple[Mapping, Mapping] | None:
    raw = payload.get("points")
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    first, second = raw
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return None
    if not all(key in first for key in ("time", "price")) or not all(
        key in second for key in ("time", "price")
    ):
        return None
    return first, second


def _draw_candles(axis, bars: Sequence[object]) -> None:
    from matplotlib.patches import Rectangle

    for index, bar in enumerate(bars):
        opened = _finite(bar.o)
        closed = _finite(bar.c)
        high = _finite(bar.h)
        low = _finite(bar.l)
        color = "#ef5350" if closed >= opened else "#26a69a"
        axis.vlines(index, low, high, color=color, linewidth=0.55, zorder=2)
        bottom = min(opened, closed)
        height = max(abs(closed - opened), max(high - low, 1e-9) * 0.025)
        axis.add_patch(
            Rectangle(
                (index - 0.31, bottom),
                0.62,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.35,
                zorder=3,
            )
        )


def _draw_segments(axis, cl_data: ICL, times: Sequence[int]) -> None:
    for segment in cl_data.get_xds():
        start_time = _epoch(segment.start.k.date)
        end_time = _epoch(segment.end.k.date)
        if end_time < times[0] or start_time > times[-1]:
            continue
        axis.plot(
            [_x_for_time(times, start_time), _x_for_time(times, end_time)],
            [_finite(segment.start.val), _finite(segment.end.val)],
            color="#2f6fed",
            linewidth=1.0,
            linestyle="--" if getattr(segment, "forming", False) else "-",
            alpha=0.82,
            zorder=4,
        )


def _draw_center_payloads(
    axis,
    times: Sequence[int],
    payloads: Sequence[Mapping[str, object]],
    *,
    color: str,
    linestyle: str,
    alpha: float,
) -> None:
    from matplotlib.patches import Rectangle

    for payload in payloads:
        points = _payload_points(payload)
        if points is None:
            continue
        first, second = points
        start_time = int(first["time"])
        end_time = int(second["time"])
        if end_time < times[0] or start_time > times[-1]:
            continue
        x0 = _x_for_time(times, start_time)
        x1 = _x_for_time(times, end_time)
        top = _finite(first["price"])
        bottom = _finite(second["price"])
        low, high = sorted((bottom, top))
        axis.add_patch(
            Rectangle(
                (min(x0, x1), low),
                max(abs(x1 - x0), 0.8),
                max(high - low, 1e-9),
                facecolor=color,
                edgecolor=color,
                linewidth=1.15,
                linestyle=linestyle,
                alpha=alpha,
                zorder=5,
            )
        )


def _draw_points(
    axis,
    times: Sequence[int],
    payloads: Sequence[Mapping[str, object]],
    *,
    approaching: bool,
) -> None:
    for payload in payloads:
        raw = payload.get("points")
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], Mapping):
            continue
        point = raw[0]
        if "time" not in point or "price" not in point:
            continue
        stamp = int(point["time"])
        if stamp < times[0] or stamp > times[-1]:
            continue
        side = str(payload.get("side") or "")
        is_buy = side.lower() == "buy" or "buy" in str(payload.get("point_type"))
        marker = "^" if is_buy else "v"
        color = "#d32f2f" if is_buy else "#16856b"
        x = _x_for_time(times, stamp)
        y = _finite(point["price"])
        axis.scatter(
            [x],
            [y],
            marker=marker,
            s=38 if not approaching else 30,
            facecolors="none" if approaching else color,
            edgecolors=color,
            linewidths=1.0,
            zorder=7,
        )
        label = _POINT_LABELS.get(
            str(payload.get("point_type") or ""),
            str(payload.get("point_type") or "买卖点"),
        )
        if approaching:
            label = f"接近{label}"
        axis.annotate(
            label,
            (x, y),
            xytext=(0, 8 if is_buy else -12),
            textcoords="offset points",
            ha="center",
            va="bottom" if is_buy else "top",
            fontsize=7,
            fontproperties=_chinese_font(),
            color=color,
            zorder=8,
        )


def _draw_strict_structure(axis, cl_data: ICL, times: Sequence[int]) -> None:
    evidence = cl_data.get_strict_evidence()
    snapshot = build_strict_structure_snapshot(
        evidence,
        interval=cl_data.get_frequency(),
    )
    levels = snapshot.get("levels")
    if not isinstance(levels, list):
        raise ValueError("strict snapshot levels are unavailable")
    level_zero = next(
        (
            value
            for value in levels
            if isinstance(value, Mapping) and value.get("structural_level") == 0
        ),
        None,
    )
    if level_zero is None:
        return
    _draw_center_payloads(
        axis,
        times,
        level_zero.get("centers", ()),
        color="#8e44ad",
        linestyle="-",
        alpha=0.18,
    )
    _draw_center_payloads(
        axis,
        times,
        level_zero.get("center_previews", ()),
        color="#f39c12",
        linestyle="--",
        alpha=0.15,
    )
    _draw_center_payloads(
        axis,
        times,
        level_zero.get("center_projections", ()),
        color="#f1c40f",
        linestyle=":",
        alpha=0.10,
    )
    _draw_points(
        axis,
        times,
        level_zero.get("confirmed_points", ()),
        approaching=False,
    )
    _draw_points(
        axis,
        times,
        level_zero.get("approaching_points", ()),
        approaching=True,
    )


def _aligned_htf_macd(
    cl_data: ICL,
    all_bars: Sequence[object],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]] | None:
    """返回与每根来源 K 线对齐、仅供绘图的平滑高周期 MACD。"""

    from chanlun.core.macd_htf import interpolate_causal_htf_for_chart

    result = getattr(cl_data, "_strict_htf_macd_by_level", {}).get(0)

    def valid(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        return all(
            isinstance(value.get(name), (list, tuple))
            and len(value[name]) == len(all_bars)
            for name in ("dif", "dea", "hist")
        )

    if not valid(result):
        return None
    result = interpolate_causal_htf_for_chart(result)
    if not valid(result):
        return None
    return tuple(
        tuple(_finite(item) for item in result[name])
        for name in ("dif", "dea", "hist")
    )


def _aligned_macd(
    cl_data: ICL,
    all_bars: Sequence[object],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]] | None:
    """返回与来源 K 线严格等长的标准 MACD。"""

    indexes = cl_data.get_idx()
    result = indexes.get("macd") if isinstance(indexes, Mapping) else None
    if not isinstance(result, Mapping):
        return None
    if any(
        not isinstance(result.get(name), (list, tuple))
        or len(result[name]) != len(all_bars)
        for name in ("dif", "dea", "hist")
    ):
        return None
    return tuple(
        tuple(_finite(item) for item in result[name])
        for name in ("dif", "dea", "hist")
    )


def _draw_macd_series(
    axis,
    aligned: tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]] | None,
    *,
    visible_count: int,
    label: str,
    dif_color: str,
    dea_color: str,
) -> None:
    """绘制一块带最新数值和动能强弱配色的 MACD 面板。"""

    axis.axhline(0.0, color="#9aa4b2", linewidth=0.55, alpha=0.75, zorder=1)
    if aligned is None:
        axis.text(
            0.01,
            0.90,
            f"{label}（历史不足）",
            transform=axis.transAxes,
            ha="left",
            va="top",
            fontsize=8,
            color="#6b7280",
            fontproperties=_chinese_font(),
        )
        return

    dif, dea, hist = (values[-visible_count:] for values in aligned)
    previous = 0.0
    colors: list[str] = []
    for value in hist:
        if value >= 0:
            colors.append("#ef5350" if value >= previous else "#ffcdd2")
        else:
            colors.append("#b2dfdb" if value > previous else "#26a69a")
        previous = value
    positions = tuple(range(visible_count))
    axis.bar(
        positions,
        hist,
        width=0.82,
        color=colors,
        edgecolor="none",
        zorder=2,
    )
    axis.plot(positions, dif, color=dif_color, linewidth=0.9, zorder=3)
    axis.plot(positions, dea, color=dea_color, linewidth=0.9, zorder=3)
    axis.text(
        0.01,
        0.92,
        f"{label}  DIF / DEA / 柱",
        transform=axis.transAxes,
        ha="left",
        va="top",
        fontsize=8,
        color="#374151",
        fontproperties=_chinese_font(),
    )
    axis.text(
        0.99,
        0.92,
        f"最新  DIF {dif[-1]:.4f}  DEA {dea[-1]:.4f}  柱 {hist[-1]:.4f}",
        transform=axis.transAxes,
        ha="right",
        va="top",
        fontsize=7.5,
        color="#4b5563",
        fontproperties=_chinese_font(),
    )


def _draw_macd(
    axis,
    cl_data: ICL,
    all_bars: Sequence[object],
    visible_count: int,
) -> None:
    config = cl_data.get_config()
    values = config if isinstance(config, Mapping) else {}
    fast = int(values.get("idx_macd_fast", 12))
    slow = int(values.get("idx_macd_slow", 26))
    signal = int(values.get("idx_macd_signal", 9))
    _draw_macd_series(
        axis,
        _aligned_macd(cl_data, all_bars),
        visible_count=visible_count,
        label=f"MACD {fast}/{slow}/{signal}",
        dif_color="#2962ff",
        dea_color="#ff6d00",
    )


def _draw_macd_htf(
    axis,
    cl_data: ICL,
    all_bars: Sequence[object],
    visible_count: int,
) -> None:
    from chanlun.core.macd_htf import HIGHER_FREQ_MAP

    aligned = _aligned_htf_macd(cl_data, all_bars)
    frequency = cl_data.get_frequency()
    higher = HIGHER_FREQ_MAP.get(frequency, "高一级")
    _draw_macd_series(
        axis,
        aligned,
        visible_count=visible_count,
        label=f"MACD_HTF {frequency}→{higher}",
        dif_color="#7c3aed",
        dea_color="#ca8a04",
    )


def _time_ticks(bars: Sequence[object]) -> tuple[list[int], list[str]]:
    tick_count = min(7, len(bars))
    if not tick_count:
        return [], []
    positions = sorted(
        {
            round(index * (len(bars) - 1) / max(tick_count - 1, 1))
            for index in range(tick_count)
        }
    )
    return positions, [
        bars[index].date.strftime("%m-%d\n%H:%M") for index in positions
    ]


def _style_axis(axis) -> None:
    axis.yaxis.tick_right()
    axis.grid(True, color="#dfe4ea", linewidth=0.45, alpha=0.75)
    axis.set_facecolor("#ffffff")
    axis.tick_params(axis="both", labelsize=7, colors="#4b5563")
    for spine in axis.spines.values():
        spine.set_color("#cfd6df")
        spine.set_linewidth(0.6)


def _draw_chart(
    price_axis,
    macd_axis,
    macd_htf_axis,
    title: str,
    cl_data: ICL,
    kline_count: int,
) -> None:
    all_bars = tuple(cl_data.get_src_klines())
    bars = all_bars[-kline_count:]
    if not bars:
        raise ValueError(f"{title} has no completed K-lines")
    times = tuple(_epoch(bar.date) for bar in bars)
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise ValueError(f"{title} K-line timestamps must be strictly increasing")

    _draw_candles(price_axis, bars)
    _draw_segments(price_axis, cl_data, times)
    _draw_strict_structure(price_axis, cl_data, times)
    _draw_macd(macd_axis, cl_data, all_bars, len(bars))
    _draw_macd_htf(macd_htf_axis, cl_data, all_bars, len(bars))

    price_axis.set_title(
        f"{title} · {len(bars):,}根 · 截止 {bars[-1].date.strftime('%m-%d %H:%M')}",
        loc="left",
        fontsize=11,
        fontweight="bold",
        fontproperties=_chinese_font(),
    )
    price_axis.set_xlim(-1, len(bars))
    macd_axis.set_xlim(-1, len(bars))
    macd_htf_axis.set_xlim(-1, len(bars))
    _style_axis(price_axis)
    _style_axis(macd_axis)
    _style_axis(macd_htf_axis)
    price_axis.tick_params(axis="x", labelbottom=False)
    macd_axis.tick_params(axis="x", labelbottom=False)
    positions, labels = _time_ticks(bars)
    macd_htf_axis.set_xticks(positions)
    macd_htf_axis.set_xticklabels(labels)


def render_multi_timeframe_png(
    charts: Iterable[tuple[str, ICL]],
    *,
    width: int = 1200,
    height_per_chart: int = 720,
    kline_count: int = 1200,
) -> bytes:
    """返回一张纵向对齐物理 30m、5m、1m 图表的 PNG 图片。"""

    values = tuple(charts)
    if not values:
        raise ValueError("at least one chart is required")
    if isinstance(width, bool) or not isinstance(width, int) or width < 320:
        raise ValueError("width must be an integer of at least 320 pixels")
    if (
        isinstance(height_per_chart, bool)
        or not isinstance(height_per_chart, int)
        or height_per_chart < 480
    ):
        raise ValueError("height_per_chart must be at least 480 pixels")
    if isinstance(kline_count, bool) or not isinstance(kline_count, int) or kline_count <= 0:
        raise ValueError("kline_count must be a positive integer")

    with _RENDER_LOCK:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        figure = Figure(
            figsize=(width / 100, height_per_chart * len(values) / 100),
            dpi=100,
            facecolor="white",
            constrained_layout=True,
        )
        FigureCanvasAgg(figure)
        grid = figure.add_gridspec(
            len(values) * 3,
            1,
            height_ratios=[3.45, 1.15, 1.15] * len(values),
        )
        for index, (title, cl_data) in enumerate(values):
            price_axis = figure.add_subplot(grid[index * 3, 0])
            macd_axis = figure.add_subplot(
                grid[index * 3 + 1, 0],
                sharex=price_axis,
            )
            macd_htf_axis = figure.add_subplot(
                grid[index * 3 + 2, 0],
                sharex=price_axis,
            )
            _draw_chart(
                price_axis,
                macd_axis,
                macd_htf_axis,
                title,
                cl_data,
                kline_count,
            )
        output = BytesIO()
        figure.savefig(output, format="png", dpi=100, facecolor="white")
        figure.clear()
        data = output.getvalue()

    if not data.startswith(_PNG_HEADER):
        raise RuntimeError("chart renderer returned an invalid PNG")
    return data


__all__ = ["render_multi_timeframe_png"]
