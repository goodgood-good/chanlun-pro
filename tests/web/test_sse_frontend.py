"""Task8-11: 前端 SSE 接入静态断言(无 JS 测试框架, 读文件正则校验关键接入点)。

真实行为验证靠交易时段浏览器实测(Phase D Task13)。
"""
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BUNDLE = _ROOT / "web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js"
_CHARTS = _ROOT / "web/chanlun_chart/cl_app/static/js/charts.js"
_INDEX = _ROOT / "web/chanlun_chart/cl_app/templates/index.html"

BUNDLE = _BUNDLE.read_text(encoding="utf-8")
CHARTS = _CHARTS.read_text(encoding="utf-8")
INDEX = _INDEX.read_text(encoding="utf-8")


def test_bundle_exposes_apply_chanlun_update():
    # SSE onmessage 与 getBars 共用同一份 response→bars_result 合并逻辑。
    assert "applyChanlunUpdate" in BUNDLE
    assert "_processHistoryResponse" in BUNDLE


def test_charts_opens_eventsource_stream():
    assert "new EventSource" in CHARTS
    assert "/tv/stream" in CHARTS
    assert "_openSseStream" in CHARTS
    # onmessage 复用 datafeed 合并入口
    assert "applyChanlunUpdate" in CHARTS
    # 监听具名事件 chanlun(非默认 message)
    assert "addEventListener('chanlun'" in CHARTS


def test_charts_visibility_fallback_registered():
    assert "visibilitychange" in CHARTS
    assert "_visibilityHandler" in CHARTS


def test_charts_closes_stream_on_dispose():
    assert "_closeSseStream" in CHARTS


def test_index_injects_sse_flag():
    assert "__CHANLUN_SSE_ENABLED" in INDEX
    assert "enable_sse" in INDEX
