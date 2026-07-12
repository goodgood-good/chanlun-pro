from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import pytest

from chanlun.decision_support.mutation_fence import (
    MutationFenceError,
    MutationLeaseGuard,
    mutation_fenced,
)
from chanlun.decision_support.strategy_run import (
    ActiveStrategyRun,
    StrategyRunIntegrityError,
    StrategyRunMutationLease,
    _MUTATION_LEASE_CONTEXT,
    _MutationLeaseContextEntry,
)


_FINGERPRINT = "sha256:" + "1" * 64
_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)


def _active(tmp_path: Path, name: str = "one") -> ActiveStrategyRun:
    active = object.__new__(ActiveStrategyRun)
    object.__setattr__(active, "registry_path", tmp_path / f"{name}.sqlite3")
    object.__setattr__(active, "run_id", f"paper-run-{name}")
    object.__setattr__(active, "epoch", 1)
    object.__setattr__(active, "strategy_run_fingerprint", _FINGERPRINT)
    return active


def _entry(
    active: ActiveStrategyRun,
    *,
    closing: bool = False,
) -> _MutationLeaseContextEntry:
    closed = Event()
    if closing:
        closed.set()
    return _MutationLeaseContextEntry(
        registry_path=active.registry_path,
        run_id=active.run_id,
        strategy_run_fingerprint=active.strategy_run_fingerprint,
        lease=StrategyRunMutationLease(
            lease_id="mutation-lease-fixture",
            run_id=active.run_id,
            epoch=active.epoch,
            strategy_run_fingerprint=active.strategy_run_fingerprint,
            operation="fixture.commit",
            owner_token="fixture-owner-token",
            acquired_at=_NOW,
        ),
        execution_thread_id=get_ident(),
        execution_task=None,
        closing=closed,
    )


class _TopOnlyContext:
    def __init__(self, entry: _MutationLeaseContextEntry) -> None:
        self.entry = entry
        self.lookups = 0

    def __bool__(self) -> bool:
        return True

    def __getitem__(self, index: int) -> _MutationLeaseContextEntry:
        self.lookups += 1
        if index != -1:
            raise AssertionError("mutation capability lookup must be O(1)")
        return self.entry

    def __iter__(self):
        raise AssertionError("mutation capability lookup must not scan context")


def test_active_run_requires_exact_current_mutation_context_in_o1_top_slot(
    tmp_path: Path,
) -> None:
    active = _active(tmp_path)
    context = _TopOnlyContext(_entry(active))
    token = _MUTATION_LEASE_CONTEXT.set(context)  # type: ignore[arg-type]
    try:
        assert active.require_current_mutation_lease() is None
    finally:
        _MUTATION_LEASE_CONTEXT.reset(token)

    assert context.lookups == 1


def test_active_run_rejects_missing_mismatched_or_closing_context(
    tmp_path: Path,
) -> None:
    active = _active(tmp_path)
    matching = _entry(active)
    mismatches = (
        (),
        (replace(matching, registry_path=tmp_path / "other.sqlite3"),),
        (replace(matching, run_id="paper-run-other"),),
        (
            replace(
                matching,
                strategy_run_fingerprint="sha256:" + "2" * 64,
            ),
        ),
        (replace(matching, execution_thread_id=get_ident() + 1),),
        (replace(matching, execution_task=object()),),
        (_entry(active, closing=True),),
        (
            replace(
                matching,
                lease=replace(matching.lease, epoch=active.epoch + 1),
            ),
        ),
        (matching, replace(matching, run_id="paper-run-top-mismatch")),
    )

    for context in mismatches:
        token = _MUTATION_LEASE_CONTEXT.set(context)
        try:
            with pytest.raises(
                StrategyRunIntegrityError,
                match="strategy_run_mutation_lease_required",
            ):
                active.require_current_mutation_lease()
        finally:
            _MUTATION_LEASE_CONTEXT.reset(token)


class _ActiveProbe:
    def __init__(self, name: str, *, root: Path) -> None:
        self.name = name
        self.run_id = f"paper-run-{name}"
        self.epoch = 7
        self.strategy_run_fingerprint = _FINGERPRINT
        self.registry_path = root / f"registry-{name}.sqlite3"
        self.entered: list[str] = []
        self.required = 0
        self.store_paths = {"ledger": root / f"ledger-{name}.sqlite3"}
        self.store_bindings = {
            "ledger": SimpleNamespace(
                store_role="ledger",
                store_instance_id=f"ledger-instance-{name}",
                run_id=self.run_id,
                epoch=self.epoch,
                strategy_run_fingerprint=self.strategy_run_fingerprint,
            )
        }

    @contextmanager
    def mutation_lease(self, operation: str):
        self.entered.append(operation)
        try:
            yield object()
        finally:
            assert self.entered.pop() == operation

    def require_current_mutation_lease(self) -> None:
        self.required += 1


def test_mutation_lease_guard_unbound_is_noop_and_same_active_is_idempotent(
    tmp_path: Path,
) -> None:
    guard = MutationLeaseGuard()
    with guard.scope("unbound.commit") as capability:
        assert capability is None
    assert guard.require() is None

    active = _ActiveProbe("one", root=tmp_path)
    guard.bind(active)
    guard.bind(active)
    with guard.scope("bound.commit"):
        assert active.entered == ["bound.commit"]
        assert guard.require() is None

    assert active.entered == []
    assert active.required == 1


