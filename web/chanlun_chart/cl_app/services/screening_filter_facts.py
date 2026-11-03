"""Explicit clocks and unrounded distance for presentation-only filtering."""
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from zoneinfo import ZoneInfo

from chanlun.screening.rules import CN, trading_context
from chanlun.screening.markets import market_context


@lru_cache(maxsize=512)
def _sessions(market, code, frequency, cutoff):
    observed = datetime.fromtimestamp(cutoff, CN)
    context = (trading_context(observed, frequency, 1, 1) if market == 'a' else
               market_context(market, code, observed, frequency, 1, 1))
    return tuple(context['trading_days']), ZoneInfo(context.get('session_timezone', 'Asia/Shanghai'))


def _age(stamp, cutoff, days, zone):
    if type(stamp) is not int or type(cutoff) is not int or stamp > cutoff or not days:
        return None
    day = datetime.fromtimestamp(stamp, zone).date().isoformat()
    position = bisect_left(days, day)
    if position == len(days) or days[position] != day:
        return None
    return len(days) - position - 1


def filter_facts(item):
    point = item['point']
    confirmed = point.get('status') == 'confirmed'
    historical = item.get('historical_only') is True
    frequency = item['frequency']
    trade_ready = (confirmed and not historical and (
        frequency == '1m' or frequency == '5m'
        and item.get('selection_status') == 'confirmed'
        and (item.get('nested_confirmation') or {}).get('state') == 'confirmed'))
    times = {'structure': point.get('available_at') if confirmed else None,
             'trade': item.get('selection_available_at', point.get('available_at')) if trade_ready else None,
             'anchor': point.get('anchor_at')}
    cutoff = item.get('source_closed_at')
    days, zone, calendar_state = (), CN, 'unavailable'
    if type(cutoff) is int:
        try:
            market = item.get('market', 'a')
            # A-share calendars do not depend on the stock identity.
            days, zone = _sessions(market, '' if market == 'a' else item['code'], frequency, cutoff)
            calendar_state = 'verified'
        except (OSError, ValueError, KeyError, TypeError):
            pass
    ages = {key: _age(stamp, cutoff, days, zone) for key, stamp in times.items()}
    distance = None
    close = item.get('latest_price', item.get('latest_close'))
    try:
        anchor = Decimal(str(point.get('anchor_price')))
        price = Decimal(str(close))
        if anchor.is_finite() and price.is_finite() and anchor > 0 and price > 0:
            distance = float(abs(price / anchor - 1) * 100)
    except (InvalidOperation, ValueError, TypeError):
        pass
    return {'times': times, 'ages': ages, 'calendar_state': calendar_state,
            'absolute_distance_pct': distance, 'price_closed_at': cutoff}
