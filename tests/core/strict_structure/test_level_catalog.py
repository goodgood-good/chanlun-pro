import pytest

from chanlun.core.strict_structure.level_catalog import (
    effective_frequency,
    effective_frequency_rank,
    recursive_level_labels,
)


@pytest.mark.parametrize(
    ("frequency", "expected"),
    (
        ("1m", ("1m", "5m", "30m", "日线")),
        ("5m", ("5m", "30m", "日线")),
        ("30m", ("30m", "日线")),
        ("d", ("日线",)),
        ("1D", ("日线",)),
        ("15m", ("15m",)),
        ("15", ("15m",)),
    ),
)
def test_recursive_level_labels_are_fixed_and_never_invent_week_or_month(
    frequency,
    expected,
):
    assert recursive_level_labels(frequency) == expected


@pytest.mark.parametrize(
    ("source_frequency", "recursive_level", "expected", "rank"),
    (
        ("1m", 0, "1m", 0),
        ("1m", 1, "5m", 1),
        ("1m", 3, "d", 3),
        ("5m", 2, "d", 3),
        ("30m", 0, "30m", 2),
    ),
)
def test_effective_frequency_combines_physical_source_and_recursive_level(
    source_frequency,
    recursive_level,
    expected,
    rank,
):
    assert effective_frequency(source_frequency, recursive_level) == expected
    assert effective_frequency_rank(source_frequency, recursive_level) == rank


def test_effective_frequency_rejects_nonexistent_recursive_level() -> None:
    with pytest.raises(ValueError, match="unsupported physical frequency"):
        effective_frequency("5m", 3)
