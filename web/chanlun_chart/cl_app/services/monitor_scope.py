"""Cadence only: verified negative automatic 1m candidates use five-minute checks.

This module neither changes pool membership nor calculates/filters signals.
All clocks are existing market closed-bar timestamps supplied by the caller.
"""

MINUTE_BACKGROUND_SECONDS = 300
CHECKPOINTS = 'minute_cadence_checkpoints'


def _symbol(row):
    if isinstance(row, dict) and isinstance(row.get('code'), str) and row['code']:
        return f"{row.get('market', 'a')}:{row['code']}"
    return None


def build_minute_scope_index(state):
    """Index protections once per tick/projection; each decision is then O(1)."""
    seed = state.get('seed') or {}
    pool = set(seed.get('codes', []))
    automatic = {_symbol(row) for row in seed.get('screening_symbols', [])}
    automatic.update(seed.get('incremental_screening_symbols', {}))
    reasons = {}

    def protect(key, reason):
        if key in pool:
            reasons.setdefault(key, reason)

    def known_row(row):
        if isinstance(row, dict):
            return True
        # Public projections deliberately retain opaque private placeholders.
        # An unreadable protection source cannot justify slower verification.
        for key in pool:
            protect(key, 'protection_metadata_unverified')
        return False

    for key in state.get('holding_symbols', []):
        protect(key, 'holding')
    for row in seed.get('symbols', []):
        if not known_row(row):
            continue
        key = _symbol(row)
        if row.get('holding') is True:
            protect(key, 'holding')
        if row.get('origin') == 'watchlist':
            protect(key, 'watchlist')
        if key not in automatic:
            protect(key, 'independent_member')
    for key in pool - automatic:
        protect(key, 'automatic_membership_unproven')
    for plan in state.get('holding_risk_plans', {}).values():
        if known_row(plan) and plan.get('active') is True:
            protect(_symbol(plan), 'active_risk_plan')
    for row in state.get('unverified_signals', {}).values():
        # Another native period does not require an extra 1m confirmation.
        if known_row(row) and row.get('frequency') in (None, '1m'):
            protect(_symbol(row), 'unverified_signal')
    for key in seed.get('screening_rechecks', {}):
        protect(key, 'candidate_recovery_pending')
    for row in state.get('signals', {}).values():
        if known_row(row) and row.get('frequency') == '1m' and row.get('stage') not in {'left', 'invalidated'}:
            protect(_symbol(row), 'active_minute_signal')
    if state.get('dingtalk_enabled') is not False:
        # This is the existing retained notification journal, not page history.
        for event in (state.get('pending_notifications') or {}).values():
            if (known_row(event) and event.get('frequency') in (None, '1m')
                    and event.get('external_notifications') is not False
                    and event.get('notification_authorized') is not False
                    and event.get('stage') not in {'left', 'invalidated'}):
                protect(_symbol(event), 'pending_minute_notification')
    return {'pool': pool, 'automatic': automatic, 'protected': reasons,
            'seed_run_id': seed.get('run_id')}


def _successful_check(state, key, revision):
    check = state.get('selection_checks', {}).get(f'{key}:1m', {})
    if not isinstance(check, dict):
        return {}, False
    valid = (isinstance(revision, str) and bool(revision)
             and check.get('revision') == revision and check.get('error') is False
             and type(check.get('cutoff')) is int and isinstance(check.get('identities'), list)
             and isinstance(check.get('input_fingerprint'), str) and bool(check['input_fingerprint']))
    return check, valid


