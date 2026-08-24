import os
import pathlib
import threading
from urllib.parse import urlsplit

import pytest

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application
from werkzeug.serving import make_server

from cl_app import create_app
from cl_app.handlers.cached_static import CachedStaticFileHandler


ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "web/chanlun_chart/cl_app/static"
CHARTING_ROOT = STATIC_ROOT / "charting_library"
STANDALONE = CHARTING_ROOT / "charting_library.standalone.js"
SAMEORIGIN = CHARTING_ROOT / "sameorigin.html"
HASHED_BUNDLE = "bundles/library.668013b6b41ce2feaa5c.js"


class TestCachedStaticRuntime(AsyncHTTPTestCase):
    def get_app(self):
        return Application(
            [
                (
                    r"/(.*)",
                    CachedStaticFileHandler,
                    {"path": str(CHARTING_ROOT)},
                )
            ]
        )

    def test_only_content_hashed_files_receive_immutable_cache(self):
        hashed = self.fetch(f"/{HASHED_BUNDLE}", method="HEAD")
        assert hashed.code == 200
        assert hashed.headers["Cache-Control"] == (
            "public, max-age=31536000, immutable"
        )

        for path in ("charting_library.standalone.js", "sameorigin.html"):
            response = self.fetch(f"/{path}", method="HEAD")
            assert response.code == 200
            assert response.headers["Cache-Control"] == (
                "public, max-age=300, must-revalidate"
            )

    def test_official_tornado_path_serves_sameorigin_bootstrap(self):
        response = self.fetch("/sameorigin.html")

        assert response.code == 200
        assert b"sameOriginLoad" in response.body
        assert response.headers["Content-Type"].startswith("text/html")


def _wsgi_app():
    return create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_generic_wsgi_sameorigin_matches_official_csp_boundary():
    app = _wsgi_app()
    try:
        client = app.test_client()
        response = client.get(
            "/static/charting_library/sameorigin.html"
        )
        adjacent = client.get(
            "/static/charting_library/charting_library.standalone.js"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 200
    assert "Content-Security-Policy" not in response.headers
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["Permissions-Policy"]
    assert adjacent.status_code == 200
    assert "Content-Security-Policy" in adjacent.headers

def test_generic_wsgi_browser_executes_sameorigin_bootstrap():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    app = _wsgi_app()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console_errors = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script(
                """
                window.__sameOriginLoadReceived = false;
                window.addEventListener('sameOriginLoad', () => {
                    window.__sameOriginLoadReceived = true;
                });
                """
            )
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            response = page.goto(
                f"http://127.0.0.1:{server.server_port}/static/"
                "charting_library/sameorigin.html"
            )

            assert response is not None
            assert response.status == 200
            csp_errors = [
                message
                for message in console_errors
                if "Content Security Policy" in message
            ]
            assert not csp_errors, csp_errors
            assert page.evaluate("window.__sameOriginLoadReceived") is True
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.extensions["shutdown_scheduler"]()

def test_generic_wsgi_full_chart_has_no_vendor_inline_csp_blocks(monkeypatch):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    from cl_app.blueprints import tv as tv_module
    from cl_app.services import constants as constants_service

    markets = tuple(constants_service.market_types)
    defaults = {market: "" for market in markets}
    defaults["a"] = "SZ.000001"
    frequencies = {market: [] for market in markets}
    frequencies["a"] = ["1m", "5m", "d"]
    monkeypatch.setattr(
        constants_service.market_default_codes,
        "cached_snapshot",
        lambda keys=None: dict(defaults),
    )
    monkeypatch.setattr(
        constants_service.market_default_codes,
        "snapshot",
        lambda keys=None: dict(defaults),
    )
    monkeypatch.setattr(
        constants_service.market_frequencys,
        "cached_snapshot",
        lambda keys=None: dict(frequencies),
    )
    monkeypatch.setattr(
        constants_service.market_frequencys,
        "snapshot",
        lambda keys=None: dict(frequencies),
    )

    class FakeExchange:
        stock_info_query_scope = "SINGLE_SYMBOL_STOCK_INFO"

        def stock_info(self, code):
            return {"code": code, "name": "Preview", "precision": 100}

        def stock_owner_plate(self, _code):
            return {"GN": [], "HY": []}

    monkeypatch.setattr(tv_module, "get_exchange", lambda _market: FakeExchange())
    app = _wsgi_app()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console_errors = []
    page_errors = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()

            def route_request(route):
                path = urlsplit(route.request.url).path
                if path == "/" or path.startswith("/static/") or path == "/tv/config":
                    route.continue_()
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"s":"no_data","ok":true,"market_state":"closed","ticks":[]}',
                )

            page.route("**/*", route_request)
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            response = page.goto(
                f"http://127.0.0.1:{server.server_port}/",
                wait_until="domcontentloaded",
            )
            page.wait_for_function(
                """
                () => Array.isArray(window.chart_widgets)
                    && window.chart_widgets.some((item) => item && item.chart)
                """,
                timeout=15_000,
            )

            assert response is not None
            assert response.status == 200
            assert page.locator("iframe").count() >= 1
            blocked = [
                message
                for message in console_errors + page_errors
                if "Content Security Policy" in message
                or "disabledFeatures" in message
            ]
            assert not blocked, blocked
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.extensions["shutdown_scheduler"]()


def test_index_uses_runtime_asset_token_for_unhashed_charting_entrypoint():
    template = (ROOT / "web/chanlun_chart/cl_app/templates/index.html").read_text(
        encoding="utf-8"
    )

    assert (
        "charting_library/charting_library.standalone.js') }}?asset={{ static_asset_token }}"
        in template
    )
    assert "charting_library.standalone.js') }}?asset=1.0.0" not in template


def test_static_asset_token_changes_when_standalone_entrypoint_changes(monkeypatch):
    app = _wsgi_app()
    processor = next(
        function
        for function in app.template_context_processors[None]
        if function.__name__ == "inject_static_asset_token"
    )
    original_getmtime = os.path.getmtime
    standalone_path = os.path.normcase(os.path.abspath(STANDALONE))

    try:
        with app.test_request_context("/"):
            first = processor()["static_asset_token"]
            monkeypatch.setattr(
                os.path,
                "getmtime",
                lambda path: original_getmtime(path) + 1
                if os.path.normcase(os.path.abspath(path)) == standalone_path
                else original_getmtime(path),
            )
            second = processor()["static_asset_token"]
    finally:
        app.extensions["shutdown_scheduler"]()

    assert second != first
