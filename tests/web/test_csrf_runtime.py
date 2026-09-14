import json
from unittest.mock import Mock

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


@pytest.mark.parametrize("path", ["/screening/start", "/screening/cancel", "/screening/reviews"])
def test_ajax_post_endpoints_share_json_csrf_contract(monkeypatch, path):
    actions = Mock(side_effect=AssertionError("rejected request executed a screening action"))
    for name in ("manager.start", "manager.cancel", "workbench.save_review"):
        monkeypatch.setattr(f"cl_app.blueprints.screening.{name}", actions)
    app = create_app(
        {
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
        }
    )

    try:
        response = app.test_client().post(path)
    finally:
        app.extensions["shutdown_runtime_services"]()

    actions.assert_not_called()
    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "s": "error",
        "code": "csrf_failed",
        "errmsg": "CSRF token is missing or expired.",
    }
