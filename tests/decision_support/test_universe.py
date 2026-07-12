from __future__ import annotations

from datetime import datetime

import pytest

from chanlun.decision_support.universe import (
    SecuritySnapshot,
    UniversePolicy,
    filter_universe,
)
from tests.decision_support.conftest import ts


def make_security(**changes) -> SecuritySnapshot:
    values = {
        "market": "a",
        "code": "SH.600519",
        "name": "Example",
        "listed_days": 1000,
        "suspended": False,
        "delisting": False,
        "avg_turnover_20d": 200_000_000.0,
        "quote_time": ts("2026-07-13T10:35:00+08:00"),
        "limit_up_locked": False,
        "limit_down_locked": False,
    }
    return SecuritySnapshot(**(values | changes))


@pytest.fixture
def asof() -> datetime:
    return ts("2026-07-13T10:35:00+08:00")


@pytest.mark.parametrize(
    ("reason", "security"),
    (
        ("st", make_security(name="*ST Example")),
        ("new_listing", make_security(listed_days=59)),
        ("suspended", make_security(suspended=True)),
        ("low_liquidity", make_security(avg_turnover_20d=99_999_999.0)),
    ),
)
def test_universe_excludes_ineligible_a_shares(reason, security, asof):
    result = filter_universe(
        [security],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert result.included == ()
    assert len(result.excluded) == 1
    assert result.excluded[0].security is security
    assert result.excluded[0].reason == reason


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("listed_days", None),
        ("suspended", None),
        ("delisting", None),
        ("avg_turnover_20d", None),
        ("quote_time", None),
        ("limit_up_locked", None),
        ("limit_down_locked", None),
    ),
)
def test_universe_fails_closed_on_missing_metadata(field, value, asof):
    security = make_security(**{field: value})

    result = filter_universe(
        [security],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert result.included == ()
    assert result.excluded[0].reason == "missing_metadata"


@pytest.mark.parametrize(
    ("code", "name", "board", "limit_pct"),
    (
        ("SH.600519", "Main", "main", 0.10),
        ("SZ.300001", "Growth", "gem", 0.20),
        ("SH.688001", "STAR", "star", 0.20),
        ("BJ.920001", "Beijing", "bj", 0.30),
    ),
)
def test_universe_assigns_a_share_board_limits(
    code,
    name,
    board,
    limit_pct,
    asof,
):
    security = make_security(code=code, name=name)

    result = filter_universe(
        [security],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert len(result.included) == 1
    assert result.included[0].board == board
    assert result.included[0].limit_pct == limit_pct


def test_main_board_st_keeps_five_percent_rule_when_policy_includes_it(asof):
    security = make_security(name="*ST Example")
    policy = UniversePolicy(
        min_listed_days=60,
        min_avg_turnover_20d=100_000_000.0,
        exclude_st=False,
        exclude_delisting=True,
        require_complete_metadata=True,
    )

    result = filter_universe([security], asof, policy)

    assert result.included[0].board == "main_st"
    assert result.included[0].limit_pct == 0.05


def test_growth_board_st_retains_twenty_percent_rule_when_policy_includes_it(
    asof,
):
    security = make_security(code="SZ.300001", name="*ST Growth")
    policy = UniversePolicy(
        min_listed_days=60,
        min_avg_turnover_20d=100_000_000.0,
        exclude_st=False,
        exclude_delisting=True,
        require_complete_metadata=True,
    )

    result = filter_universe([security], asof, policy)

    assert result.included[0].board == "gem"
    assert result.included[0].limit_pct == 0.20


def test_limit_up_lock_remains_scannable_but_blocks_entry(asof):
    security = make_security(limit_up_locked=True)

    result = filter_universe(
        [security],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert len(result.included) == 1
    assert result.included[0].entry_tradable is False
    assert result.included[0].exit_tradable is True


def test_limit_down_lock_remains_scannable_but_blocks_exit(asof):
    security = make_security(limit_down_locked=True)

    result = filter_universe(
        [security],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert len(result.included) == 1
    assert result.included[0].entry_tradable is True
    assert result.included[0].exit_tradable is False


def test_universe_returns_deterministic_code_order_and_audited_exclusions(asof):
    valid_b = make_security(code="SH.600002", name="B")
    invalid = make_security(code="SH.600003", name="*ST C")
    valid_a = make_security(code="SH.600001", name="A")

    result = filter_universe(
        [valid_b, invalid, valid_a],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert [item.code for item in result.included] == ["SH.600001", "SH.600002"]
    assert [(item.code, item.reason) for item in result.excluded] == [
        ("SH.600003", "st")
    ]


def test_universe_rejects_unsupported_market_and_malformed_code(asof):
    unsupported = make_security(market="hk", code="HK.00700")
    malformed = make_security(code="SH.BAD")

    result = filter_universe(
        [malformed, unsupported],
        asof,
        UniversePolicy.a_share_short_term(),
    )

    assert [item.reason for item in result.excluded] == [
        "unsupported_market",
        "invalid_code",
    ]


def test_universe_requires_timezone_aware_asof():
    with pytest.raises(ValueError, match="timezone-aware"):
        filter_universe(
            [make_security()],
            datetime(2026, 7, 13, 10, 35),
            UniversePolicy.a_share_short_term(),
        )
