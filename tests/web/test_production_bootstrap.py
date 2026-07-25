import threading

import pytest

from cl_app import create_app
from cl_app import alert_tasks
from cl_app.services import readiness
from cl_app.services import stock_list
from cl_app.services import chart_revalidate
from cl_app.services import chart_cache
from cl_app.services import constants
from cl_app.services import trading_screening
from cl_app.handlers import sse_stream
from chanlun import config as chanlun_config
from chanlun.persistence import file_db
from chanlun.signal_monitor import scheduler as signal_scheduler
from chanlun.recursive_bt.monitor import app_monitor


def test_scheduler_enabled_factory_runs_the_production_lifecycle(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chanlun_config,
        "RECURSIVE_MONITOR_CONFIG",
        {"enabled": True},
    )

    class _Handle:
        def __init__(self, name):
            self.name = name

        def is_alive(self):
            return True

        def stop(self):
            calls.append((f"stop-{self.name}", None))

        def join(self, timeout=None):
            calls.append((f"join-{self.name}", timeout))

    warmup_thread = _Handle("metadata")
    ticks_thread = _Handle("ticks")
    symbols_thread = _Handle("symbols")

    monkeypatch.setattr(
        readiness,
        "start_metadata_warmup",
        lambda _constants, market: calls.append(("warmup", market)) or warmup_thread,
    )
    monkeypatch.setattr(
        constants,
        "start_market_metadata_loaders",
        lambda: calls.append(("metadata-loaders", None)),
    )
    monkeypatch.setattr(
        constants,
        "shutdown_market_metadata_loaders",
        lambda **_kwargs: calls.append(("stop-metadata-loaders", None)),
    )
    monkeypatch.setattr(
        chart_cache,
        "start_chart_cache_runtime",
        lambda: calls.append(("chart-cache", None)),
    )
    monkeypatch.setattr(
        chart_cache,
        "shutdown_chart_cache_runtime",
        lambda **_kwargs: calls.append(("stop-chart-cache", None)),
    )
    monkeypatch.setattr(
        file_db,
        "start_pickle_writes",
        lambda: calls.append(("pickle-writes", None)),
    )
    monkeypatch.setattr(
        file_db,
        "shutdown_pickle_writes",
        lambda **_kwargs: calls.append(("stop-pickle-writes", None)),
    )
    monkeypatch.setattr(
        readiness,
        "start_ticks_warmup",
        lambda _registry, _probe, market: calls.append(("ticks", market)) or ticks_thread,
    )
    monkeypatch.setattr(
        stock_list,
        "start_symbol_preload_thread",
        lambda: calls.append(("symbols", None)) or symbols_thread,
    )
    monkeypatch.setattr(
        chart_revalidate,
        "start_revalidation_runtime",
        lambda: calls.append(("revalidation", None)),
    )
    monkeypatch.setattr(
        chart_revalidate,
        "shutdown_revalidation",
        lambda **_kwargs: calls.append(("stop-revalidation", None)),
    )
    monkeypatch.setattr(
        sse_stream,
        "start_sse_runtime",
        lambda: calls.append(("sse", None)),
    )
    monkeypatch.setattr(
        sse_stream,
        "shutdown_sse_runtime",
        lambda: calls.append(("stop-sse", None)),
    )
    monkeypatch.setattr(stock_list, "shutdown_symbol_preload", lambda **_kwargs: None)
    monkeypatch.setattr(
        trading_screening.TradingScreeningService,
        "start_background",
        lambda _self: calls.append(("trading-screening", None)),
    )
    monkeypatch.setattr(
        trading_screening.TradingScreeningService,
        "shutdown_background",
        lambda _self, **_kwargs: calls.append(("stop-trading-screening", None)),
    )
    monkeypatch.setattr(
        alert_tasks.AlertTasks,
        "run",
        lambda _self: calls.append(("alerts", None)) or True,
    )
    monkeypatch.setattr(
        signal_scheduler,
        "register_signal_jobs",
        lambda _scheduler: calls.append(("signals", None)),
    )
    monkeypatch.setattr(
        app_monitor,
        "register_recursive_monitor_jobs",
        lambda _scheduler: calls.append(("recursive", None)) or [],
    )

    app = create_app(
        start_scheduler=True,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": True,
        },
    )
    try:
        assert app.extensions["scheduler"].running is True
        assert app.extensions["metadata_warmup_thread"] is warmup_thread
        assert calls == [
            ("metadata-loaders", None),
            ("chart-cache", None),
            ("pickle-writes", None),
            ("warmup", "a"),
            ("symbols", None),
            ("ticks", "a"),
            ("revalidation", None),
            ("sse", None),
            ("trading-screening", None),
            ("alerts", None),
            ("signals", None),
            ("recursive", None),
        ]
    finally:
        app.extensions["shutdown_runtime_services"]()

    assert app.extensions["scheduler"].running is False
    assert ("stop-metadata", None) in calls
    assert ("stop-ticks", None) in calls
    assert ("stop-sse", None) in calls
    assert ("stop-trading-screening", None) in calls
    assert ("stop-revalidation", None) in calls
    assert ("stop-chart-cache", None) in calls
    assert ("stop-pickle-writes", None) in calls
    assert ("stop-metadata-loaders", None) in calls

    # Shutdown is idempotent and does not enqueue work on a stopped IOLoop.
    before = list(calls)
    app.extensions["shutdown_runtime_services"]()
    assert calls == before


