import json

import pytest

from flask import Flask
from flask_wtf.csrf import generate_csrf

from cl_app.blueprints import tv as tv_module
from cl_app.blueprints.alert import alert_bp
from cl_app import create_app
from cl_app.blueprints import other as other_module
from cl_app.csrf import csrf


def _csrf_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-only-csrf-secret",
        TESTING=True,
        LOGIN_DISABLED=True,
        WTF_CSRF_TIME_LIMIT=3600,
    )
    csrf.init_app(app)

    @app.get("/csrf-token")
    def csrf_token():
        return {"token": generate_csrf()}

    app.register_blueprint(tv_module.tv_bp)
    app.register_blueprint(alert_bp)
    return app


def test_tv_write_requires_valid_flask_wtf_token(monkeypatch):
    app = _csrf_app()
    deleted = []
    monkeypatch.setattr(tv_module, "_parse_tv_symbol", lambda _symbol: ("a", "SH.000001"))
    monkeypatch.setattr(
        tv_module.db,
        "marks_del_all_by_code",
        lambda market, code: deleted.append((market, code)),
    )

    with app.test_client() as client:
        missing = client.post("/tv/del_marks", data={"symbol": "a:SH.000001"})
        assert missing.status_code == 400
        token = client.get("/csrf-token").get_json()["token"]
        accepted = client.post(
            "/tv/del_marks",
            data={"symbol": "a:SH.000001"},
            headers={"X-CSRFToken": token},
        )

    assert accepted.status_code == 200
    assert accepted.get_json() == {"status": "ok"}
    assert deleted == [("a", "SH.000001")]


def test_alert_delete_rejects_get():
    app = _csrf_app()
    response = app.test_client().get("/alert_del/1")
    assert response.status_code == 405


class _EmptyTicksExchange:
    def ticks(self, _codes):
        return {}

    def now_trading(self):
        return True


def test_create_app_accepts_its_own_csrf_token(monkeypatch):
    monkeypatch.setattr(other_module, "get_exchange", lambda _market: _EmptyTicksExchange())
    app = create_app(
        {
            "TESTING": True,
            "PROPAGATE_EXCEPTIONS": False,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
        }
    )

    with app.test_client() as client:
        page = client.get("/")
        marker = 'name="csrf-token" content="'
        token = page.get_data(as_text=True).partition(marker)[2].partition('"')[0]
        response = client.post(
            "/ticks",
            data={"market": "a", "codes": json.dumps([])},
            headers={"X-CSRFToken": token},
        )

    assert token
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "market_state": "open",
        "now_trading": True,
        "ticks": [],
        "error": None,
    }


@pytest.mark.parametrize("path", ["/alert_del/1", "/xuangu/task_add"])
def test_ajax_post_endpoints_share_json_csrf_contract(path):
    app = create_app(
        {
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
        }
    )

    response = app.test_client().post(path)

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "s": "error",
        "code": "csrf_failed",
        "errmsg": "CSRF token is missing or expired.",
    }
