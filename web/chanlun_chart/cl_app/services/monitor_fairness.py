"""Bounded fair admission across hot minute, key-period and cold-minute work."""
from collections import defaultdict, deque
import math


SERVICE_PATTERN = ('hot', 'hot', 'critical', 'hot', 'hot', 'cold',
                   'hot', 'critical', 'hot', 'hot', 'critical', 'hot')
LANES = ('hot', 'critical', 'cold')
SERVICE_SECONDS = 60


def task_lane(frequency, minute_decision):
    if frequency != '1m':
        return 'critical'
    return 'cold' if minute_decision.get('cold') else 'hot'


def task_deadline(state, key, frequency, target, decision, now):
    """Keep overdue work old even when newer market bars replace its target."""
    interval = int(frequency[:-1]) * 60
    period_key = f'{key}:{frequency}'
    saved = state.get('monitor_pending_deadlines', {}).get(period_key, {})
    if type(saved.get('deadline')) in (int, float):
        return saved['deadline']
    check = state.get('selection_checks', {}).get(period_key, {})
    due = target if type(target) is int else now
    if frequency == '1m' and decision.get('cold'):
        if type(decision.get('next_due_at')) is int:
            due = decision['next_due_at']
        return due + SERVICE_SECONDS
    previous = check.get('cutoff')
    if type(previous) is int and previous < due:
        due = min(due, previous + interval)
    # A long candle is not permission to delay its newly available signal by
    # the candle's entire duration. All lanes target service within one minute.
    return due + SERVICE_SECONDS


def remember_pending(state, periods, targets, decisions, revision, now):
    """Persist first admission age, independently of market/proof contents."""
    seed = state.get('seed') or {}
    saved = state.setdefault('monitor_pending_deadlines', {})
    changed = False
    pool = set(seed.get('codes', []))
    enabled = set(seed.get('settings', {}).get('frequencies', []))
    bound_periods = {f"{plan['market']}:{plan['code']}:{plan['frequency']}"
                     for plan in state.get('holding_risk_plans', {}).values() if plan.get('active')}
    for period_key, record in list(saved.items()):
        key, frequency = period_key.rsplit(':', 1)
        bound = period_key in bound_periods
        check = state.get('selection_checks', {}).get(period_key, {})
        completed = (check.get('revision') == revision and check.get('error') is False
                     and type(check.get('cutoff')) is int and type(record.get('target')) is int
                     and check['cutoff'] >= record['target'])
        if (key not in pool or frequency not in enabled and not bound
                or record.get('seed') != seed.get('run_id') or record.get('revision') != revision or completed):
            del saved[period_key]
            changed = True
    for key, frequencies in periods.items():
        for frequency in frequencies:
            period_key = f'{key}:{frequency}'
            if period_key not in saved:
                target = targets.get(period_key)
                deadline = task_deadline(state, key, frequency, target, decisions.get(key, {}), now)
                saved[period_key] = {'seed': seed.get('run_id'), 'revision': revision,
                                     'target': target, 'first_pending_at': now, 'deadline': deadline}
                changed = True
    return changed


def record_costs(state, periods):
    """Successful committed worker timings only; never infer time from candles."""
    costs = state.setdefault('monitor_period_costs', {})
    for period in periods:
        timeline = period.get('processing_timeline', {})
        start, finish = timeline.get('calculation_started_at'), timeline.get('calculation_finished_at')
        if (type(start) not in (int, float) or type(finish) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(finish) or not 0 < finish-start <= 180):
            continue
        frequency = period.get('frequency')
        if frequency not in ('1m', '5m', '30m'):
            continue
        previous = costs.get(frequency, {})
        sample = finish-start
        costs[frequency] = {'seconds': round(.8*previous['seconds']+.2*sample, 6)
                           if previous.get('samples') else sample,
                           'samples': min(100000, previous.get('samples', 0)+1)}


