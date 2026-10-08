import json
from copy import deepcopy
import pytest

from cl_app.services.monitor_view import paged_state


def sample(count=125):
    symbols = [
        {"market": "a", "code": f"SH.{i:06d}", "name": f"stock {i}"}
        for i in range(count)
    ]
    signals = {
        str(i): {
            **row,
            "id": str(i),
            "stage": "confirmed",
            "point_type": "3buy",
            "frequency": "5m",
            "available_at": i,
            "evidence": {"large": "x" * 5000},
            "exit_plan": {"unused": "x" * 5000},
        }
        for i, row in enumerate(symbols)
    }
    return {
        "enabled": True,
        "signals": signals,
        "events": list(signals.values()),
        "known": signals,
        "seed": {
            "symbols": symbols,
            "codes": [r["code"] for r in symbols],
            "screening_symbols": symbols,
            "settings": {"frequencies": ["5m"]},
        },
        "selection_checks": {
            f"a:{r['code']}:5m": {"revision": "current", "error": i == 0}
            for i, r in enumerate(symbols)
        },
    }


def test_remote_pagination_searches_all_rows_and_keeps_full_counts_without_evidence():
    state = sample()
    original = deepcopy(state)
    result = paged_state(
        state,
        {"signals_page": "2", "events_size": "25", "pool_q": "stock 124"},
        "current",
        True,
    )
    assert len(result["signals"]) == 50 and len(result["events"]) == 25
    assert (
        result["pagination"]["signals"]["overall"] == 125
        and result["pagination"]["pool"]["total"] == 1
    )
    assert result["seed"]["symbols"][0]["code"] == "SH.000124"
    assert result["coverage"] == {
        "total": 125,
        "ready": 124,
        "limited": 1,
        "pending": 0,
    }
    assert "evidence" not in json.dumps(result) and "selection_checks" not in result
    assert len(json.dumps(result)) < 45000 and state == original


def test_page_clamps_after_exits_and_hides_old_revision_signals():
    state = sample(2)
    state["selection_checks"]["a:SH.000001:5m"]["revision"] = "old"
    result = paged_state(state, {"signals_page": 999}, "current", True)
    assert result["pagination"]["signals"]["page"] == 1 and len(result["signals"]) == 1
    assert result["coverage"]["pending"] == 1


@pytest.mark.parametrize(
    "options", [{"signals_size": "1000"}, {"pool_page": "x"}, {"events_q": "x" * 101}]
)
def test_invalid_page_arguments_are_rejected(options):
    with pytest.raises(ValueError):
        paged_state(sample(), options, "current", True)


def test_authenticated_endpoint_uses_bounded_projection_and_rejects_invalid_size(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock
    from cl_app import create_app
    from cl_app.services.signal_monitor import SignalMonitor

    manual, live = Mock(), Mock()
    for job in (manual, live):
        job.status.return_value = {
            "status": "completed",
            "source_current": True,
            "source_revision": "current",
        }
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service = SignalMonitor(
        tmp_path, screening=manual, monitor=live, notifications=notifications
    )
    service._load().update(sample())
    monkeypatch.setattr("cl_app.blueprints.monitor.service", service)
    app = create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "LOGIN_DISABLED": True,
        }
    )
    client = app.test_client()
    response = client.get("/monitor/status?view=paged&signals_size=25&signals_page=2")
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["signals"]) == 25 and body["pagination"]["signals"]["total"] == 125
    assert len(response.data) < 55000 and "selection_checks" not in body
    unchanged=client.get('/monitor/status?view=paged&signals_size=25&signals_page=2',headers={'If-None-Match':response.headers['ETag']})
    assert unchanged.status_code==304 and unchanged.data==b''
    assert client.get("/monitor/status?view=paged&signals_size=5000").status_code == 400
