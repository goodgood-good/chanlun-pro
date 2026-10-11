import io

from cl_app import create_app
import cl_app.blueprints.zixuan as zixuan_blueprint


def test_import_reports_setup_failure_before_any_group_write(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    class _ZiXuan:
        zixuan_list = [{"name": "test"}]

        def __init__(self, _market):
            pass

        def merge_zx_stocks(self, *_args, **_kwargs):
            raise AssertionError("group must not be written before setup completes")

    monkeypatch.setattr(zixuan_blueprint, "ZiXuan", _ZiXuan)

    def fail_exchange(_market):
        raise RuntimeError("forced setup failure")

    monkeypatch.setattr(zixuan_blueprint, "get_exchange", fail_exchange)

    response = app.test_client().post(
            "/zixuan_opt_import",
            data={
                "market": "a",
                "zx_group": "test",
                "file": (io.BytesIO(b"SH.600000,test\n"), "stocks.txt"),
            },
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False and body["imported"] == 0 and body["skipped"] == 1
    assert body["issues"][0]["code"] == "SH.600000"
    assert "暂不可核验" in body["issues"][0]["reason"]
