import pytest

from chanlun.core.strict_structure.level_catalog import recursive_level_labels


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
