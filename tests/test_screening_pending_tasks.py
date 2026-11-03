from collections import OrderedDict, deque
import random

from chanlun.screening.pending_tasks import PendingTasks
from chanlun.screening.worker_pool import _affinity_key, _priority_group


SETTINGS = {'frequencies': ['5m']}


def reference_take(pending, worker, affinity):
    group = _priority_group(pending[0][1], SETTINGS)
    selected, recent = 0, -1
    for index, (_, stock) in enumerate(pending):
        if _priority_group(stock, SETTINGS) != group:
            break
        owner = affinity.get(_affinity_key(stock, SETTINGS))
        if owner is not None and owner[0] == worker and owner[2] > recent:
            selected, recent = index, owner[2]
    value = pending[selected]
    del pending[selected]
    return value, recent >= 0


def test_indexed_dispatch_matches_fifo_affinity_and_priority_with_duplicates_and_requeues():
    rng = random.Random(1087)
    stocks = [{'code': f'SH.{rng.randrange(20):06}', 'holding': rng.choice([False, False, True]),
               'origin': rng.choice(['watchlist', 'screening']), '_monitor_periods': [rng.choice(['1m', '5m', '30m'])]}
              for _ in range(500)]
    expected = deque(enumerate(stocks))
    pending = PendingTasks(stocks, SETTINGS, group_key=_priority_group, affinity_key=_affinity_key)
    affinity = OrderedDict()
    sequence = 0
    while expected:
        worker = rng.randrange(4)
        actual = pending.take(worker, affinity)
        assert actual == reference_take(expected, worker, affinity)
        ordinal, stock = actual[0]
        if rng.random() < .15:
            pending.appendleft((ordinal, stock))
            expected.appendleft((ordinal, stock))
        elif rng.random() < .1:
            affinity.clear()  # Worker retirement loses every old affinity.
        else:
            sequence += 1
            affinity[_affinity_key(stock, SETTINGS)] = worker, False, sequence
        assert bool(pending) == bool(expected)


def test_market_size_does_not_add_remaining_queue_scans_per_dispatch():
    calls = []
    def group(stock, settings):
        calls.append(True)
        return _priority_group(stock, settings)
    stocks = [{'code': f'SH.{i:06}'} for i in range(16000)]
    pending = PendingTasks(stocks, SETTINGS, group_key=group, affinity_key=_affinity_key)
    assert len(calls) == len(stocks)
    # An already warm unrelated symbol used to force a full remaining scan.
    affinity = {('a', 'SZ.000001', '5m'): ('worker', False, 1)}
    for ordinal in range(len(stocks)):
        task, reused = pending.take('worker', affinity)
        assert task[0] == ordinal and not reused
    assert len(calls) == len(stocks) and not pending
