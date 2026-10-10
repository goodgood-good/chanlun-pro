"""Exercise real PowerShell handoffs without scheduling tasks or restarting Web."""

import json
import os
from pathlib import Path
import socket
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows deployment")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_script(script):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def test_limited_handoff_explicitly_forwards_disabled_catalog(tmp_path):
    script = tmp_path / "limited_handoff.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$tokens=$null; $errors=$null\n"
        "$ast=[Management.Automation.Language.Parser]::ParseFile("
        f"{ps_quote(ROOT / 'ops/restart_web.ps1')},[ref]$tokens,[ref]$errors)\n"
        "if ($errors.Count) { throw 'restart script failed parsing' }\n"
        "$definition=$ast.Find({param($node) "
        "$node -is [Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Register-LimitedWebLaunchTask'}, $true)\n"
        ". ([scriptblock]::Create($definition.Extent.Text))\n"
        # All scheduler commands are replaced before the actual function runs.
        "function New-ScheduledTaskAction { param($Execute,$Argument) "
        "[pscustomobject]@{execute=$Execute; arguments=$Argument} }\n"
        "function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) "
        "[pscustomobject]@{run_level=$RunLevel} }\n"
        "function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries,"
        "[switch]$DontStopIfGoingOnBatteries,$ExecutionTimeLimit) 'fake-settings' }\n"
        "function Register-ScheduledTask { param($TaskName,$Action,$Principal,$Settings,"
        "$Description,[switch]$Force) $script:captured=$Action; "
        "$script:registrations += 1 }\n"
        "$script:registrations=0\n"
        "Register-LimitedWebLaunchTask -TaskName 'isolated-test-only' "
        f"-ScriptPath {ps_quote(tmp_path / 'never-executed.ps1')} "
        "-TimeoutSeconds 45 -FullSymbolCatalogEnabled $false\n"
        "@{action=$script:captured; registrations=$script:registrations} | ConvertTo-Json\n",
        encoding="utf-8-sig",
    )
    result = run_script(script)
    assert result.returncode == 0, result.stdout + result.stderr
    captured = json.loads(result.stdout)
    assert captured["registrations"] == 1
    assert captured["action"]["execute"] == "powershell.exe"
    arguments = captured["action"]["arguments"].split()
    assert "-DisableFullSymbolCatalog" in arguments
    assert "-EnableFullSymbolCatalog" not in arguments
    assert "-SkipWatchdog" in arguments


def test_watchdog_explicit_false_overrides_saved_true_in_recovery(tmp_path):
    ops = tmp_path / "ops"
    ops.mkdir()
    received = tmp_path / "received.json"
    (ops / "restart_web.ps1").write_text(
        "param([switch]$SkipWatchdog,[int]$WebReadinessTimeoutSeconds,"
        "[switch]$EnableFullSymbolCatalog,[switch]$DisableFullSymbolCatalog)\n"
        "@{enable=[bool]$EnableFullSymbolCatalog; disable=[bool]$DisableFullSymbolCatalog; "
        "skip=[bool]$SkipWatchdog; bound=@($PSBoundParameters.Keys)} | ConvertTo-Json | "
        f"Set-Content -LiteralPath {ps_quote(received)} -Encoding UTF8\nexit 0\n",
        encoding="utf-8-sig",
    )
    # Keep the socket bound and non-listening, ensuring the watchdog's isolated
    # liveness check fails without contacting any service or losing the port.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        state = tmp_path / ".cache" / "chanlun_web_watchdog"
        state.mkdir(parents=True)
        (state / "deployment_scope.json").write_text(
            json.dumps({
                "schema": "chanlun-web-watchdog-deployment-scope-v1",
                "project_root": str(tmp_path),
                "web_port": port,
                "enable_full_symbol_catalog": True,
            }),
            encoding="utf-8",
        )
        script = tmp_path / "watchdog_probe.ps1"
        # Use a PowerShell call operator so the switch false remains a Boolean,
        # rather than the string '$false' passed by native -File argument parsing.
        script.write_text(
            f"& {ps_quote(ROOT / 'ops/watch_web.ps1')} -ProjectRoot {ps_quote(tmp_path)} "
            f"-WebPort {port} -FailureThreshold 1 -LivenessTimeoutSeconds 5 "
            "-RestartCooldownSeconds 10 -Once -EnableFullSymbolCatalog:$false\n"
            "exit $LASTEXITCODE\n",
            encoding="utf-8-sig",
        )
        result = run_script(script)

    assert result.returncode == 1, result.stdout + result.stderr
    parameters = json.loads(received.read_text(encoding="utf-8-sig"))
    assert parameters["disable"] is True
    assert parameters["enable"] is False
    assert parameters["skip"] is True
    assert "DisableFullSymbolCatalog" in parameters["bound"]
    assert "EnableFullSymbolCatalog" not in parameters["bound"]
    heartbeat = json.loads((state / "heartbeat.json").read_text(encoding="utf-8-sig"))
    assert heartbeat["status"] == "recovered"
    assert heartbeat["scope_source"] == "explicit"
    assert heartbeat["deployment_scope"]["enable_full_symbol_catalog"] is False
