import json

import pytest

from cl_app import create_app
from cl_app.blueprints import other as other_module


class _EmptyTicksExchange:
    def ticks(self, _codes):
        return {}

    def now_trading(self, _market=None):
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


@pytest.mark.parametrize("path", ["/xuangu/task_add"])
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
