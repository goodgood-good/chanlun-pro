"""Keep screening facts separate from large, frozen chart presentation arrays."""

from contextlib import contextmanager
from contextvars import ContextVar
import json

PENDING = "_screening_chart_deferred"
_scope = ContextVar("screening_chart_plans", default=None)
_mode = ContextVar("screening_chart_mode", default=None)


@contextmanager
def presentation_scope():
    if _scope.get() is not None:
        yield
        return
    token = _scope.set({})
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def facts_only():
    token = _mode.set("replay")
    try:
        yield
    finally:
        _mode.reset(token)


@contextmanager
def defer_presentation(cache_root, revision, *, enabled, eager=False):
    # Live pools already consist of admitted setups and usually need frozen
    # evidence. Render once while their runtime is owned, rather than rebuild
    # the same geometry after selection. Independent replays still override
    # this mode with facts_only(), and sparse universe scans remain deferred.
    token = _mode.set(None if eager else (cache_root, revision, enabled))
    try:
        yield
    finally:
        _mode.reset(token)


def mode():
    return _mode.get()


def register(snapshot):
    settings = _mode.get()
    if not isinstance(settings, tuple):
        return
    snapshot[PENDING] = True
    plans = _scope.get()
    if plans is not None:
        plans[id(snapshot)] = (snapshot, settings)


def materialize(snapshot, frame, code, frequency):
    if snapshot.get(PENDING) is not True:
        return snapshot
    from .runtime_cache import incremental_snapshot_scope, runtime_for_snapshot
    from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart

    plans = _scope.get()
    saved = plans.get(id(snapshot)) if plans is not None else None
    root, revision, enabled = (
        saved[1] if saved is not None and saved[0] is snapshot else (None, None, False)
    )
    # The cache reacquires its own lock and verifies the complete input before
    # lending a runtime. An evicted/changed runtime is safely rebuilt.
    with (
        incremental_snapshot_scope(root, revision, enabled=enabled),
        runtime_for_snapshot(frame, code, frequency) as runtime,
    ):
        if runtime.cd is None:
            raise ValueError("Frozen evidence runtime is unavailable")
        chart = cl_data_to_tv_chart(
            frame,
            {"chart_show_bi": "1", "chart_show_xd": "1", "chart_show_fx": "1"},
            market=frame.attrs.get("screening_market", "a"),
            code=code,
            frequency=frequency,
            strict_runtime=runtime,
        )
    if chart.get("strict_structure_mode") != "replace":
        raise ValueError("Frozen evidence presentation is unavailable")
    rebuilt = chart["strict_structure"]
    # Cache publication uses JSON: tuples in view-selection evidence return as
    # lists on disk. Compare the complete wire representation, still checking
    # every structural field, value and source clock without coercing strings.
    stored = {key: snapshot.get(key) for key in rebuilt}
    if json.dumps(stored, sort_keys=True, allow_nan=False) != json.dumps(
        rebuilt, sort_keys=True, allow_nan=False
    ):
        raise ValueError("Structure changed while materializing frozen evidence")
    presentation = {
        key: value
        for key, value in chart.items()
        if key in ("fxs", "bis", "xds") or key.startswith(("macd_", "higher_macd_"))
    }
    # Replace the marker at its original position, preserving the exact JSON
    # field order of the eager snapshot as well as its semantic contents.
    ordered = {
        ("chart" if key == PENDING else key): (
            presentation if key == PENDING else value
        )
        for key, value in snapshot.items()
    }
    snapshot.clear()
    snapshot.update(ordered)
    if plans is not None:
        plans.pop(id(snapshot), None)
    return snapshot
