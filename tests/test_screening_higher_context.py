from datetime import datetime, timezone
from types import SimpleNamespace
import json
import time

from chanlun.screening import higher_context as context


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc)


def _level(*, cutoff, trend=None, unit=None):
    return SimpleNamespace(trend_types=() if trend is None else (trend,),
                           units=() if unit is None else (unit,),
                           center_result=SimpleNamespace(centers=()))


def _trend(cutoff, *, direction="up", kind="trend", state="forming", lag=0):
    return SimpleNamespace(kind=kind, direction=direction, state=state, centers=(),
                           available_at=_stamp(cutoff-lag), market_end=_stamp(cutoff-lag))


def _unit(cutoff, *, direction="up", forming=True, locked=False, lag=0):
    return SimpleNamespace(direction=direction, forming=forming, locked=locked,
                           available_at=_stamp(cutoff-lag), market_end=_stamp(cutoff-lag))


def test_historical_uptrend_cannot_be_presented_as_current_direction():
    cutoff = 1_790_060_400
    result = context.classify_level(_level(cutoff=cutoff,
                                           trend=_trend(cutoff, lag=1800),
                                           unit=_unit(cutoff, lag=1800)), cutoff)
    assert result["category"] == "unknown"
    assert result["evidence_available_at"] == cutoff-1800


def test_current_trend_and_open_segment_keep_distinct_proofs():
    cutoff = 1_790_060_400
    formal = context.classify_level(_level(cutoff=cutoff, trend=_trend(cutoff),
                                           unit=_unit(cutoff)), cutoff)
    early = context.classify_level(_level(cutoff=cutoff,
                                          trend=_trend(cutoff, kind="consolidation"),
                                          unit=_unit(cutoff)), cutoff)
    ended = context.classify_level(_level(cutoff=cutoff, unit=_unit(cutoff, forming=False, locked=True)), cutoff)
    assert formal["category"] == "up_trend_forming"
    assert early["category"] == "up_segment"
    assert early["trend"]["kind"] == "consolidation"
    assert ended["category"] == "unknown"


def test_30m_cutoff_must_equal_5m_frozen_cutoff(monkeypatch):
    cutoff = 1_790_060_400
    monkeypatch.setattr(context, "trading_context", lambda *_: {"cutoff": cutoff-1800})
    monkeypatch.setattr(context, "fetch_frame", lambda *_: (_ for _ in ()).throw(AssertionError("must not fetch")))
    result = context.assess_symbol({"market": "a", "code": "SH.600000", "source_closed_at": cutoff})
    assert result["category"] == "unknown"
    assert result["reason"] == "30M_CUTOFF_NOT_ALIGNED"


def test_incomplete_30m_bars_never_publish_a_direction(monkeypatch):
    cutoff = 1_790_060_400
    monkeypatch.setattr(context, "trading_context", lambda *_: {"cutoff": cutoff})
    monkeypatch.setattr(context, "fetch_frame", lambda *_: object())
    monkeypatch.setattr(context, "frame_quality", lambda *_: {"status": "unknown", "errors": ["DATA_GAPS"]})
    monkeypatch.setattr(context, "build_strict_chart_cd", lambda **_: (_ for _ in ()).throw(AssertionError("no structure on gaps")))
    result = context.assess_symbol({"market": "a", "code": "SH.600000", "source_closed_at": cutoff})
    assert result["category"] == "unknown"
    assert result["reason"] == "DATA_GAPS"


def test_background_pass_yields_to_active_live_monitor(tmp_path):
    run_id = "a" * 32
    directory = tmp_path / run_id
    directory.mkdir()
    (tmp_path / "latest.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    status = {"status": "running", "updated_at": time.time()}
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert context._monitor_active(tmp_path)
    status["status"] = "completed"
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert not context._monitor_active(tmp_path)
