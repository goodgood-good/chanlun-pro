import json
import os
from threading import Event
from types import SimpleNamespace

import pytest

from cl_app import create_app
from cl_app.blueprints import other as other_mod
from cl_app.services import constants as constants_service
from cl_app.services import readiness as readiness_service
from cl_app.services import stock_list as stock_list_service


def _raise_external_call(*_args, **_kwargs):
    raise AssertionError("readiness endpoint attempted an external call")


class _FakeExchange:
    def ticks(self, codes):
        return {
            code: SimpleNamespace(last=2.5, rate=1.25)
            for code in codes
        }


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("CHANLUN_BUILD_REVISION", "test-revision")
    flask_app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    monkeypatch.setattr(
        constants_service.market_frequencys,
        "status",
        lambda _market: {"state": "loaded", "ready": True},
    )
    monkeypatch.setattr(
        constants_service.market_default_codes,
        "status",
        lambda _market: {"state": "loaded", "ready": True},
    )
    monkeypatch.setattr(
        stock_list_service,
        "get_symbol_readiness",
        lambda market: {
            "market": market,
            "ready": True,
            "status": "ready",
            "count": 1,
            "last_error": None,
        },
        raising=False,
    )
    yield flask_app
    flask_app.extensions["shutdown_scheduler"]()


def test_livez_and_healthz_have_distinct_compatible_contracts(app):
    client = app.test_client()

    live = client.get("/livez")
    health = client.get("/healthz")

    assert live.status_code == 200
    assert live.get_json() == {"status": "alive", "revision": "test-revision"}
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"
    assert health.get_json()["revision"] == "test-revision"


def test_index_loads_only_requested_market_metadata(app, monkeypatch):
    markets = tuple(constants_service.market_types)
    calls = []

    def cached_defaults():
        return {market: "" for market in markets}

    def cached_frequencies():
        return {market: [] for market in markets}

    def selected_defaults(keys=None):
        calls.append(("defaults", tuple(keys) if keys is not None else None))
        result = cached_defaults()
        result["hk"] = "HK.00700"
        return result

    def selected_frequencies(keys=None):
        calls.append(("frequencies", tuple(keys) if keys is not None else None))
        result = cached_frequencies()
        result["hk"] = ["d", "30m"]
        return result

    monkeypatch.setattr(
        constants_service.market_default_codes,
        "cached_snapshot",
        cached_defaults,
        raising=False,
    )
    monkeypatch.setattr(
        constants_service.market_frequencys,
        "cached_snapshot",
        cached_frequencies,
        raising=False,
    )
    monkeypatch.setattr(
        constants_service.market_default_codes, "snapshot", selected_defaults
    )
    monkeypatch.setattr(
        constants_service.market_frequencys, "snapshot", selected_frequencies
    )

    response = app.test_client().get("/?market=hk")

    assert response.status_code == 200
    assert calls == [("defaults", ("hk",)), ("frequencies", ("hk",))]
    body = response.get_data(as_text=True)
    assert 'var initial_market = "hk";' in body
    assert 'location.replace("/?market="' in body
    assert 'location.assign("/?market="' in body

def test_readyz_uses_only_local_snapshots(app, monkeypatch):
    monkeypatch.setattr(stock_list_service, "get_exchange", _raise_external_call)
    monkeypatch.setattr(other_mod, "get_exchange", _raise_external_call)
    app.extensions["readiness"].record_ticks_success("a")

    response = app.test_client().get("/readyz?market=a")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ready"
    assert payload["revision"] == "test-revision"
    assert payload["pid"] == os.getpid()
    assert payload["market"] == "a"
    assert payload["reasons"] == []
    assert payload["components"]["scheduler"] == {
        "required": False,
        "ready": True,
        "status": "disabled",
    }
    assert payload["components"]["metadata"] == {
        "ready": True,
        "status": "ready",
    }
    assert payload["components"]["symbols"]["ready"] is True
    assert payload["components"]["ticks"] == {
        "required": True,
        "ready": True,
        "status": "ok",
        "error": None,
    }


