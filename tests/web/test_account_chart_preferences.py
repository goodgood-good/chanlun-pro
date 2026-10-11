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


def test_login_retains_notification_chart_symbol_period_and_layout(app):
    from urllib.parse import parse_qs,urlsplit
    client=app.test_client()
    target='/?market=us&code=TSLA.US&frequency=5m&layout=single&workspace_layout=1'
    redirect=client.get(target)
    assert redirect.status_code==302
    login=urlsplit(redirect.headers['Location'])
    assert login.path=='/login' and parse_qs(login.query)['next']==[target]
    page=client.get(redirect.headers['Location']).get_data(as_text=True)
    assert 'name="next"' in page and 'TSLA.US' in page
    response=client.post('/login',data={'username':'alice-layout-test','password':'alice-password','next':target})
    assert response.status_code==302 and response.headers['Location']==target


@pytest.mark.parametrize('target',['https://example.test/','//example.test/','/\\example.test/','/%2fexample.test/','/\n/evil'])
def test_login_rejects_external_or_ambiguous_redirects(app,target):
    response=app.test_client().post('/login',data={'username':'alice-layout-test','password':'alice-password','next':target})
    assert response.status_code==302 and response.headers['Location']=='/'


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


def test_phone_preferences_and_layouts_do_not_overwrite_desktop(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")
    original = _preferences("four", "dark", 520)
    assert client.put("/api/chart/preferences", json=original).status_code == 200
    phone = json.dumps({"chart_layout_type": "single", "us_interval_1": "1"})
    assert client.put("/api/chart/preferences", json={
        "schema": original["schema"], "merge": True,
        "changed_keys": ["phone_tv_chart"], "values": {"phone_tv_chart": phone},
    }).status_code == 200
    values = client.get("/api/chart/preferences").get_json()["preferences"]["values"]
    assert json.loads(values["tv_chart"])["chart_layout_type"] == "four"
    assert json.loads(values["phone_tv_chart"])["us_interval_1"] == "1"
    desktop_url = "/tv/1.1/charts?client=device-regression_us_1&user=session"
    phone_url = "/tv/1.1/charts?client=device-regression_us_1_phone&user=session"
    for url, name in [(desktop_url, "Desktop MACD"), (phone_url, "Phone MACD HTF")]:
        assert client.post(url, data={"name": name, "content": '{"charts":[]}',
                                      "symbol": "US:AAPL.US", "resolution": "5"}).status_code == 200
    assert [r["name"] for r in client.get(desktop_url).get_json()["data"]] == ["Desktop MACD"]
    assert [r["name"] for r in client.get(phone_url).get_json()["data"]] == ["Phone MACD HTF"]


def test_stale_chart_id_from_other_market_creates_persisted_us_layout(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")
    a_url = "/tv/1.1/charts?client=chanlun_pro_a_1&user=session"
    us_url = "/tv/1.1/charts?client=chanlun_pro_us_1&user=session"
    a_content = '{"charts":[{"studies":[]}]}'
    us_content = '{"charts":[{"studies":[{"name":"MACD"}]}]}'

    a_id = client.post(a_url, data={
        "name": "A layout", "content": a_content,
        "symbol": "a:SH.600519", "resolution": "5",
    }).get_json()["id"]
    empty_us_id = client.post(us_url, data={
        "name": "Empty US layout", "content": a_content,
        "symbol": "us:AAPL.US", "resolution": "30",
    }).get_json()["id"]
    first_us_save = client.post(us_url + f"&chart={a_id}", data={
        "name": "US layout", "content": us_content,
        "symbol": "us:AAPL.US", "resolution": "30",
    })

    assert first_us_save.status_code == 200
    new_id = first_us_save.get_json()["id"]
    assert new_id != a_id
    assert [row["id"] for row in client.get(us_url).get_json()["data"]] == [
        new_id, empty_us_id,
    ]
    assert client.get(us_url + f"&chart={new_id}").get_json()["data"]["content"] == us_content
    assert client.get(a_url + f"&chart={a_id}").get_json()["data"]["content"] == a_content

    revised = '{"charts":[{"studies":[{"name":"MACD"},{"name":"RSI"}]}]}'
    updated = client.post(us_url + f"&chart={new_id}", data={
        "name": "US layout", "content": revised,
        "symbol": "us:AAPL.US", "resolution": "30",
    })
    assert updated.get_json() == {"status": "ok", "id": new_id}
    assert client.get(us_url + f"&chart={new_id}").get_json()["data"]["content"] == revised


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
        json={"state": state,'base_state':{'schema':'chanlun-user-drawings','sources':{},'groups':{}}},
    ).get_json()['status'] == 'ok'
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
    assert saved["schema"] == "chanlun-chart-config-v9"
    for key, value in display.items():
        if key != "schema":
            assert saved[key] is value


def test_physical_period_and_trend_preferences_roundtrip(app):
    client = app.test_client()
    _login(client, "alice-layout-test", "alice-password")
    display = {"schema": "chanlun-chart-config-v9", "trend_all": True,
               "center_period_1m": True, "center_period_5m": True,
               "trend_period_30m": False, "point_period_5m": True,
               "divergence_period_30m": True}
    response = client.put("/api/chart/preferences", json={
        "schema": "chanlun-account-chart-preferences/v1",
        "values": {"cl_show_config_1_1": json.dumps(display)},
    })
    assert response.status_code == 200
    saved = json.loads(client.get("/api/chart/preferences").get_json()
                       ["preferences"]["values"]["cl_show_config_1_1"])
    assert saved == display


def _normalized_chart_preferences(chart, bucket="tv_chart"):
    from cl_app.services.account_preferences import PREFERENCE_SCHEMA, normalize_preferences

    result = normalize_preferences({"schema": PREFERENCE_SCHEMA, "values": {bucket: json.dumps(chart)}})
    return json.loads(result["values"][bucket])


@pytest.mark.parametrize("bucket", ["tv_chart", "phone_tv_chart"])
def test_pane_ratios_preserve_all_supported_layouts_and_bucket_contract(bucket):
    ratios = {
        "horizontal-2": {"rows": 0.5},
        "horizontal-7-3": {"rows": 0.7},
        "vertical-2": {"columns": 0.5},
        "three": {"rows": 0.3, "top": 0.6},
        "four": {"rows": 0.1, "top": 0.9, "bottom": 0.45},
    }
    chart = {"theme": "dark", "chart_layout_type": "single", "a_interval_1": "5", "chart_pane_ratios": ratios}
    assert _normalized_chart_preferences(chart, bucket) == chart


@pytest.mark.parametrize("bucket", ["tv_chart", "phone_tv_chart"])
def test_pane_ratios_filter_unknown_layouts_splits_and_other_tv_keys(bucket):
    chart = {
        "theme": "Light", "chart_layout_type": "four", "arbitrary_state": {"anything": True},
        "chart_pane_ratios": {
            "single": {"rows": 0.5}, "unknown": {"rows": 0.5},
            "horizontal-2": {"rows": 0.4, "columns": 0.6, "top": 0.3},
            "horizontal-7-3": {"rows": 0.8, "bottom": 0.5},
            "vertical-2": {"columns": 0.65, "rows": 0.35},
            "three": {"rows": 0.3, "top": 0.7, "bottom": 0.5},
            "four": {"rows": 0.2, "top": 0.6, "bottom": 0.8, "extra": 0.5},
        },
    }
    assert _normalized_chart_preferences(chart, bucket) == {
        "theme": "Light", "chart_layout_type": "four",
        "chart_pane_ratios": {
            "horizontal-2": {"rows": 0.4}, "horizontal-7-3": {"rows": 0.8},
            "vertical-2": {"columns": 0.65}, "three": {"rows": 0.3, "top": 0.7},
            "four": {"rows": 0.2, "top": 0.6, "bottom": 0.8},
        },
    }


@pytest.mark.parametrize("value", [True, False, None, "0.5", [], {}, 0, 1, -0.1, 0.099999, 0.900001,
                                  float("nan"), float("inf"), float("-inf"), 10 ** 400])
def test_invalid_pane_values_are_removed_without_discarding_valid_siblings(value):
    chart = {"chart_pane_ratios": {"four": {"rows": value, "top": 0.4, "bottom": 0.7}}}
    assert _normalized_chart_preferences(chart) == {"chart_pane_ratios": {"four": {"top": 0.4, "bottom": 0.7}}}


@pytest.mark.parametrize("value", [None, True, 0.5, "0.5", [], {}])
def test_malformed_or_empty_pane_ratio_containers_are_omitted(value):
    assert _normalized_chart_preferences({"theme": "dark", "chart_pane_ratios": value}) == {"theme": "dark"}
    assert _normalized_chart_preferences({"theme": "dark", "chart_pane_ratios": {"four": value}}) == {"theme": "dark"}


def test_chart_pane_ratios_roundtrip_per_account_and_device_with_existing_merge_api(app):
    alice, bob = app.test_client(), app.test_client()
    _login(alice, "alice-layout-test", "alice-password")
    _login(bob, "bob-layout-test", "bob-password")
    original = _preferences("four", "dark", 520)
    desktop = json.loads(original["values"]["tv_chart"])
    desktop["chart_pane_ratios"] = {"four": {"rows": 0.35, "top": 0.6, "bottom": 0.4}}
    original["values"]["tv_chart"] = json.dumps(desktop)
    assert alice.put("/api/chart/preferences", json=original).status_code == 200
    phone = {"chart_layout_type": "single", "chart_pane_ratios": {"four": {"rows": 0.8}}}
    assert alice.put("/api/chart/preferences", json={
        "schema": original["schema"], "merge": True, "changed_keys": ["phone_tv_chart"],
        "values": {"phone_tv_chart": json.dumps(phone)},
    }).status_code == 200
    assert bob.put("/api/chart/preferences", json=_preferences("single", "Light", 340)).status_code == 200
    values = alice.get("/api/chart/preferences").get_json()["preferences"]["values"]
    assert json.loads(values["tv_chart"]) == desktop
    assert json.loads(values["phone_tv_chart"]) == phone
    assert values["chart_menu_width"] == "520"
    bob_values = bob.get("/api/chart/preferences").get_json()["preferences"]["values"]
    assert "chart_pane_ratios" not in json.loads(bob_values["tv_chart"])
    assert "phone_tv_chart" not in bob_values
    # Clearing just the ratio map removes the old split positions on readback.
    cleared = {**desktop, "chart_pane_ratios": {}}
    assert alice.put("/api/chart/preferences", json={
        "schema": original["schema"], "merge": True, "changed_keys": ["tv_chart"],
        "values": {"tv_chart": json.dumps(cleared)},
    }).status_code == 200
    values = alice.get("/api/chart/preferences").get_json()["preferences"]["values"]
    assert "chart_pane_ratios" not in json.loads(values["tv_chart"])
    assert json.loads(values["phone_tv_chart"]) == phone


def test_pane_ratios_are_not_a_new_top_level_preference_bucket():
    from cl_app.services.account_preferences import InvalidAccountPreferences, PREFERENCE_SCHEMA, normalize_preferences

    with pytest.raises(InvalidAccountPreferences, match="unsupported preference key"):
        normalize_preferences({"schema": PREFERENCE_SCHEMA, "values": {"chart_pane_ratios": "{}"}})
