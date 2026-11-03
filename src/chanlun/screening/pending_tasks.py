"""Indexed FIFO priority waves for bounded worker affinity.

Only the leading contiguous priority wave is eligible. Each affinity key
indexes its first remaining occurrence, preserving duplicate-task FIFO order.
Dispatch examines the bounded affinity registry, never the remaining universe.
"""
from collections import OrderedDict, deque


class _Wave:
    def __init__(self, group):
        self.group = group
        self.items = OrderedDict()
        self.keys = {}

    def add(self, ordinal, stock, key, *, front=False):
        self.items[ordinal] = stock, key
        if front:
            self.items.move_to_end(ordinal, last=False)
        if key is not None:
            queue = self.keys.setdefault(key, deque())
            (queue.appendleft if front else queue.append)(ordinal)

    def pop(self, key):
        ordinal = self.keys[key][0] if key is not None else next(iter(self.items))
        stock, stored_key = self.items.pop(ordinal)
        if stored_key is not None:
            queue = self.keys[stored_key]
            first = queue.popleft()
            if first != ordinal:
                raise RuntimeError('Pending affinity index lost FIFO order')
            if not queue:
                del self.keys[stored_key]
        return ordinal, stock


class PendingTasks:
    def __init__(self, stocks, settings, *, group_key, affinity_key):
        self.settings = settings
        self.group_key = group_key
        self.affinity_key = affinity_key
        self.waves = deque()
        self.count = 0
        for ordinal, stock in enumerate(stocks):
            group = group_key(stock, settings)
            if not self.waves or self.waves[-1].group != group:
                self.waves.append(_Wave(group))
            self.waves[-1].add(ordinal, stock, affinity_key(stock, settings))
            self.count += 1

    def __bool__(self):
        return bool(self.count)

    def extend(self,tasks):
        for ordinal,stock in tasks:
            group=self.group_key(stock,self.settings)
            if not self.waves or self.waves[-1].group!=group:self.waves.append(_Wave(group))
            self.waves[-1].add(ordinal,stock,self.affinity_key(stock,self.settings))
            self.count+=1

    def appendleft(self, task):
        ordinal, stock = task
        group = self.group_key(stock, self.settings)
        if not self.waves or self.waves[0].group != group:
            self.waves.appendleft(_Wave(group))
        self.waves[0].add(ordinal, stock, self.affinity_key(stock, self.settings), front=True)
        self.count += 1

    def take(self, owner, affinity):
        wave = self.waves[0]
        selected, recent = None, -1
        for key, (worker, _protected, sequence) in affinity.items():
            if worker == owner and sequence > recent and key in wave.keys:
                selected, recent = key, sequence
        value = wave.pop(selected)
        self.count -= 1
        if not wave.items:
            self.waves.popleft()
        return value, recent >= 0
