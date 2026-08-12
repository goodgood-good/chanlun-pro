"""Capture the authenticated TradingView page for DingTalk alert images.

The browser path is intentionally loopback-only.  It uses an in-memory Flask
session cookie supplied by the running app, loads the same chart/datafeed and
strict overlays that the operator sees, and calls Charting Library's public
``takeClientScreenshot`` API.  No password, account fact or order capability is
passed to the browser.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import base64
from urllib.parse import urlencode, urlsplit


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True, slots=True)
class TradingViewCaptureSpec:
    frequency: str
    interval: str
    lookback_days: int
    label: str
    bar_spacing: float


DEFAULT_CAPTURE_SPECS = (
    TradingViewCaptureSpec("30m", "30", 180, "30分钟", 1.20),
    TradingViewCaptureSpec("5m", "5", 45, "5分钟", 0.80),
    TradingViewCaptureSpec("1m", "1", 10, "1分钟", 0.55),
)


class TradingViewClientScreenshotRenderer:
    """Render physical charts through the page's own screenshot capability."""

    def __init__(
        self,
        *,
        base_url: str,
        session_cookie_provider: Callable[[], str],
        specs: Sequence[TradingViewCaptureSpec] = DEFAULT_CAPTURE_SPECS,
        viewport_width: int = 2400,
        viewport_height: int = 1100,
        timeout_ms: int = 45_000,
    ) -> None:
        parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("TradingView capture URL must be HTTP(S)")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("TradingView capture URL must be loopback-only")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("TradingView capture URL must not contain credentials")
        if not callable(session_cookie_provider):
            raise TypeError("session_cookie_provider must be callable")
        values = tuple(specs)
        if not values or len({item.frequency for item in values}) != len(values):
            raise ValueError("TradingView capture specs must be non-empty and unique")
        if any(item.lookback_days <= 0 for item in values):
            raise ValueError("TradingView capture lookback must be positive")
        if any(item.bar_spacing <= 0 for item in values):
            raise ValueError("TradingView capture bar spacing must be positive")
        if viewport_width < 800 or viewport_height < 600:
            raise ValueError("TradingView capture viewport is too small")
        if timeout_ms < 5_000:
            raise ValueError("TradingView capture timeout is too small")
        self.base_url = parsed.geturl().rstrip("/")
        self._session_cookie_provider = session_cookie_provider
        self.specs = values
        self.viewport_width = int(viewport_width)
        self.viewport_height = int(viewport_height)
        self.timeout_ms = int(timeout_ms)

    def _url(self, market: str, code: str, spec: TradingViewCaptureSpec) -> str:
        query = urlencode(
            {
                "market": market,
                "code": code,
                "layout": "single",
                "intervals": spec.interval,
                "chart_sidebar": "collapsed",
                "default_study": "MACD_HTF",
            }
        )
        return f"{self.base_url}/?{query}"

    @staticmethod
    def _decode_png(data_url: object) -> bytes:
        value = str(data_url or "")
        prefix = "data:image/png;base64,"
        if not value.startswith(prefix):
            raise RuntimeError("TradingView screenshot did not return a PNG data URL")
        try:
            png = base64.b64decode(value[len(prefix) :], validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("TradingView screenshot PNG is invalid") from exc
        if not png.startswith(_PNG_HEADER):
            raise RuntimeError("TradingView screenshot PNG header is invalid")
        return png

    def __call__(
        self,
        *,
        market: str,
        code: str,
        name: str,
    ) -> Sequence[Mapping[str, object]]:
        del name  # 已认证页面用于解析规范展示名称。
        cookie = str(self._session_cookie_provider() or "")
        if not cookie:
            raise RuntimeError("TradingView capture session is unavailable")

        from playwright.sync_api import sync_playwright

        output: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={
                        "width": self.viewport_width,
                        "height": self.viewport_height,
                    },
                    device_scale_factor=1,
                )
                try:
                    context.add_cookies(
                        [
                            {
                                "name": "session",
                                "value": cookie,
                                "url": self.base_url,
                                "httpOnly": True,
                                "sameSite": "Lax",
                            }
                        ]
                    )
                    for spec in self.specs:
                        page = context.new_page()
                        try:
                            page.goto(
                                self._url(market, code, spec),
                                wait_until="domcontentloaded",
                                timeout=self.timeout_ms,
                            )
                            if urlsplit(page.url).path == "/login":
                                raise RuntimeError(
                                    "TradingView capture session was rejected"
                                )
                            page.wait_for_function(
                                """() => {
                                    try {
                                        const widget = window.tvWidget;
                                        if (!widget || typeof widget.takeClientScreenshot !== 'function') return false;
                                        const chart = widget.activeChart && widget.activeChart();
                                        if (!chart || !chart.dataReady || !chart.dataReady()) return false;
                                        const studies = chart.getAllStudies ? chart.getAllStudies() : [];
                                        return studies.some(value => value && value.name === 'MACD_HTF');
                                    } catch (_error) {
                                        return false;
                                    }
                                }""",
                                timeout=self.timeout_ms,
                            )
                            capture = page.evaluate(
                                """async ({lookbackDays, barSpacing}) => {
                                    const widget = window.tvWidget;
                                    const chart = widget.activeChart();
                                    const visible = chart.getVisibleRange();
                                    const to = Number(visible && visible.to) || Math.floor(Date.now() / 1000);
                                    const targetFrom = to - lookbackDays * 86400;
                                    const timeScale = chart.getTimeScale && chart.getTimeScale();
                                    // Charting Library may ignore `from` when the requested
                                    // range does not fit the canvas. Reduce bar spacing first,
                                    // then request the range. This preserves the exact frontend
                                    // rendering path while allowing dense 1m history to fit.
                                    if (timeScale && typeof timeScale.setBarSpacing === 'function') {
                                        timeScale.setBarSpacing(barSpacing);
                                    }
                                    await chart.setVisibleRange(
                                        {from: targetFrom, to},
                                        {percentRightMargin: 1, rejectByTimeout: 20000}
                                    );
                                    if (timeScale && typeof timeScale.setBarSpacing === 'function') {
                                        timeScale.setBarSpacing(barSpacing);
                                    }
                                    await new Promise(resolve => setTimeout(resolve, 1800));
                                    if (!chart.dataReady()) throw new Error('chart data is not ready');
                                    const studies = chart.getAllStudies();
                                    if (!studies.some(value => value && value.name === 'MACD_HTF')) {
                                        throw new Error('MACD_HTF is absent');
                                    }
                                    const canvas = await widget.takeClientScreenshot({
                                        hideStudiesFromLegend: false,
                                        hideResolution: false,
                                    });
                                    const finalRange = chart.getVisibleRange();
                                    return {
                                        dataUrl: canvas.toDataURL('image/png'),
                                        visibleFrom: Number(finalRange && finalRange.from) || 0,
                                        visibleTo: Number(finalRange && finalRange.to) || 0,
                                    };
                                }""",
                                {
                                    "lookbackDays": spec.lookback_days,
                                    "barSpacing": spec.bar_spacing,
                                },
                            )
                            if not isinstance(capture, Mapping):
                                raise RuntimeError(
                                    "TradingView screenshot result is invalid"
                                )
                            visible_from = int(capture.get("visibleFrom") or 0)
                            visible_to = int(capture.get("visibleTo") or 0)
                            requested_seconds = spec.lookback_days * 24 * 60 * 60
                            if visible_to - visible_from < requested_seconds * 0.70:
                                raise RuntimeError(
                                    "TradingView screenshot range is shorter than requested"
                                )
                            output.append(
                                {
                                    "frequency": spec.frequency,
                                    "label": spec.label,
                                    "lookback_days": spec.lookback_days,
                                    "visible_from": visible_from,
                                    "visible_to": visible_to,
                                    "png": self._decode_png(capture.get("dataUrl")),
                                }
                            )
                        finally:
                            page.close()
                finally:
                    context.close()
            finally:
                browser.close()
        if len(output) != len(self.specs):
            raise RuntimeError("TradingView capture did not return every timeframe")
        return tuple(output)


__all__ = [
    "DEFAULT_CAPTURE_SPECS",
    "TradingViewCaptureSpec",
    "TradingViewClientScreenshotRenderer",
]
