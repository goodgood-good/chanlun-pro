from datetime import datetime
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.provisional import (
    extract_provisional_candidates,
)
from tests.trading_system.helpers import fake_cd_with_unfinished_down_line


AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_unfinished_line_never_becomes_confirmed_point() -> None:
    cd = fake_cd_with_unfinished_down_line(
        mmds=("2buy",),
        divergences=("qs",),
    )

    candidates = extract_provisional_candidates(
        cd,
        code="SZ.000001",
        source_frequency="5m",
        as_of=AS_OF,
    )

    assert len(candidates) == 1
    assert candidates[0].status == "provisional"
    assert candidates[0].point_type == "2buy"
    assert candidates[0].missing_conditions == (
        "bottom_fractal_confirmed",
        "terminal_line_confirmed",
    )
    assert all(candidate.actionable is False for candidate in candidates)


def test_consolidation_divergence_is_not_first_point_candidate() -> None:
    cd = fake_cd_with_unfinished_down_line(mmds=(), divergences=("pz",))

    candidates = extract_provisional_candidates(
        cd,
        code="SZ.000001",
        source_frequency="5m",
        as_of=AS_OF,
    )

    assert candidates == ()


def test_candidate_has_no_probability_score() -> None:
    cd = fake_cd_with_unfinished_down_line(mmds=("2buy",), divergences=())

    candidate = extract_provisional_candidates(
        cd,
        code="SZ.000001",
        source_frequency="5m",
        as_of=AS_OF,
    )[0]

    assert not hasattr(candidate, "progress")
    assert not hasattr(candidate, "probability")
    assert not hasattr(candidate, "score")
