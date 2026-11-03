"""Share independent cold replay results within one immutable screening call.

Primary calculations never populate this cache. A replay remains independent
of the runtime cache; repeated candidate audits only share an identical cold
rebuild after hashing the whole frame, metadata and builder identity.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

import pandas as pd

MAX_ENTRIES = 8
MAX_BARS = 80000
_scope = ContextVar("screening_independent_replays", default=None)


@contextmanager
def replay_scope():
    if _scope.get() is not None:
        yield
        return
    token = _scope.set(OrderedDict())
    try:
        yield
    finally:
        _scope.reset(token)


def replay_snapshot(builder, frame, code, frequency, *, include_consolidation=False):
    cache = _scope.get()

    def build():
        return (
            builder(frame, code, frequency, include_consolidation=True)
            if include_consolidation
            else builder(frame, code, frequency)
        )

    if cache is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return build()
    try:
        raw = json.dumps(
            {
                "code": code,
                "frequency": frequency,
                "attrs": frame.attrs,
                "columns": list(frame.columns),
                "dtypes": [str(t) for t in frame.dtypes],
            },
            sort_keys=True,
            allow_nan=False,
        ).encode()
        digest = hashlib.sha256(raw)
        digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
        key = builder, digest.digest(), include_consolidation
    except (TypeError, ValueError, OverflowError):
        return build()
    if key in cache:
        cache.move_to_end(key)
        return deepcopy(cache[key][1])
    value = build()
    if len(frame) <= MAX_BARS:
        cache[key] = (len(frame), deepcopy(value))
        while (
            len(cache) > MAX_ENTRIES
            or sum(item[0] for item in cache.values()) > MAX_BARS
        ):
            cache.popitem(last=False)
    return value
