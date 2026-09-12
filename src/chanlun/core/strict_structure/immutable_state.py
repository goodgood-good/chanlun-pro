"""Fast, complete pickle state for the frozen evidence models."""

from dataclasses import fields
from functools import lru_cache
from operator import attrgetter


@lru_cache(maxsize=64)
def _state_reader(model_type):
    # Cache the schema, never the values. Newly added fields and subclasses
    # participate automatically, in the same order as dataclasses' getstate.
    names = tuple(field.name for field in fields(model_type))
    if len(names) < 2:
        return lambda value: tuple(getattr(value, name) for name in names)
    return attrgetter(*names)


def frozen_dataclass_state(value):
    # Reading every field on each call also preserves the proof validator's
    # detection of illicit object.__setattr__ changes to nested evidence.
    return list(_state_reader(type(value))(value))
