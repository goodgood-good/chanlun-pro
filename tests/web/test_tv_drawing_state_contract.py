import json
from types import SimpleNamespace

import pytest

from cl_app import create_app
from cl_app.blueprints import tv


SCHEMA = "chanlun-user-drawings/v2"
URL = (
    "/tv/1.1/drawings?client=test&user=1&chart=default&layout=default"
    "&symbol=US:QQQ.US&resolution=all"
)
CURRENT_NAME = "drawings_default_default_US:QQQ.US_all"
LEGACY_NAME = "drawings_US:QQQ.US_all"


@pytest.fixture
def app():
    flask_app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    flask_app.logger.disabled = True
    try:
        yield flask_app
    finally:
        flask_app.extensions["shutdown_scheduler"]()


def _install_fake_store(monkeypatch, records=None):
    stored = dict(records or {})
    writes = []

    def get_by_name(chart_type, name, client_id, user_id):
        assert chart_type == "drawing"
        return stored.get(name)

    def save(chart_type, client_id, user_id, name, content, symbol, resolution):
        assert chart_type == "drawing"
        writes.append((name, content, symbol, resolution))
        stored[name] = SimpleNamespace(content=content)

    monkeypatch.setattr(tv.db, "tv_chart_get_by_name", get_by_name)
    monkeypatch.setattr(tv.db, "tv_chart_save", save)
    return stored, writes


def test_legacy_open_tab_cannot_overwrite_drawing_state(app, monkeypatch):
    _, writes = _install_fake_store(monkeypatch)

    response = app.test_client().post(
        URL,
        json={
            "state": {
                "sources": {
                    "GiRd26": {
                        "type": "LineToolTrendLine",
                        "state": {"linecolor": "#FF8C00", "interval": "1"},
                    }
                }
            }
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "ignored": True,
        "reason_code": "LEGACY_DRAWING_STATE_QUARANTINED",
    }
    assert writes == []


def test_v2_write_is_normalized_to_explicit_manual_sources(app, monkeypatch):
    _, writes = _install_fake_store(monkeypatch)

    response = app.test_client().post(
        URL,
        json={
            "state": {
                "schema": SCHEMA,
                "sources": {
                    "manual": {"type": "LineToolRectangle", "state": {}},
                    "null-source": None,
                    "array-source": [],
                },
                "groups": {"legacy-group": {"lineTools": ["manual"]}},
            }
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert len(writes) == 1
    name, raw_content, symbol, resolution = writes[0]
    assert (name, symbol, resolution) == (CURRENT_NAME, "US:QQQ.US", "all")
    assert json.loads(raw_content) == {
        "schema": SCHEMA,
        "sources": {
            "manual": {"type": "LineToolRectangle", "state": {}},
        },
        "groups": {},
    }


def test_legacy_current_record_is_quarantined_on_read(app, monkeypatch):
    _, writes = _install_fake_store(
        monkeypatch,
        {
            CURRENT_NAME: SimpleNamespace(
                content=json.dumps(
                    {
                        "sources": {
                            "GiRd26": {
                                "type": "LineToolTrendLine",
                                "state": {"linecolor": "#FF8C00"},
                            }
                        }
                    }
                )
            )
        },
    )

    response = app.test_client().get(URL)

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "data": {"schema": SCHEMA, "sources": {}, "groups": {}},
    }
    assert writes == []


def test_only_v2_legacy_key_may_migrate_to_current_key(app, monkeypatch):
    legacy_state = {
        "schema": SCHEMA,
        "sources": {"manual": {"type": "LineToolTrendLine", "state": {}}},
        "groups": {},
    }
    _, writes = _install_fake_store(
        monkeypatch,
        {LEGACY_NAME: SimpleNamespace(content=json.dumps(legacy_state))},
    )

    response = app.test_client().get(URL)

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "data": legacy_state}
    assert len(writes) == 1
    assert writes[0][0] == CURRENT_NAME
    assert json.loads(writes[0][1]) == legacy_state

