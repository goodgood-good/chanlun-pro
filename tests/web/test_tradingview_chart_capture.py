from __future__ import annotations

import base64

import pytest

from cl_app.services import tradingview_chart_capture as capture_module
from cl_app.services.tradingview_chart_capture import (
    DEFAULT_CAPTURE_SPECS,
    REQUIRED_CAPTURE_STUDIES,
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
    assert renderer.viewport_height == 1400
    assert REQUIRED_CAPTURE_STUDIES == ("MACD", "MACD_HTF")
    assert renderer._url("a", "SH.513100", DEFAULT_CAPTURE_SPECS[0]).endswith(
        "market=a&code=SH.513100&layout=single&intervals=30&"
        "chart_sidebar=collapsed&default_study=MACD_HTF&default_study=MACD"
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


def test_capture_timeout_is_one_total_budget(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: clock[0])
    renderer = TradingViewClientScreenshotRenderer(
        base_url="http://127.0.0.1:9900",
        session_cookie_provider=lambda: "signed-session",
        timeout_ms=5_000,
    )

    clock[0] += 3.25
    assert renderer._remaining_timeout_ms(100.0) == 1_750
    clock[0] += 0.751
    with pytest.raises(TimeoutError, match="total time budget"):
        renderer._remaining_timeout_ms(100.0)
