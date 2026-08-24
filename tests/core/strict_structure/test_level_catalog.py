import pytest

from chanlun.core.strict_structure.level_catalog import (
    MAX_RECURSIVE_STRUCTURE_LEVELS,
    effective_frequency,
    effective_frequency_rank,
    recursive_level_labels,
)


@pytest.mark.parametrize(
    ("frequency", "source"),
    (
        ("1m", "1m"),
        ("5m", "5m"),
        ("30m", "30m"),
        ("d", "d"),
        ("1D", "d"),
        ("15m", "15m"),
        ("15", "15m"),
    ),
)
def test_recursive_level_labels_keep_physical_source_and_explicit_level(
    frequency,
    source,
):
    assert recursive_level_labels(frequency) == tuple(
        f"{source}/L{level}" for level in range(MAX_RECURSIVE_STRUCTURE_LEVELS)
    )


@pytest.mark.parametrize(
    ("source_frequency", "recursive_level", "expected", "rank"),
    (
        ("1m", 0, "1m", 0),
        ("1m", 1, "1m", 0),
        ("1m", 3, "1m", 0),
        ("5m", 2, "5m", 1),
        ("30m", 3, "30m", 2),
        ("1D", 2, "d", 3),
    ),
)
def test_effective_frequency_preserves_physical_source_period(
    source_frequency,
    recursive_level,
    expected,
    rank,
):
    assert effective_frequency(source_frequency, recursive_level) == expected
    assert effective_frequency_rank(source_frequency, recursive_level) == rank


@pytest.mark.parametrize("invalid", (-1, 1.0, True, None))
def test_effective_frequency_rejects_invalid_recursive_level(invalid) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        effective_frequency("5m", invalid)


def test_effective_frequency_rejects_level_outside_catalog() -> None:
    with pytest.raises(ValueError, match="outside the bounded catalog"):
        effective_frequency("5m", MAX_RECURSIVE_STRUCTURE_LEVELS)
