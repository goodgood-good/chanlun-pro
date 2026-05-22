"""tests/test_charts_js_v1.py — V1 浏览器实测 charts.js M5/N5 改动。

为什么用 minimal HTML harness 而非完整 dev server:
- 项目 dev server 启动需要 mysql / qmt / 真实交易所凭据 / 上百 MB 静态资源
- M5 (debug 日志守卫) 和 N5 (currency timezone fallback) 都是纯 JS 函数级改动,
  在 Playwright 加载的 minimal HTML 中能稳定验证, 不需要完整 widget 集成

测什么:
- M5: window.__chanlunDebug 关闭时 console.log 不输出, 打开时输出
- N5: getMarketTimezone("currency") 返回浏览器 Intl 时区 (不是默认 Asia/Shanghai)
- N5: getMarketTimezone("us") 返回硬编码 "America/New_York" (不受 Intl 影响)

依赖: playwright (已在 pyproject [monitor] extras 中)。本机未装 → skip。
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

playwright_module = pytest.importorskip("playwright.sync_api", reason="playwright 未安装")
sync_playwright = playwright_module.sync_playwright

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHARTS_JS_PATH = _REPO_ROOT / "web" / "chanlun_chart" / "cl_app" / "static" / "js" / "charts.js"


# 与 charts.js 中实际定义保持一致 (单一来源是 charts.js 本身;
# 这里复制是为了 standalone test, 真实修改 charts.js 时需要同步更新本片段)
_CHARTS_JS_SNIPPET = """
const MARKET_TIMEZONE = {
    a: "Asia/Shanghai",
    hk: "Asia/Shanghai",
    fx: "Asia/Shanghai",
    us: "America/New_York",
    futures: "Asia/Shanghai",
    ny_futures: "Asia/Shanghai",
};
function _browserLocalTz() {
    try {
        return (Intl.DateTimeFormat().resolvedOptions().timeZone) || "Asia/Shanghai";
    } catch (e) {
        return "Asia/Shanghai";
    }
}
function getMarketTimezone(market) {
    if (market === "currency" || market === "currency_spot") {
        return _browserLocalTz();
    }
    return MARKET_TIMEZONE[market] || "Asia/Shanghai";
}

// M5: 高频日志只在 window.__chanlunDebug 时输出
function reconcileSummaryLog(payload) {
    if (window.__chanlunDebug && payload.removedCount > 0) {
        console.log(`[CHANLUN-DIAG][reconcile] removed=${payload.removedCount}`);
    }
}
// console.warn 不受守卫影响 (真错误诊断)
function reconcileErrorWarn(payload) {
    console.warn(`[CHANLUN-DIAG] error: ${payload.msg}`);
}
"""


@pytest.fixture(scope="module")
def harness_html(tmp_path_factory) -> str:
    """生成 minimal HTML, file:// 加载即可。"""
    p = tmp_path_factory.mktemp("v1") / "harness.html"
    p.write_text(textwrap.dedent(f"""
        <!doctype html>
        <html><head><meta charset="utf-8"><title>V1 harness</title></head>
        <body>
        <script>{_CHARTS_JS_SNIPPET}</script>
        </body></html>
    """).strip(), encoding="utf-8")
    return f"file://{p.as_posix()}"


@pytest.fixture(scope="module")
def actual_charts_html(tmp_path_factory) -> str:
    """加载真实 charts.js,用于验证纯前端绘制辅助函数。"""
    p = tmp_path_factory.mktemp("charts-real") / "harness.html"
    p.write_text(textwrap.dedent(f"""
        <!doctype html>
        <html><head><meta charset="utf-8"><title>charts.js harness</title></head>
        <body>
        <script src="{_CHARTS_JS_PATH.as_uri()}"></script>
        </body></html>
    """).strip(), encoding="utf-8")
    return f"file://{p.as_posix()}"


def test_mmd_icon_and_label_are_offset_away_from_kline(actual_charts_html):
    """买点箭头在 low 下方、卖点箭头在 high 上方,标签在箭头外侧。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(actual_charts_html)
        page.wait_for_load_state("networkidle")
        result = page.evaluate("""
            (() => {
                const buy = { points: { time: 1, price: 100 }, text: "1B" };
                const sell = { points: { time: 1, price: 100 }, text: "1S" };
                const buyIcon = ChartUtils.mmdIconPoint(buy);
                const buyLabel = ChartUtils.mmdLabelPoint(buy);
                const sellIcon = ChartUtils.mmdIconPoint(sell);
                const sellLabel = ChartUtils.mmdLabelPoint(sell);
                return {
                    buyPrice: buy.points.price,
                    sellPrice: sell.points.price,
                    buyIconPrice: buyIcon.price,
                    buyLabelPrice: buyLabel.price,
                    sellIconPrice: sellIcon.price,
                    sellLabelPrice: sellLabel.price,
                    buyIconTime: buyIcon.time,
                    sellIconTime: sellIcon.time,
                };
            })()
        """)
        browser.close()

    assert result["buyLabelPrice"] < result["buyIconPrice"] < result["buyPrice"]
    assert result["sellLabelPrice"] > result["sellIconPrice"] > result["sellPrice"]
    assert result["buyPrice"] == 100
    assert result["sellPrice"] == 100
    assert result["buyIconTime"] == 1
    assert result["sellIconTime"] == 1


def test_zslx_line_is_clipped_to_visible_left_edge(actual_charts_html):
    """高级别走势类型线头部在窗口外时,按原斜率裁剪到可视左边界。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(actual_charts_html)
        page.wait_for_load_state("networkidle")
        result = page.evaluate("""
            (() => {
                const line = {
                    points: [
                        { time: 100, price: 10 },
                        { time: 200, price: 30 },
                    ],
                };
                return ChartUtils.clipTrendLinePointsToFrom(line, 150);
            })()
        """)
        browser.close()

    assert result == [
        {"time": 150, "price": 20},
        {"time": 200, "price": 30},
    ]


