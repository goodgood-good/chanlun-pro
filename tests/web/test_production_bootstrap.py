import threading


from cl_app import create_app
from cl_app.services import readiness
from cl_app.services import stock_list
from cl_app.services import chart_revalidate
from cl_app.services import chart_cache
from cl_app.services import constants
from cl_app.handlers import sse_stream
from chanlun.persistence import file_db






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
        }
    )
    app.extensions["start_runtime_services"]()
    assert app.extensions["runtime_status"]()["status"] == "running"
    app.extensions["start_runtime_services"]()

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
        }
    )

    def start():
        try:
            app.extensions["start_runtime_services"]()
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