def test_mutation_lease_guard_rejects_bind_during_unbound_decorated_write(
    tmp_path: Path,
) -> None:
    guard = MutationLeaseGuard()
    active = _ActiveProbe("one", root=tmp_path)
    body_entered = Event()
    allow_persist = Event()
    persisted: list[str] = []
    worker_errors: list[BaseException] = []

    class Surface:
        def __init__(self) -> None:
            self._mutation_fence = guard

        @mutation_fenced("surface.unbound-write")
        def write(self) -> None:
            body_entered.set()
            if not allow_persist.wait(timeout=5):
                raise TimeoutError("test did not release unbound write")
            persisted.append("unbound-write")

    def run_write() -> None:
        try:
            Surface().write()
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    worker = Thread(target=run_write)
    worker.start()
    try:
        assert body_entered.wait(timeout=5)
        with pytest.raises(
            MutationFenceError,
            match="mutation_fence_bind_during_unbound_scope",
        ):
            guard.bind(active)
    finally:
        allow_persist.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert worker_errors == []
    assert persisted == ["unbound-write"]
    assert active.entered == []

    guard.bind(active)
    with guard.scope("surface.bound-write"):
        assert active.entered == ["surface.bound-write"]
    assert active.entered == []


def test_mutation_lease_guard_cleans_unbound_scope_after_body_exception(
    tmp_path: Path,
) -> None:
    guard = MutationLeaseGuard()
    active = _ActiveProbe("one", root=tmp_path)

    class Surface:
        def __init__(self) -> None:
            self._mutation_fence = guard

        @mutation_fenced("surface.unbound-error")
        def write(self) -> None:
            raise ValueError("expected write failure")

    with pytest.raises(ValueError, match="expected write failure"):
        Surface().write()

    guard.bind(active)
    with guard.scope("surface.after-error"):
        assert active.entered == ["surface.after-error"]
    assert active.entered == []


def test_internal_fail_stop_persistence_requires_bound_mutation_capability(
    tmp_path: Path,
) -> None:
    unbound = MutationLeaseGuard()
    assert unbound.can_persist_internal_fail_stop() is True

    active = _ActiveProbe("one", root=tmp_path)
    bound = MutationLeaseGuard()
    bound.bind(active)
    assert bound.can_persist_internal_fail_stop() is True
    assert active.required == 1

    def reject_missing_capability() -> None:
        active.required += 1
        raise RuntimeError("strategy_run_mutation_lease_required")

    active.require_current_mutation_lease = reject_missing_capability  # type: ignore[method-assign]
    assert bound.can_persist_internal_fail_stop() is False
    assert active.required == 2


def test_mutation_lease_guard_rejects_different_active_without_losing_binding(
    tmp_path: Path,
) -> None:
    first = _ActiveProbe("one", root=tmp_path)
    second = _ActiveProbe("two", root=tmp_path)
    guard = MutationLeaseGuard()
    guard.bind(first)

    with pytest.raises(MutationFenceError, match="mutation_fence_rebind_forbidden"):
        guard.bind(second)
    with pytest.raises(MutationFenceError, match="mutation_fence_rebind_forbidden"):
        guard.bind(object())

    with guard.scope("still-first.commit"):
        guard.require()
    assert first.required == 1
    assert second.required == 0


def test_mutation_fenced_decorator_enters_guard_and_rejects_missing_guard(
    tmp_path: Path,
) -> None:
    active = _ActiveProbe("one", root=tmp_path)

    class Surface:
        def __init__(self) -> None:
            self._mutation_fence = MutationLeaseGuard()
            self._mutation_fence.bind(active)

        @mutation_fenced("surface.write")
        def write(self, value: str) -> str:
            assert active.entered == ["surface.write"]
            self._mutation_fence.require()
            return value

    class MissingGuard:
        @mutation_fenced("surface.missing")
        def write(self) -> None:
            raise AssertionError("missing guard must fail before method body")

    assert Surface().write("persisted") == "persisted"
    assert active.entered == []
    assert active.required == 1
    with pytest.raises(MutationFenceError, match="mutation_fence_guard_missing"):
        MissingGuard().write()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"expected_store_role": "risk"}, "store_role"),
        ({"expected_store_path": Path("wrong.sqlite3")}, "store_path"),
        ({"expected_store_instance_id": "wrong-instance"}, "store_instance"),
        ({"expected_run_id": "paper-run-wrong"}, "run_id"),
        ({"expected_epoch": 8}, "epoch"),
        (
            {"expected_strategy_run_fingerprint": "sha256:" + "2" * 64},
            "fingerprint",
        ),
    ),
)
def test_mutation_lease_guard_validates_optional_store_binding_contract(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    active = _ActiveProbe("one", root=tmp_path)
    expected: dict[str, object] = {
        "expected_store_role": "ledger",
        "expected_store_path": active.store_paths["ledger"],
        "expected_store_instance_id": "ledger-instance-one",
        "expected_run_id": active.run_id,
        "expected_epoch": active.epoch,
        "expected_strategy_run_fingerprint": (
            active.strategy_run_fingerprint
        ),
    }
    expected.update(overrides)

    with pytest.raises(MutationFenceError, match=reason):
        MutationLeaseGuard().bind(active, **expected)

    valid = MutationLeaseGuard()
    valid.bind(
        active,
        expected_store_role="ledger",
        expected_store_path=active.store_paths["ledger"],
        expected_store_instance_id="ledger-instance-one",
        expected_run_id=active.run_id,
        expected_epoch=active.epoch,
        expected_strategy_run_fingerprint=active.strategy_run_fingerprint,
    )
