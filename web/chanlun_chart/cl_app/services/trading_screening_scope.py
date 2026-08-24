"""Fail-closed scope admission for Web-owned screening work.

The validation profile deliberately keeps code-change feedback loops small.  A
larger monitor universe and full-market coverage are separate operational
decisions: neither may be inferred from a generous batch-size setting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re


DEFAULT_VALIDATION_COHORT_SIZE = 12
DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS = 20
LARGE_SCOPE_THRESHOLD = 20


class ScreeningScopeAuthorizationError(ValueError):
    """Raised before market-data work when a requested scope is unauthorized."""

    reason_code = "SCREENING_LARGE_SCOPE_AUTHORIZATION_REQUIRED"


_EXPLICIT_CODE_SPLIT_RE = re.compile(r"[\s,，;；]+")
_FORBIDDEN_EXPLICIT_SCOPE_SENTINELS = frozenset({"*", "all"})
_SCREENING_SCOPE_MODES = frozenset(
    {"VALIDATION_COHORT", "LARGE_SCOPE", "FULL_MARKET"}
)


@dataclass(frozen=True, slots=True)
class ScreeningUniverseAdmission:
    """A deterministic, tier-preserving monitor-universe admission result."""

    mandatory_codes: tuple[str, ...]
    signal_codes: tuple[str, ...]
    supportive_codes: tuple[str, ...]
    recheck_codes: tuple[str, ...]
    deferred_signal_codes: tuple[str, ...]
    deferred_supportive_codes: tuple[str, ...]
    deferred_recheck_codes: tuple[str, ...]
    admitted_codes: tuple[str, ...]


def _unique_codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def configured_screening_allowlist(
    *,
    scope_mode: str,
    admitted_codes: Iterable[str] = (),
) -> frozenset[str] | None:
    """Return the exact configured allowlist for one bounded process.

    ``None`` deliberately means that no exact allowlist applies.  FULL_MARKET
    always keeps its historical unbounded semantics, while an empty bounded
    list remains available to low-level callers that only exercise the numeric
    admission ceiling.  Production validation startup supplies a non-empty
    explicit cohort.
    """

    if scope_mode not in _SCREENING_SCOPE_MODES:
        raise ValueError("screening scope_mode is invalid")
    if scope_mode == "FULL_MARKET":
        return None
    codes = _unique_codes(admitted_codes)
    return frozenset(codes) if codes else None


def project_configured_screening_codes(
    values: Iterable[str],
    *,
    scope_mode: str,
    admitted_codes: Iterable[str] = (),
) -> tuple[str, ...]:
    """Project optional work onto the exact configured bounded allowlist."""

    codes = _unique_codes(values)
    allowlist = configured_screening_allowlist(
        scope_mode=scope_mode,
        admitted_codes=admitted_codes,
    )
    if allowlist is None:
        return codes
    return tuple(code for code in codes if code in allowlist)


def require_configured_screening_codes(
    values: Iterable[str],
    *,
    scope_mode: str,
    admitted_codes: Iterable[str] = (),
    subject: str,
) -> tuple[str, ...]:
    """Fail before I/O when mandatory work escapes a configured allowlist."""

    codes = _unique_codes(values)
    allowlist = configured_screening_allowlist(
        scope_mode=scope_mode,
        admitted_codes=admitted_codes,
    )
    if allowlist is None:
        return codes
    rejected = tuple(code for code in codes if code not in allowlist)
    if rejected:
        raise ScreeningScopeAuthorizationError(
            f"{subject} contains codes outside the configured screening allowlist: "
            + ", ".join(rejected)
        )
    return codes


def parse_explicit_scope_codes(values: object) -> tuple[str, ...]:
    """Parse a user-supplied code list without ever expanding a market/group.

    Strings may use whitespace or Chinese/ASCII comma/semicolon separators.
    The ``all``/``*`` sentinels are rejected deliberately: callers of this
    helper are validation-only legacy entry points, not full-market runners.
    """

    if values is None:
        raw_values: tuple[object, ...] = ()
    elif isinstance(values, str):
        raw_values = (values,)
    elif isinstance(values, Iterable):
        raw_values = tuple(values)
    else:
        raise ValueError("codes must be a string or list")

    parsed: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ValueError("each code must be a string")
        parsed.extend(
            segment.strip()
            for segment in _EXPLICIT_CODE_SPLIT_RE.split(raw_value)
            if segment.strip()
        )
    codes = _unique_codes(parsed)
    if not codes:
        raise ValueError("必须提供显式标的代码；不会自动展开全市场或自选组")
    if any(code.casefold() in _FORBIDDEN_EXPLICIT_SCOPE_SENTINELS for code in codes):
        raise ValueError("codes 不接受 all/*；必须逐项提供显式标的代码")
    return codes


def parse_explicit_scope_limit(value: object) -> int:
    """Resolve a legacy validation-entry limit (default 12, hard cap 20)."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_VALIDATION_COHORT_SIZE
    if isinstance(value, bool):
        raise ValueError("scope_limit must be an integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("scope_limit must be an integer") from exc
    if limit <= 0:
        raise ValueError("scope_limit must be positive")
    if limit > DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS:
        raise ScreeningScopeAuthorizationError(
            f"普通 Web 入口最多允许 {DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS} 只显式标的"
        )
    return limit


def admit_explicit_validation_codes(
    values: object,
    *,
    max_symbols: int = DEFAULT_VALIDATION_COHORT_SIZE,
) -> tuple[str, ...]:
    """Admit an explicit validation cohort with no implicit universe expansion."""

    limit = parse_explicit_scope_limit(max_symbols)
    codes = parse_explicit_scope_codes(values)
    return admit_screening_universe(
        mandatory_codes=codes,
        max_symbols=limit,
        large_scope_authorized=False,
    ).admitted_codes


def admit_screening_universe(
    *,
    mandatory_codes: Iterable[str] = (),
    signal_codes: Iterable[str] = (),
    supportive_codes: Iterable[str] = (),
    recheck_codes: Iterable[str] = (),
    max_symbols: int = DEFAULT_VALIDATION_COHORT_SIZE,
    large_scope_authorized: bool = False,
) -> ScreeningUniverseAdmission:
    """Admit one deduplicated Web monitor universe without exceeding its limit.

    Mandatory holdings/watchlist symbols are never silently dropped.  Optional
    signal, supportive-sector and rule-recheck tiers are admitted in that order;
    overflow remains explicitly deferred for a later authorized run.
    """

    if isinstance(max_symbols, bool) or not isinstance(max_symbols, int):
        raise TypeError("screening universe limit must be an integer")
    if max_symbols <= 0:
        raise ValueError("screening universe limit must be positive")
    if max_symbols > LARGE_SCOPE_THRESHOLD and not large_scope_authorized:
        raise ScreeningScopeAuthorizationError(
            f"screening universe limit {max_symbols} exceeds the safe maximum "
            f"{LARGE_SCOPE_THRESHOLD}; explicit large-scope authorization is required"
        )

    mandatory = _unique_codes(mandatory_codes)
    if len(mandatory) > max_symbols:
        raise ScreeningScopeAuthorizationError(
            f"mandatory screening universe has {len(mandatory)} symbols, exceeding "
            f"the authorized limit {max_symbols}"
        )

    admitted = list(mandatory)
    admitted_set = set(mandatory)

    def admit_optional(
        values: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        tier = _unique_codes(values)
        accepted: list[str] = []
        deferred: list[str] = []
        for code in tier:
            if code in admitted_set:
                # An overlapping tier is already covered and consumes no extra slot.
                accepted.append(code)
                continue
            if len(admitted) >= max_symbols:
                deferred.append(code)
                continue
            admitted.append(code)
            admitted_set.add(code)
            accepted.append(code)
        return tuple(accepted), tuple(deferred)

    admitted_signals, deferred_signals = admit_optional(signal_codes)
    admitted_supportive, deferred_supportive = admit_optional(supportive_codes)
    admitted_rechecks, deferred_rechecks = admit_optional(recheck_codes)
    return ScreeningUniverseAdmission(
        mandatory_codes=mandatory,
        signal_codes=admitted_signals,
        supportive_codes=admitted_supportive,
        recheck_codes=admitted_rechecks,
        deferred_signal_codes=deferred_signals,
        deferred_supportive_codes=deferred_supportive,
        deferred_recheck_codes=deferred_rechecks,
        admitted_codes=tuple(admitted),
    )


def validate_screening_scope_configuration(
    *,
    validation_cohort_size: int,
    max_admitted_universe_symbols: int,
    large_scope_authorized: bool,
    full_coverage_enabled: bool,
    force_full_coverage_until_complete: bool,
    per_refresh_limits: Mapping[str, int],
) -> int:
    """Validate independent small/large/full scope gates and return monitor limit."""

    scalar_limits = {
        "validation_cohort_size": validation_cohort_size,
        "max_admitted_universe_symbols": max_admitted_universe_symbols,
        **dict(per_refresh_limits),
    }
    for label, value in scalar_limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
    if validation_cohort_size > max_admitted_universe_symbols:
        raise ValueError(
            "validation_cohort_size cannot exceed max_admitted_universe_symbols"
        )
    if force_full_coverage_until_complete and not full_coverage_enabled:
        raise ScreeningScopeAuthorizationError(
            "forced full coverage requires the independent full-coverage flag"
        )
    if full_coverage_enabled and not large_scope_authorized:
        raise ScreeningScopeAuthorizationError(
            "full-market coverage requires explicit large-scope authorization"
        )
    if (
        max_admitted_universe_symbols > LARGE_SCOPE_THRESHOLD
        and not large_scope_authorized
    ):
        raise ScreeningScopeAuthorizationError(
            f"monitor universe limit {max_admitted_universe_symbols} exceeds the safe "
            f"maximum {LARGE_SCOPE_THRESHOLD}"
        )

    effective_limit = (
        max_admitted_universe_symbols
        if large_scope_authorized
        else validation_cohort_size
    )
    oversized = {
        label: value
        for label, value in per_refresh_limits.items()
        if value > effective_limit
    }
    if oversized:
        rendered = ", ".join(
            f"{label}={value}" for label, value in sorted(oversized.items())
        )
        raise ScreeningScopeAuthorizationError(
            f"screening batch limits exceed the authorized universe "
            f"{effective_limit}: {rendered}"
        )
    return effective_limit


__all__ = (
    "DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS",
    "DEFAULT_VALIDATION_COHORT_SIZE",
    "LARGE_SCOPE_THRESHOLD",
    "ScreeningScopeAuthorizationError",
    "ScreeningUniverseAdmission",
    "admit_explicit_validation_codes",
    "admit_screening_universe",
    "configured_screening_allowlist",
    "parse_explicit_scope_codes",
    "parse_explicit_scope_limit",
    "project_configured_screening_codes",
    "require_configured_screening_codes",
    "validate_screening_scope_configuration",
)
