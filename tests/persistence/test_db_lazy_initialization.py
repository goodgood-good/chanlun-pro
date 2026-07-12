import os
import pathlib
import subprocess
import pytest
import sys


def test_importing_db_module_does_not_create_an_engine():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    script = r'''
import os
import pathlib
import sys

repo_root = pathlib.Path(os.environ["CHANLUN_REPO_ROOT"])
sys.path.insert(0, str(repo_root / "src"))

import sqlalchemy
import chanlun.config as config

config.DB_TYPE = "mysql"
engine_calls = []


def fail_if_engine_is_created(*args, **kwargs):
    engine_calls.append((args, kwargs))
    raise AssertionError("database engine created during module import")


sqlalchemy.create_engine = fail_if_engine_is_created
import chanlun.persistence.db as db_module

assert engine_calls == []
assert db_module.db.is_initialized() is False
'''
    env = os.environ.copy()
    env["CHANLUN_REPO_ROOT"] = str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_mode_rejects_mysql_before_engine_creation(monkeypatch):
    from chanlun import config
    from chanlun.persistence import db as db_module

    engine_calls = []

    def fail_if_engine_is_created(*args, **kwargs):
        engine_calls.append((args, kwargs))
        raise AssertionError("unsafe engine creation reached")

    monkeypatch.setattr(config, "DB_TYPE", "mysql")
    monkeypatch.setattr(db_module, "create_engine", fail_if_engine_is_created)

    with pytest.raises(RuntimeError, match="isolated SQLite"):
        db_module.DB.__wrapped__()

    assert engine_calls == []