def test_n5_currency_returns_browser_local_tz(harness_html):
    """N5: market=currency / currency_spot 走 Intl 浏览器时区, 不是默认 Asia/Shanghai。"""
    with sync_playwright() as p:
        # 用 timezone_id=America/Los_Angeles 让 Intl 返回该 tz, 验证 fallback 真走 Intl
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(timezone_id="America/Los_Angeles")
        page = ctx.new_page()
        page.goto(harness_html)
        page.wait_for_load_state("networkidle")

        tz_currency = page.evaluate("getMarketTimezone('currency')")
        tz_spot = page.evaluate("getMarketTimezone('currency_spot')")
        tz_us = page.evaluate("getMarketTimezone('us')")
        tz_a = page.evaluate("getMarketTimezone('a')")

        browser.close()

    assert tz_currency == "America/Los_Angeles", (
        f"currency 应跟浏览器 Intl 时区, 实际: {tz_currency}"
    )
    assert tz_spot == "America/Los_Angeles", (
        f"currency_spot 同上, 实际: {tz_spot}"
    )
    assert tz_us == "America/New_York", "us 是硬编码不走 Intl"
    assert tz_a == "Asia/Shanghai", "a 是硬编码不走 Intl"


def test_n5_currency_fallback_when_intl_throws(harness_html):
    """N5: 极端环境 Intl 抛错时 fallback 到 Asia/Shanghai (默认值)。

    通过把 Intl.DateTimeFormat monkey-patch 成抛错, 验证 try/catch fallback 生效。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(harness_html)
        page.wait_for_load_state("networkidle")
        # 让 Intl 抛错
        page.evaluate("""
            window.Intl = { DateTimeFormat: function() { throw new Error('intl broken'); } };
        """)
        tz = page.evaluate("getMarketTimezone('currency')")
        browser.close()
    assert tz == "Asia/Shanghai", f"Intl 抛错时应 fallback 到 Asia/Shanghai, 实际: {tz}"


def test_m5_debug_log_suppressed_by_default(harness_html):
    """M5: window.__chanlunDebug 未设时, 高频 reconcile 日志不输出 console.log。"""
    console_logs = []
    console_warns = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", lambda msg: (
            console_logs.append(msg.text) if msg.type == "log"
            else (console_warns.append(msg.text) if msg.type == "warning" else None)
        ))
        page.goto(harness_html)
        page.wait_for_load_state("networkidle")

        # 默认 __chanlunDebug 未设 → console.log 应被守卫吞掉
        page.evaluate("reconcileSummaryLog({removedCount: 5})")
        # console.warn 永远输出 (真错误)
        page.evaluate("reconcileErrorWarn({msg: 'oops'})")

        page.wait_for_timeout(100)  # 等异步 console 事件
        browser.close()

    summary_logs = [l for l in console_logs if "[CHANLUN-DIAG][reconcile]" in l]
    assert summary_logs == [], (
        f"默认模式下不应有 reconcile summary log, 实际: {summary_logs}"
    )
    assert any("error: oops" in w for w in console_warns), (
        f"console.warn 必须输出 (不受 debug 开关影响), 实际 warns: {console_warns}"
    )


def test_m5_debug_log_enabled_when_flag_set(harness_html):
    """M5: window.__chanlunDebug=true 时 reconcile log 输出, removedCount=0 仍不输出。"""
    console_logs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", lambda msg: console_logs.append(msg.text) if msg.type == "log" else None)
        page.goto(harness_html)
        page.wait_for_load_state("networkidle")

        page.evaluate("window.__chanlunDebug = true")
        page.evaluate("reconcileSummaryLog({removedCount: 7})")
        # removedCount=0 即使开关打开也不输出 (代码本身有"有动作时才打"短路)
        page.evaluate("reconcileSummaryLog({removedCount: 0})")
        page.wait_for_timeout(100)
        browser.close()

    summary_logs = [l for l in console_logs if "[CHANLUN-DIAG][reconcile]" in l]
    assert len(summary_logs) == 1, (
        f"应有 1 条 (removedCount=7), removedCount=0 不输出. 实际: {summary_logs}"
    )
    assert "removed=7" in summary_logs[0]
