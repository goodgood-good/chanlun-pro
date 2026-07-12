from cl_app import create_app
from cl_app.blueprints import zixuan as zixuan_module


def test_invalid_market_is_rejected_before_zixuan_is_created(monkeypatch):
    created = []

    class FakeZiXuan:
        def __init__(self, market):
            created.append(market)

        def get_zx_groups(self):
            return []

    monkeypatch.setattr(zixuan_module, "ZiXuan", FakeZiXuan)
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )

    response = app.test_client().get("/get_zixuan_groups/not-a-market")

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "msg": "无效的市场"}
    assert created == []
    app.extensions["shutdown_scheduler"]()