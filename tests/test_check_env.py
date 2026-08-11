from types import SimpleNamespace

import pytest

import check_env as check_env_module


class _CloseableConnection:
    def close(self):
        pass


class _RedisClient:
    def get(self, _key):
        return None


def _modules(*, db_connect=None, config_overrides=None):
    config_values = {
        "PROXY_HOST": "",
        "PROXY_PORT": 0,
        "REDIS_HOST": "",
        "REDIS_PORT": 0,
        "DB_TYPE": "sqlite",
        "DB_HOST": "",
        "DB_PORT": 0,
        "DB_USER": "",
        "DB_PWD": "",
        "DB_DATABASE": "",
    }
    config_values.update(config_overrides or {})
    return {
        "pymysql": SimpleNamespace(
            connect=db_connect or (lambda **_kwargs: _CloseableConnection())
        ),
        "redis": SimpleNamespace(Redis=lambda **_kwargs: _RedisClient()),
        "chanlun.core.cl": object(),
        "chanlun.config": SimpleNamespace(**config_values),
    }


def _importer(modules, failed_module=None):
    def import_module(name):
        if name == failed_module or name not in modules:
            raise ImportError(name)
        return modules[name]

    return import_module


def _run_check(modules, **kwargs):
    messages = []
    result = check_env_module.check_env(
        version_info=kwargs.pop("version_info", (3, 10)),
        importer=_importer(modules, kwargs.pop("failed_module", None)),
        connection_factory=kwargs.pop(
            "connection_factory", lambda *_args, **_kwargs: _CloseableConnection()
        ),
        output=messages.append,
        **kwargs,
    )
    return result, messages


def test_unsupported_python_is_hard_failure_without_environment_ok():
    result, messages = _run_check(_modules(), version_info=(3, 9))

    assert result is False
    assert "环境OK" not in messages


@pytest.mark.parametrize("failed_module", ["pymysql", "redis"])
def test_missing_dependency_is_hard_failure_without_environment_ok(failed_module):
    result, messages = _run_check(_modules(), failed_module=failed_module)

    assert result is False
    assert "环境OK" not in messages


def test_missing_project_config_is_hard_failure_without_environment_ok():
    result, messages = _run_check(_modules(), failed_module="chanlun.config")

    assert result is False
    assert "环境OK" not in messages


def test_database_failure_is_hard_failure_without_environment_ok():
    def fail_db(**_kwargs):
        raise OSError("database unavailable")

    modules = _modules(
        db_connect=fail_db,
        config_overrides={
            "DB_TYPE": "mysql",
            "DB_HOST": "db",
            "DB_PORT": 3306,
            "DB_USER": "user",
            "DB_PWD": "password",
            "DB_DATABASE": "chanlun",
        },
    )

    result, messages = _run_check(modules)

    assert result is False
    assert "环境OK" not in messages


def test_proxy_probe_uses_socket_factory_with_timeout():
    calls = []

    def connect(address, timeout):
        calls.append((address, timeout))
        return _CloseableConnection()

    modules = _modules(
        config_overrides={"PROXY_HOST": "proxy.local", "PROXY_PORT": 1080}
    )

    result, messages = _run_check(modules, connection_factory=connect)

    assert result is True
    assert calls == [(('proxy.local', 1080), 3.0)]
    assert messages[-1] == "环境OK"


def test_main_returns_nonzero_when_check_fails(monkeypatch):
    monkeypatch.setattr(check_env_module, "check_env", lambda: False)

    assert check_env_module.main() == 1


def test_telnetlib_is_not_used():
    source = check_env_module.__file__

    with open(source, "r", encoding="utf-8") as fp:
        assert "telnetlib" not in fp.read()
