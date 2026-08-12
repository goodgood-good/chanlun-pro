from datetime import date, datetime
import json
import os
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cl_app import create_app
from cl_app.blueprints import other as other_mod
from cl_app.services import constants as constants_service
from cl_app.services import readiness as readiness_service
from cl_app.services import stock_list as stock_list_service
from chanlun.decision_support.trading_system.trading_session import (
    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
)


def _raise_external_call(*_args, **_kwargs):
    raise AssertionError("readiness endpoint attempted an external call")


class _FakeExchange:
    def ticks(self, codes):
        return {code: SimpleNamespace(last=2.5, rate=1.25) for code in codes}


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("CHANLUN_BUILD_REVISION", "test-revision")
    flask_app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "TRADING_SCREENING_SNAPSHOT_PATH": (
                tmp_path / "trading_screening_snapshot.json"
            ),
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


def test_disabled_scheduler_monitor_cannot_bypass_virtual_paper_gate(app):
    service = app.extensions["decision_support_human_review"]

    ready, reason = service._paper_forward_operations_eligibility(
        source_session=date(2026, 7, 31)
    )

    assert ready is False
    assert reason == "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"


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
    assert payload["components"]["trading_screening"] == {
        "required": False,
        "ready": True,
        "status": "disabled",
        "reasons": [],
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
    app.extensions["readiness"].record_ticks_success("a")
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
    app.extensions["readiness"].record_ticks_success("a")
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


def test_metadata_warmup_is_background_and_eventually_makes_ready(app, monkeypatch):
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


def test_readyz_requires_the_app_owned_qmt_runtime(app):
    app.config["SCHEDULER_ENABLED"] = True
    app.config["QMT_RUNTIME_MODE"] = "APP"
    app.extensions["readiness"].record_ticks_success("a")
    scheduler = app.extensions["scheduler"]
    scheduler.start(paused=True)
    try:
        app.extensions["runtime_status"] = lambda: {
            "ready": True,
            "status": "running",
            "error": None,
        }
        app.extensions["app_qmt_runtime"] = SimpleNamespace(
            snapshot=lambda: {
                "schema": "chanlun-qmt-runtime-readiness",
                "contract_id": "chanlun-qmt-runtime/app-runtime-contract",
                "execution_owner": "APP_RUNTIME",
                "ready": False,
                "status": "not_ready",
                "reason_code": "QMT_MAIN_PROCESS_MISSING",
            }
        )

        blocked = app.test_client().get("/readyz?market=a")

        assert blocked.status_code == 503
        payload = blocked.get_json()
        assert payload["components"]["qmt_runtime"]["required"] is True
        assert payload["components"]["qmt_runtime"]["ready"] is False
        assert payload["reasons"] == ["qmt_runtime_not_ready"]

        app.extensions["app_qmt_runtime"] = SimpleNamespace(
            snapshot=lambda: {
                "schema": "chanlun-qmt-runtime-readiness",
                "contract_id": "chanlun-qmt-runtime/app-runtime-contract",
                "execution_owner": "APP_RUNTIME",
                "ready": True,
                "status": "ready",
                "reason_code": "READY",
            }
        )
        recovered = app.test_client().get("/readyz?market=a")
        assert recovered.status_code == 200
        assert recovered.get_json()["reasons"] == []
    finally:
        scheduler.shutdown(wait=False)


def test_readyz_requires_screening_attestation_when_runtime_is_running(app):
    app.config["SCHEDULER_ENABLED"] = True
    app.config["TRADING_SCREENING_BACKGROUND_ENABLED"] = True
    app.extensions["readiness"].record_ticks_success("a")
    scheduler = app.extensions["scheduler"]
    scheduler.start(paused=True)
    try:
        app.extensions["runtime_status"] = lambda: {
            "ready": True,
            "status": "running",
            "error": None,
        }
        app.extensions["decision_support_trading_screening"] = SimpleNamespace(
            health_snapshot=lambda: {
                "ready": False,
                "status": "not_ready",
                "worker_alive": False,
                "reasons": ["screening_worker_not_running"],
            }
        )

        blocked = app.test_client().get("/readyz?market=a")

        assert blocked.status_code == 503
        payload = blocked.get_json()
        assert payload["components"]["trading_screening"] == {
            "required": True,
            "ready": False,
            "status": "not_ready",
            "worker_alive": False,
            "reasons": ["screening_worker_not_running"],
        }
        assert payload["reasons"] == ["trading_screening_not_ready"]

        app.extensions["decision_support_trading_screening"] = SimpleNamespace(
            health_snapshot=lambda: {
                "ready": True,
                "status": "ready",
                "worker_alive": True,
                "reasons": [],
            }
        )
        app.extensions["decision_support_human_review"] = SimpleNamespace(
            forward_archive_capture_readiness_nonblocking=lambda *, session: {
                "required": False,
                "ready": False,
                "status": "not_due",
                "reason_code": "FORWARD_SESSION_NOT_DUE",
                "session": session,
            },
            forward_delivery_readiness_nonblocking=lambda *, session: {
                "required": False,
                "ready": False,
                "status": "not_due",
                "reason_code": "FORWARD_SESSION_NOT_DUE",
                "session": session,
            },
        )
        recovered = app.test_client().get("/readyz?market=a")
        assert recovered.status_code == 200
        assert recovered.get_json()["reasons"] == []
    finally:
        scheduler.shutdown(wait=False)


def test_readyz_does_not_call_a_complete_screen_a_complete_forward_archive(app):
    """A page-complete snapshot cannot hide a missing same-session QMT capture."""

    app.config["SCHEDULER_ENABLED"] = True
    app.config["TRADING_SCREENING_BACKGROUND_ENABLED"] = True
    app.extensions["readiness"].record_ticks_success("a")
    scheduler = app.extensions["scheduler"]
    scheduler.start(paused=True)
    try:
        app.extensions["runtime_status"] = lambda: {
            "ready": True,
            "status": "running",
            "error": None,
        }
        app.extensions["decision_support_trading_screening"] = SimpleNamespace(
            health_snapshot=lambda: {
                "ready": True,
                "status": "ready",
                "worker_alive": True,
                "screening_review_ready": True,
                "screening_review_reason_code": "READY",
                "forward_review_ready": True,
                "forward_review_reason_code": "READY",
                "reasons": [],
            }
        )
        capture = {
            "ready": False,
            "status": "not_ready",
            "reason_code": "REQUIRED_CAPTURE_MISSING",
            "session": "2026-07-29",
            "receipt_proven": False,
            "live_status": "LIVE_DISABLED",
        }
        delivery = {
            "ready": False,
            "status": "not_ready",
            "reason_code": "EVALUATION_MISSING_AFTER_DEADLINE",
            "session": "2026-07-29",
            "capture_ready": True,
            "evaluation_ready": False,
            "live_status": "LIVE_DISABLED",
        }
        app.extensions["decision_support_human_review"] = SimpleNamespace(
            forward_archive_capture_readiness=lambda *, session: _raise_external_call(),
            forward_archive_capture_readiness_nonblocking=lambda *, session: dict(
                capture
            ),
            forward_delivery_readiness=lambda *, session: _raise_external_call(),
            forward_delivery_readiness_nonblocking=lambda *, session: dict(delivery),
        )

        blocked = app.test_client().get("/readyz?market=a&forward_session=2026-07-29")

        # Forward evidence is a research pipeline component. Its absence must
        # not falsely make the Web process itself unavailable.
        assert blocked.status_code == 200
        payload = blocked.get_json()
        assert payload["status"] == "ready"
        archive = payload["components"]["forward_archive"]
        assert archive["ready"] is False
        assert archive["status"] == "not_ready"
        assert archive["reason_code"] == "REQUIRED_CAPTURE_MISSING"
        assert archive["screening_review_ready"] is True
        assert archive["sector_capture_ready"] is False
        assert archive["sector_capture_reason_code"] == ("REQUIRED_CAPTURE_MISSING")
        delivery_component = payload["components"]["forward_delivery"]
        assert delivery_component["ready"] is False
        assert delivery_component["reason_code"] == (
            "EVALUATION_MISSING_AFTER_DEADLINE"
        )

        capture.update(
            required=None,
            requirement_resolved=False,
            trading_session_status="UNRESOLVED",
            reason_code="TRADING_SESSION_EVIDENCE_UNAVAILABLE",
        )
        delivery.update(
            required=None,
            requirement_resolved=False,
            trading_session_status="UNRESOLVED",
            reason_code="TRADING_SESSION_EVIDENCE_UNAVAILABLE",
        )
        unresolved = (
            app.test_client()
            .get("/readyz?market=a&forward_session=2026-07-29")
            .get_json()["components"]
        )
        assert unresolved["forward_archive"]["required"] is None
        assert unresolved["forward_archive"]["requirement_resolved"] is False
        assert unresolved["forward_delivery"]["required"] is None

        capture.update(
            required=True,
            requirement_resolved=True,
            ready=True,
            status="ready",
            reason_code="READY",
            receipt_proven=True,
        )
        ready = app.test_client().get("/readyz?market=a&forward_session=2026-07-29")
        archive = ready.get_json()["components"]["forward_archive"]
        assert archive["ready"] is True
        assert archive["status"] == "ready"
        assert archive["reason_code"] == "READY"
        assert archive["sector_capture_ready"] is True
        # A complete input gate still does not prove that Evaluate archived
        # the day.  The actual delivery component remains independently red.
        assert ready.get_json()["components"]["forward_delivery"]["ready"] is False

        delivery.update(
            required=True,
            requirement_resolved=True,
            ready=True,
            status="ready",
            reason_code="READY",
            evaluation_ready=True,
        )
        completed = (
            app.test_client()
            .get("/readyz?market=a&forward_session=2026-07-29")
            .get_json()
        )
        assert completed["components"]["forward_archive"]["ready"] is True
        assert completed["components"]["forward_delivery"]["ready"] is True
    finally:
        scheduler.shutdown(wait=False)


def test_readyz_exposes_forward_scheduler_contract_without_masking_web_health(
    app,
) -> None:
    app.config["SCHEDULER_ENABLED"] = True
    app.config["TRADING_SCREENING_BACKGROUND_ENABLED"] = True
    app.config["FORWARD_SCHEDULER_MONITOR_ENABLED"] = True
    app.config["FORWARD_SCHEDULER_MODE"] = "APP"
    app.extensions["readiness"].record_ticks_success("a")
    scheduler = app.extensions["scheduler"]
    scheduler.start(paused=True)
    try:
        app.extensions["runtime_status"] = lambda: {
            "ready": True,
            "status": "running",
            "error": None,
        }
        app.extensions["decision_support_trading_screening"] = SimpleNamespace(
            health_snapshot=lambda: {
                "ready": True,
                "status": "ready",
                "worker_alive": True,
                "screening_review_ready": False,
                "screening_review_reason_code": "COVERAGE_INCOMPLETE",
                "reasons": [],
            }
        )
        app.extensions["forward_scheduler_probe"] = SimpleNamespace(
            snapshot=lambda: {
                "schema": "chanlun-forward-scheduler-readiness",
                "contract_id": ("chanlun-forward-scheduler/app-runtime-contract"),
                "execution_owner": "APP_RUNTIME",
                "ready": False,
                "status": "not_ready",
                "reason_code": "SCHEDULED_TASK_PRINCIPAL_MISMATCH",
                "reason_codes": ["SCHEDULED_TASK_PRINCIPAL_MISMATCH"],
                "tasks": [],
                "task_count": 0,
                "live_status": "LIVE_DISABLED",
            }
        )
        app.extensions["decision_support_human_review"] = SimpleNamespace(
            forward_archive_capture_readiness=lambda *, session: {
                "required": False,
                "ready": False,
                "status": "not_due",
                "reason_code": "FORWARD_SESSION_NOT_DUE",
                "session": None,
            },
            forward_delivery_readiness=lambda *, session: {
                "required": True,
                "ready": False,
                "status": "pending",
                "reason_code": "CAPTURE_NOT_DUE",
                "session": "2026-07-31",
            },
        )

        response = app.test_client().get("/readyz?market=a")

        assert response.status_code == 200
        payload = response.get_json()
        component = payload["components"]["forward_scheduler"]
        assert component["required"] is True
        assert component["ready"] is False
        assert component["reason_code"] == ("SCHEDULED_TASK_PRINCIPAL_MISMATCH")
        # Research delivery remains a distinct red component; it must not make
        # the chart/Web process itself unavailable.
        assert payload["status"] == "ready"
        assert payload["reasons"] == []
    finally:
        scheduler.shutdown(wait=False)


def test_readyz_rejects_an_invalid_forward_session(app):
    response = app.test_client().get("/readyz?market=a&forward_session=not-a-session")

    assert response.status_code == 400
    assert response.get_json()["reasons"] == ["invalid_forward_session"]


def test_production_calendar_provider_resolves_before_any_qmt_fallback(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHANLUN_BUILD_REVISION", "test-official-calendar")
    flask_app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "TRADING_SCREENING_SNAPSHOT_PATH": (
                tmp_path / "trading_screening_snapshot.json"
            ),
            "WTF_CSRF_ENABLED": False,
            "TRADING_SESSION_OFFICIAL_CALENDAR_PATH": (
                DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH
            ),
        }
    )
    try:
        service = flask_app.extensions["decision_support_human_review"]
        evidence = service._trading_session_provider(
            session=date(2026, 7, 31),
            observed_at=datetime(
                2026,
                7,
                31,
                1,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )

        assert evidence["classification"] == "TRADING_SESSION"
        assert evidence["source_method"] == "SSE_OFFICIAL_ANNUAL_CALENDAR"
        assert evidence["tick_data_used"] is False
        assert evidence["real_account_accessed"] is False
        assert evidence["real_order_transport_enabled"] is False
    finally:
        flask_app.extensions["shutdown_scheduler"]()


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
