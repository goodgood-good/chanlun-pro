"""Content-checked reuse of quality work within a single screening call.

No certificate survives a scan. Every lookup fingerprints the complete frame,
attributes and calendar context, so in-place repairs and metadata changes are
checked again. Failed or unsupported fingerprints simply use the normal path.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import hashlib
import json

import pandas as pd


_scope = ContextVar("screening_validation_cache", default=None)
MAX_ENTRIES = 16


@contextmanager
def validation_scope():
    token = _scope.set(OrderedDict())
    try:
        yield
    finally:
        _scope.reset(token)


def reuse_validation(check):
    @wraps(check)
    def validated(frame, context):
        cache = _scope.get()
        if cache is None or frame is None or frame.empty:
            return check(frame, context)
        try:
            metadata = json.dumps({"context": context, "attrs": frame.attrs,
                                   "columns": list(frame.columns),
                                   "dtypes": [str(dtype) for dtype in frame.dtypes]},
                                  sort_keys=True, allow_nan=False).encode()
            digest = hashlib.sha256(metadata)
            digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
            key = check, digest.digest()
        except (TypeError, ValueError, OverflowError):
            return check(frame, context)
        if key in cache:
            cache.move_to_end(key)
            return deepcopy(cache[key])
        result = check(frame, context)
        cache[key] = deepcopy(result)
        while len(cache) > MAX_ENTRIES:
            cache.popitem(last=False)
        return result
    return validated
