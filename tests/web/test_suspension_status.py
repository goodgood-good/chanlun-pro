from datetime import datetime
from zoneinfo import ZoneInfo
import json
import pytest
from chanlun.screening import suspension_status as status
from chanlun.screening.sessions import _digest, SCHEMA
from chanlun.screening.fund_suspensions import SOURCE_URL
from cl_app.services.signal_monitor import SignalMonitor, _monitor_periods

CN = ZoneInfo("Asia/Shanghai")
CODE = "SH.513100"
KEY = "a:" + CODE


def at(value):
    return datetime.fromisoformat("2026-10-28T" + value).replace(tzinfo=CN)


def save(root, fetched):
    data = {
        "schema": SCHEMA,
        "source_url": SOURCE_URL,
        "symbol": CODE,
        "query_from": "2026-10-28",
        "query_through": "2026-10-28",
        "fetched_at": int(fetched.timestamp()),
        "records": [
            {
                "secCode": "513100",
                "dateSource": "2",
                "startStopType": "LSTP",
                "startStopDate": "20261028",
                "endStopDate": "20261028",
                "stopTime": "915",
            }
        ],
    }
    p = root / "market_status" / f"{CODE}_2026-10-28.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**data, "sha256": _digest(data)}), encoding="utf-8")
    return p


def test_active_halt_and_first_post_resume_closed_bar(tmp_path):
    save(tmp_path, at("09:49:50"))
    fact = status.fund_halt(
        CODE,
        at("09:50:00"),
        cutoff=int(at("09:50:00").timestamp()),
        root=tmp_path,
        refresh=False,
    )
    assert fact["state"] == "suspended" and fact["until"] == at("10:30:00").timestamp()
    save(tmp_path, at("10:30:50"))
    assert (
        status.fund_halt(
            CODE,
            at("10:31:00"),
            cutoff=int(at("10:31:00").timestamp()),
            root=tmp_path,
            refresh=False,
        )
        is None
    )
    assert (
        status.fund_halt(
            CODE,
            at("10:31:00"),
            cutoff=int(at("10:30:00").timestamp()),
            root=tmp_path,
            refresh=False,
        )["state"]
        == "awaiting_first_closed_bar"
    )
    save(tmp_path, at("10:34:50"))
    assert (
        status.fund_halt(
            CODE,
            at("10:35:00"),
            cutoff=int(at("10:35:00").timestamp()),
            root=tmp_path,
            refresh=False,
        )
        is None
    )


@pytest.mark.parametrize("change", ["hash", "old", "future"])
def test_unknown_or_stale_status_never_excuses_missing_prices(tmp_path, change):
    fetched = (
        at("09:49:50")
        if change == "hash"
        else at("09:40:00")
        if change == "old"
        else at("09:51:00")
    )
    p = save(tmp_path, fetched)
    if change == "hash":
        p.write_text(p.read_text().replace("915", "WH"), encoding="utf-8")
    assert status.fund_halt(CODE, at("09:50:00"), root=tmp_path, refresh=False) is None


def test_scheduler_pauses_halted_periods_without_claiming_current_price(
    tmp_path, monkeypatch
):
    save(tmp_path, at("09:49:50"))
    monkeypatch.setattr(status, "_root", lambda root=None: tmp_path)
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff",
        lambda *a: int(at("09:50:00").timestamp()),
    )
    from chanlun.screening.timeframe_policy import POLICY

    state = {
        "seed": {
            "symbols": [{"market": "a", "code": CODE, "name": "纳指ETF国泰"}],
            "settings": {
                "frequencies": ["1m", "5m", "30m"],
                "timeframe_policy": POLICY,
            },
        },
        "holding_source_current": True,
        "holding_symbols": [KEY],
        "errors": [
            {
                "market": "a",
                "code": CODE,
                "frequency": "1m",
                "reasons": ["STALE_DATA", "NO_VOLUME"],
            }
        ],
        "selection_checks": {KEY + ":1m": {"cutoff": 123, "error": True}},
        "retry_symbols": {KEY:{'periods':['1m'],'failures':3,'cutoff':123}},
    }
    service = object.__new__(SignalMonitor)
    service._save = lambda: None
    service._refresh_suspension_status(state, at("09:50:00"))
    assert not _monitor_periods(state, KEY) and not state["errors"]
    assert not state['retry_symbols'],'an official halt must not exhaust the data failure budget'
    assert state["selection_checks"][KEY + ":1m"]["cutoff"] == 123, (
        "a halt must not rejuvenate yesterday prices"
    )
