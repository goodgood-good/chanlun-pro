from datetime import datetime
from decimal import Decimal

from chanlun.screening.rules import CN
from cl_app.services.screening_filter_facts import filter_facts


def timestamp(day, hour=15, minute=0):
    return int(datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=CN).timestamp())


def candidate():
    return {'code': 'BJ.920719', 'market': 'a', 'frequency': '5m',
            'source_closed_at': timestamp('2026-10-13'), 'latest_price': 7.2,
            'selection_status': 'confirmed', 'selection_available_at': timestamp('2026-10-09', 10, 53),
            'nested_confirmation': {'state': 'confirmed'},
            'point': {'status': 'confirmed', 'available_at': timestamp('2026-09-30', 13, 15),
                      'anchor_at': timestamp('2026-09-28', 13, 25), 'anchor_price': 6.71}}


def test_structure_and_trade_clocks_count_actual_sessions_independently():
    facts = filter_facts(candidate())
    assert facts['calendar_state'] == 'verified'
    assert facts['ages']['structure'] == 4
    assert facts['ages']['trade'] == 2
    assert facts['times']['structure'] != facts['times']['trade']


def test_historical_first_retains_its_own_age_without_creating_a_current_trade():
    item = {**candidate(), 'historical_only': True, 'selection_status': 'historical'}
    facts = filter_facts(item)
    assert facts['ages']['structure'] == 4
    assert facts['times']['trade'] is None and facts['ages']['trade'] is None


def test_forming_point_never_uses_the_last_scan_as_its_confirmation_time():
    item = candidate()
    item['point'].update(status='approaching', available_at=item['source_closed_at'])
    facts = filter_facts(item)
    assert facts['times']['structure'] is None and facts['ages']['structure'] is None
    assert facts['times']['trade'] is None


def test_distance_uses_absolute_price_ratio_before_display_rounding():
    item = candidate()
    item['point']['anchor_price'] = 100
    item['latest_price'] = 96.59
    assert filter_facts(item)['absolute_distance_pct'] == 3.41
    item['latest_price'] = Decimal('101.004')
    assert filter_facts(item)['absolute_distance_pct'] > 1
    item['latest_price'] = 101
    assert filter_facts(item)['absolute_distance_pct'] == 1


def test_missing_price_or_unsupported_calendar_does_not_become_zero():
    item = candidate()
    item.update(market='fx', code='FE.CNYUSD', latest_price=None)
    facts = filter_facts(item)
    assert facts['calendar_state'] == 'unavailable'
    assert set(facts['ages'].values()) == {None}
    assert facts['absolute_distance_pct'] is None


def test_us_session_age_uses_its_own_calendar_and_clock():
    item = candidate()
    item.update(market='us', code='AAPL.US', source_closed_at=timestamp('2026-10-14', 4),
                selection_available_at=timestamp('2026-10-13', 23))
    item['point'].update(available_at=timestamp('2026-10-13', 23), anchor_at=timestamp('2026-10-13', 22))
    assert filter_facts(item)['ages']['structure'] == 0
