"""Verify real Windows process behavior without touching application services."""
import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console creation")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    root = tmp_path_factory.mktemp("no console 测试")
    command = (
        "$ErrorActionPreference='Stop'; "
        f". {ps_quote(ROOT / 'ops/no_console.ps1')}; "
        f"Get-NoConsoleLauncher -ProjectRoot {ps_quote(root)}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return Path(result.stdout.strip()), root


def test_launcher_is_gui_and_child_has_no_console_with_original_arguments(launcher, tmp_path):
    executable, _ = launcher
    image = executable.read_bytes()
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    assert struct.unpack_from("<H", image, pe_offset + 24 + 68)[0] == 2
    probe = tmp_path / "probe with spaces.py"
    probe.write_text(
        "import ctypes,json,os,sys\n"
        "from pathlib import Path\n"
        "ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p\n"
        "Path('receipt.json').write_text(json.dumps({"
        "'console':ctypes.windll.kernel32.GetConsoleWindow(),"
        "'args':sys.argv[1:],'cwd':os.getcwd(),'marker':os.getenv('NO_CONSOLE_TEST')}),encoding='utf-8')\n"
        "print('stdout-marker'); print('stderr-marker',file=sys.stderr)\n"
        "sys.exit(7)\n", encoding="utf-8",
    )
    args = ["spaced value", "中文", 'a"b', "", "trailing\\"]
    prefix = tmp_path / "console logs"
    result = subprocess.run(
        [str(executable), str(prefix), sys.executable, str(probe), *args],
        cwd=tmp_path, env={**os.environ, "NO_CONSOLE_TEST": "inherited"},
        capture_output=True, timeout=10,
    )
    assert result.returncode == 7
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt == {"console": None, "args": args, "cwd": str(tmp_path), "marker": "inherited"}
    assert "stdout-marker" in Path(str(prefix) + ".stdout.log").read_text()
    assert "stderr-marker" in Path(str(prefix) + ".stderr.log").read_text()
    assert not result.stdout and not result.stderr


def test_direct_launch_returns_real_child_id_and_exit_code(launcher, tmp_path):
    _, build_root = launcher
    child = tmp_path / "direct.py"
    child.write_text("import os,sys\nprint(os.getpid())\nsys.exit(23)\n", encoding="utf-8")
    output = tmp_path / "direct.out"
    error = tmp_path / "direct.err"
    command = (
        "$ErrorActionPreference='Stop'; "
        f". {ps_quote(ROOT / 'ops/no_console.ps1')}; "
        f"$p=Start-NoConsoleProcess -ProjectRoot {ps_quote(build_root)} "
        f"-FilePath {ps_quote(sys.executable)} -ArgumentList {ps_quote(chr(34)+str(child)+chr(34))} "
        f"-WorkingDirectory {ps_quote(tmp_path)} "
        f"-RedirectStandardOutput {ps_quote(output)} -RedirectStandardError {ps_quote(error)}; "
        "try { $p.WaitForExit(); $p.Refresh(); "
        "@{pid=$p.Id;exit=$p.ExitCode;exited=$p.HasExited}|ConvertTo-Json -Compress } "
        "finally {$p.Dispose()}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {"pid": int(output.read_text().strip()), "exit": 23, "exited": True}


def test_launcher_returns_while_background_descendant_keeps_running(launcher, tmp_path):
    executable, _ = launcher
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import ctypes,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "root=Path(__file__).parent\n"
        "if len(sys.argv)>1:\n"
        " ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p\n"
        " (root/'console').write_text(str(ctypes.windll.kernel32.GetConsoleWindow()))\n"
        " (root/'child.pid').write_text(str(os.getpid()))\n"
        " deadline=time.monotonic()+15\n"
        " while not (root/'stop').exists() and time.monotonic()<deadline: time.sleep(.05)\n"
        " print('child-finished',flush=True); (root/'done').write_text('done')\n"
        "else:\n"
        " subprocess.Popen([sys.executable,__file__,'child'],stdout=sys.stdout,stderr=sys.stderr,creationflags=subprocess.CREATE_NO_WINDOW)\n"
        " print('parent-finished')\n", encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [str(executable), str(tmp_path / "tree"), sys.executable, str(worker)],
            capture_output=True, timeout=8,
        )
        assert result.returncode == 0
        deadline = time.monotonic() + 3
        while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (tmp_path / "child.pid").exists()
        assert (tmp_path / "console").read_text() == "None"
        assert not (tmp_path / "done").exists()
    finally:
        (tmp_path / "stop").touch()
        deadline = time.monotonic() + 3
        while not (tmp_path / "done").exists() and time.monotonic() < deadline:
            time.sleep(.05)
    assert (tmp_path / "done").exists()
    assert "child-finished" in (tmp_path / "tree.stdout.log").read_text()


def test_missing_target_returns_failure_and_keeps_diagnostic(launcher, tmp_path):
    executable, _ = launcher
    prefix = tmp_path / "failure"
    result = subprocess.run(
        [str(executable), str(prefix), str(tmp_path / "missing.exe")], timeout=5,
    )
    assert result.returncode == 127
    assert "launcher:" in Path(str(prefix) + ".stderr.log").read_text(encoding="utf-8")


def test_web_recovery_liveness_accepts_native_child(launcher, tmp_path):
    _, build_root = launcher
    child = tmp_path / "waiting.py"
    child.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop'; "
        f". {ps_quote(ROOT / 'ops/no_console.ps1')}; "
        "$tokens=$null;$errors=$null;"
        "$ast=[Management.Automation.Language.Parser]::ParseFile("
        f"{ps_quote(ROOT / 'ops/restart_web.ps1')},[ref]$tokens,[ref]$errors);"
        "$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] "
        "-and $node.Name -eq 'Test-WebLiveness'},$true);"
        ". ([scriptblock]::Create($function.Extent.Text));"
        "function Get-ListeningProcessIds {param($Port) return $script:owned.Id};"
        "function Invoke-RestMethod {param($Uri,$Method,$TimeoutSec) @{status='alive';revision='isolated'}};"
        "$webPort=1;$liveUri='unused';$deploymentRevision='isolated';"
        f"$script:owned=Start-NoConsoleProcess -ProjectRoot {ps_quote(build_root)} "
        f"-FilePath {ps_quote(sys.executable)} -ArgumentList {ps_quote(chr(34)+str(child)+chr(34))} "
        f"-RedirectStandardOutput {ps_quote(tmp_path/'out')} -RedirectStandardError {ps_quote(tmp_path/'err')};"
        "try {Test-WebLiveness -Process $script:owned -TimeoutSeconds 2} "
        "finally {Stop-Process -Id $script:owned.Id -ErrorAction SilentlyContinue;"
        "$script:owned.WaitForExit();$script:owned.Dispose()}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "True"


def test_nested_powershell_capture_stays_without_console(launcher, tmp_path):
    executable, _ = launcher
    nested = tmp_path / 'nested.ps1'
    nested.write_text(
        "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
        "public class NestedConsole{[DllImport(\"kernel32.dll\")]public static extern IntPtr GetConsoleWindow();}'\n"
        "[NestedConsole]::GetConsoleWindow().ToInt64()\n", encoding='utf-8-sig',
    )
    outer = tmp_path / 'outer.ps1'
    outer.write_text(
        f"$captured = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File {ps_quote(nested)} 2>&1\n"
        "Write-Output $captured\nexit $LASTEXITCODE\n", encoding='utf-8-sig',
    )
    powershell = str(Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    prefix = tmp_path / 'nested-output'
    result = subprocess.run(
        [str(executable), str(prefix), powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(outer)],
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert Path(str(prefix)+'.stdout.log').read_text().strip() == '0'


@pytest.mark.parametrize("own_tree", [False, True])
def test_terminating_launcher_stops_the_owned_processes(launcher, tmp_path, own_tree):
    executable, _ = launcher
    worker = tmp_path / "owned.py"
    worker.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "if len(sys.argv)>1:\n time.sleep(15)\n"
        "else:\n"
        " child=subprocess.Popen([sys.executable,__file__,'leaf'],creationflags=subprocess.CREATE_NO_WINDOW)\n"
        " Path(__file__).with_suffix('.json').write_text(json.dumps([os.getpid(),child.pid]))\n"
        " time.sleep(15)\n", encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    process = subprocess.Popen([
        str(executable), *(["--tree"] if own_tree else []),
        str(tmp_path / "owned"), sys.executable, str(worker),
    ])
    handles = []
    try:
        deadline = time.monotonic() + 5
        while not worker.with_suffix('.json').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        pids = json.loads(worker.with_suffix('.json').read_text())
        handles = [kernel.OpenProcess(0x100001, False, pid) for pid in pids]
        assert all(handles)
        process.terminate()
        process.wait(timeout=5)
        assert kernel.WaitForSingleObject(handles[0], 3000) == 0
        assert kernel.WaitForSingleObject(handles[1], 3000 if own_tree else 0) == (0 if own_tree else 258)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        for handle in handles:
            if handle:
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    kernel.TerminateProcess(handle, 0)
                    kernel.WaitForSingleObject(handle, 3000)
                kernel.CloseHandle(handle)
