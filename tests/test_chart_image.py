from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
import math
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from PIL import Image

import chanlun.chart_image as chart_module


def test_notification_chart_renders_standard_and_higher_timeframe_macd(
    monkeypatch,
) -> None:
    count = 90
    start = datetime(2026, 8, 28, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    bars = tuple(
        SimpleNamespace(
            date=start + timedelta(minutes=5 * index),
            o=10.0 + index * 0.01,
            h=10.12 + index * 0.01,
            l=9.94 + index * 0.01,
            c=10.04 + index * 0.01,
        )
        for index in range(count)
    )
    dif = [math.sin(index / 9) * 0.08 for index in range(count)]
    dea = [math.sin((index - 2) / 9) * 0.065 for index in range(count)]
    hist = [(left - right) * 2 for left, right in zip(dif, dea, strict=True)]

    class Chart:
        _strict_htf_macd_by_level = {
            0: {
                "dif": [value * 0.7 for value in dif],
                "dea": [value * 0.7 for value in dea],
                "hist": [value * 0.7 for value in hist],
                "bucket_keys": [index // 6 for index in range(count)],
            }
        }

        @staticmethod
        def get_src_klines():
            return bars

        @staticmethod
        def get_idx():
            return {"macd": {"dif": dif, "dea": dea, "hist": hist}}

        @staticmethod
        def get_config():
            return {
                "idx_macd_fast": 12,
                "idx_macd_slow": 26,
                "idx_macd_signal": 9,
            }

        @staticmethod
        def get_frequency():
            return "5m"

        @staticmethod
        def get_xds():
            return ()

        @staticmethod
        def get_strict_evidence():
            return object()

    monkeypatch.setattr(
        chart_module,
        "build_strict_structure_snapshot",
        lambda *_args, **_kwargs: {
            "levels": [
                {
                    "structural_level": 0,
                    "centers": [],
                    "center_previews": [],
                    "center_projections": [],
                    "confirmed_points": [],
                    "approaching_points": [],
                }
            ]
        },
    )
    labels = []
    original_draw = chart_module._draw_macd_series

    def record_panel(axis, aligned, **kwargs):
        labels.append(kwargs["label"])
        return original_draw(axis, aligned, **kwargs)

    monkeypatch.setattr(chart_module, "_draw_macd_series", record_panel)

    png = chart_module.render_multi_timeframe_png(
        (("示例 SZ.000001 · 5分钟", Chart()),),
        width=800,
        height_per_chart=720,
        kline_count=60,
    )

    assert labels == ["MACD 12/26/9", "MACD_HTF 5m→30m"]
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.size == (800, 720)
