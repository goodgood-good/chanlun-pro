from __future__ import annotations

import pytest

from chanlun.decision_support.trading_system.screening_warmup import (
    expected_screening_warmup_suffix_bar_count,
    screening_warmup_reason_code,
)


@pytest.mark.parametrize(
    ("frequency", "full_count", "expected_suffix"),
    (("d", 480, 320), ("30m", 481, 321), ("5m", 960, 640), ("1m", 1440, 960)),
)
def test_screening_warmup_split_keeps_the_exact_two_thirds_suffix(
    frequency: str,
    full_count: int,
    expected_suffix: int,
) -> None:
    assert expected_screening_warmup_suffix_bar_count(full_count) == expected_suffix
    assert screening_warmup_reason_code(
        frequency=frequency,
        converged=True,
        full_bar_count=full_count,
        suffix_bar_count=expected_suffix,
    ) == "WARMUP_TAIL_STABLE"


def test_screening_warmup_insufficient_history_is_fail_closed() -> None:
    assert screening_warmup_reason_code(
        frequency="d",
        converged=False,
        full_bar_count=479,
        suffix_bar_count=0,
    ) == "WARMUP_HISTORY_INSUFFICIENT"
    with pytest.raises(ValueError, match="insufficient warmup history"):
        screening_warmup_reason_code(
            frequency="d",
            converged=True,
            full_bar_count=479,
            suffix_bar_count=0,
        )


def test_screening_warmup_rejects_a_forged_suffix_count() -> None:
    with pytest.raises(ValueError, match="suffix count"):
        screening_warmup_reason_code(
            frequency="30m",
            converged=False,
            full_bar_count=480,
            suffix_bar_count=319,
        )
