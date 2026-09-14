import json

import pytest
from werkzeug.security import generate_password_hash

from cl_app import create_app


@pytest.fixture
def app(monkeypatch):
    users = {
        "alice-layout-test": generate_password_hash("alice-password"),
        "bob-layout-test": generate_password_hash("bob-password"),
    }
    monkeypatch.setenv("CHANLUN_LOGIN_USERS", json.dumps(users))
    flask_app = create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    try:
        yield flask_app
    finally:
        flask_app.extensions["shutdown_runtime_services"]()


def _login(client, username, password):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 302


def _preferences(layout, theme, width):
    return {
        "schema": "chanlun-account-chart-preferences/v1",
        "values": {
            "tv_chart": json.dumps(
                {
                    "theme": theme,
                    "market": "us" if layout == "four" else "a",
                    "chart_layout_type": layout,
                    "us_code": "AAPL.US",
                    "us_interval_1": "30",
                    "currency_interval_1": "720",
                }
            ),
            "chart_menu_width": str(width),
            "chart_menu_collapsed": "0",
            "cl_show_config_1_30": json.dumps(
                {
                    "schema": "chanlun-chart-config-v7",
                    "bi": layout != "four",
                }
            ),
        },
    }


def test_login_exposes_and_returns_the_named_account(app):
    client = app.test_client()

    login_page = client.get("/login").get_data(as_text=True)
    assert 'name="username"' in login_page
    assert "图表布局与显示偏好会跟随账号保存" in login_page

    _login(client, "ALICE-LAYOUT-TEST", "alice-password")
    payload = client.get("/api/session").get_json()

    assert payload["account"] == {"username": "alice-layout-test"}


def test_chart_preferences_are_isolated_between_accounts(app):
    alice = app.test_client()
    bob = app.test_client()
    _login(alice, "alice-layout-test", "alice-password")
    _login(bob, "bob-layout-test", "bob-password")

    assert alice.put(
        "/api/chart/preferences",
        json=_preferences("four", "dark", 520),
    ).status_code == 200
    assert bob.put(
        "/api/chart/preferences",
        json=_preferences("single", "Light", 340),
    ).status_code == 200

    alice_values = alice.get("/api/chart/preferences").get_json()["preferences"]["values"]
    bob_values = bob.get("/api/chart/preferences").get_json()["preferences"]["values"]

    assert json.loads(alice_values["tv_chart"])["chart_layout_type"] == "four"
    assert json.loads(alice_values["tv_chart"])["currency_interval_1"] == "720"
    assert alice_values["chart_menu_width"] == "520"
    assert json.loads(bob_values["tv_chart"])["chart_layout_type"] == "single"
    assert bob_values["chart_menu_width"] == "340"


def test_tradingview_storage_ignores_client_supplied_user_and_uses_session(app):
    alice = app.test_client()
    bob = app.test_client()
    _login(alice, "alice-layout-test", "alice-password")
    _login(bob, "bob-layout-test", "bob-password")
    url = "/tv/1.1/charts?client=shared-browser-client&user="

    saved = alice.post(
        url + "bob-layout-test",
        data={
            "name": "Alice layout",
            "content": '{"owner":"alice"}',
            "symbol": "US:AAPL.US",
            "resolution": "30",
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["status"] == "ok"

    assert bob.get(url + "alice-layout-test").get_json() == {
        "status": "ok",
        "data": [],
    }
    alice_rows = alice.get(url + "anything-the-client-wants").get_json()["data"]
    assert [row["name"] for row in alice_rows] == ["Alice layout"]


def test_manual_drawings_are_isolated_by_authenticated_account(app):
    alice = app.test_client()
    bob = app.test_client()
    _login(alice, "alice-layout-test", "alice-password")
    _login(bob, "bob-layout-test", "bob-password")
    base_url = (
        "/tv/1.1/drawings?client=shared-browser-client&chart=default"
        "&layout=default&symbol=US:AAPL.US&resolution=all&user="
    )
    state = {
        "schema": "chanlun-user-drawings",
        "sources": {
            "alice-line": {
                "type": "LineToolTrendLine",
                "state": {"linecolor": "#ff0000"},
            }
        },
        "groups": {},
    }

    assert alice.post(
        base_url + "bob-layout-test",
        json={"state": state},
    ).get_json() == {"status": "ok"}
    assert bob.get(base_url + "alice-layout-test").get_json()["data"] == {
        "schema": "chanlun-user-drawings",
        "sources": {},
        "groups": {},
    }
    assert alice.get(base_url + "ignored").get_json()["data"] == state


def test_invalid_or_oversized_preference_keys_are_rejected(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")

    response = client.put(
        "/api/chart/preferences",
        json={
            "schema": "chanlun-account-chart-preferences/v1",
            "values": {"unscoped_secret": "must-not-be-stored"},
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_chart_preferences"


def test_key_merge_prevents_stale_tabs_from_overwriting_unrelated_preferences(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")
    original = _preferences("single", "Light", 340)
    assert client.put("/api/chart/preferences", json=original).status_code == 200

    first_tab = {
        "schema": original["schema"],
        "values": {**original["values"], "chart_menu_width": "520"},
        "merge": True,
        "changed_keys": ["chart_menu_width"],
    }
    second_tab = {
        "schema": original["schema"],
        # This snapshot intentionally still contains the old width.
        "values": {
            **original["values"],
            "chart_menu_collapsed": "1",
        },
        "merge": True,
        "changed_keys": ["chart_menu_collapsed"],
    }

    assert client.put("/api/chart/preferences", json=first_tab).status_code == 200
    assert client.put("/api/chart/preferences", json=second_tab).status_code == 200
    values = client.get("/api/chart/preferences").get_json()["preferences"]["values"]

    assert values["chart_menu_width"] == "520"
    assert values["chart_menu_collapsed"] == "1"


def test_restored_analysis_controls_roundtrip_with_account_preferences(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")
    display = {
        "schema": "chanlun-chart-config-v7",
        "fx": True, "bi": False, "xd": True,
        "center_L0": False, "center_L1": True, "center_observation": True,
        "point_1buy": False, "point_2buy": True, "point_3buy": False,
        "point_1sell": True, "point_2sell": False, "point_3sell": True,
        "divergence_consolidation_L0": False, "divergence_trend_L0": True,
        "trend_L0": True,
    }
    response = client.put("/api/chart/preferences", json={
        "schema": "chanlun-account-chart-preferences/v1",
        "values": {"cl_show_config_1_1": json.dumps(display)},
    })
    assert response.status_code == 200
    saved = json.loads(client.get("/api/chart/preferences").get_json()
                       ["preferences"]["values"]["cl_show_config_1_1"])
    assert saved["schema"] == "chanlun-chart-config-v8"
    assert "trend_L0" not in saved
    for key, value in display.items():
        if key not in {"schema", "trend_L0"}:
            assert saved[key] is value
