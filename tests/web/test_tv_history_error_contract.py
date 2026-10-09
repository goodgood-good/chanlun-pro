from cl_app import create_app
from cl_app.blueprints import tv as tv_module
from cl_app.services import chart_compute
from chanlun.exchange.errors import LongbridgeConnectionLimitError, USmartAPIError
from types import SimpleNamespace

import pytest


def test_internal_history_failure_is_not_reported_as_no_data(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    monkeypatch.setattr(
        tv_module,
        "query_cl_chart_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    try:
        response = app.test_client().get(
            "/tv/history?symbol=a:SZ.000001&resolution=1&from=1&to=2"
        )
    finally:
        app.extensions["shutdown_runtime_services"]()

    assert response.status_code == 503
    assert response.get_json() == {
        "s": "error",
        "errmsg": "History service is temporarily unavailable.",
    }


@pytest.mark.parametrize("first", ["true", "false"])
@pytest.mark.parametrize("provider", ["longbridge", "usmart"])
def test_provider_failure_reaches_history_as_retryable_error_not_empty_data(monkeypatch, first, provider):
    app = create_app(test_config={
        "TESTING": True, "LOGIN_DISABLED": True, "VALIDATE_WEB_SECURITY": False,
        "WTF_CSRF_ENABLED": False,
    })
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        if provider == "longbridge":
            raise LongbridgeConnectionLimitError()
        raise USmartAPIError("upstream HTTP 503", status_code=503)

    # Keep the real route -> chart fetch -> provider boundary. Failed history
    # must not become no_data, seed an empty cache or expose raw SDK details.
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _: SimpleNamespace(klines=unavailable))
    monkeypatch.setattr(tv_module, "query_cl_chart_config", lambda *_: {})
    monkeypatch.setattr(tv_module, "market_now_trading", lambda _: True)
    monkeypatch.setattr(tv_module, "_should_suppress_realtime_history_poll", lambda **_: False)
    monkeypatch.setattr(tv_module, "_mark_user_request", lambda *_: None)
    monkeypatch.setattr(tv_module, "_get_chart_cache_entry_ram_only", lambda _: None)
    monkeypatch.setattr(tv_module, "_get_chart_cache_entry", lambda _: None)
    monkeypatch.setattr(tv_module, "market_frequencys", SimpleNamespace(cached_snapshot=lambda _: {"us": ["30m"]}))
    monkeypatch.setattr(tv_module, "_set_chart_cache_entry", lambda *_a, **_k: pytest.fail("failed history must not overwrite chart cache"))
    try:
        response = app.test_client().get("/tv/history", query_string={
            "symbol": "us:TSLA.US", "resolution": "30", "firstDataRequest": first,
            "from": 2000, "to": 3000,
        })
    finally:
        app.extensions["shutdown_runtime_services"]()
    assert len(calls) == 1
    assert response.status_code == 503
    if provider == "usmart":
        assert response.headers["Retry-After"] == "15"
        assert response.get_json() == {
            "s": "error", "error_code": "usmart_history_unavailable",
            "errmsg": "盈立行情暂不可用，请稍后重试。", "retry_after": 15,
        }
    else:
        assert response.headers["Retry-After"] == "30"
        assert response.get_json() == {
        "s": "error", "error_code": "longbridge_connection_limit",
        "errmsg": "行情连接数已达上限，请稍后重试。", "retry_after": 30,
        }
