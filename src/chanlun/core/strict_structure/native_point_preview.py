"""Revisable native-period point previews, separate from confirmed points.

Local author text: D:/缠论/chanlun_lesson_corpus/L024_*:310-319 (provisional
MACD area), L020_*:427-454 (first return), L021_*:247-262 (second point),
L061_*:97-121 (hypothesis/refutation). The closed-bar local turn and the
conditional completion of one real unfinished segment are engineering gates.
They never make the actual segment, center or point confirmed.
"""
from copy import deepcopy
from dataclasses import replace
import math

from .center_machine import advance_center
from .identity import stable_structure_id
from .models import CenterState
from .native_center_signals import build_native_center_signals
from .strength import _comparison_leg, MacdStrengthUnavailable

PREVIEW_RULE = 'native_point_unconfirmed_v1'
PREVIEW_AUDIT = ('cold_rebuild', 'closed_bar_replay')


def preview_codes(point):
    names = {'completed_third_class_departure':'forming_third_class_departure',
             'native_macd_histogram_area_decay':'provisional_macd_area_margin',
             'first_complete_retest_held':'first_unfinished_retest_held',
             'first_complete_return_held_core':'first_unfinished_return_holds_core'}
    return [names.get(code,code) for code in point['evidence_codes']] + ['closed_local_turn','conditional_point_preview']


def is_point_preview(point):
    return (point.get('rule_basis') == PREVIEW_RULE and point.get('status') == 'approaching'
            and point.get('formal_point_issued') is False and point.get('confirmed_at') is None)


def preview_key(kind, center_id, basis, unit, related_ids, parent):
    return stable_structure_id('native-point-lifecycle-v1', basis, kind, center_id,
        int(unit.market_start.timestamp()), tuple(key for key in related_ids if key != unit.unit_id), parent)


def build_point_previews(units, centers, strength, bars, histogram, *, as_of, serialize_center):
    values, centers, bars = tuple(units), tuple(centers), tuple(bars)
    tail_index = next((i for i, unit in enumerate(values) if not unit.locked), None)
    if tail_index is None:
        return []
    tail = values[tail_index]
    if (tail.confirmed_at is not None or any(unit.available_at > as_of for unit in values)
            or any(unit.locked or not unit.forming for unit in values[tail_index+1:])):
        return []
    closed = [(bar, float(hist)) for bar, hist in zip(bars, histogram) if bar.date <= as_of]
    if len(histogram) != len(bars) or not closed or closed[-1][0].date != as_of:
        return []
    if any(not math.isfinite(hist) for _, hist in closed):
        return []
    pivot = next((i for i, (bar, _) in enumerate(closed) if bar.date == tail.market_end), None)
    if pivot is None or pivot < 1 or pivot + 1 >= len(closed):
        return []
    left, middle, right = [bar for bar, _ in closed[pivot-1:pivot+2]]
    up = tail.direction == 'up'
    price = lambda bar: getattr(bar, 'high' if up else 'low') if hasattr(bar, 'high') else getattr(bar, 'h' if up else 'l')
    if not (price(middle) > max(price(left), price(right)) if up else price(middle) < min(price(left), price(right))):
        return []
    if any((price(bar) > price(middle) if up else price(bar) < price(middle)) for bar, _ in closed[pivot+1:]):
        return []
    projected_tail = replace(tail, locked=True, forming=False, confirmed_at=as_of, available_at=as_of)
    projected_units = (*values[:tail_index], projected_tail, *values[tail_index+1:])
    projected_centers = []
    for center in centers:
        updated = center
        if center.state is CenterState.ONGOING:
            try:
                updated, _ = advance_center(center, projected_tail)
            except ValueError:
                pass  # This tail does not belong to that center's next role.
        projected_centers.append(updated)
    actual = build_native_center_signals(values, centers, strength, as_of=as_of)
    actual_ids = {point['point_id'] for point in actual}
    originals = {center.center_id: center for center in centers}
    projected = {center.center_id: center for center in projected_centers}
    by_id = {unit.unit_id: unit for unit in projected_units}
    output = []
    for point in build_native_center_signals(projected_units, tuple(projected_centers), strength, as_of=as_of):
        if point['anchor_unit_id'] != tail.unit_id or point['point_id'] in actual_ids:
            continue
        if point['point_type'].startswith('2') and point.get('parent_point_id') not in actual_ids:
            continue
        center = originals.get(point['center_id'])
        if center is None or point.get('view_id') != center.view_id:
            continue
        witness = {'turn_times':[int(bar.date.timestamp()) for bar in (left,middle,right)],
                   'turn_prices':[float(price(bar)) for bar in (left,middle,right)]}
        if point['point_type'].startswith('1'):
            sign = 1 if up else -1
            momentum = [max(0., sign*hist) for _,hist in closed]
            weakening = next((i for i in range(len(momentum)-1,max(1,pivot),-1)
                              if momentum[i-2]>momentum[i-1]>momentum[i]), None)
            if weakening is None or any(value>momentum[weakening] for value in momentum[weakening+1:]):
                continue
            try:
                divergence = point['divergence']
                a = _comparison_leg(tuple(by_id[key] for key in divergence['compare_leg_unit_ids']))
                c = _comparison_leg(tuple(by_id[key] for key in divergence['signal_leg_unit_ids']))
                area_a, area_c = (strength.snapshot(leg.measurement_unit).histogram_area for leg in (a,c))
                factor = 2 if tail.forming else 1
                if not area_a or not area_c or not 0 < factor*area_c < area_a:
                    continue
            except (KeyError, ValueError, TypeError, MacdStrengthUnavailable):
                continue
            witness.update(macd_area_a=area_a, macd_area_c=area_c, estimate_factor=factor,
                           momentum_values=momentum[weakening-2:weakening+1])
        hypothesis = point['preview_key']
        output.append({**point, 'point_id':stable_structure_id(PREVIEW_RULE, hypothesis),
            'render_kind':'point_approaching', 'status':'approaching', 'strict_status':'approaching',
            'rule_basis':PREVIEW_RULE, 'member_basis':'native_unlocked_segment',
            'formal_point_issued':False, 'tradable':False, 'confirmed_at':None,
            'available_at':int(as_of.timestamp()), 'missing_conditions':['unfinished_segment_lock'],
            'evidence_codes':preview_codes(point),
            'terminal_start_at':int(tail.market_start.timestamp()), 'terminal_end_at':int(tail.market_end.timestamp()),
            'preview_witness':witness,
            'preview_projection':{'point':point, 'center':serialize_center(projected[point['center_id']])}})
    return output


