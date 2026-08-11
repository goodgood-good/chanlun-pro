import json
from types import SimpleNamespace

import pytest

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

    monkeypatch.setattr(tv.db, "tv_chart_get_by_name", get_by_name)
    monkeypatch.setattr(tv.db, "tv_chart_save", save)
    return stored, writes


def test_schema_less_state_is_rejected(app, monkeypatch):
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

    assert response.status_code == 400
    assert response.get_json() == {
        "status": "error",
        "message": "unsupported drawing state schema",
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
