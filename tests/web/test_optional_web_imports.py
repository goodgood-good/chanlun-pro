import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run_import_with_blocked_module(module_to_import: str, blocked: str):
    code = f"""
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == {blocked!r} or fullname.startswith({blocked!r} + '.'):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, Blocker())
__import__({module_to_import!r})
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "web" / "chanlun_chart")]
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_ai_blueprint_imports_without_optional_openai():
    result = _run_import_with_blocked_module("cl_app.blueprints.ai", "openai")
    assert result.returncode == 0, result.stderr


def test_alert_tasks_import_without_optional_pyecharts():
    result = _run_import_with_blocked_module("cl_app.alert_tasks", "pyecharts")
    assert result.returncode == 0, result.stderr
