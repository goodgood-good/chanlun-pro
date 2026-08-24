from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.incremental_scan import ScanPlan
from cl_app.services.trading_screening import (
    TradingScreeningConfig,
    TradingScreeningService,
    _project_scan_plan_to_configured_scope,
)
from cl_app.services.trading_screening_scope import (
    DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS,
    DEFAULT_VALIDATION_COHORT_SIZE,
    ScreeningScopeAuthorizationError,
    admit_explicit_validation_codes,
    admit_screening_universe,
    configured_screening_allowlist,
    parse_explicit_scope_codes,
    parse_explicit_scope_limit,
    project_configured_screening_codes,
    require_configured_screening_codes,
    validate_screening_scope_configuration,
)


def _codes(start: int, count: int) -> tuple[str, ...]:
    return tuple(f"SZ.{value:06d}" for value in range(start, start + count))


def test_validation_defaults_are_small_with_an_independent_safety_ceiling() -> None:
    assert DEFAULT_VALIDATION_COHORT_SIZE == 12
    assert DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS == 20


def test_admission_caps_deduplicated_union_and_reports_optional_overflow() -> None:
    mandatory = _codes(1, 3)
    signals = (*mandatory[-1:], *_codes(10, 4))
    supportive = _codes(20, 8)
    rechecks = (*mandatory[:1], *_codes(30, 5))

    admission = admit_screening_universe(
        mandatory_codes=mandatory,
        signal_codes=signals,
        supportive_codes=supportive,
        recheck_codes=rechecks,
        max_symbols=12,
    )

    assert len(admission.admitted_codes) == 12
    assert len(set(admission.admitted_codes)) == 12
    assert admission.signal_codes[0] == mandatory[-1]
    assert admission.deferred_supportive_codes == supportive[-3:]
    assert admission.recheck_codes == mandatory[:1]
    assert admission.deferred_recheck_codes == rechecks[1:]


def test_oversized_mandatory_scope_fails_before_optional_admission() -> None:
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="mandatory screening universe has 13 symbols",
    ):
        admit_screening_universe(
            mandatory_codes=_codes(1, 13),
            supportive_codes=_codes(100, 2),
            max_symbols=12,
        )


def test_more_than_twenty_requires_explicit_large_scope_authorization() -> None:
    with pytest.raises(ScreeningScopeAuthorizationError):
        admit_screening_universe(
            supportive_codes=_codes(1, 21),
            max_symbols=21,
        )

    admitted = admit_screening_universe(
        supportive_codes=_codes(1, 21),
        max_symbols=21,
        large_scope_authorized=True,
    )
    assert len(admitted.admitted_codes) == 21


def test_full_market_and_large_scope_are_independent_gates() -> None:
    limits = {
        "symbols": 12,
        "priority": 12,
        "candidate_5m": 12,
        "candidate_30m": 12,
    }
    assert (
        validate_screening_scope_configuration(
            validation_cohort_size=12,
            max_admitted_universe_symbols=20,
            large_scope_authorized=False,
            full_coverage_enabled=False,
            force_full_coverage_until_complete=False,
            per_refresh_limits=limits,
        )
        == 12
    )
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="full-market coverage requires",
    ):
        validate_screening_scope_configuration(
            validation_cohort_size=12,
            max_admitted_universe_symbols=20,
            large_scope_authorized=False,
            full_coverage_enabled=True,
            force_full_coverage_until_complete=False,
            per_refresh_limits=limits,
        )
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="independent full-coverage flag",
    ):
        validate_screening_scope_configuration(
            validation_cohort_size=12,
            max_admitted_universe_symbols=20,
            large_scope_authorized=True,
            full_coverage_enabled=False,
            force_full_coverage_until_complete=True,
            per_refresh_limits=limits,
        )


def test_large_scope_still_requires_an_explicit_bound_for_each_batch() -> None:
    assert (
        validate_screening_scope_configuration(
            validation_cohort_size=12,
            max_admitted_universe_symbols=24,
            large_scope_authorized=True,
            full_coverage_enabled=True,
            force_full_coverage_until_complete=False,
            per_refresh_limits={"candidate_5m": 24},
        )
        == 24
    )
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="batch limits exceed",
    ):
        validate_screening_scope_configuration(
            validation_cohort_size=12,
            max_admitted_universe_symbols=24,
            large_scope_authorized=True,
            full_coverage_enabled=False,
            force_full_coverage_until_complete=False,
            per_refresh_limits={"candidate_5m": 25},
        )


