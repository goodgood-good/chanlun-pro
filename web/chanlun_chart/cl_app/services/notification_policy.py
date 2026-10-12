"""DingTalk point preferences, independent of signal computation and risk alerts."""

import math


FREQUENCIES = ('1m', '5m', '30m')
BUY_POINTS = ('1buy', '2buy', '3buy')
POINTS = (*BUY_POINTS, '1sell', '2sell', '3sell')
SCOPES = ('holdings', 'others')
SPECIAL_SIGNALS = {'holding_risk', 'consolidation_divergence'}


def default_point_notifications():
    return {'frequencies': list(FREQUENCIES), 'holdings': list(POINTS), 'others': list(BUY_POINTS)}


def normalize_point_notifications(payload):
    """Validate a complete replacement. Empty selections mean disabled."""
    allowed = {'frequencies': FREQUENCIES, 'holdings': POINTS, 'others': BUY_POINTS}
    if not isinstance(payload, dict) or set(payload) != set(allowed):
        raise ValueError('请完整提交通知周期、我的持仓及其他标的的买卖点条件')
    result = {}
    for field, values in allowed.items():
        selected = payload[field]
        if (not isinstance(selected, list) or any(not isinstance(value, str) or value not in values for value in selected)
                or len(selected) != len(set(selected))):
            raise ValueError('通知条件包含无效或重复的选项；非持仓只允许买点')
        result[field] = [value for value in values if value in selected]
    return result


def _enabled(policy, scope, frequency, point):
    return frequency in policy['frequencies'] and point in policy[scope]


def next_point_notification_since(previous, current, old_since, now):
    """Re-enabling any combination starts a new window, without replaying it."""
    previous = normalize_point_notifications(previous)
    current = normalize_point_notifications(current)
    result = dict(old_since)
    for scope in SCOPES:
        for frequency in FREQUENCIES:
            for point in POINTS:
                if _enabled(current, scope, frequency, point) and not _enabled(previous, scope, frequency, point):
                    result[f'{scope}:{frequency}:{point}'] = now
    return result


def point_notification_decision(state, event, fresh_holdings):
    """Return allow/deny/pending; a recorded holding hint cannot authorize a sell.

    Unknown membership only allows a buy when both possible scopes allow it.
    Special holding alerts retain their separate evidence/binding validation.
    """
    point = event.get('point_type')
    if event.get('signal_type') in SPECIAL_SIGNALS or point not in POINTS:
        return 'allow'
    try:
        policy = normalize_point_notifications(state.get('point_notifications', default_point_notifications()))
    except ValueError:
        return 'deny'
    frequency = event.get('frequency')
    if frequency not in policy['frequencies']:
        return 'deny'
    since = state.get('point_notification_since') or {}

    def permitted(scope):
        if not _enabled(policy, scope, frequency, point):
            return False
        started = since.get(f'{scope}:{frequency}:{point}', 0)
        recorded = event.get('recorded_at', 0)
        return (type(started) in (int, float) and math.isfinite(started)
                and type(recorded) in (int, float) and math.isfinite(recorded) and recorded >= started)

    if fresh_holdings is not None:
        scope = 'holdings' if f"{event.get('market', 'a')}:{event.get('code')}" in fresh_holdings else 'others'
        return 'allow' if permitted(scope) else 'deny'
    possible = [permitted(scope) for scope in SCOPES]
    if all(possible):
        return 'allow'
    return 'pending' if any(possible) else 'deny'
