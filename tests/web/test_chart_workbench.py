"""Chart startup, shared user data and retired endpoint boundaries."""

from types import SimpleNamespace

from chanlun import config
from chanlun.zixuan import ZiXuan
from cl_app import create_app
from cl_app.handlers import sse_stream
from cl_app.services import chart_cache, chart_revalidate, constants, readiness, stock_list
from chanlun.persistence import file_db


def _app():
    return create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "WTF_CSRF_ENABLED": False,
    })




def test_watchlist_uses_existing_global_rows_without_cohort_filter():
    # tests/conftest.py isolates this real persistence layer before collection.
    zx = ZiXuan("a")
    group = "仅图表关注列表回归"
    assert zx.add_zx_group(group)
    codes = [f"SH.{600000 + i}" for i in range(13)]
    for code in codes:
        assert zx.add_stock(group, code, f"已有标的 {code}")
    assert ZiXuan("us").add_stock(group, "AAPL", "Apple")
    try:
        app = _app()
        response = app.test_client().get(f"/get_zixuan_stocks/a/{group}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 14
        assert {(row["market"], row["code"]) for row in data["data"]} == {
            *(("a", code) for code in codes), ("us", "AAPL"),
        }
    finally:
        zx.del_zx_group(group)




def test_chart_runtime_start_is_idempotent(monkeypatch):
    started = []
    handle = SimpleNamespace(stop=lambda: None, join=lambda timeout=None: None)
    for module, name in (
        (constants, "start_market_metadata_loaders"),
        (chart_cache, "start_chart_cache_runtime"),
        (file_db, "start_pickle_writes"),
        (chart_revalidate, "start_revalidation_runtime"),
        (sse_stream, "start_sse_runtime"),
    ):
        monkeypatch.setattr(module, name, lambda name=name: started.append(name))
    monkeypatch.setattr(readiness, "start_metadata_warmup", lambda *_args: handle)
    monkeypatch.setattr(readiness, "start_ticks_warmup", lambda *_args: handle)
    monkeypatch.setattr(stock_list, "start_symbol_preload_thread", lambda: handle)
    app = _app()
    try:
        app.extensions["start_runtime_services"]()
        # The desktop retry path must not duplicate running services.
        app.extensions["start_runtime_services"]()
        assert app.extensions["runtime_status"]()["status"] == "running"
        assert started.count("start_chart_cache_runtime") == 1
        assert started.count("start_sse_runtime") == 1
    finally:
        app.extensions["shutdown_runtime_services"]()


def test_native_app_preserves_storage_and_excludes_retired_endpoints():
    storage = (config.DATA_PATH, config.DB_TYPE, config.DB_DATABASE)
    app = _app()
    assert (config.DATA_PATH, config.DB_TYPE, config.DB_DATABASE) == storage
    assert "decision_support_trading_screening" not in app.extensions
    assert "holding_group_monitor" not in app.extensions
    client = app.test_client()
    for route in ("/jobs", "/research-audit", "/xuangu/task_add"):
        assert client.post(route).status_code == 404
    # Restored read-only workbench entry; a GET still never launches a task.
    assert client.get("/early-screening").status_code == 200
    assert client.post("/early-screening").status_code == 405