def service_pressure(state, periods, targets, decisions, recent, now, capacity):
    """Estimate live service demand; closed markets never trigger a boost."""
    costs = state.get('monitor_period_costs', {})
    recurring, due_work, oldest = 0., 0., 0.
    seed = state.get('seed') or {}
    for key in recent:
        decision = decisions.get(key, {})
        for frequency in seed.get('settings', {}).get('frequencies', []):
            seconds = costs.get(frequency, {}).get('seconds', .25)
            cadence = 300 if frequency == '1m' and decision.get('cold') else int(frequency[:-1])*60
            recurring += seconds / cadence
        for frequency in periods.get(key, []):
            deadline = task_deadline(state, key, frequency, targets.get(f'{key}:{frequency}'), decision, now)
            remaining = max(15., deadline-now)
            due_work += costs.get(frequency, {}).get('seconds', .25) / remaining
            queued_at = state.get('monitor_pending_deadlines', {}).get(f'{key}:{frequency}', {}).get('first_pending_at', now)
            oldest = max(oldest, now-queued_at)
    required = math.ceil(max(recurring*1.1, due_work))
    normal = min((3*capacity+3)//4, max(0, capacity-1))
    # Measurements or demonstrated waiting must precede an expanded lease.
    measured = any(value.get('samples', 0) for value in costs.values())
    ready = bool(recent and (measured or oldest >= SERVICE_SECONDS/2))
    boost = ready and required > normal
    return {'overdue_pressure': boost, 'boost_ready': ready, 'required_workers': required,
            'recurring_worker_seconds': round(recurring, 3),
            'deadline_worker_seconds': round(due_work, 3), 'oldest_wait_seconds': round(max(0, oldest), 3)}


def plan_batch(symbols, periods, *, state, targets, minute_decisions, urgent,
               limit, cursor, now, excluded=(), critical_cursor=None):
    """One symbol/period per batch; unused shares are borrowed immediately.

    The cursor survives batch boundaries, so even tiny batches reach every
    nonempty lane. Holdings and notification repairs lead their lane; deadline
    and the last attempted clock retain age within the same protection class.
    """
    queues = defaultdict(list)
    held = set(state.get('holding_symbols', []))
    for symbol in symbols:
        key = f"{symbol.get('market', 'a')}:{symbol['code']}"
        decision = minute_decisions.get(key, {})
        for frequency in periods.get(key, []):
            lane = task_lane(frequency, decision)
            deadline = task_deadline(state, key, frequency, targets.get(f'{key}:{frequency}'), decision, now)
            priority = (key not in held, key not in urgent, symbol.get('origin') != 'watchlist',
                        deadline, state.get('attempted_cutoffs', {}).get(key, 0), key, frequency)
            queues[frequency if lane == 'critical' else lane].append({'symbol': symbol, 'key': key, 'frequency': frequency,
                                 'lane': lane, 'deadline': deadline, 'priority': priority})
    queues = {lane: deque(sorted(rows, key=lambda row: row['priority'])) for lane, rows in queues.items()}
    selected, used = [], set(excluded)
    critical_cursor = state.get('monitor_critical_cursor', 0) if critical_cursor is None else critical_cursor
    for _ in range(limit):
        for queue in queues.values():
            while queue and queue[0]['key'] in used:
                queue.popleft()
        critical = [frequency for frequency in ('5m', '30m') if queues.get(frequency)]
        critical_period = min(critical, key=lambda frequency: (
            queues[frequency][0]['priority'][:4],
            frequency != ('5m', '30m')[critical_cursor % 2],
            queues[frequency][0]['priority'][4:])) if critical else None
        heads = {lane: queues[critical_period if lane == 'critical' else lane][0]
                 for lane in LANES if (critical_period if lane == 'critical' else queues.get(lane))}
        available = list(heads)
        if not available:
            break
        preferred = SERVICE_PATTERN[cursor % len(SERVICE_PATTERN)]
        lane = preferred if preferred in available else min(
            available, key=lambda item: (heads[item]['priority'], LANES.index(item)))
        task = queues[critical_period if lane == 'critical' else lane].popleft()
        if lane == 'critical':
            critical_cursor += 1
        selected.append(task)
        used.add(task['key'])
        cursor = (cursor + 1) % len(SERVICE_PATTERN)
    return selected, cursor
