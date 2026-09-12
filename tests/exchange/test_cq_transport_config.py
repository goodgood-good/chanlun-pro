from types import SimpleNamespace

import pytest

from chanlun.exchange import exchange_cq


@pytest.mark.parametrize("direct", [False, True])
def test_quote_proxy_bypass_preserves_other_routes_and_config(monkeypatch, direct):
    calls = []
    monkeypatch.setattr(exchange_cq, "Config", SimpleNamespace(
        from_apikey=lambda *args, **kwargs: calls.append((args, kwargs)) or "config",
    ))
    for key in ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN"):
        monkeypatch.setenv("LONGBRIDGE_" + key, "test-only")
    monkeypatch.setenv("LONGBRIDGE_HTTP_URL", "https://openapi.longbridge.cn")
    monkeypatch.setenv("LONGBRIDGE_QUOTE_WS_URL", "wss://openapi-quote.longbridge.cn")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:10808")
    monkeypatch.setenv("NO_PROXY", "localhost,internal.example")
    # On Windows environment variable names are case-insensitive.
    monkeypatch.setenv("no_proxy", "localhost,internal.example")
    before = {key: exchange_cq.os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    if direct:
        monkeypatch.setenv("CHANLUN_LONGBRIDGE_DIRECT_QUOTES", "true")
    else:
        monkeypatch.delenv("CHANLUN_LONGBRIDGE_DIRECT_QUOTES", raising=False)

    assert exchange_cq._build_longbridge_config() == "config"
    assert exchange_cq._build_longbridge_config() == "config"
    assert exchange_cq.os.environ["HTTPS_PROXY"] == "http://proxy.example:10808"
    assert calls[-1][1]["http_url"] == "https://openapi.longbridge.cn"
    assert calls[-1][1]["quote_ws_url"] == "wss://openapi-quote.longbridge.cn"
    if direct:
        expected = {
            "localhost", "internal.example", "openapi.longbridge.com",
            "openapi-quote.longbridge.com", "openapi.longbridge.cn",
            "openapi-quote.longbridge.cn",
        }
        for key in before:
            hosts = exchange_cq.os.environ[key].split(",")
            assert set(hosts) == expected
            assert len(hosts) == len(expected)
    else:
        assert {key: exchange_cq.os.environ.get(key) for key in before} == before
