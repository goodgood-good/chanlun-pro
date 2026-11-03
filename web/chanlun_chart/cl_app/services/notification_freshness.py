"""Notification lifetime follows market evidence, never a later recalculation.

The existing thirty-minute delivery allowance is a wall-clock budget, including
market breaks. A current selection result can remain visible after that budget;
it cannot become a new real-time alert merely because a worker found it later.
"""

import math


NOTIFICATION_LIFETIME = 1800


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _clocks(event):
    values = [event.get(key) for key in ('recorded_at', 'source_closed_at', 'available_at')]
    if event.get('point_status', event.get('stage')) == 'confirmed' and event.get('point_confirmed_at') is not None:
        from chanlun.screening.timeframe_policy import POLICY
        trade = event.get('selection_available_at')
        main = event['point_confirmed_at']
        # The first fully confirmed 5m trade is a different event from its
        # earlier structural confirmation. Its frozen selection clock already
        # includes the corresponding 1m proof; discovery/recalculation times
        # must never rejuvenate that event.
        two_stage = (event.get('timeframe_policy') == POLICY and event.get('frequency') == '5m'
                     and event.get('operating_role') == 'trade'
                     and event.get('stage') == event.get('selection_status') == 'confirmed'
                     and event.get('lower_confirmation_state') == 'confirmed')
        if two_stage:
            values.append(trade if _timestamp(trade) and _timestamp(main)
                          and trade >= main and event.get('available_at') == trade else None)
        else:
            values.append(main)
    return values


def notification_deadline(event):
    """Keep the earliest evidence deadline, including a previously frozen one."""
    values = _clocks(event)
    if not all(_timestamp(value) for value in values):
        return 0
    deadline = min(values) + NOTIFICATION_LIFETIME
    if 'notification_expires_at' in event:
        frozen = event['notification_expires_at']
        if not _timestamp(frozen):
            return 0
        deadline = min(deadline, frozen)
    return deadline


def notification_is_live(event, stamp):
    return (_timestamp(stamp) and notification_deadline(event) > stamp
            and all(value <= stamp for value in _clocks(event)))


def notification_rejection(event, stamp):
    if not _timestamp(stamp) or not all(_timestamp(value) for value in _clocks(event)):
        return 'INVALID_MARKET_CLOCK'
    if any(value>stamp for value in _clocks(event)):
        return 'FUTURE_MARKET_CLOCK'
    return 'MARKET_EVENT_EXPIRED' if notification_deadline(event)<=stamp else None


def stamp_notification(event, stamp):
    """Persist an honest expiry/suppression reason without hiding the result."""
    event['notification_expires_at'] = notification_deadline(event)
    if not notification_is_live(event, stamp):
        event['external_notifications'] = False
        event.setdefault('notification_suppressed_reason', notification_rejection(event,stamp))
    return event
