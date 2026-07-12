import io

import pytest

from cl_app import create_app
import cl_app.blueprints.zixuan as zixuan_blueprint


def test_import_propagates_setup_failure_before_any_group_write(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    class _ZiXuan:
        zixuan_list = [{"name": "test"}]

        def __init__(self, _market):
            pass

        def replace_zx_stocks(self, *_args, **_kwargs):
            raise AssertionError("group must not be written before setup completes")

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)

    def fail_exchange(_market):
        raise RuntimeError("forced setup failure")

    monkeypatch.setattr(zixuan_blueprint, "get_exchange", fail_exchange)

    with pytest.raises(RuntimeError, match="forced setup failure"):
        app.test_client().post(
            "/zixuan_opt_import",
            data={
                "market": "a",
                "zx_group": "test",
                "file": (io.BytesIO(b"SH.600000,test\n"), "stocks.txt"),
            },
            content_type="multipart/form-data",
        )
