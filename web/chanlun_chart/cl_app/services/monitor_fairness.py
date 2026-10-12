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


def _critical_period(queues, cursor):
    available = [frequency for frequency in ('5m', '30m') if queues.get(frequency)]
    return min(available, key=lambda frequency: (
        queues[frequency][0]['priority'][:4],
        frequency != ('5m', '30m')[cursor % 2],
        queues[frequency][0]['priority'][4:])) if available else None


def _holding_task(tasks, state, now, critical_cursor):
    """Give the first minute a turn, then serve its overdue older siblings."""
    minute = next((task for task in tasks if task['frequency'] == '1m'), None)
    siblings = [task for task in tasks if task['frequency'] != '1m']

    def attempted_at(task):
        period_key = f"{task['key']}:{task['frequency']}"
        pending = state.get('monitor_pending_deadlines', {}).get(period_key, {})
        attempt = state.get('attempted_monitor_periods', {}).get(period_key, {})
        return attempt.get('scheduled_at', 0) if attempt.get('revision') == pending.get('revision') else 0

    if minute:
        # A minute from an older source revision cannot consume this revision's
        # initial fast check. A retried minute cannot consume every later turn.
        minute_attempt = state.get('attempted_monitor_periods', {}).get(f"{minute['key']}:1m", {})
        older = []
        for task in siblings:
            pending = state.get('monitor_pending_deadlines', {}).get(f"{task['key']}:{task['frequency']}", {})
            minute_at = (minute_attempt.get('scheduled_at', 0)
                         if minute_attempt.get('revision') == pending.get('revision') else 0)
            if (task['deadline'] <= now and minute_at > attempted_at(task)
                    and minute_at >= pending.get('first_pending_at', now)):
                older.append(task)
        if not older:
            return minute
        siblings = older
    return min(siblings, key=lambda task: (
        task['priority'][:4], attempted_at(task),
        task['frequency'] != ('5m', '30m')[critical_cursor % 2], task['priority'][4:]))


def _reserve_older_siblings(queues, cursor, critical_cursor, limit, now, excluded, state):
    """Protect the critical share from earlier claims by the same symbol.

    Reserve only symbols that fit the batch's existing critical slots. Global
    earliest-deadline filtering would otherwise suppress every hot minute in
    a large pool while a slow key-period backlog drains.
    """
    slots = sum(SERVICE_PATTERN[(cursor+i) % len(SERVICE_PATTERN)] == 'critical' for i in range(limit))
    preview = {frequency: deque(queues.get(frequency, ())) for frequency in ('5m', '30m')}
    minutes = {task['key']: task for lane in ('hot', 'cold') for task in queues.get(lane, ())}
    used, reserved = set(excluded), set()
    for _ in range(slots):
        for queue in preview.values():
            while queue and queue[0]['key'] in used:
                queue.popleft()
        frequency = _critical_period(preview, critical_cursor)
        if frequency is None:
            break
        task = preview[frequency].popleft()
        minute = minutes.get(task['key'])
        period_key = f"{task['key']}:{frequency}"
        age = state.get('monitor_pending_deadlines', {}).get(period_key, {})
        attempts = state.get('attempted_monitor_periods', {})
        minute_attempt = attempts.get(f"{task['key']}:1m", {})
        sibling_attempt = attempts.get(period_key, {})
        minute_at = minute_attempt.get('scheduled_at', 0) if minute_attempt.get('revision') == age.get('revision') else 0
        sibling_at = sibling_attempt.get('scheduled_at', 0) if sibling_attempt.get('revision') == age.get('revision') else 0
        waiting_turn = minute_at > sibling_at and minute_at >= age.get('first_pending_at', now)
        # The first minute admission remains immediate. Once it has had a
        # turn during this sibling's wait, an overdue critical task keeps its
        # share even if failed minute attempts retained an equally old age.
        if minute and task['deadline'] <= now and waiting_turn:
            reserved.add(task['key'])
        used.add(task['key'])
        critical_cursor += 1
    for lane in ('hot', 'cold'):
        if queues.get(lane):
            queues[lane] = deque(task for task in queues[lane] if task['key'] not in reserved)
    return reserved


def plan_batch(symbols, periods, *, state, targets, minute_decisions, urgent,
               limit, cursor, now, excluded=(), critical_cursor=None):
    """One symbol/period per batch; unused shares are borrowed immediately.

    Due holdings precede all ordinary lanes and do not spend their fair-share
    cursor. The ordinary cursor survives batch boundaries, so even tiny spare
    batches reach every nonempty lane. Notification repairs lead their lane.
    """
    queues = defaultdict(list)
    holding_tasks = defaultdict(list)
    held = set(state.get('holding_symbols', []))
    for symbol in symbols:
        key = f"{symbol.get('market', 'a')}:{symbol['code']}"
        decision = minute_decisions.get(key, {})
        for frequency in periods.get(key, []):
            lane = task_lane(frequency, decision)
            deadline = task_deadline(state, key, frequency, targets.get(f'{key}:{frequency}'), decision, now)
            priority = (key not in held, key not in urgent, symbol.get('origin') != 'watchlist',
                        deadline, state.get('attempted_cutoffs', {}).get(key, 0), key, frequency)
            task = {'symbol': symbol, 'key': key, 'frequency': frequency,
                    'lane': lane, 'deadline': deadline, 'priority': priority}
            if key in held:
                holding_tasks[key].append(task)
            else:
                queues[frequency if lane == 'critical' else lane].append(task)
    queues = {lane: deque(sorted(rows, key=lambda row: row['priority'])) for lane, rows in queues.items()}
    selected, used = [], set(excluded)
    critical_cursor = state.get('monitor_critical_cursor', 0) if critical_cursor is None else critical_cursor
    holdings = [_holding_task(tasks, state, now, critical_cursor)
                for key, tasks in holding_tasks.items() if key not in used]
    for task in sorted(holdings, key=lambda task: task['priority'])[:max(0, limit)]:
        selected.append(task)
        used.add(task['key'])
        critical_cursor += task['lane'] == 'critical'
    remaining = max(0, limit-len(selected))
    _reserve_older_siblings(queues, cursor, critical_cursor, remaining, now, used, state)
    for _ in range(remaining):
        for queue in queues.values():
            while queue and queue[0]['key'] in used:
                queue.popleft()
        critical_period = _critical_period(queues, critical_cursor)
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
