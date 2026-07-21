from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.incremental import (
    IncrementalCenterEngine,
    PrefixStabilityViolation,
)
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def valid_extension_sequence():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 110),
        unit(6, "up", 110, 135),
        unit(7, "down", 135, 120),
    )


def test_incremental_rejects_rewriting_a_locked_unit():
    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    values = valid_extension_sequence()
    engine.update(values[:5])
    changed = list(values[:6])
    changed[1] = replace(changed[1], unit_id="rewritten-unit")

    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        engine.update(tuple(changed))


def test_incremental_matches_batch_at_every_prefix():
    values = valid_extension_sequence()
    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)

    for size in range(1, len(values) + 1):
        incremental = engine.update(values[:size])
        batch = calculate_centers(values[:size], 0, SourceKind.SEGMENT)
        assert incremental == batch


def test_incremental_rejects_rewriting_locked_confirmation_or_availability():
    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    values = valid_extension_sequence()
    engine.update(values[:5])
    changed = list(values[:5])
    changed[1] = replace(
        changed[1],
        confirmed_at=changed[1].confirmed_at + timedelta(minutes=1),
        available_at=changed[1].available_at + timedelta(minutes=1),
    )

    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        engine.update(tuple(changed))


def test_incremental_rejects_rewriting_locked_basis_or_children():
    values = valid_extension_sequence()

    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    engine.update(values[:5])
    changed_basis = list(values[:5])
    changed_basis[1] = replace(
        changed_basis[1],
        price_basis_revision="test-forward-adjusted-v2",
    )
    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        engine.update(tuple(changed_basis))

    child_engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    child_engine.update(values[:5])
    changed_children = list(values[:5])
    changed_children[1] = replace(
        changed_children[1],
        child_ids=("child-rewritten",),
    )
    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        child_engine.update(tuple(changed_children))


def test_incremental_rejects_shortening_or_unlocking_frozen_prefix():
    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    values = valid_extension_sequence()
    engine.update(values[:5])

    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        engine.update(values[:4])

    unlocked = replace(values[4], locked=False, confirmed_at=None)
    with pytest.raises(PrefixStabilityViolation, match="locked prefix changed"):
        engine.update(values[:4] + (unlocked,))


def test_incremental_allows_an_active_tail_to_become_locked():
    values = valid_extension_sequence()
    engine = IncrementalCenterEngine(0, SourceKind.SEGMENT)
    active_tail = replace(values[5], locked=False, confirmed_at=None)

    engine.update(values[:5] + (active_tail,))
    incremental = engine.update(values[:6])

    assert incremental == calculate_centers(values[:6], 0, SourceKind.SEGMENT)
