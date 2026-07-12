"""Reusable opt-in fencing for strategy-run-bound mutation surfaces.

The guard deliberately depends only on the public duck-typed strategy-run
capability.  This keeps it importable from monitor and persistence modules
without introducing a cycle through ``strategy_run``.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Callable, ContextManager, TypeVar


_Result = TypeVar("_Result")


class MutationFenceError(RuntimeError):
    """A mutation surface could not be bound to one exact strategy run."""


class _UnboundMutationScope:
    """A registered standalone mutation that prevents concurrent binding."""

    def __init__(self, guard: MutationLeaseGuard) -> None:
        self._guard = guard
        self._entered = False
        self._closed = False

    def __enter__(self) -> None:
        if self._entered or self._closed:
            raise MutationFenceError("mutation_fence_scope_reuse_forbidden")
        self._entered = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if not self._closed:
            self._closed = True
            self._guard._release_unbound_scope()
        return False


class MutationLeaseGuard:
    """One-time strategy-run binding with a standalone-compatible no-op mode."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active: object | None = None
        self._unbound_scopes_in_flight = 0

    def _release_unbound_scope(self) -> None:
        with self._lock:
            if self._unbound_scopes_in_flight <= 0:
                raise MutationFenceError("mutation_fence_unbound_scope_underflow")
            self._unbound_scopes_in_flight -= 1

    @staticmethod
    def _active_field(active: object, field_name: str) -> object:
        value = getattr(active, field_name, None)
        if value is None:
            raise MutationFenceError(
                f"mutation_fence_active_{field_name}_unavailable"
            )
        return value

    @classmethod
    def _validate_binding_contract(
        cls,
        active: object,
        *,
        expected_store_role: str | None,
        expected_store_path: str | Path | None,
        expected_store_instance_id: str | None,
        expected_run_id: str | None,
        expected_epoch: int | None,
        expected_strategy_run_fingerprint: str | None,
    ) -> None:
        if not callable(getattr(active, "mutation_lease", None)):
            raise MutationFenceError(
                "mutation_fence_active_mutation_lease_unavailable"
            )
        if not callable(
            getattr(active, "require_current_mutation_lease", None)
        ):
            raise MutationFenceError(
                "mutation_fence_active_requirement_unavailable"
            )

        run_id = cls._active_field(active, "run_id")
        epoch = cls._active_field(active, "epoch")
        fingerprint = cls._active_field(
            active,
            "strategy_run_fingerprint",
        )
        if expected_run_id is not None and run_id != expected_run_id:
            raise MutationFenceError("mutation_fence_run_id_mismatch")
        if expected_epoch is not None and epoch != expected_epoch:
            raise MutationFenceError("mutation_fence_epoch_mismatch")
        if (
            expected_strategy_run_fingerprint is not None
            and fingerprint != expected_strategy_run_fingerprint
        ):
            raise MutationFenceError("mutation_fence_fingerprint_mismatch")

        store_contract_requested = any(
            value is not None
            for value in (
                expected_store_role,
                expected_store_path,
                expected_store_instance_id,
            )
        )
        if not store_contract_requested:
            return
        if not isinstance(expected_store_role, str) or not expected_store_role:
            raise MutationFenceError("mutation_fence_store_role_required")
        bindings = getattr(active, "store_bindings", None)
        paths = getattr(active, "store_paths", None)
        if not isinstance(bindings, Mapping) or not isinstance(paths, Mapping):
            raise MutationFenceError("mutation_fence_store_role_unavailable")
        binding = bindings.get(expected_store_role)
        raw_path = paths.get(expected_store_role)
        if (
            binding is None
            or getattr(binding, "store_role", None) != expected_store_role
            or raw_path is None
        ):
            raise MutationFenceError("mutation_fence_store_role_mismatch")
        if (
            getattr(binding, "run_id", None) != run_id
            or getattr(binding, "epoch", None) != epoch
            or getattr(binding, "strategy_run_fingerprint", None)
            != fingerprint
        ):
            raise MutationFenceError("mutation_fence_store_binding_mismatch")
        if expected_store_path is not None:
            actual_path = Path(raw_path).expanduser().absolute()
            required_path = Path(expected_store_path).expanduser().absolute()
            if actual_path != required_path:
                raise MutationFenceError("mutation_fence_store_path_mismatch")
        if (
            expected_store_instance_id is not None
            and getattr(binding, "store_instance_id", None)
            != expected_store_instance_id
        ):
            raise MutationFenceError(
                "mutation_fence_store_instance_mismatch"
            )

    def bind(
        self,
        active: object,
        *,
        expected_store_role: str | None = None,
        expected_store_path: str | Path | None = None,
        expected_store_instance_id: str | None = None,
        expected_run_id: str | None = None,
        expected_epoch: int | None = None,
        expected_strategy_run_fingerprint: str | None = None,
    ) -> None:
        with self._lock:
            if self._active is not None and self._active is not active:
                raise MutationFenceError(
                    "mutation_fence_rebind_forbidden"
                )
            if self._active is None and self._unbound_scopes_in_flight:
                raise MutationFenceError(
                    "mutation_fence_bind_during_unbound_scope"
                )
            self._validate_binding_contract(
                active,
                expected_store_role=expected_store_role,
                expected_store_path=expected_store_path,
                expected_store_instance_id=expected_store_instance_id,
                expected_run_id=expected_run_id,
                expected_epoch=expected_epoch,
                expected_strategy_run_fingerprint=(
                    expected_strategy_run_fingerprint
                ),
            )
            if self._active is None:
                self._active = active

    def scope(self, operation: str) -> ContextManager[object | None]:
        with self._lock:
            active = self._active
            if active is None:
                self._unbound_scopes_in_flight += 1
                return _UnboundMutationScope(self)
        lease_provider = getattr(active, "mutation_lease")
        return lease_provider(operation)

    def require(self) -> None:
        with self._lock:
            active = self._active
        if active is None:
            return
        requirement = getattr(active, "require_current_mutation_lease")
        requirement()

    def can_persist_internal_fail_stop(self) -> bool:
        """Return whether an internal fail-stop may mutate durable state.

        Standalone stores retain their historical behavior.  Once a store is
        bound to a strategy run, integrity detection from a read path may only
        persist its fail-stop marker while the caller already holds the exact
        current mutation capability.
        """

        with self._lock:
            active = self._active
        if active is None:
            return True
        requirement = getattr(active, "require_current_mutation_lease")
        try:
            requirement()
        except RuntimeError:
            return False
        return True


def mutation_fenced(
    operation: str,
) -> Callable[[Callable[..., _Result]], Callable[..., _Result]]:
    """Run a synchronous mutation method inside its instance fence scope."""

    if (
        not isinstance(operation, str)
        or not operation
        or operation != operation.strip()
        or not operation.isprintable()
        or len(operation) > 255
    ):
        raise ValueError("operation must be bounded printable text")

    def decorate(method: Callable[..., _Result]) -> Callable[..., _Result]:
        @wraps(method)
        def wrapped(instance: object, *args: object, **kwargs: object) -> _Result:
            guard = getattr(instance, "_mutation_fence", None)
            if guard is None:
                raise MutationFenceError("mutation_fence_guard_missing")
            if not isinstance(guard, MutationLeaseGuard):
                raise MutationFenceError("mutation_fence_guard_invalid")
            with guard.scope(operation):
                return method(instance, *args, **kwargs)

        return wrapped

    return decorate


__all__ = [
    "MutationFenceError",
    "MutationLeaseGuard",
    "mutation_fenced",
]
