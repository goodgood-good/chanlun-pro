from flask import Flask

from cl_app.blueprints import setting as setting_module


class _SettingsDB:
    def __init__(self):
        self.many_calls = []

    def cache_get(self, key):
        assert key == "fs_keys"
        return {}

    def cache_set(self, key, value):
        raise AssertionError("setting_save must not commit cache keys separately")

    def cache_set_many(self, values):
        self.many_calls.append(values)
        return True


def test_setting_save_persists_proxy_and_fs_keys_in_one_atomic_call(monkeypatch):
    fake_db = _SettingsDB()
    monkeypatch.setattr(setting_module, "db", fake_db)
    monkeypatch.setattr(setting_module, "encrypt_str", lambda value: f"encrypted:{value}")
    app = Flask(__name__)

    with app.test_request_context(
        "/setting/save",
        method="POST",
        data={
            "proxy_host": "127.0.0.1",
            "proxy_port": "8080",
            "fs_app_id": "app-id",
            "fs_app_secret": "secret",
            "fs_user_id": "user-id",
        },
    ):
        response = setting_module.setting_save.__wrapped__()

    assert response == {"ok": True}
    assert fake_db.many_calls == [
        {
            "req_proxy": {"host": "127.0.0.1", "port": "8080"},
            "fs_keys": {
                "fs_app_id": "app-id",
                "fs_app_secret": "encrypted:secret",
                "fs_user_id": "user-id",
            },
        }
    ]


def test_setting_save_rejects_missing_fields_before_reading_or_writing_db(monkeypatch):
    class _UntouchedDB:
        def __getattr__(self, name):
            raise AssertionError(f"database must not be accessed: {name}")

    monkeypatch.setattr(setting_module, "db", _UntouchedDB())
    app = Flask(__name__)

    with app.test_request_context(
        "/setting/save",
        method="POST",
        data={"proxy_host": "", "proxy_port": ""},
    ):
        payload, status = setting_module.setting_save.__wrapped__()

    assert status == 400
    assert payload == {
        "ok": False,
        "msg": "缺少表单字段",
        "fields": ["fs_app_id", "fs_app_secret", "fs_user_id"],
    }


def test_setting_save_rejects_incomplete_or_invalid_proxy_before_writing(monkeypatch):
    class _ReadOnlyDB:
        def cache_get(self, key):
            return {}

        def cache_set_many(self, values):
            raise AssertionError("invalid proxy settings must not be written")

    monkeypatch.setattr(setting_module, "db", _ReadOnlyDB())
    app = Flask(__name__)
    base_form = {
        "fs_app_id": "",
        "fs_app_secret": "",
        "fs_user_id": "",
    }

    for proxy, expected_message in (
        ({"proxy_host": "proxy.local", "proxy_port": ""}, "代理 Host 和 Port 必须同时填写或同时留空"),
        ({"proxy_host": "proxy.local", "proxy_port": "70000"}, "代理 Port 必须是 1 到 65535 的整数"),
    ):
        with app.test_request_context(
            "/setting/save", method="POST", data={**base_form, **proxy}
        ):
            payload, status = setting_module.setting_save.__wrapped__()

        assert status == 400
        assert payload == {"ok": False, "msg": expected_message}
