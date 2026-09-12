"""Bound explicit symbol imports and manual chart-cache requests."""
from collections.abc import Iterable
import re

DEFAULT_VALIDATION_COHORT_SIZE = 12
DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS = 20
_EXPLICIT_CODE_SPLIT_RE = re.compile(r"[\s,，;；]+")
_FORBIDDEN_EXPLICIT_SCOPE_SENTINELS = frozenset({"*", "all"})

class SymbolScopeError(ValueError):
    reason_code = "SYMBOL_SCOPE_LIMIT_EXCEEDED"

def _unique_codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )

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
        raise SymbolScopeError(
            f"普通 Web 入口最多允许 {DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS} 只显式标的"
        )
    return limit

def admit_explicit_validation_codes(values, *, max_symbols=DEFAULT_VALIDATION_COHORT_SIZE):
    limit = parse_explicit_scope_limit(max_symbols)
    codes = parse_explicit_scope_codes(values)
    if len(codes) > limit:
        raise SymbolScopeError(f"一次最多处理 {limit} 只显式标的")
    return codes