def test_readyz_blocks_until_ticks_have_been_verified(app):
    response = app.test_client().get("/readyz?market=a")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["components"]["ticks"] == {
        "required": True,
        "ready": False,
        "status": "unknown",
        "error": None,
    }
    assert payload["reasons"] == ["ticks_not_ready"]


def test_health_snapshot_extension_matches_flask_readiness_contract(app):
    app.extensions["readiness"].record_ticks_success("a")

    payload, status_code = app.extensions["health_snapshot"]("readyz", "a")

    assert status_code == 200
    assert payload["status"] == "ready"
    assert payload["pid"] == os.getpid()
    assert payload["components"]["ticks"]["status"] == "ok"


def test_readyz_reports_metadata_not_ready(app, monkeypatch):
    monkeypatch.setattr(
        constants_service.market_default_codes,
        "status",
        lambda _market: {"state": "failed", "ready": False},
    )

    response = app.test_client().get("/readyz?market=a")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["components"]["metadata"]["ready"] is False
    assert payload["reasons"] == ["metadata_not_ready"]


def test_readyz_reports_symbols_not_ready(app, monkeypatch):
    monkeypatch.setattr(
        stock_list_service,
        "get_symbol_readiness",
        lambda market: {
            "market": market,
            "ready": False,
            "status": "degraded",
            "count": 0,
            "last_error": "empty symbol list",
        },
    )

    response = app.test_client().get("/readyz?market=a")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["components"]["symbols"]["status"] == "degraded"
    assert payload["components"]["symbols"]["error"] == "empty symbol list"
    assert payload["reasons"] == ["symbols_not_ready"]


def test_readyz_rejects_invalid_market_without_reading_components(app, monkeypatch):
    monkeypatch.setattr(
        constants_service.market_frequencys, "status", _raise_external_call
    )
    monkeypatch.setattr(
        constants_service.market_default_codes, "status", _raise_external_call
    )
    monkeypatch.setattr(
        stock_list_service, "get_symbol_readiness", _raise_external_call
    )

    response = app.test_client().get("/readyz?market=invalid")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["market"] == "invalid"
    assert payload["components"] == {}
    assert payload["reasons"] == ["invalid_market"]


def test_readyz_rejects_supported_but_unmonitored_market(app):
    response = app.test_client().get("/readyz?market=hk")

    assert response.status_code == 400
    assert response.get_json()["reasons"] == ["market_not_monitored"]


def test_metadata_warmup_is_background_and_eventually_makes_ready(
    app, monkeypatch
):
    started = Event()
    release = Event()
    loaded = set()
    calls = []

    class _BlockingMap:
        def __init__(self, name, block=False):
            self.name = name
            self.block = block

        def __getitem__(self, market):
            calls.append((self.name, market))
            if self.block:
                started.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("warmup blocked its caller")
            loaded.add(self.name)
            return ["1m"] if self.name == "frequencies" else "SZ.000001"

        def status(self, _market):
            ready = self.name in loaded
            return {
                "state": "loaded" if ready else "unloaded",
                "ready": ready,
            }

    fake_constants = SimpleNamespace(
        market_frequencys=_BlockingMap("frequencies", block=True),
        market_default_codes=_BlockingMap("default_code"),
    )
    monkeypatch.setattr(
        constants_service.market_frequencys,
        "status",
        lambda _market: {
            "state": "loaded" if "frequencies" in loaded else "unloaded",
            "ready": "frequencies" in loaded,
        },
    )
    monkeypatch.setattr(
        constants_service.market_default_codes,
        "status",
        lambda _market: {
            "state": "loaded" if "default_code" in loaded else "unloaded",
            "ready": "default_code" in loaded,
        },
    )

    thread = readiness_service.start_metadata_warmup(fake_constants, "a")
    try:
        assert started.wait(timeout=2)
        assert thread.is_alive()
        assert app.test_client().get("/readyz?market=a").status_code == 503
        assert calls == [("frequencies", "a")]
    finally:
        release.set()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert calls == [("frequencies", "a"), ("default_code", "a")]
    app.extensions["readiness"].record_ticks_success("a")
    assert app.test_client().get("/readyz?market=a").status_code == 200


