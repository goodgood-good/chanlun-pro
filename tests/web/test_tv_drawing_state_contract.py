import json
from types import SimpleNamespace

import pytest

from chanlun.db_models.tv_charts import TableByTVCharts
from cl_app import create_app
from cl_app.blueprints import tv


SCHEMA = "chanlun-user-drawings"
URL = (
    "/tv/1.1/drawings?client=test&user=1&chart=default&layout=default"
    "&symbol=US:QQQ.US&resolution=all"
)
CURRENT_NAME = "drawings_default_default_US:QQQ.US_all"


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

    def delete_by_name(chart_type, name, client_id, user_id):
        assert chart_type == "drawing"
        stored.pop(name, None)

    monkeypatch.setattr(tv.db, "tv_chart_get_by_name", get_by_name)
    monkeypatch.setattr(tv.db, "tv_chart_save", save)
    monkeypatch.setattr(tv.db, "tv_chart_del_by_name", delete_by_name)
    return stored, writes


def test_currency_spot_drawing_name_fits_persistence_column():
    name = tv._drawing_storage_name(
        "default",
        "default",
        "CURRENCY_SPOT:BTC/USDT",
        "all",
    )

    assert len(name) == 51
    assert len(name) <= TableByTVCharts.name.type.length


@pytest.mark.parametrize(
    "state",
    [
        {
            "sources": {
                "GiRd26": {
                    "type": "LineToolTrendLine",
                    "state": {"linecolor": "#FF8C00", "interval": "1"},
                }
            }
        },
        {
            "schema": "chanlun-user-drawings/v2",
            "sources": {"manual": {"type": "LineToolRectangle", "state": {}}},
            "groups": {},
        },
    ],
    ids=["schema-less", "previous-version"],
)
def test_legacy_open_tab_state_is_quarantined(app, monkeypatch, state):
    _, writes = _install_fake_store(monkeypatch)

    response = app.test_client().post(
        URL,
        json={"state": state},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "ignored": True,
        "reason_code": "LEGACY_DRAWING_STATE_QUARANTINED",
    }
    assert writes == []


def test_malformed_current_state_is_rejected(app, monkeypatch):
    _, writes = _install_fake_store(monkeypatch)

    response = app.test_client().post(
        URL,
        json={
            "state": {
                "schema": SCHEMA,
                "sources": [],
                "groups": {},
            }
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "status": "error",
        "message": "invalid drawing state",
    }
    assert writes == []


def test_write_is_normalized_to_explicit_manual_sources(app, monkeypatch):
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
                "groups": {"ignored-group": {"lineTools": ["manual"]}},
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


def test_empty_current_state_removes_redundant_record(app, monkeypatch):
    stored, writes = _install_fake_store(
        monkeypatch,
        {
            CURRENT_NAME: SimpleNamespace(
                content=json.dumps(
                    {
                        "schema": SCHEMA,
                        "sources": {"manual": {"type": "LineToolRectangle"}},
                        "groups": {},
                    }
                )
            )
        },
    )

    response = app.test_client().post(
        URL,
        json={"state": {"schema": SCHEMA, "sources": {}, "groups": {}}},
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert CURRENT_NAME not in stored
    assert writes == []


def test_invalid_current_record_is_not_restored(app, monkeypatch):
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
