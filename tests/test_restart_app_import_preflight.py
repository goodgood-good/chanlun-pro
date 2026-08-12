from pathlib import Path


def test_restart_compiles_sources_without_importing_app_before_stopping_service():
    source = (
        Path(__file__).resolve().parents[1] / "ops" / "restart_web.ps1"
    ).read_text(encoding="utf-8")

    preflight = source.index("$preflightCode =")
    preflight_run = source.index("$preflightProcess.Start()")
    stop_phase = source.index("# --- 1. 先停止网页项目")

    assert preflight < preflight_run < stop_phase
    assert "compile(source, str(path), 'exec')" in source
    assert "runpy.run_path" not in source
    assert "py_compile" not in source
    assert "WaitForExit($PreflightTimeoutSec * 1000)" in source
    assert "$preflightProcess.Kill()" in source
    assert "preflight timed out" in source
    assert source.index("if ($PreflightOnly)") < stop_phase
