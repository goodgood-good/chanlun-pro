from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from chanlun.core.strict_structure.center_machine import advance_center, calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.native_consolidation_preview import build_native_consolidation_previews
from chanlun.core.strict_structure.native_consolidation import build_native_consolidation_alerts
from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.screening.rules import native_consolidation_source_valid, audit_native_consolidation_alerts
from tests.core.strict_structure.helpers import BASE, ongoing_center, unit
from tests.core.strict_structure.test_native_consolidation import Areas


def sample(down=False):
    center = ongoing_center()
    returned = unit(5, "down", 130, 108)
    center, _ = advance_center(center, returned)
    values = (*center.establishment_units, returned, unit(6, "up", 108, 125, locked=False))
    frame = pd.DataFrame([dict(date=BASE + timedelta(minutes=i), open=110., close=110.,
                              high=111., low=109., volume=100.) for i in range(41)])
    frame.loc[34:36, "high"] = [124., 125., 123.]
    frame.loc[34:36, "low"] = [120., 121., 119.]
    frame.loc[34:36, "open"] = [121., 122., 120.]
    frame.loc[34:36, "close"] = [122., 123., 121.]
    hist = [5.] * 30 + [3., 3.5, 4., 4., 3.5, 3., 2., 1., 0., 0., 0.]
    if down:
        values = tuple(replace(u, direction="down" if u.direction == "up" else "up",
                               start_tick=300-u.start_tick, end_tick=300-u.end_tick,
                               low_tick=300-u.high_tick, high_tick=300-u.low_tick) for u in values)
        center = calculate_centers(values[:-1], 0, SourceKind.SEGMENT).centers[0]
        frame["open"], frame["close"] = 300-frame.open, 300-frame.close
        frame["high"], frame["low"] = 300-frame.low.copy(), 300-frame.high.copy()
        hist = [-v for v in hist]
    strength = Areas({u.unit_id: 200. for u in values})
    return values, center, strength, frame, hist


def build(parts):
    values, center, strength, frame, hist = parts
    return build_native_consolidation_previews(values, (center,), strength, frame, hist,
                                               as_of=frame.date.iloc[-1].to_pydatetime())


def snapshot(parts, alerts):
    values, center, _, frame, _ = parts
    fields = ("unit_id", "direction", "structural_level", "price_basis_revision", "start_tick", "end_tick",
              "low_tick", "high_tick", "locked", "forming")
    serialized = [{**{key: getattr(u, key) for key in fields}, "source_kind": "segment",
                   "start_time": int(u.market_start.timestamp()), "end_time": int(u.market_end.timestamp()),
                   "confirmed_at": int(u.confirmed_at.timestamp()) if u.confirmed_at else None,
                   "available_at": int(u.available_at.timestamp())} for u in values]
    return {"analysis_scope": "native_centers", "source_frequency": "5m",
            "source_closed_at": int(frame.date.iloc[-1].timestamp()),
            "price_basis_revision": center.price_basis_revision, "structure_price_quantum": "1",
            "screening_segments": serialized, "native_consolidation_alerts": alerts,
            "levels": [{"centers": [strict_center_to_chart_dict(center)], "points": []}]}


@pytest.mark.parametrize("down", [False, True])
def test_unfinished_comparison_needs_momentum_and_turn_evidence_and_reaches_reader(down):
    parts = sample(down)
    alerts = build(parts)
    assert alerts and all(a["status"] == "warning" and a["confirmed_at"] is None for a in alerts)
    assert not parts[0][-1].locked
    source = snapshot(parts, alerts)
    assert all(native_consolidation_source_valid(source, a) for a in alerts)
    context = {"frequency": "5m", "cutoff": source["source_closed_at"],
               "recent_from": "2026-01-01", "anchor_from": "2026-01-01"}
    selected = audit_native_consolidation_alerts(source, parts[3], context)
    assert {c["alert"]["comparison_form"] for c in selected} == {"entry_departure", "center_oscillation"}


@pytest.mark.parametrize("fault", ["area", "momentum", "new_extreme", "no_turn", "locked"])
def test_small_partial_area_alone_cannot_trigger_and_later_strength_can_refute(fault):
    values, center, strength, frame, hist = sample()
    if fault == "area": strength = Areas({u.unit_id: 20. for u in values})
    elif fault == "momentum": hist[-1] = 7.
    elif fault == "new_extreme": frame.loc[40, "high"] = 126.
    elif fault == "no_turn": frame.loc[36, "high"] = 125.
    elif fault == "locked": values = (*values[:-1], replace(values[-1], locked=True, confirmed_at=values[-1].available_at))
    assert not build((values, center, strength, frame, hist))


def test_hypothesis_identity_survives_the_unfinished_endpoint_revision():
    parts = sample()
    first = build(parts)
    values, center, strength, frame, hist = parts
    values = (*values[:-1], replace(values[-1], unit_id="revised-live-endpoint"))
    second = build((values, center, strength, frame, hist))
    assert [a["hypothesis_id"] for a in first] == [a["hypothesis_id"] for a in second]
    assert first[0]["signal_leg_unit_ids"] != second[0]["signal_leg_unit_ids"]


def test_a_completed_tail_confirms_the_same_hypothesis_without_projecting_a_fake_lock():
    parts = sample(); previews = build(parts)
    values, center, _, frame, _ = parts
    tail = replace(values[-1], locked=True, confirmed_at=values[-1].available_at)
    completed, _ = advance_center(center, tail)
    values = (*values[:-1], tail)
    strength = Areas({u.unit_id: (20. if u is tail else 200.) for u in values})
    confirmed = build_native_consolidation_alerts(values, (completed,), strength, as_of=frame.date.iloc[-1])
    assert {p["hypothesis_id"] for p in previews} <= {c["hypothesis_id"] for c in confirmed}


@pytest.mark.parametrize('fault,reason',[('momentum','MOMENTUM_RECOVERED'),('price','PRICE_EXTREME_BROKEN'),('turn','LOCAL_TURN_CHANGED')])
def test_negative_checks_explain_the_current_failed_gate_without_altering_alert_rules(fault,reason):
    values,center,strength,frame,hist=sample()
    if fault=='momentum':hist[-1]=7.
    elif fault=='price':frame.loc[40,'high']=126.
    else:frame.loc[36,'high']=125.
    checks={}
    result=build_native_consolidation_previews(values,(center,),strength,frame,hist,as_of=frame.date.iloc[-1],diagnostics=checks)
    assert not result and checks['reason_code']==reason
    assert checks['source_closed_at']==int(frame.date.iloc[-1].timestamp())


@pytest.mark.parametrize("fault", ["future", "turn", "momentum", "basis", "source", "ratio", "confirmed"])
def test_frozen_preview_rejects_forged_or_future_evidence(fault):
    parts = sample(); alerts = build(parts); source = snapshot(parts, alerts)
    alert = deepcopy(alerts[0])
    if fault == "future": alert["momentum_times"][-1] = source["source_closed_at"] + 1
    elif fault == "turn": alert["turn_prices"][1] = min(alert["turn_prices"])
    elif fault == "momentum": alert["momentum_values"].reverse()
    elif fault == "basis": alert["price_basis_revision"] = "different"
    elif fault == "source": alert["source_unit_ids"].pop(0)
    elif fault == "ratio": alert["macd_area_ratio"] = 1.5
    elif fault == "confirmed": alert["confirmed_at"] = alert["available_at"]
    assert not native_consolidation_source_valid(source, alert)
