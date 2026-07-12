import os
import pathlib
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from cl_app import create_app


def _app():
    return create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_app_factory_is_scheduler_side_effect_free_by_default():
    first = _app()
    second = _app()

    for app in (first, second):
        scheduler = app.extensions["scheduler"]
        assert scheduler.running is False
        assert scheduler.get_jobs() == []
        app.extensions["shutdown_scheduler"]()


def test_app_factory_does_not_start_cache_writer_threads_by_default():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    python_path = os.pathsep.join(
        [
            str(repo_root / "src"),
            str(repo_root / "web" / "chanlun_chart"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    script = """
import threading
from chanlun import config
config.DATA_PATH = __import__('os').environ['CHANLUN_TEST_DATA_PATH']
config.DB_TYPE = 'sqlite'
config.DB_DATABASE = 'factory_side_effect_test'
from cl_app import create_app
app = create_app(test_config={
    'TESTING': True,
    'VALIDATE_WEB_SECURITY': False,
    'SCHEDULER_ENABLED': False,
    'WTF_CSRF_ENABLED': False,
})
prefixes = ('FileDbPickleWriter-', 'ChartCacheDisk-')
print('|'.join(t.name for t in threading.enumerate() if t.name.startswith(prefixes)))
app.extensions['shutdown_runtime_services']()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


def test_security_headers_and_cookie_defaults():
    app = _app()
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["MAX_CONTENT_LENGTH"] == 8 * 1024 * 1024

    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    script_src = next(part for part in csp.split(";") if "script-src" in part)
    assert "'self'" in script_src
    assert re.search(r"'nonce-[A-Za-z0-9_-]+'", script_src)
    assert "unsafe-inline" not in script_src
    assert "unsafe-eval" in script_src
    assert "font-src 'self' data: http://at.alicdn.com https://at.alicdn.com" in csp
    assert "frame-src 'self'" in csp
    assert "frame-src 'self' blob:" not in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'self'" in csp


def test_desktop_entrypoint_bootstraps_src_before_chanlun_import():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    app_path = repo_root / "web/chanlun_chart/app.py"
    script = f"""
import importlib.util
import pathlib
spec = importlib.util.spec_from_file_location('desktop_app_probe', {str(app_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import chanlun
print(pathlib.Path(chanlun.__file__).resolve())
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert pathlib.Path(result.stdout.strip()).is_relative_to(repo_root / "src")


def test_desktop_main_returns_failure_without_interactive_prompt(monkeypatch):
    import builtins

    import app as desktop_app
    import chanlun.utils as chanlun_utils

    monkeypatch.setattr(chanlun_utils, "install_stdout_noise_filter", lambda: None)
    monkeypatch.setattr(
        desktop_app,
        "create_app",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("startup failed")),
    )
    monkeypatch.setattr(desktop_app.LogUtil, "exception", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda *_args, **_kwargs: pytest.fail("startup failure must not prompt"),
    )
    monkeypatch.setattr(desktop_app.sys, "argv", ["app.py", "nobrowser"])

    assert desktop_app.main() == 1

def test_desktop_main_starts_runtime_after_http_listener(monkeypatch):
    import tornado.web
    import app as desktop_app
    import chanlun.utils as chanlun_utils
    from cl_app.handlers import sse_stream
    from cl_app.services import static_precompress

    events = []

    class _FakeFlaskApp:
        def __init__(self):
            self.config = {}
            self.extensions = {
                "health_snapshot": lambda *_args: ({"status": "ok"}, 200),
                "start_runtime_services": lambda **_kwargs: events.append("runtime-start"),
                "shutdown_runtime_services": lambda: events.append("runtime-stop"),
            }

    class _FakeExecutor:
        def __init__(self, *_args, **_kwargs):
            events.append("executor-create")

        def shutdown(self, **_kwargs):
            events.append("executor-stop")

        def submit(self, callback):
            callback()
            return SimpleNamespace(done=lambda: True)

    class _FakeServer:
        def __init__(self, *_args, **_kwargs):
            events.append("server-create")

        def bind(self, *_args, **_kwargs):
            events.append("server-bind")

        def start(self, *_args, **_kwargs):
            events.append("server-start")

        def stop(self):
            events.append("server-stop")

    class _FakeLoop:
        def __init__(self):
            self.callbacks = []

        def start(self):
            events.append("loop-start")
            for callback in self.callbacks:
                callback()

        def stop(self):
            events.append("loop-stop")

        def add_callback(self, callback):
            self.callbacks.append(callback)

    fake_loop = _FakeLoop()
    monkeypatch.setattr(chanlun_utils, "install_stdout_noise_filter", lambda: None)
    monkeypatch.setattr(
        desktop_app,
        "create_app",
        lambda **kwargs: events.append(("create-app", kwargs)) or _FakeFlaskApp(),
    )
    monkeypatch.setattr(desktop_app, "DaemonExecutor", _FakeExecutor)
    monkeypatch.setattr(
        desktop_app, "BoundedWSGIContainer", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(desktop_app, "HTTPServer", _FakeServer)
    monkeypatch.setattr(desktop_app.IOLoop, "current", lambda: fake_loop)
    monkeypatch.setattr(tornado.web, "Application", lambda *_a, **_k: object())
    monkeypatch.setattr(
        static_precompress,
        "precompress_static_assets",
        lambda *_a: events.append("static-precompress"),
    )
    monkeypatch.setattr(sse_stream, "build_routes", lambda *_a, **_k: [])
    monkeypatch.setattr(desktop_app, "_warm_chart_cache_from_disk", lambda: None)
    monkeypatch.setattr(desktop_app, "validate_web_security_config", lambda *_a: None)
    monkeypatch.setattr(desktop_app, "get_web_host", lambda: "127.0.0.1")
    monkeypatch.setattr(desktop_app, "get_login_password", lambda: "")
    monkeypatch.setattr(desktop_app, "is_https_enabled", lambda: False)
    monkeypatch.setattr(desktop_app.sys, "argv", ["app.py", "nobrowser"])

    assert desktop_app.main() == 0
    assert ("create-app", {"start_scheduler": False}) in events
    assert events.index("server-bind") < events.index("server-start")
    assert events.index("server-start") < events.index("loop-start")
    assert events.index("loop-start") < events.index("runtime-start")
    assert events.index("loop-start") < events.index("static-precompress")
    assert events.index("loop-start") < events.index("runtime-stop")


def test_runtime_bootstrap_retries_transient_start_failure(monkeypatch):
    import app as desktop_app

    calls = []

    class _App:
        config = {}
        extensions = {}

    app = _App()

    def start_runtime_services(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")

    app.extensions["start_runtime_services"] = start_runtime_services
    monkeypatch.setattr(desktop_app.LogUtil, "exception", lambda *_a, **_k: None)

    assert desktop_app._start_runtime_with_retry(
        app,
        threading.Event(),
        initial_delay=0.01,
        max_delay=0.01,
    ) is True
    assert len(calls) == 2
    assert "RUNTIME_BOOTSTRAP_ERROR" not in app.config


def test_desktop_entrypoint_enables_proxy_headers_only_for_https_mode():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "web/chanlun_chart/app.py"
    ).read_text(encoding="utf-8")
    assert "is_https_enabled" in source
    assert "HTTPServer(tornado_app, xheaders=is_https_enabled())" in source
    assert 'if "CHANLUN_WEB_HOST" not in os.environ and not is_https_enabled():' in source
    assert 'os.environ["CHANLUN_WEB_HOST"] = "127.0.0.1"' in source


def test_factory_blocks_external_request_when_runtime_bind_bypasses_config(
    monkeypatch,
):
    monkeypatch.setenv("CHANLUN_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("CHANLUN_HTTPS", "0")
    monkeypatch.delenv("CHANLUN_LOGIN_PWD", raising=False)
    monkeypatch.setattr("cl_app.get_login_password", lambda: "")
    app = create_app(test_config={"TESTING": True, "SCHEDULER_ENABLED": False})

    response = app.test_client().get(
        "/login", environ_overrides={"REMOTE_ADDR": "203.0.113.10"}
    )

    assert response.status_code == 503
    assert response.get_json() == {"status": "security_misconfigured"}
    app.extensions["shutdown_scheduler"]()

def test_factory_rejects_external_plaintext_password(monkeypatch):
    monkeypatch.setenv("CHANLUN_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("CHANLUN_LOGIN_PWD", "plaintext-is-rejected")
    monkeypatch.setenv("CHANLUN_HTTPS", "1")

    with pytest.raises(ValueError, match="hash"):
        create_app(test_config={"TESTING": True, "SCHEDULER_ENABLED": False})


def test_factory_rejects_external_hash_without_https(monkeypatch):
    monkeypatch.setenv("CHANLUN_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("CHANLUN_LOGIN_PWD", "scrypt:32768:8:1$stub$stub")
    monkeypatch.setenv("CHANLUN_HTTPS", "0")

    with pytest.raises(ValueError, match="HTTPS"):
        create_app(test_config={"TESTING": True, "SCHEDULER_ENABLED": False})


def test_factory_accepts_external_hash_with_https_and_forces_secure_cookies(
    monkeypatch,
):
    monkeypatch.setenv("CHANLUN_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("CHANLUN_LOGIN_PWD", "scrypt:32768:8:1$stub$stub")
    monkeypatch.setenv("CHANLUN_HTTPS", "1")
    monkeypatch.setenv("CHANLUN_SESSION_COOKIE_SECURE", "0")

    app = create_app(test_config={"TESTING": True, "SCHEDULER_ENABLED": False})

    assert app.config["WEB_HOST"] == "0.0.0.0"
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["REMEMBER_COOKIE_SECURE"] is True
    app.extensions["shutdown_scheduler"]()


def test_factory_allows_loopback_passwordless_http(monkeypatch):
    monkeypatch.setenv("CHANLUN_WEB_HOST", "127.0.0.1")
    monkeypatch.delenv("CHANLUN_LOGIN_PWD", raising=False)
    monkeypatch.setattr("cl_app.get_login_password", lambda: "")
    monkeypatch.setenv("CHANLUN_HTTPS", "0")

    app = create_app(test_config={"TESTING": True, "SCHEDULER_ENABLED": False})

    assert app.config["SESSION_COOKIE_SECURE"] is False
    app.extensions["shutdown_scheduler"]()
