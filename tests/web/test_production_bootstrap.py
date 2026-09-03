import threading
import types

import pytest

from cl_app import create_app
from cl_app.services import readiness
from cl_app.services import stock_list
from cl_app.services import chart_revalidate
from cl_app.services import chart_cache
from cl_app.services import constants
from cl_app.services import trading_screening
from cl_app.services import app_forward_scheduler
from cl_app.services import app_qmt_runtime
from cl_app.handlers import sse_stream
from chanlun.persistence import file_db
from chanlun.persistence.db import db


def test_production_screening_ignores_formal_ledger_and_legacy_result_groups(
    monkeypatch,
):
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            # 即使旧部署残留此开关，生产实时服务也不得恢复正式账本依赖。
            "TRADING_SCREENING_FORMAL_RESEARCH_REQUIRED": True,
            "TRADING_SCREENING_PRIORITY_WATCHLIST_GROUPS": (
                "我的关注",
                "人工观察",
            ),
        },
    )
    rows = {
        "我的关注": (
            types.SimpleNamespace(
                market="a", stock_code="SH.513100", stock_name="纳指ETF"
            ),
            types.SimpleNamespace(
                market="us", stock_code="QQQ.US", stock_name="纳指100ETF"
            ),
        ),
        "人工观察": (
            types.SimpleNamespace(
                market="a", stock_code="SZ.301004", stock_name="嘉益股份"
            ),
        ),
        "三买": (
            types.SimpleNamespace(
                market="a", stock_code="SH.600000", stock_name="旧版结果"
            ),
        ),
        "我的持仓": (
            types.SimpleNamespace(
                market="a", stock_code="SZ.000001", stock_name="人工持仓"
            ),
            types.SimpleNamespace(
                market="us", stock_code="QCOM.US", stock_name="高通"
            ),
        ),
    }
    monkeypatch.setattr(
        db,
        "zx_get_global_groups",
        lambda: tuple(types.SimpleNamespace(zx_group=name) for name in rows),
    )
    monkeypatch.setattr(
        db,
        "zx_get_global_group_stocks",
        lambda name: rows[name],
    )
    try:
        screening = app.extensions["decision_support_trading_screening"]
        gateway = app.extensions["decision_support_trading_screening_gateway"]

        assert app.config["TRADING_SCREENING_FORMAL_RESEARCH_REQUIRED"] is False
        assert screening._formal_selection_required is False
        assert screening._selection_research == ()
        assert gateway._watchlist_provider() == [
            {"code": "SH.513100", "name": "纳指ETF", "group": "我的关注"},
            {"code": "SZ.301004", "name": "嘉益股份", "group": "人工观察"},
        ]
        holdings = app.config[
            "TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"
        ]()
        assert [
            (row["market"], row["code"]) for row in holdings["positions"]
        ] == [("a", "SZ.000001"), ("us", "QCOM.US")]
        assert holdings["priority_monitor_count"] == 1
        assert holdings["cross_market_monitor_count"] == 1
    finally:
        app.extensions["shutdown_runtime_services"]()


