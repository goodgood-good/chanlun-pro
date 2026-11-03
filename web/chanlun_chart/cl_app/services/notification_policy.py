"""DingTalk point preferences, independent of signal computation and risk alerts."""

import math


FREQUENCIES = ('1m', '5m', '30m')
BUY_POINTS = ('1buy', '2buy', '3buy')
POINTS = (*BUY_POINTS, '1sell', '2sell', '3sell')
SCOPES = ('holdings', 'others')
SPECIAL_SIGNALS = {'holding_risk', 'consolidation_divergence'}


def trade_notification_rejection(event, *, policy_enabled=False):
    """Share the application's confirmation gate across all delivery stages."""
    from chanlun.screening.timeframe_policy import POLICY
    if event.get('signal_type') in SPECIAL_SIGNALS or event.get('point_type') not in POINTS:
        return None
    if not policy_enabled and event.get('timeframe_policy') != POLICY:
        return None
    if event.get('frequency') == '30m':
        return 'TIMEFRAME_CONFIRMATION_REQUIRED'
    if event.get('frequency') == '5m' and event.get('point_status', event.get('stage')) == 'confirmed':
        if (event.get('timeframe_policy') != POLICY or event.get('stage') != 'confirmed'
                or event.get('selection_status') != 'confirmed'
                or event.get('lower_confirmation_state') != 'confirmed'):
            return 'TIMEFRAME_CONFIRMATION_REQUIRED'
    return None


def default_point_notifications():
    return {'frequencies': list(FREQUENCIES), 'other_frequencies': ['5m'],
            'holdings': list(POINTS), 'others': list(BUY_POINTS)}


def normalize_point_notifications(payload, *, legacy_ordinary_sells=False):
    """Validate a complete replacement. Empty selections mean disabled."""
    allowed = {'frequencies': FREQUENCIES, 'other_frequencies': ('5m',), 'holdings': POINTS,
               'others': POINTS if legacy_ordinary_sells else BUY_POINTS}
    if isinstance(payload, dict) and set(payload) == {'frequencies', 'holdings', 'others'}:
        # Old clients keep their selected types; their shared clock now maps to
        # the holding scope and its 5m intersection to ordinary symbols.
        payload = {**payload, 'other_frequencies': ['5m'] if isinstance(payload['frequencies'], list)
                   and '5m' in payload['frequencies'] else []}
    if not isinstance(payload, dict) or set(payload) != set(allowed):
        raise ValueError('请完整提交通知周期、我的持仓及其他标的的买卖点条件')
    result = {}
    for field, values in allowed.items():
        selected = payload[field]
        if (not isinstance(selected, list) or any(not isinstance(value, str) or value not in values for value in selected)
                or len(selected) != len(set(selected))):
            raise ValueError('通知条件包含无效或重复的选项；非持仓只允许 5m 买点通知')
        result[field] = [value for value in values if value in selected]
    return result


def _enabled(policy, scope, frequency, point):
    periods = policy['frequencies' if scope == 'holdings' else 'other_frequencies']
    return frequency in periods and point in policy[scope]


def next_point_notification_since(previous, current, old_since, now):
    """Re-enabling any combination starts a new window, without replaying it."""
    previous = normalize_point_notifications(previous, legacy_ordinary_sells=True)
    current = normalize_point_notifications(current)
    result = dict(old_since)
    for scope in SCOPES:
        for frequency in FREQUENCIES:
            for point in POINTS:
                if _enabled(current, scope, frequency, point) and not _enabled(previous, scope, frequency, point):
                    result[f'{scope}:{frequency}:{point}'] = now
    return result


def point_notification_decision(state, event, fresh_holdings):
    """Return allow/deny/pending using current membership and separate clocks.

    Unknown membership allows a point only when both possible scopes allow it.
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
    if frequency not in set(policy['frequencies']) | set(policy['other_frequencies']):
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
