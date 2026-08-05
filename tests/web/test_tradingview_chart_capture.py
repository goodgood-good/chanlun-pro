from __future__ import annotations

import base64

import pytest

from cl_app.services.tradingview_chart_capture import (
    DEFAULT_CAPTURE_SPECS,
    TradingViewClientScreenshotRenderer,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"capture"


def test_capture_contract_is_loopback_and_uses_longer_ranges() -> None:
    renderer = TradingViewClientScreenshotRenderer(
        base_url="http://127.0.0.1:9900",
        session_cookie_provider=lambda: "signed-session",
    )
    assert [
        (value.frequency, value.lookback_days, value.bar_spacing)
        for value in renderer.specs
    ] == [
        ("30m", 180, 1.2),
        ("5m", 45, 0.8),
        ("1m", 10, 0.55),
    ]
    assert renderer.viewport_width == 2400
    assert renderer._url("a", "SH.513100", DEFAULT_CAPTURE_SPECS[0]).endswith(
        "market=a&code=SH.513100&layout=single&intervals=30&"
        "chart_sidebar=collapsed&default_study=MACD_HTF"
    )
    with pytest.raises(ValueError, match="loopback-only"):
        TradingViewClientScreenshotRenderer(
            base_url="http://47.96.40.233:8890",
            session_cookie_provider=lambda: "signed-session",
        )


def test_capture_decodes_only_png_data_urls() -> None:
    encoded = base64.b64encode(PNG).decode("ascii")
    assert (
        TradingViewClientScreenshotRenderer._decode_png(
            "data:image/png;base64," + encoded
        )
        == PNG
    )
    with pytest.raises(RuntimeError, match="PNG data URL"):
        TradingViewClientScreenshotRenderer._decode_png("https://example.test/a.png")
