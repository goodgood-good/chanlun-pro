"""A normal restart retains the deployed catalog, with explicit narrowing."""

import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows deployment")
@pytest.mark.parametrize(
    "saved,explicit,expected",
    [
        (True, None, True),
        (False, None, False),
        (True, False, False),
        (False, True, True),
        (None, None, False),
    ],
)
def test_resolve_catalog_scope_retains_or_explicitly_changes_choice(
    tmp_path, saved, explicit, expected
):
    path = tmp_path / "deployment_scope.json"
    if saved is not None:
        path.write_text(
            json.dumps(
                {
                    "schema": "chanlun-web-watchdog-deployment-scope-v1",
                    "project_root": str(tmp_path),
                    "web_port": 9900,
                    "enable_full_symbol_catalog": saved,
                }
            ),
            encoding="utf-8",
        )
    script = tmp_path / "probe.ps1"
    argument = "" if explicit is None else " -ExplicitValue $" + str(explicit).lower()
    script.write_text(
        f". '{ROOT / 'ops/deploy_common.ps1'}'\n"
        f"Resolve-DeploymentCatalogScope -Path '{path}' -ProjectRoot '{tmp_path}' -Port 9900{argument} | ConvertTo-Json\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows deployment")
@pytest.mark.parametrize(
    "change",
    [
        {"web_port": 9999},
        {"project_root": "D:\\other-app"},
        {"enable_full_symbol_catalog": "false"},
    ],
)
def test_catalog_scope_rejects_foreign_or_malformed_saved_settings(tmp_path, change):
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {
                "schema": "chanlun-web-watchdog-deployment-scope-v1",
                "project_root": str(tmp_path),
                "web_port": 9900,
                "enable_full_symbol_catalog": True,
                **change,
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "probe.ps1"
    script.write_text(
        f"$ErrorActionPreference='Stop'\n. '{ROOT / 'ops/deploy_common.ps1'}'\n"
        f"Resolve-DeploymentCatalogScope -Path '{path}' -ProjectRoot '{tmp_path}' -Port 9900\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert "does not match" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows watchdog")
def test_watchdog_checks_us_after_healthy_a_and_reports_provider_degradation(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            body = {"status": "alive", "pid": 1234, "revision": "test"}
            status = 200
            if self.path.startswith("/readyz"):
                failed = self.path.endswith("market=us")
                body.update(
                    status="not_ready" if failed else "ready",
                    reasons=["symbols_not_ready"] if failed else [],
                    components={
                        "runtime": {"required": True, "ready": True},
                        "monitor": {
                            "required": True,
                            "ready": True,
                            "recovery_recommended": False,
                        },
                    },
                )
                status = 503 if failed else 200
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops/restart_web.ps1").write_text("exit 99\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "ops/watch_web.ps1"),
                "-ProjectRoot",
                str(tmp_path),
                "-WebPort",
                str(server.server_port),
                "-Once",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "/readyz?market=a" in calls and "/readyz?market=us" in calls
    heartbeat = json.loads(
        (tmp_path / ".cache/chanlun_web_watchdog/heartbeat.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert heartbeat["status"] == "operational_degraded"
    assert heartbeat["recovery_recommended"] is False
    assert "us:" in heartbeat["detail"]