def test_scheduler_enabled_factory_runs_the_production_lifecycle(
    monkeypatch,
    tmp_path,
):
    calls = []
    tick_probes = {}

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

    def capture_tick_probe(_registry, probe, market):
        tick_probes[market] = probe
        calls.append(("ticks", market))
        return ticks_thread

    monkeypatch.setattr(readiness, "start_ticks_warmup", capture_tick_probe)
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
    qmt_data = tmp_path / "qmt"
    (qmt_data / "Sector" / "Temple" / "GICS").mkdir(parents=True)
    monkeypatch.setattr(
        app_forward_scheduler.AppForwardSchedulerController,
        "register_jobs",
        lambda _self: calls.append(("app-forward-register", None)),
    )
    monkeypatch.setattr(
        app_forward_scheduler.AppForwardSchedulerController,
        "stop",
        lambda _self: calls.append(("app-forward-stop", None)),
    )
    monkeypatch.setattr(
        app_qmt_runtime.AppQmtRuntimeController,
        "startup",
        lambda _self: calls.append(("app-qmt-startup", None)),
    )
    monkeypatch.setattr(
        app_qmt_runtime.AppQmtRuntimeController,
        "register_jobs",
        lambda _self: calls.append(("app-qmt-register", None)),
    )
    monkeypatch.setattr(
        app_qmt_runtime.AppQmtRuntimeController,
        "stop",
        lambda _self: calls.append(("app-qmt-stop", None)),
    )

    app = create_app(
        start_scheduler=True,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": True,
            "FORWARD_SCHEDULER_MODE": "APP",
            "FORWARD_QMT_LOCAL_DATA_DIR": str(qmt_data),
            "QMT_RUNTIME_MODE": "APP",
            "QMT_RUNTIME_WARMUP_SECONDS": 0,
        },
    )
    try:
        assert app.extensions["scheduler"].running is True
        assert app.extensions["metadata_warmup_thread"] is warmup_thread
        screening = app.extensions["decision_support_trading_screening"]
        assert screening._config.priority_monitoring_enabled is True
        assert screening._config.max_five_minute_candidate_symbols_per_refresh == 12
        assert screening._config.max_thirty_minute_candidate_symbols_per_refresh == 12
        assert screening._config.max_symbols_per_refresh == 12
        assert screening._config.max_total_symbols_per_refresh == 12
        assert screening._config.priority_monitor_interval_seconds == 60
        gateway = app.extensions["decision_support_trading_screening_gateway"]
        monkeypatch.setattr(
            constants.market_default_codes,
            "cached_snapshot",
            lambda _keys: {"a": "SH.000001"},
        )
        monkeypatch.setattr(
            gateway,
            "realtime_ticks",
            lambda codes: types.SimpleNamespace(
                requested_codes=codes,
                market_open=False,
                ticks=lambda: {},
            ),
        )
        assert tick_probes["a"]("a") == {"__market_closed__": True}

        quote = object()
        monkeypatch.setattr(
            gateway,
            "realtime_ticks",
            lambda codes: types.SimpleNamespace(
                requested_codes=codes,
                market_open=True,
                ticks=lambda: {codes[0]: quote},
            ),
        )
        assert tick_probes["a"]("a") == {"SH.000001": quote}

        monkeypatch.setattr(
            gateway,
            "realtime_ticks",
            lambda codes: types.SimpleNamespace(
                requested_codes=codes,
                market_open=True,
                ticks=lambda: {},
            ),
        )
        assert tick_probes["a"]("a") == {}
        assert calls == [
            ("app-qmt-startup", None),
            ("metadata-loaders", None),
            ("chart-cache", None),
            ("pickle-writes", None),
            ("warmup", "a"),
            ("symbols", None),
            ("ticks", "a"),
            ("revalidation", None),
            ("sse", None),
            ("app-qmt-register", None),
            ("app-forward-register", None),
            ("trading-screening", None),
        ]
    finally:
        app.extensions["shutdown_runtime_services"]()

    assert app.extensions["scheduler"].running is False
    assert ("stop-metadata", None) in calls
    assert ("stop-ticks", None) in calls
    assert ("stop-sse", None) in calls
    assert ("stop-trading-screening", None) in calls
    assert ("app-qmt-stop", None) in calls
    assert ("stop-revalidation", None) in calls
    assert ("stop-chart-cache", None) in calls
    assert ("stop-pickle-writes", None) in calls
    assert ("stop-metadata-loaders", None) in calls
    assert ("app-forward-stop", None) in calls

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

    monkeypatch.setattr(
        constants, "start_market_metadata_loaders", blocking_metadata_start
    )
    monkeypatch.setattr(
        constants, "shutdown_market_metadata_loaders", lambda **_k: None
    )
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
