"""Captured restart commands must return while their own service stays alive."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

import pytest

from chanlun.screening.worker_lifetime import process_identity

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows deployment process contract')


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize('exit_code,flags', [(0, []), (7, ['-SkipWatchdog', '-EnableFullSymbolCatalog', '-OpenBrowser'])])
def test_captured_restart_returns_direct_exit_code_before_background_service_stops(tmp_path, exit_code, flags):
    root = tmp_path / 'project with spaces'
    ops = root / 'ops'
    ops.mkdir(parents=True)
    source = (ROOT / 'ops/restart_web.ps1').read_text(encoding='utf-8-sig')
    prefix = source.split('# --------------------------------', 1)[0]
    (ops / 'deploy_common.ps1').write_bytes((ROOT / 'ops/deploy_common.ps1').read_bytes())
    child = root / 'fake_service.py'
    child.write_text(
        "from pathlib import Path\nimport time\n"
        "root=Path(__file__).resolve().parent\n"
        "deadline=time.monotonic()+40\n"
        "while not (root/'stop').exists() and time.monotonic()<deadline:time.sleep(.1)\n"
        "(root/'child.done').write_text('finished',encoding='ascii')\n", encoding='utf-8')
    body = (
        f"$child = Start-Process -FilePath {literal(sys.executable)} -ArgumentList {literal(chr(34)+str(child)+chr(34))} "
        f"-WindowStyle Hidden -PassThru -RedirectStandardOutput {literal(root/'child.out')} -RedirectStandardError {literal(root/'child.err')}\n"
        f"[IO.File]::WriteAllText({literal(root/'child.pid')}, [string]$child.Id)\n"
        "$received = @{ skip_watchdog=[bool]$SkipWatchdog; full_catalog=[bool]$EnableFullSymbolCatalog; "
        "open_browser=[bool]$OpenBrowser; timeout=$WebReadinessTimeoutSeconds; marker=$env:CHANLUN_RESTART_LAUNCH_TEST }\n"
        f"$received | ConvertTo-Json | Set-Content -LiteralPath {literal(root/'parameters.json')} -Encoding UTF8\n"
        f"exit {exit_code}\n")
    restart = ops / 'restart_web.ps1'
    restart.write_text(prefix + body, encoding='utf-8-sig')
    outer = root / 'capture.ps1'
    outer.write_text(
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {literal(restart)} {' '.join(flags)} "
        f"-WebReadinessTimeoutSeconds 37 *> {literal(root/'capture.log')}\nexit $LASTEXITCODE\n", encoding='utf-8-sig')
    process = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(outer)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                               env={**os.environ, 'CHANLUN_RESTART_LAUNCH_TEST': 'inherited-test-value'})
    returned = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=8)
            returned = True
        except subprocess.TimeoutExpired:
            pass
        child_pid = int((root/'child.pid').read_text(encoding='ascii'))
        child_alive = process_identity(child_pid)['alive']
    finally:
        (root/'stop').write_text('stop', encoding='ascii')
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        deadline = time.monotonic() + 5
        while not (root/'child.done').exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if (root/'child.pid').exists():
            assert (root/'child.done').exists(), 'Isolated service has not completed cleanup'
    assert returned, 'Restart output waited for the background service instead of the deployment process'
    assert child_alive is True
    assert process.returncode == exit_code, stdout + stderr
    assert json.loads((root/'parameters.json').read_text(encoding='utf-8-sig')) == {
        'skip_watchdog': bool(flags), 'full_catalog': bool(flags),
        'open_browser': bool(flags), 'timeout': 37, 'marker': 'inherited-test-value'}


def test_preflight_output_and_failure_remain_available_to_the_caller(tmp_path):
    ops = tmp_path / 'ops'
    ops.mkdir()
    prefix = (ROOT/'ops/restart_web.ps1').read_text(encoding='utf-8-sig').split('# --------------------------------', 1)[0]
    (ops/'deploy_common.ps1').write_bytes((ROOT/'ops/deploy_common.ps1').read_bytes())
    restart = ops/'restart_web.ps1'
    restart.write_text(prefix + "Write-Output ('worker=' + [bool]$DeploymentWorker)\n"
                       "Write-Output 'preflight diagnostic'\nexit 9\n", encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                             str(restart), '-PreflightOnly'], capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 9
    assert 'worker=False' in result.stdout and 'preflight diagnostic' in result.stdout
