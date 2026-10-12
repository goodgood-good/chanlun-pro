"""Warn on the first closed contraction, including a still-extending price leg."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict, _unit_audit_payload
from chanlun.core.strict_structure.native_point_preview import build_point_previews, preview_source_valid
from chanlun.core.strict_structure.native_center_signals import build_native_center_signals
from chanlun.screening.rules import native_consolidation_source_valid
from tests.core.strict_structure.test_native_point_preview import trend_fixture, third_fixture
from tests.core.strict_structure.test_native_consolidation_preview import sample, build, snapshot


def extending_point(kind, mirror):
    if kind == '3':
        units, centers, _, _ = third_fixture(mirror)
        strength = None
    else:
        units, centers, _, _, strength = trend_fixture(kind, mirror)
    tail = replace(units[-1], forming=True, available_at=units[-1].market_end)
    units = (*units[:-1], tail)
    sign = 1 if tail.direction == 'up' else -1
    bars = []
    for i in range(-6,1):
        extreme = tail.end_tick + sign*i
        bars.append(SimpleNamespace(date=tail.market_end+timedelta(minutes=i),
            low=extreme if sign<0 else extreme-.2, high=extreme if sign>0 else extreme+.2))
    hist = [sign*v for v in [1,2,3,4,5,6,5]]
    return units, centers, bars, hist, strength


def point_snapshot(parts, point):
    units, centers, bars, _, strength = parts
    actual = build_native_center_signals(units,centers,strength,as_of=bars[-1].date)
    serialized = [strict_center_to_chart_dict(center) for center in centers]
    return {'source_closed_at':int(bars[-1].date.timestamp()),
        'price_basis_revision':units[-1].price_basis_revision,'structure_price_quantum':'1',
        'levels':[{'centers':serialized,'points':[*actual,point]}],
        'screening_segments':[{**_unit_audit_payload(u),'source_kind':'segment','structural_level':0,
                              'price_basis_revision':u.price_basis_revision,'forming':u.forming} for u in units]}


def points(parts):
    units,centers,bars,hist,strength = parts
    return build_point_previews(units,centers,strength,bars,hist,as_of=bars[-1].date,
                                serialize_center=strict_center_to_chart_dict)


@pytest.mark.parametrize('kind',['1','2','3'])
@pytest.mark.parametrize('mirror',[False,True])
def test_all_point_classes_can_warn_at_the_current_extreme_without_a_price_reversal(kind,mirror):
    parts = extending_point(kind,mirror)
    before = deepcopy(parts[:2])
    point, = [p for p in points(parts) if p['point_type'].startswith(kind)]
    assert point['status']=='approaching' and point['confirmed_at'] is None
    assert point['available_at']==point['anchor_at']==int(parts[2][-1].date.timestamp())
    assert point['preview_witness']['momentum_values']==[6,5]
    assert 'turn_times' not in point['preview_witness']
    assert 'macd_first_contraction' in point['evidence_codes']
    assert parts[:2] == before
    source = point_snapshot(parts,point)
    centers = {center['center_id']:center for center in source['levels'][0]['centers']}
    assert preview_source_valid(source,point,centers,source['screening_segments'])
    broken = deepcopy(point); broken['preview_witness']['momentum_values']=[5,6]
    assert not preview_source_valid(source,broken,centers,source['screening_segments'])
    if kind=='1':
        assert point['preview_witness']['estimate_factor']==2
    if kind=='2':
        assert point['parent_point_id'] in {p['point_id'] for p in source['levels'][0]['points'] if p['status']=='confirmed'}


@pytest.mark.parametrize('kind',['1','2','3'])
@pytest.mark.parametrize('mirror',[False,True])
def test_a_growing_or_flat_macd_column_alone_never_becomes_an_early_point(kind,mirror):
    units,centers,bars,hist,strength = extending_point(kind,mirror)
    hist[-1] = hist[-2]
    assert not points((units,centers,bars,hist,strength))


@pytest.mark.parametrize('area,expected',[(49,True),(50,False),(51,False)])
def test_doubled_area_is_compared_before_price_has_turned(area,expected):
    units,centers,bars,hist,strength = extending_point('1',False)
    class Areas:
        def snapshot(self,segment):
            value = strength.snapshot(segment)
            return replace(value,histogram_area=area) if segment.market_start>=units[10].market_start else value
    assert bool(points((units,centers,bars,hist,Areas()))) is expected


@pytest.mark.parametrize('down',[False,True])
def test_consolidation_warns_on_first_contraction_before_any_right_hand_price_bar(down):
    values,center,strength,frame,hist = sample(down)
    frame = frame.iloc[:36].copy()
    values = (*values[:-1],replace(values[-1],forming=True,available_at=frame.date.iloc[-1].to_pydatetime()))
    # MACD was still expanding on the prior close. Only this final column shrinks.
    sign = -1 if down else 1
    hist = [sign*5.]*30+[sign*v for v in [1,2,3,4,6,5]]
    parts = values,center,strength,frame,hist
    alerts = build(parts)
    assert alerts and all(a['anchor_at']==a['available_at'] for a in alerts)
    assert all(a['momentum_values']==[6,5] and a['estimated_area_c']==2*a['macd_area_c'] for a in alerts)
    source = snapshot(parts,alerts)
    assert all(native_consolidation_source_valid(source,a) for a in alerts)
    # A bullish/bearish price extreme at the last candle is not a completed turn.
    assert all('turn_times' not in a and a['confirmed_at'] is None for a in alerts)


def test_missing_local_turn_does_not_block_a_valid_area_estimate():
    values,center,strength,frame,hist = sample()
    frame.loc[36,'high'] = 125.
    assert build((values,center,strength,frame,hist))


def test_legacy_frozen_consolidation_witness_remains_readable():
    parts=sample(); alerts=build(parts); source=snapshot(parts,alerts)
    legacy=deepcopy(alerts[0])
    for key in ['preview_trigger','momentum_rule','momentum_latest','momentum_checked_at']:
        legacy.pop(key,None)
    dates=parts[3].date
    legacy.update(momentum_times=[int(t.timestamp()) for t in dates.iloc[34:37]],momentum_values=[3.5,3.,2.],
        turn_times=[int(t.timestamp()) for t in dates.iloc[34:37]],turn_prices=[124.,125.,123.])
    assert native_consolidation_source_valid(source,legacy)