def preview_source_valid(snapshot, point, centers, units):
    """Check the conditional proof without changing the frozen real structure."""
    from chanlun.screening.rules import _native_center_signal_source_valid
    try:
        if not is_point_preview(point) or not units or snapshot.get('segment_construction',{}).get('status') == 'unresolved':
            return False
        index = next((i for i, unit in enumerate(units) if unit.get('locked') is False), None)
        if index is None:
            return False
        tail, cutoff = units[index], snapshot['source_closed_at']
        if (tail.get('locked') is not False or tail.get('confirmed_at') is not None
                or tail['unit_id'] != point['anchor_unit_id'] or tail['start_time'] != point['terminal_start_at']
                or tail['end_time'] != point['terminal_end_at'] or point['available_at'] != cutoff
                or any(unit.get('locked') is not True for unit in units[:index])
                or any(unit.get('locked') is not False or unit.get('forming') is not True for unit in units[index+1:])
                or point.get('missing_conditions') != ['unfinished_segment_lock']):
            return False
        proof = point['preview_projection']
        formal, owner = proof['point'], proof['center']
        original = centers[point['center_id']]
        immutable = ('center_id','price_basis_revision','core','entry_unit_id','core_unit_ids',
                     'establishment_leave_unit_id','view_id','view_available_at')
        if any(owner.get(key) != original.get(key) for key in immutable):
            return False
        changed = {'point_id','render_kind','status','strict_status','rule_basis','member_basis',
                   'formal_point_issued','tradable','confirmed_at','available_at','missing_conditions',
                   'terminal_start_at','terminal_end_at','preview_witness','preview_projection',
                   'render_id','points','anchor_price','invalidation_price','center_zd_price','center_zg_price','evidence_codes'}
        if any(point.get(key) != value for key,value in formal.items() if key not in changed):
            return False
        if point.get('evidence_codes') != preview_codes(formal):
            return False
        expected_key = stable_structure_id('native-point-lifecycle-v1', point['price_basis_revision'],
            point['point_type'],point['center_id'],tail['start_time'],
            tuple(key for key in point['related_unit_ids'] if key != tail['unit_id']),point.get('parent_point_id'))
        if point.get('preview_key') != expected_key or point['point_id'] != stable_structure_id(PREVIEW_RULE, expected_key):
            return False
        if formal.get('confirmed_at') != cutoff or formal.get('status') != 'confirmed' or formal.get('formal_point_issued') is not True:
            return False
        witness=point['preview_witness']
        times, prices=witness['turn_times'],witness['turn_prices']
        if (len(times)!=3 or len(prices)!=3 or not times[0]<times[1]<times[2]<=cutoff
                or times[1]!=tail['end_time'] or not all(math.isfinite(value) for value in prices)
                or not (prices[1]>max(prices[0],prices[2]) if tail['direction']=='up' else prices[1]<min(prices[0],prices[2]))):
            return False
        if point['point_type'].startswith('1'):
            a,c=witness['macd_area_a'],witness['macd_area_c']
            momentum=witness['momentum_values']
            factor = 2 if tail.get('forming') else 1
            if (witness.get('estimate_factor') != factor
                    or not all(math.isfinite(value) for value in (a,c,*momentum)) or not 0<factor*c<a
                    or len(momentum)!=3 or not momentum[0]>momentum[1]>momentum[2]>=0):
                return False
        virtual = deepcopy(snapshot)
        virtual_units = [*units[:index],{**tail,'locked':True,'forming':False,'confirmed_at':cutoff,'available_at':cutoff},*units[index+1:]]
        virtual_centers = {**centers,owner['center_id']:owner}
        virtual['screening_segments'] = virtual_units
        level = virtual['levels'][0]
        level['centers'] = list(virtual_centers.values())
        level['points'] = [p for p in level['points'] if p.get('status')=='confirmed'] + [formal]
        return _native_center_signal_source_valid(virtual, formal, virtual_centers, virtual_units)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return False
