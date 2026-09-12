"""Bounded reuse of pure geometry within one complete branch analysis.

Candidate windows and proof paths frequently replay the same immutable
center seed or transition. Only those geometry operations are shared here;
MACD, completion permissions, branch selection and observation scopes are
still evaluated by their original callers.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from chanlun.core.strict_structure.models import TrendCenter


_active_replay = ContextVar("strict_center_geometry_replay", default=None)
_MISSING = object()


class ReplayKey:
    """Compare complete immutable values, computing their hash only once."""
    __slots__ = ("values", "value_hash")

    def __init__(self, values):
        self.values = values
        self.value_hash = hash(values)

    def __hash__(self):
        return self.value_hash

    def __eq__(self, other):
        if not isinstance(other, ReplayKey):
            return NotImplemented
        return self.values == other.values

    def __reduce__(self):
        # CL prefix caches can be persisted. Python salts string/datetime
        # hashes per process, so a loaded key must compute the new local hash.
        return type(self), (self.values,)


class GeometryReplayCache:
    def __init__(self, capacity=4096):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("geometry replay capacity must be positive")
        self.capacity = capacity
        self.entries = OrderedDict()
        self.hits = 0
        self.misses = 0

    def call(self, function, args, kwargs):
        # Frozen dataclass values, including geometry, price basis, ownership
        # and availability times, are compared in full. IDs alone are not keys.
        try:
            key = ReplayKey((function, args, tuple(sorted(kwargs.items()))))
            cached = self.entries.get(key, _MISSING)
        except TypeError:
            # Public geometry functions also accept lists/generators. Their
            # normal validation remains responsible for those uncached calls.
            return function(*args, **kwargs)
        if cached is not _MISSING:
            self.hits += 1
            self.entries.move_to_end(key)
            return cached
        self.misses += 1
        result = function(*args, **kwargs)
        self.entries[key] = result
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        return result


@contextmanager
def geometry_replay_cache(capacity=4096):
    cache = GeometryReplayCache(capacity)
    token = _active_replay.set(cache)
    try:
        yield cache
    finally:
        _active_replay.reset(token)
        cache.entries.clear()


def replay_geometry(function):
    @wraps(function)
    def replayed(*args, **kwargs):
        cache = _active_replay.get()
        # Stateful iterators must be consumed normally on every call. The
        # internal scanner supplies tuples and frozen TrendCenter objects.
        if cache is None or not args or not isinstance(args[0], (tuple, TrendCenter)):
            return function(*args, **kwargs)
        return cache.call(function, args, kwargs)
    return replayed


def with_geometry_replay(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        if _active_replay.get() is not None:
            return function(*args, **kwargs)
        with geometry_replay_cache():
            return function(*args, **kwargs)
    return scoped