def test_inactive_app_shutdown_does_not_stop_process_shared_services(monkeypatch):
    calls = []
    monkeypatch.setattr(
        constants,
        "shutdown_market_metadata_loaders",
        lambda **_kwargs: calls.append("metadata"),
    )
    monkeypatch.setattr(
        chart_cache,
        "shutdown_chart_cache_runtime",
        lambda **_kwargs: calls.append("chart-cache"),
    )
    monkeypatch.setattr(
        file_db,
        "shutdown_pickle_writes",
        lambda **_kwargs: calls.append("pickle"),
    )
    monkeypatch.setattr(
        stock_list,
        "shutdown_symbol_preload",
        lambda **_kwargs: calls.append("symbols"),
    )
    monkeypatch.setattr(
        chart_revalidate,
        "shutdown_revalidation",
        lambda **_kwargs: calls.append("revalidation"),
    )
    monkeypatch.setattr(
        sse_stream,
        "shutdown_sse_runtime",
        lambda: calls.append("sse"),
    )

    app = create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "SCHEDULER_ENABLED": False,
        }
    )
    app.extensions["shutdown_runtime_services"]()

    assert calls == []
    assert app.extensions["runtime_status"]()["status"] == "stopped"


def test_runtime_cleanup_continues_after_one_component_fails(monkeypatch):
    cleaned = []

    class _Handle:
        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(constants, "start_market_metadata_loaders", lambda: None)
    monkeypatch.setattr(chart_cache, "start_chart_cache_runtime", lambda: None)
    monkeypatch.setattr(file_db, "start_pickle_writes", lambda: None)
    monkeypatch.setattr(readiness, "start_metadata_warmup", lambda *_args: _Handle())
    monkeypatch.setattr(readiness, "start_ticks_warmup", lambda *_args: _Handle())
    monkeypatch.setattr(stock_list, "start_symbol_preload_thread", lambda: _Handle())
    monkeypatch.setattr(chart_revalidate, "start_revalidation_runtime", lambda: None)
    monkeypatch.setattr(sse_stream, "start_sse_runtime", lambda: None)
    monkeypatch.setattr(
        sse_stream,
        "shutdown_sse_runtime",
        lambda: (_ for _ in ()).throw(RuntimeError("sse cleanup failed")),
    )
    monkeypatch.setattr(
        chart_revalidate,
        "shutdown_revalidation",
        lambda **_kwargs: cleaned.append("revalidation"),
    )
    monkeypatch.setattr(
        stock_list,
        "shutdown_symbol_preload",
        lambda **_kwargs: cleaned.append("symbols"),
    )
    monkeypatch.setattr(
        chart_cache,
        "shutdown_chart_cache_runtime",
        lambda **_kwargs: cleaned.append("chart-cache"),
    )
    monkeypatch.setattr(
        file_db,
        "shutdown_pickle_writes",
        lambda **_kwargs: cleaned.append("pickle"),
    )
    monkeypatch.setattr(
        constants,
        "shutdown_market_metadata_loaders",
        lambda **_kwargs: cleaned.append("metadata"),
    )

    app = create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "SCHEDULER_ENABLED": False,
        }
    )
    app.extensions["start_runtime_services"](enable_scheduler=False)
    assert app.extensions["runtime_status"]()["status"] == "running"
    with pytest.raises(RuntimeError, match="different scheduler mode"):
        app.extensions["start_runtime_services"](enable_scheduler=True)

    app.extensions["shutdown_runtime_services"]()

    assert cleaned == ["revalidation", "symbols", "chart-cache", "pickle", "metadata"]
    status = app.extensions["runtime_status"]()
    assert status["status"] == "stopped"
    assert "sse cleanup failed" in status["error"]


def test_shutdown_cancels_an_inflight_runtime_start(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking_metadata_start():
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(constants, "start_market_metadata_loaders", blocking_metadata_start)
    monkeypatch.setattr(constants, "shutdown_market_metadata_loaders", lambda **_k: None)
    monkeypatch.setattr(chart_cache, "shutdown_chart_cache_runtime", lambda **_k: None)
    monkeypatch.setattr(file_db, "shutdown_pickle_writes", lambda **_k: None)
    monkeypatch.setattr(stock_list, "shutdown_symbol_preload", lambda **_k: None)
    monkeypatch.setattr(chart_revalidate, "shutdown_revalidation", lambda **_k: None)
    monkeypatch.setattr(sse_stream, "shutdown_sse_runtime", lambda: None)
    app = create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "SCHEDULER_ENABLED": False,
        }
    )

    def start():
        try:
            app.extensions["start_runtime_services"](enable_scheduler=False)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    assert entered.wait(timeout=1)

    app.extensions["shutdown_runtime_services"]()
    release.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert errors and str(errors[0]) == "runtime services are stopping"
    assert app.extensions["runtime_status"]()["status"] == "stopped"