def minute_cadence(state, key, revision, closed_at, *, manual_override=False,
                   notification_recheck=False, scope_index=None):
    """Return the 1m dispatch gate, preserving every unproved case as realtime.

    ``next_due_at`` is a closed-bar-clock threshold, not a predicted next market
    session or a wall-clock wakeup. A due background candidate stays classified
    as background; discovering a current buy *or* sell restores realtime.
    """
    index = build_minute_scope_index(state) if scope_index is None else scope_index
    check, successful = _successful_check(state, key, revision)
    cutoff = check.get('cutoff') if type(check.get('cutoff')) is int else None

    def realtime(reason):
        return {'classification': 'realtime', 'cold': False, 'eligible': True,
                'due': True, 'reason': reason, 'cadence_seconds': 60,
                'last_check_cutoff': cutoff, 'next_due_at': None,
                'clock_known': type(closed_at) is int}

    if manual_override:
        return realtime('explicit_check')
    if notification_recheck:
        return realtime('minute_notification_recheck')
    if key not in index['pool']:
        return realtime('membership_unproven')
    if key in index['protected']:
        return realtime(index['protected'][key])
    if check.get('error') is True:
        return realtime('verification_failed')
    if not revision or check.get('revision') != revision:
        return realtime('source_revision_unverified')
    if not successful:
        return realtime('input_unverified')
    if check['identities']:
        return realtime('active_minute_signal')
    checkpoint = state.get(CHECKPOINTS, {}).get(key, {})
    if not isinstance(checkpoint, dict):
        return realtime('checkpoint_unverified')
    if (not index['seed_run_id'] or checkpoint.get('seed_run_id') != index['seed_run_id']
            or checkpoint.get('revision') != revision
            or type(checkpoint.get('cutoff')) is not int):
        return realtime('new_candidate')
    admitted_at = state.get('notification_symbol_since', {}).get(key, 0)
    if admitted_at and (checkpoint.get('checked_at') or 0) < admitted_at:
        return realtime('new_candidate')
    if checkpoint['cutoff'] > cutoff:
        return realtime('checkpoint_unverified')
    if type(closed_at) is int and closed_at < cutoff:
        return realtime('closed_clock_reversed')
    next_due = checkpoint['cutoff'] + MINUTE_BACKGROUND_SECONDS
    due = type(closed_at) is not int or closed_at >= next_due
    return {'classification': 'background', 'cold': True, 'eligible': due,
            'due': due, 'reason': 'verified_negative',
            'cadence_seconds': MINUTE_BACKGROUND_SECONDS, 'last_check_cutoff': cutoff,
            'next_due_at': next_due, 'clock_known': type(closed_at) is int}


def record_minute_checkpoint(state, key, revision, closed_at):
    """Record only successful 1m verification, never an attempted/failed task."""
    check, successful = _successful_check(state, key, revision)
    seed = state.get('seed') or {}
    if (not successful or type(closed_at) is not int or check['cutoff'] != closed_at
            or not seed.get('run_id') or key not in seed.get('codes', [])):
        return False
    checkpoints = state.setdefault(CHECKPOINTS, {})
    previous = checkpoints.get(key, {})
    if not isinstance(previous, dict):
        previous = {}
    if (previous.get('seed_run_id') == seed['run_id'] and previous.get('revision') == revision
            and type(previous.get('cutoff')) is int and previous['cutoff'] > closed_at):
        return False
    checkpoints[key] = {'seed_run_id': seed['run_id'], 'revision': revision,
                        'cutoff': closed_at, 'checked_at': check.get('checked_at')}
    return True


def minute_scope_summary(state, revision, closed_at_by_symbol=None):
    """Compact projection without provider access or mutable signal payloads."""
    index = build_minute_scope_index(state)
    seed = state.get('seed') or {}
    enabled = '1m' in seed.get('settings', {}).get('frequencies', [])
    risk_keys = {_symbol(plan) for plan in state.get('holding_risk_plans', {}).values()
                 if isinstance(plan, dict) and plan.get('active') is True and plan.get('frequency') == '1m'}
    keys = index['pool'] if enabled else index['pool'] & risk_keys
    per_symbol = {}
    clocks = closed_at_by_symbol or {}
    for key in sorted(keys):
        # A projection may only know the last verified candle. Show the cadence
        # classification without inventing a future weekend/session wakeup.
        actual = clocks.get(key)
        check, _ = _successful_check(state, key, revision)
        reference = actual if type(actual) is int else check.get('cutoff')
        value = minute_cadence(state, key, revision, reference, scope_index=index)
        value['clock_known'] = type(actual) is int
        if not value['clock_known'] and value['cold']:
            # Unknown is not "not due": UI callers must not hide a genuinely
            # overdue verification by comparing against its own old cutoff.
            value.update(due=None, eligible=True)
        per_symbol[key] = value
    background = sum(value['cold'] for value in per_symbol.values())
    return {'frequency': '1m', 'foreground': len(per_symbol) - background,
            'background': background, 'interval_seconds': MINUTE_BACKGROUND_SECONDS,
            'total': len(per_symbol), 'per_symbol': per_symbol}
