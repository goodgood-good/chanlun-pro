from pathlib import Path
import threading

import pytest

from flask import Flask, render_template
from flask_wtf import CSRFProtect
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
from werkzeug.serving import make_server


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "web/chanlun_chart/cl_app/static"
TEMPLATES = ROOT / "web/chanlun_chart/cl_app/templates"


def test_real_dompurify_rejects_active_markdown_payloads():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(path=STATIC / "marked.min.js")
        page.add_script_tag(path=STATIC / "vendor/dompurify-3.4.11.min.js")
        page.add_script_tag(path=STATIC / "js/safe_html.js")
        results = page.evaluate(
            """() => [
              '<img src=x onerror="window.__xss=1">',
              '<svg onload="window.__xss=1"><circle /></svg>',
              '<math><mtext data-x="1">bad</mtext></math>',
              '<p style="background:url(javascript:alert(1))" data-x="1">x</p>',
              '[bad](javascript:alert(1))',
              '<script>window.__xss=1</script>'
            ].map(payload => SafeHtml.renderMarkdown(payload))"""
        )
        for html in results:
            lowered = html.lower()
            assert "onerror" not in lowered
            assert "onload" not in lowered
            assert "javascript:" not in lowered
            assert "<script" not in lowered
            assert "<svg" not in lowered
            assert "<math" not in lowered
            assert "style=" not in lowered
            assert "data-x" not in lowered
        assert page.evaluate("window.__xss") is None
        browser.close()


def test_browser_enforces_nonce_csp_and_injects_real_csrf_token():
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES),
        static_folder=str(STATIC),
        static_url_path="/static",
    )
    app.config.update(SECRET_KEY="browser-test-only", TESTING=True)
    CSRFProtect(app)

    @app.get("/")
    def index():
        return render_template("dark.html", csp_nonce="browser-test-nonce")

    @app.post("/write")
    def write():
        return {"ok": True}

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'nonce-browser-test-nonce'; "
            "style-src 'self' 'unsafe-inline'; object-src 'none'"
        )
        return response

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script("window.__nativeFetch = window.fetch.bind(window);")
            page = context.new_page()
            response = page.goto(f"http://127.0.0.1:{server.server_port}/")
            assert response is not None
            assert "nonce-browser-test-nonce" in response.headers["content-security-policy"]
            missing_status = page.evaluate(
                "() => window.__nativeFetch('/write', {method: 'POST'}).then(r => r.status)"
            )
            protected_status = page.evaluate(
                "() => window.fetch('/write', {method: 'POST'}).then(r => r.status)"
            )
            assert missing_status == 400
            assert protected_status == 200
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
