"""Bounded fair admission across hot minute, key-period and cold-minute work."""
from collections import defaultdict, deque


SERVICE_PATTERN = ('hot', 'hot', 'critical', 'hot', 'hot', 'cold',
                   'hot', 'critical', 'hot', 'hot', 'critical', 'hot')
LANES = ('hot', 'critical', 'cold')


def task_lane(frequency, minute_decision):
    if frequency != '1m':
        return 'critical'
    return 'cold' if minute_decision.get('cold') else 'hot'


def task_deadline(state, key, frequency, target, decision, now):
    """Keep overdue work old even when newer market bars replace its target."""
    interval = int(frequency[:-1]) * 60
    check = state.get('selection_checks', {}).get(f'{key}:{frequency}', {})
    due = target if type(target) is int else now
    if frequency == '1m' and decision.get('cold'):
        if type(decision.get('next_due_at')) is int:
            due = decision['next_due_at']
        return due + 60
    previous = check.get('cutoff')
    if type(previous) is int and previous < due:
        due = min(due, previous + interval)
    return due + interval


def plan_batch(symbols, periods, *, state, targets, minute_decisions, urgent,
               limit, cursor, now, excluded=()):
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
            queues[lane].append({'symbol': symbol, 'key': key, 'frequency': frequency,
                                 'lane': lane, 'deadline': deadline, 'priority': priority})
    queues = {lane: deque(sorted(rows, key=lambda row: row['priority'])) for lane, rows in queues.items()}
    selected, used = [], set(excluded)
    for _ in range(limit):
        for queue in queues.values():
            while queue and queue[0]['key'] in used:
                queue.popleft()
        available = [lane for lane in LANES if queues.get(lane)]
        if not available:
            break
        preferred = SERVICE_PATTERN[cursor % len(SERVICE_PATTERN)]
        lane = preferred if preferred in available else min(
            available, key=lambda item: (queues[item][0]['priority'], LANES.index(item)))
        task = queues[lane].popleft()
        selected.append(task)
        used.add(task['key'])
        cursor = (cursor + 1) % len(SERVICE_PATTERN)
    return selected, cursor
