import os
from pathlib import Path
import subprocess

import pytest


def test_restart_script_validates_and_reuses_configured_web_port():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_qmt_daily.ps1"
    ).read_text(encoding="utf-8")

    port_preflight = source.index("$webPort = 9900")
    stop_phase = source.index("# --- 1. Stop the web project")

    assert port_preflight < stop_phase
    assert "$env:CHANLUN_WEB_PORT = [string]$webPort" in source
    assert '$healthUri = "http://${probeHost}:$webPort/readyz?market=a"' in source
    assert "Invoke-RestMethod -Uri $healthUri" in source
    assert "-HealthUri $healthUri" in source


@pytest.mark.skipif(os.name != "nt", reason="restart script targets Windows")
def test_restart_dotenv_loader_preserves_equals_and_quotes(tmp_path):
    script = Path(__file__).resolve().parents[1] / "ops" / "restart_qmt_daily.ps1"
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        b'CHANLUN_WEB_HOST="127.0.0.1"\nCHANLUN_WEB_PORT=19999\n'
        + b'CHANLUN_TEST_VALUE="'
        + bytes.fromhex("d7f3")
        + b"="
        + bytes.fromhex("d3d2")
        + b'"\n'
    )
    script_arg = str(script).replace("'", "''")
    env_arg = str(env_file).replace("'", "''")
    command = (
        f"$tokens=$null; $errors=$null; "
        f"$ast=[Management.Automation.Language.Parser]::ParseFile('{script_arg}',"
        "[ref]$tokens,[ref]$errors); "
        "$definition=$ast.Find({param($node) "
        "$node -is [Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Import-ProjectDotEnv'}, $true); "
        ". ([scriptblock]::Create($definition.Extent.Text)); "
        "$expected=([string][char]0x5de6) + '=' + ([string][char]0x53f3); "
        "Remove-Item Env:CHANLUN_WEB_HOST,Env:CHANLUN_WEB_PORT,"
        "Env:CHANLUN_TEST_VALUE -ErrorAction SilentlyContinue; "
        f"Import-ProjectDotEnv -Path '{env_arg}'; "
        "if ($env:CHANLUN_WEB_HOST -ne '127.0.0.1' -or "
        "$env:CHANLUN_WEB_PORT -ne '19999' -or "
        "$env:CHANLUN_TEST_VALUE -ne $expected) { exit 1 }"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_restart_owns_only_the_configured_port_process_and_confirms_shutdown():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_qmt_daily.ps1"
    ).read_text(encoding="utf-8")

    assert "[regex]::Escape($AppScript)" in source
    assert "Get-NetTCPConnection" in source
    assert "$targetWebProcs" in source
    assert "Wait-Process -Id" in source
    assert "web process or listening port remained after stop" in source
    assert "Restore-WebService" in source


def test_restart_loads_dotenv_and_resolves_the_project_python_before_preflight():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_qmt_daily.ps1"
    ).read_text(encoding="utf-8")

    dotenv = source.index("Import-ProjectDotEnv")
    python = source.index("Resolve-ProjectPython")
    preflight = source.index("$preflightCode =")

    assert dotenv < preflight
    assert python < preflight
    assert "CHANLUN_PYTHON" in source
    assert ".venv\\Scripts\\python.exe" in source
    assert "poetry" in source
    assert "C:\\Users\\lc\\miniconda3" not in source