def test_legacy_validation_scope_requires_explicit_codes_and_defaults_to_twelve() -> None:
    assert parse_explicit_scope_codes("SZ.000001, SH.600000\nSZ.000001") == (
        "SZ.000001",
        "SH.600000",
    )
    assert parse_explicit_scope_limit(None) == 12
    with pytest.raises(ValueError, match="必须提供显式标的代码"):
        parse_explicit_scope_codes("  ")
    with pytest.raises(ValueError, match="不接受 all"):
        parse_explicit_scope_codes("all")
    with pytest.raises(ScreeningScopeAuthorizationError):
        admit_explicit_validation_codes(_codes(1, 13))


def test_legacy_validation_scope_can_raise_to_twenty_but_never_above_it() -> None:
    assert len(admit_explicit_validation_codes(_codes(1, 20), max_symbols=20)) == 20
    with pytest.raises(ScreeningScopeAuthorizationError, match="最多允许 20"):
        parse_explicit_scope_limit(21)


def test_configured_allowlist_is_exact_for_bounded_modes_and_ignored_by_full_market(
) -> None:
    admitted = _codes(1, 2)
    candidates = (*admitted, *_codes(10, 2))

    assert configured_screening_allowlist(
        scope_mode="VALIDATION_COHORT",
        admitted_codes=admitted,
    ) == frozenset(admitted)
    assert project_configured_screening_codes(
        candidates,
        scope_mode="VALIDATION_COHORT",
        admitted_codes=admitted,
    ) == admitted
    with pytest.raises(
        ScreeningScopeAuthorizationError,
        match="outside the configured screening allowlist",
    ):
        require_configured_screening_codes(
            candidates,
            scope_mode="VALIDATION_COHORT",
            admitted_codes=admitted,
            subject="mandatory test scope",
        )

    assert configured_screening_allowlist(
        scope_mode="FULL_MARKET",
        admitted_codes=admitted,
    ) is None
    assert project_configured_screening_codes(
        candidates,
        scope_mode="FULL_MARKET",
        admitted_codes=admitted,
    ) == candidates
    assert require_configured_screening_codes(
        candidates,
        scope_mode="FULL_MARKET",
        admitted_codes=admitted,
        subject="full market test scope",
    ) == candidates


def test_configured_allowlist_does_not_shrink_when_runtime_lanes_are_empty(
    tmp_path,
) -> None:
    admitted = _codes(1, 2)
    service = TradingScreeningService(
        market_data=object(),
        sector_catalog=object(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=lambda **_kwargs: ScanPlan((), (), (), False, False),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        notifier=None,
        config=TradingScreeningConfig(admitted_universe_codes=admitted),
    )

    assert service._candidate_monitor_five_universe == ()
    assert service._priority_monitor_last_codes == ()
    assert service.admitted_universe_codes() == admitted


def test_scan_plan_is_projected_to_exact_bounded_allowlist_but_full_market_is_unchanged(
) -> None:
    admitted = _codes(1, 2)
    plan = ScanPlan(
        sectors=("qmt-gics3:test",),
        symbols=_codes(1, 3),
        symbol_frequencies=tuple((code, ("1m", "5m")) for code in _codes(1, 3)),
        full_market_history_scan=True,
        background_full_refresh_required=True,
    )

    bounded = _project_scan_plan_to_configured_scope(
        plan,
        TradingScreeningConfig(admitted_universe_codes=admitted),
    )
    assert bounded.symbols == admitted
    assert tuple(code for code, _ in bounded.symbol_frequencies) == admitted
    assert bounded.full_market_history_scan is False
    assert bounded.background_full_refresh_required is True

    full = _project_scan_plan_to_configured_scope(
        plan,
        TradingScreeningConfig(
            admitted_universe_codes=admitted,
            large_scope_authorized=True,
            full_coverage_refresh_enabled=True,
        ),
    )
    assert full is plan


def test_rule_recheck_seed_cannot_escape_configured_allowlist(tmp_path) -> None:
    admitted = ("SZ.000001",)
    service = TradingScreeningService(
        market_data=object(),
        sector_catalog=object(),
        engine=HumanAssistedDecisionCore(),
        scan_planner=lambda **_kwargs: ScanPlan((), (), (), False, False),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        notifier=None,
        config=TradingScreeningConfig(
            priority_monitoring_enabled=True,
            admitted_universe_codes=admitted,
        ),
    )
    service._seed_decision_rule_recheck(
        {
            "snapshot_content_sha256": "sha256:" + "7" * 64,
            "market_data_as_of": "2026-08-24T10:00:00+08:00",
            "signals": [
                {
                    "code": code,
                    "side": "buy",
                    "point_type": "1buy",
                    "lifecycle_stage": "approaching",
                }
                for code in ("SZ.000001", "SZ.000002")
            ],
        },
        cached_core_id="sha256:" + "6" * 64,
    )

    assert service._decision_rule_recheck_pending_codes == set(admitted)
