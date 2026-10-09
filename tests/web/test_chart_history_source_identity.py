"""Changing a delegated history provider cannot restore the other price basis."""
from chanlun import config
from cl_app.services.chart_cache import _build_cache_key


def test_usmart_and_longbridge_history_have_separate_chart_caches(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_US", "usmart")
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
    initial = _build_cache_key("us", "QQQ.US", "5m", {})
    mainland = _build_cache_key("a", "SH.513100", "5m", {})
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")
    assert _build_cache_key("us", "QQQ.US", "5m", {}) != initial
    assert _build_cache_key("a", "SH.513100", "5m", {}) == mainland
