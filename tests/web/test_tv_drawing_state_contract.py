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


def test_drawing_manifest_lists_only_matching_layout_contexts(app, monkeypatch):
    records = [
        SimpleNamespace(
            name="drawings_default_default_US:AAPL.US_all",
            symbol="US:AAPL.US",
            resolution="all",
            timestamp=20,
            content=json.dumps(
                {
                    "schema": SCHEMA,
                    "sources": {"manual-latest": {"type": "LineToolTrendLine"}},
                    "groups": {},
                }
            ),
        ),
        # Legacy duplicate rows collapse to one newest context.
        SimpleNamespace(
            name="drawings_default_default_US:AAPL.US_all",
            symbol="US:AAPL.US",
            resolution="all",
            timestamp=10,
            content=json.dumps(
                {
                    "schema": SCHEMA,
                    "sources": {"manual-old": {"type": "LineToolTrendLine"}},
                    "groups": {},
                }
            ),
        ),
        # A different saved layout must not affect the default-layout negative cache.
        SimpleNamespace(
            name="drawings_other_default_US:MSFT.US_all",
            symbol="US:MSFT.US",
            resolution="all",
            timestamp=30,
            content=json.dumps(
                {
                    "schema": SCHEMA,
                    "sources": {"other-layout": {"type": "LineToolTrendLine"}},
                    "groups": {},
                }
            ),
        ),
        # Current-schema empty rows and legacy payloads behave exactly like a
        # missing row and therefore must not defeat the front-end negative cache.
        SimpleNamespace(
            name="drawings_default_default_US:NVDA.US_all",
            symbol="US:NVDA.US",
            resolution="all",
            timestamp="invalid",
            content=json.dumps({"schema": SCHEMA, "sources": {}, "groups": {}}),
        ),
        SimpleNamespace(
            name="drawings_default_default_US:TSLA.US_all",
            symbol="US:TSLA.US",
            resolution="all",
            timestamp=40,
            content=json.dumps({"sources": {"legacy": {"type": "LineToolTrendLine"}}}),
        ),
    ]
    monkeypatch.setattr(
        tv.db,
        "tv_chart_list",
        lambda chart_type, client_id, user_id: records,
    )

    response = app.test_client().get(URL + "&manifest=1")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "data": {
            "complete": True,
            "entries": [{"symbol": "US:AAPL.US", "resolution": "all"}],
        },
    }


def test_drawing_manifest_all_contexts_uses_exact_nonempty_storage_names(
    app,
    monkeypatch,
):
    manual_state = json.dumps(
        {
            "schema": SCHEMA,
            "sources": {"manual": {"type": "LineToolTrendLine"}},
            "groups": {},
        }
    )
    records = [
        SimpleNamespace(
            name="drawings_default_default_US:AAPL.US_all",
            symbol="US:AAPL.US",
            resolution="all",
            timestamp=20,
            content=manual_state,
        ),
        SimpleNamespace(
            name="drawings_undefined_1_A:SZ.001270_all",
            symbol="A:SZ.001270",
            resolution="all",
            timestamp=30,
            content=manual_state,
        ),
        SimpleNamespace(
            name="drawings_undefined_1_A:SZ.000001_all",
            symbol="A:SZ.000001",
            resolution="all",
            timestamp=40,
            content=json.dumps({"schema": SCHEMA, "sources": {}, "groups": {}}),
        ),
        SimpleNamespace(
            name="malformed-name",
            symbol="US:MSFT.US",
            resolution="all",
            timestamp=50,
            content=manual_state,
        ),
    ]
    monkeypatch.setattr(
        tv.db,
        "tv_chart_list",
        lambda chart_type, client_id, user_id: records,
    )

    response = app.test_client().get(URL + "&manifest=1&scope=all")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "data": {
            "complete": True,
            "entries": [
                {"name": "drawings_undefined_1_A:SZ.001270_all"},
                {"name": "drawings_default_default_US:AAPL.US_all"},
            ],
        },
    }