def test_metadata_warmup_retries_until_both_mappings_are_ready():
    class _RetryingMap:
        def __init__(self, value):
            self.value = value
            self.attempts = 0
            self.ready = False

        def __getitem__(self, _market):
            self.attempts += 1
            self.ready = self.attempts >= 2
            return self.value if self.ready else None

        def status(self, _market):
            return {
                "state": "loaded" if self.ready else "failed",
                "ready": self.ready,
            }

    frequencies = _RetryingMap(["1m"])
    default_codes = _RetryingMap("SZ.000001")
    fake_constants = SimpleNamespace(
        market_frequencys=frequencies,
        market_default_codes=default_codes,
    )

    thread = readiness_service.start_metadata_warmup(
        fake_constants, "a", retry_seconds=0
    )
    thread.join(timeout=2)

    assert thread.is_alive() is False
    assert frequencies.attempts == 2
    assert default_codes.attempts == 2


def test_ticks_dependency_error_remains_not_ready_after_details_expire():
    now = [0.0]
    registry = readiness_service.ReadinessRegistry(
        tick_error_ttl=30.0,
        clock=lambda: now[0],
    )
    registry.record_ticks_failure("a", "service_unavailable", "temporary")

    assert registry.ticks_snapshot("a")["status"] == "error"
    assert registry.ticks_snapshot("a")["ready"] is False

    now[0] = 30.0
    assert registry.ticks_snapshot("a") == {
        "required": True,
        "ready": False,
        "status": "error",
        "error": None,
    }

def test_readyz_requires_scheduler_only_when_enabled(app):
    app.config["SCHEDULER_ENABLED"] = True
    app.extensions["readiness"].record_ticks_success("a")
    assert app.extensions["scheduler"].running is False

    response = app.test_client().get("/readyz?market=a")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["components"]["scheduler"] == {
        "required": True,
        "ready": False,
        "status": "stopped",
    }
    assert payload["components"]["runtime"] == {
        "required": True,
        "ready": False,
        "status": "stopped",
        "error": None,
    }
    assert payload["reasons"] == ["runtime_not_running", "scheduler_not_running"]


def test_ticks_dependency_failure_blocks_readiness_and_success_recovers(
    app, monkeypatch
):
    client = app.test_client()
    registry = app.extensions["readiness"]

    assert registry.ticks_snapshot("a") == {
        "required": True,
        "ready": False,
        "status": "unknown",
        "error": None,
    }
    assert client.get("/readyz?market=a").status_code == 503

    bad_request = client.post(
        "/ticks", data={"market": "invalid", "codes": json.dumps([])}
    )
    assert bad_request.status_code == 400
    assert registry.ticks_snapshot("a") == {
        "required": True,
        "ready": False,
        "status": "unknown",
        "error": None,
    }

    monkeypatch.setattr(other_mod, "get_exchange", _raise_external_call)
    failed = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )
    assert failed.status_code == 503
    blocked = client.get("/readyz?market=a")
    assert blocked.status_code == 503
    assert blocked.get_json()["components"]["ticks"]["status"] == "error"
    assert blocked.get_json()["reasons"] == ["ticks_dependency_error"]

    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _FakeExchange())
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: True)
    recovered = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )
    assert recovered.status_code == 200
    ready = client.get("/readyz?market=a")
    assert ready.status_code == 200
    assert ready.get_json()["components"]["ticks"]["status"] == "ok"


def test_empty_tick_result_does_not_mark_dependency_ready(app, monkeypatch):
    class _EmptyExchange:
        def ticks(self, _codes):
            return {}

    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _EmptyExchange())
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: True)

    response = app.test_client().post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "empty_result"
    snapshot = app.extensions["readiness"].ticks_snapshot("a")
    assert snapshot["ready"] is False
    assert snapshot["status"] == "error"


def test_empty_tick_result_is_accepted_while_market_is_closed(app, monkeypatch):
    class _EmptyExchange:
        def ticks(self, _codes):
            return {}

    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _EmptyExchange())
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: False)

    response = app.test_client().post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    assert response.status_code == 200
    assert response.get_json()["market_state"] == "closed"
    assert response.get_json()["ticks"] == []
    assert app.extensions["readiness"].ticks_snapshot("a")["ready"] is True
