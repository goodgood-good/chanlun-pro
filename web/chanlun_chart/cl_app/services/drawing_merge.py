"""Merge explicit user changes, preserving drawings created in other windows."""

from copy import deepcopy
import hashlib
import json
import math


def revision(state):
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _zorder(source):
    state = source.get("state") if isinstance(source, dict) else None
    value = state.get("zorder") if isinstance(state, dict) else None
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _renumbered_only(key, base, local):
    """TV renumbers zorder on insertion without the user reordering old shapes."""
    before, wanted = base["sources"].get(key), local["sources"].get(key)
    old_z, new_z = _zorder(before), _zorder(wanted)
    if old_z is None or new_z is None:
        return False
    normalized = deepcopy(wanted)
    normalized["state"]["zorder"] = old_z
    if normalized != before:
        return False
    for other in base["sources"].keys() & local["sources"].keys() - {key}:
        old_other, new_other = (
            _zorder(base["sources"][other]),
            _zorder(local["sources"][other]),
        )
        if old_other is None or new_other is None:
            continue
        old_order = (old_z > old_other) - (old_z < old_other)
        new_order = (new_z > new_other) - (new_z < new_other)
        if old_order != new_order:
            return False
    return True


def merge(base, local, current):
    output = deepcopy(current)
    missing = object()
    conflicts = []
    for key in base["sources"].keys() | local["sources"].keys():
        before = base["sources"].get(key, missing)
        wanted = local["sources"].get(key, missing)
        actual = current["sources"].get(key, missing)
        if wanted == before:
            continue
        if _renumbered_only(key, base, local):
            # Preserve remote edits/deletions. Locally renumbered shapes which
            # are otherwise unchanged can still accompany a newly added shape.
            if actual == before:
                output["sources"][key] = deepcopy(wanted)
            continue
        if actual != before and actual != wanted:
            conflicts.append(key)
        elif wanted is missing:
            output["sources"].pop(key, None)
        else:
            output["sources"][key] = deepcopy(wanted)
    return output, sorted(conflicts)
