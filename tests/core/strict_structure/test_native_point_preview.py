"""Previews preserve actual unlocked structure and the existing formal rules."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict, _unit_audit_payload, _with_prices
from decimal import Decimal
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.native_center_signals import build_native_center_signals
from chanlun.core.strict_structure.native_point_preview import build_point_previews, preview_source_valid, is_point_preview
from tests.core.strict_structure.helpers import unit
from chanlun.core.strict_structure.strength import StrengthSnapshot


def trend_fixture(kind='1',mirror=False):
    prices=[100,80,95,85,90,70,75,60,68,62,66,45,50,30,38,32]
    if kind=='1':
        prices=prices[:14]
    if mirror:
        prices=[200-price for price in prices]
    values=tuple(unit(i,'up' if b>a else 'down',a,b,locked=i<len(prices)-2)
                 for i,(a,b) in enumerate(zip(prices,prices[1:])))
    centers=calculate_centers(values,0,SourceKind.SEGMENT).centers
    class Strength:
        def snapshot(self,segment):
            size=5 if segment.market_start>=values[10].market_start else 100
            sign=1 if segment.direction=='up' else -1
            return StrengthSnapshot(segment.unit_id,segment.direction,size,size*sign,size*sign,'macd',segment.available_at)
    tail=values[-1]
    bars=[SimpleNamespace(date=tail.market_end+timedelta(minutes=i),
                         low=tail.end_tick+(0 if i==0 else -1 if mirror else 1)-.1,
                         high=tail.end_tick+(0 if i==0 else -1 if mirror else 1)+.1)
          for i in [-1,0,1,2,3,4,5]]
    hist=[(8-i)*(1 if mirror else -1) for i in range(len(bars))]
    return values,centers,bars,hist,Strength()


@pytest.mark.parametrize('kind',['1','2'])
@pytest.mark.parametrize('mirror',[False,True])
def test_first_and_second_previews_keep_required_trend_and_parent_evidence(kind,mirror):
    units,centers,bars,hist,strength=trend_fixture(kind,mirror)
    output=build_point_previews(units,centers,strength,bars,hist,as_of=bars[-1].date,serialize_center=strict_center_to_chart_dict)
    target=kind+('sell' if mirror else 'buy')
    point,=[item for item in output if item['point_type']==target]
    assert is_point_preview(point)
    if kind=='1':
        assert point['preview_witness']['macd_area_c']*2<point['preview_witness']['macd_area_a']
    else:
        actual=build_native_center_signals(units,centers,strength,as_of=bars[-1].date)
        assert point['parent_point_id'] in {item['point_id'] for item in actual if item['status']=='confirmed'}
    actual=build_native_center_signals(units,centers,strength,as_of=bars[-1].date)
    serialized=[strict_center_to_chart_dict(center) for center in centers]
    snapshot={'source_closed_at':int(bars[-1].date.timestamp()),'price_basis_revision':units[-1].price_basis_revision,
              'structure_price_quantum':'1','levels':[{'centers':serialized,'points':[*actual,point]}],
              'screening_segments':[{**_unit_audit_payload(unit),'source_kind':'segment','structural_level':0,
                  'price_basis_revision':unit.price_basis_revision,'forming':unit.forming} for unit in units]}
    assert preview_source_valid(snapshot,point,{center['center_id']:center for center in serialized},snapshot['screening_segments'])


def third_fixture(mirror=False):
    prices=[90,120,105,118,110,125,121]
    if mirror:
        prices=[300-p for p in prices]
    values=tuple(unit(i,'up' if b>a else 'down',a,b,locked=i<5) for i,(a,b) in enumerate(zip(prices,prices[1:])))
    centers=calculate_centers(values,0,SourceKind.SEGMENT).centers
    tail=values[-1]
    bars=[]
    for i in [-1,0,1,2,3,4,5]:
        price=tail.end_tick+(0 if i==0 else 1)
        if mirror:
            price=tail.end_tick-(0 if i==0 else 1)
        bars.append(SimpleNamespace(date=tail.market_end+timedelta(minutes=i),low=price-.1,high=price+.1))
    return values,centers,bars,[1.]*len(bars)


@pytest.mark.parametrize('mirror',[False,True])
def test_third_point_can_warn_before_return_locks_without_confirming_any_structure(mirror):
    units,centers,bars,hist=third_fixture(mirror)
    original=deepcopy((units,centers))
    point,=build_point_previews(units,centers,None,bars,hist,as_of=bars[-1].date,serialize_center=strict_center_to_chart_dict)
    assert point['point_type']==('3sell' if mirror else '3buy')
    assert is_point_preview(point) and point['confirmed_at'] is None and point['tradable'] is False
    assert (units,centers)==original and units[-1].locked is False and centers[-1].third_class_confirmed is False
    assert not build_native_center_signals(units,centers,None,as_of=bars[-1].date)
    locked=(*units[:-1],replace(units[-1],locked=True,confirmed_at=bars[-1].date,available_at=bars[-1].date))
    confirmed=build_native_center_signals(locked,calculate_centers(locked,0,SourceKind.SEGMENT).centers,None,as_of=bars[-1].date)
    assert len(confirmed)==1 and confirmed[0]['preview_key']==point['preview_key']


@pytest.mark.parametrize('damage',['no_turn','new_extreme','tail_locked','nonfinite'])
def test_preview_requires_a_real_unfinished_tail_and_closed_local_turn(damage):
    units,centers,bars,hist=third_fixture()
    if damage=='no_turn':
        bars[0].low=bars[1].low-1
    elif damage=='new_extreme':
        bars[-1].low=bars[1].low-1
    elif damage=='tail_locked':
        units=(*units[:-1],replace(units[-1],locked=True,confirmed_at=units[-1].available_at))
    else:
        hist[-1]=float('nan')
    assert build_point_previews(units,centers,None,bars,hist,as_of=bars[-1].date,serialize_center=strict_center_to_chart_dict)==[]


def test_source_verification_rejects_changed_price_center_and_fabricated_confirmation():
    units,centers,bars,hist=third_fixture()
    point,=build_point_previews(units,centers,None,bars,hist,as_of=bars[-1].date,serialize_center=strict_center_to_chart_dict)
    point=_with_prices(point,Decimal('1'))
    serialized=[_with_prices(strict_center_to_chart_dict(center),Decimal('1')) for center in centers]
    snapshot={'source_closed_at':int(bars[-1].date.timestamp()),'price_basis_revision':units[-1].price_basis_revision,
              'structure_price_quantum':'1','levels':[{'centers':serialized,'points':[point]}],
              'screening_segments':[{**_unit_audit_payload(unit),'source_kind':'segment','structural_level':0,
                  'price_basis_revision':unit.price_basis_revision,'forming':unit.forming} for unit in units]}
    centers_by_id={center['center_id']:center for center in serialized}
    assert preview_source_valid(snapshot,point,centers_by_id,snapshot['screening_segments'])
    for field,value in [('anchor_tick',999),('confirmed_at',snapshot['source_closed_at']),('preview_key','wrong')]:
        broken={**point,field:value}
        assert not preview_source_valid(snapshot,broken,centers_by_id,snapshot['screening_segments'])


@pytest.mark.parametrize('forming,area,expected',[(True,49,True),(True,50,False),(False,60,True)])
def test_first_point_area_margin_distinguishes_a_forming_leg_from_a_formed_unlocked_leg(forming,area,expected):
    units,centers,bars,hist,strength=trend_fixture()
    units=(*units[:-1],replace(units[-1],forming=forming))
    class Area:
        def snapshot(self,segment):
            sample=strength.snapshot(segment)
            return replace(sample,histogram_area=area) if segment.market_start>=units[10].market_start else sample
    output=build_point_previews(units,centers,Area(),bars,hist,as_of=bars[-1].date,serialize_center=strict_center_to_chart_dict)
    assert any(point['point_type']=='1buy' for point in output) is expected
