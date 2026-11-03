"""Repair monitor bookkeeping without creating a trading conclusion."""


def prune_diagnostics(state):
    seed = state.get('seed')
    if not seed or 'codes' not in seed:
        return False
    pool = set(seed['codes'])
    changed = False
    waiting=state.get('source_waiting_periods',{})
    current={key:value for key,value in waiting.items() if key.rsplit(':',1)[0] in pool}
    if current!=waiting:
        state['source_waiting_periods']=current;changed=True
    previous=state.get('priority_slice_yields',{})
    current={key:value for key,value in previous.items() if key.rsplit(':',1)[0] in pool}
    if current!=previous:
        state['priority_slice_yields']=current;changed=True
    previous=state.get('live_priority_rescues',{})
    current={key:value for key,value in previous.items() if key in pool}
    if current!=previous:
        state['live_priority_rescues']=current;changed=True
    for field in ('errors', 'schedule_errors'):
        previous = state.get(field, [])
        current = [row for row in previous
                   if f"{row.get('market', 'a')}:{row.get('code')}" in pool]
        if current != previous:
            state[field] = current
            changed = True
    if (not state.get('errors') and state.get('last_error') == '部分标的行情或计算异常，请查看问题列表'):
        state['last_error'] = ''
        changed = True
    return changed


def recover_warning_rechecks(state, clear_retries):
    """Give legacy suspended warnings one check with the repaired lifecycle.

    This is a versioned, targeted request. It cannot reset a failing clock's
    retry budget on each poll or force a replay of the entire selection pool.
    """
    if state.get('warning_recovery_version', 0) >= 1:
        return False
    state['warning_recovery_version'] = 1
    pool = set((state.get('seed') or {}).get('codes', []))
    requested = False
    for item in state.get('unverified_signals', {}).values():
        key = f"{item.get('market', 'a')}:{item.get('code')}"
        if key not in pool or item.get('signal_type') not in {'native_point_preview', 'consolidation_divergence'}:
            continue
        frequency = item.get('frequency')
        if frequency not in {'1m', '5m', '30m'}:
            continue
        clear_retries(state, key, {frequency})
        periods = state.setdefault('deferred_monitor_periods', {}).setdefault(key, [])
        if frequency not in periods:
            periods.append(frequency)
        requested = True
    if requested:
        state['next_check_at'] = 0
    return True


def basis_change_exit(previous, assessment, run_id, stamp):
    """End tracking an obsolete price basis; preserve its original evidence.

    Only a verified lifecycle assessment can take this path. This records a
    data revision, never a market invalidation, and never authorizes delivery.
    """
    if assessment.get('state') != 'pending' or assessment.get('reason_code') != 'PRICE_BASIS_CHANGED':
        return None
    return {**previous, 'stage': 'left', 'change': 'left',
            'exit_reasons': ['PRICE_BASIS_CHANGED'], 'warning_assessment': assessment,
            'recorded_at': stamp, 'state_changed_at': stamp, 'run_id': run_id,
            'external_notifications': False, 'notification_suppressed_reason': 'PRICE_BASIS_CHANGED'}


def notification_priority_periods(state):
    """Order same-deadline work by currently enabled signals; never skip work."""
    from .notification_policy import point_notification_decision, trade_notification_rejection
    from chanlun.screening.timeframe_policy import enabled
    if state.get('dingtalk_enabled') is False:
        return []
    holdings = set(state.get('holding_symbols', [])) if state.get('holding_source_current') is True else None
    return sorted({f"{item.get('market', 'a')}:{item['code']}:{item['frequency']}"
                   for item in state.get('signals', {}).values()
                   if item.get('stage') in {'approaching', 'warning', 'confirmed'}
                   and not trade_notification_rejection(item,
                       policy_enabled=enabled((state.get('seed') or {}).get('settings', {})))
                   and item.get('point_type') and point_notification_decision(state, item, holdings) == 'allow'})


def warning_recheck_symbols(state):
    deferred = state.get('deferred_monitor_periods', {})
    urgent = {f"{item.get('market', 'a')}:{item['code']}"
            for item in state.get('unverified_signals', {}).values()
            if item.get('frequency') in deferred.get(f"{item.get('market', 'a')}:{item['code']}", ())}
    pool = set((state.get('seed') or {}).get('codes', []))
    for item in state.get('unverified_signals', {}).values():
        key, frequency = f"{item.get('market', 'a')}:{item.get('code')}", item.get('frequency')
        retry = state.get('retry_symbols', {}).get(key, {})
        if retry.get('by_period'):
            retry = retry['by_period'].get(frequency, {})
        elif frequency not in retry.get('periods', [frequency]):
            continue
        failures = retry.get('failures', 0)
        if key in pool and type(failures) is int and failures > 0:
            # Scheduling still owns retry deadlines and caps. Only prioritize
            # recovery of already-unverified evidence, without resetting them.
            urgent.add(key)
    return urgent
