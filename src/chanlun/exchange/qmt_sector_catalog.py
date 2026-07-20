from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import (
    Context,
    Decimal,
    DecimalException,
    ROUND_HALF_UP,
    localcontext,
)
import re
from types import MappingProxyType
from typing import Protocol
import unicodedata

import pandas as pd
from xtquant import xtdata

from chanlun.decision_support.fingerprints import (
    normalize_datetime,
    sha256_json,
)
from chanlun.decision_support.opportunity_models import (
    AmbiguousGics3Membership,
    GicsCatalogSnapshot,
    SectorBreadthMetric,
    SectorDefinition,
)
from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK


_CODE_PATTERN = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ)$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_Q6 = Decimal("0.000001")
MINIMUM_QUOTE_COVERAGE_RATIO = Decimal("0.70")
_MAXIMUM_QUOTE_AGE = timedelta(seconds=90)
_QUERY_ERROR = object()
_CALCULATION_CONTEXT = Context(prec=32, rounding=ROUND_HALF_UP)


class BreadthTick(Protocol):
    code: str
    quote_timestamp: datetime
    batch_captured_at: datetime
    last_price: Decimal
    previous_close: Decimal
    session_volume: Decimal
    stock_status: int
    tradable: bool
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class _CapturedSector:
    raw_key: str
    canonical_key: str
    level: str
    normalized_name: str
    response: object


@dataclass(frozen=True, slots=True)
class _CatalogCandidate:
    source_key: str
    normalized_name: str
    parent_id: str
    parent_name: str
    members: frozenset[str]
    local_reasons: frozenset[str]

    @property
    def sector_id(self) -> str:
        return "sector:" + sha256_json(
            {
                "source": "qmt_gics",
                "level": "GICS3",
                "normalized_name": self.normalized_name,
            }
        ).removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class _BreadthStats:
    sector: SectorDefinition
    member_count: int
    usable_quote_count: int
    usable_return_count: int
    advancing_count: int
    coverage_ratio: Decimal
    coverage_score: Decimal
    advance_score: Decimal | None
    median_return: Decimal | None


def _canonical_source_key(raw_key: str) -> tuple[str, str, str] | None:
    text = unicodedata.normalize("NFKC", raw_key)
    text = " ".join(text.strip().split())
    for level in ("GICS1", "GICS3"):
        if text.startswith(level):
            normalized_name = text[len(level) :].strip()
            if normalized_name:
                return text, level, normalized_name
            return None
    return None


def _valid_sector_list(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(type(item) is str for item in value)
    )


def _sector_info_rows(value: object) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(value, pd.DataFrame) or value.empty:
        return None
    columns = list(value.columns)
    if columns.count("sector") != 1 or columns.count("category") != 1:
        return None
    try:
        rows = tuple(
            zip(value["sector"].tolist(), value["category"].tolist())
        )
    except (AttributeError, KeyError, TypeError):
        return None
    if not all(
        type(sector) is str
        and bool(sector.strip())
        and type(category) is str
        and bool(category.strip())
        for sector, category in rows
    ):
        return None
    return tuple(sorted(rows))


def _valid_member_response(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _normalize_members(values: list[str]) -> tuple[set[str], set[str]]:
    members: set[str] = set()
    invalid: set[str] = set()
    for raw_code in values:
        text = unicodedata.normalize("NFKC", raw_code).strip().upper()
        match = _CODE_PATTERN.fullmatch(text)
        if match is None:
            if text:
                invalid.add(text)
            else:
                invalid.add(
                    "invalid-code:"
                    + sha256_json("").removeprefix("sha256:")
                )
            continue
        digits, market = match.groups()
        members.add(f"{market}.{digits}")
    return members, invalid


def _normalized_gics3_collision_keys(
    captures: list[_CapturedSector],
) -> set[str]:
    source_keys_by_name: dict[str, list[str]] = {}
    for capture in captures:
        if capture.level == "GICS3":
            source_keys_by_name.setdefault(
                capture.normalized_name,
                [],
            ).append(capture.canonical_key)
    return {
        source_key
        for source_keys in source_keys_by_name.values()
        if len(source_keys) > 1
        for source_key in source_keys
    }


def _catalog_result(
    *,
    source_service_id: str,
    captured_at: datetime,
    sectors: tuple[SectorDefinition, ...] = (),
    ambiguous: tuple[AmbiguousGics3Membership, ...] = (),
    invalid_codes: set[str] | tuple[str, ...] = (),
    empty_names: set[str] | tuple[str, ...] = (),
    conflicts: set[str] | tuple[str, ...] = (),
    reasons: set[str] | tuple[str, ...],
) -> GicsCatalogSnapshot:
    reason_values = tuple(sorted(set(reasons)))
    return GicsCatalogSnapshot.create(
        source="qmt_gics",
        source_service_id=source_service_id,
        captured_at=captured_at,
        sectors=sectors,
        ambiguous_gics3_memberships=ambiguous,
        invalid_codes=tuple(sorted(set(invalid_codes))),
        empty_sector_names=tuple(sorted(set(empty_names))),
        parent_mapping_conflicts=tuple(sorted(set(conflicts))),
        eligible_for_entry=bool(sectors) and not reason_values,
        reason_codes=reason_values,
    )


class QmtGicsCatalog:
    def snapshot(self, as_of: datetime) -> GicsCatalogSnapshot:
        captured_at = normalize_datetime(as_of, "as_of")
        source_list: object = None
        sector_info: object = None
        captures: list[_CapturedSector] = []

        with _XTDATA_NATIVE_LOCK:
            try:
                source_list = xtdata.get_sector_list()
            except Exception:
                source_list = None
            try:
                sector_info = xtdata.get_sector_info("")
            except Exception:
                sector_info = None

            info_rows = _sector_info_rows(sector_info)
            if _valid_sector_list(source_list) and info_rows is not None:
                selected: list[tuple[str, str, str, str]] = []
                for raw_key in set(source_list):
                    parsed = _canonical_source_key(raw_key)
                    if parsed is None:
                        continue
                    canonical_key, level, normalized_name = parsed
                    selected.append(
                        (raw_key, canonical_key, level, normalized_name)
                    )
                selected.sort(key=lambda item: (item[1], item[0]))
                for raw_key, canonical_key, level, normalized_name in selected:
                    try:
                        response = xtdata.get_stock_list_in_sector(
                            raw_key,
                            real_timetag=-1,
                        )
                    except Exception:
                        response = _QUERY_ERROR
                    captures.append(
                        _CapturedSector(
                            raw_key=raw_key,
                            canonical_key=canonical_key,
                            level=level,
                            normalized_name=normalized_name,
                            response=response,
                        )
                    )

        info_rows = _sector_info_rows(sector_info)
        source_service_id = (
            "xtquant-sector-info:unavailable"
            if info_rows is None
            else "xtquant-sector-info:"
            + sha256_json(info_rows).removeprefix("sha256:")
        )
        if not _valid_sector_list(source_list) or info_rows is None:
            return _catalog_result(
                source_service_id=source_service_id,
                captured_at=captured_at,
                reasons={"sector_source_unavailable"},
            )
        if not captures:
            return _catalog_result(
                source_service_id=source_service_id,
                captured_at=captured_at,
                reasons={"sector_source_unavailable"},
            )

        query_failures = {
            capture.canonical_key
            for capture in captures
            if capture.response is _QUERY_ERROR
            or not _valid_member_response(capture.response)
        }
        colliding_keys = _normalized_gics3_collision_keys(captures)
        failed_gics1 = any(
            capture.level == "GICS1"
            and capture.canonical_key in query_failures
            for capture in captures
        )
        if failed_gics1:
            reasons = {"sector_membership_query_failed"}
            reasons.update(
                f"sector_membership_query_failed:{key}"
                for key in query_failures
            )
            invalid_codes: set[str] = set()
            empty_names: set[str] = set()
            conflicts: set[str] = set(colliding_keys)
            for capture in captures:
                if capture.canonical_key in query_failures:
                    continue
                members, invalid = _normalize_members(capture.response)
                invalid_codes.update(invalid)
                if capture.level == "GICS3" and not members:
                    empty_names.add(capture.canonical_key)
                    conflicts.add(capture.canonical_key)
            if invalid_codes:
                reasons.add("invalid_security_code")
            if empty_names:
                reasons.add("empty_gics3_sector")
            if conflicts:
                reasons.add("parent_mapping_conflict")
            return _catalog_result(
                source_service_id=source_service_id,
                captured_at=captured_at,
                invalid_codes=invalid_codes,
                empty_names=empty_names,
                conflicts=conflicts,
                reasons=reasons,
            )

        reasons: set[str] = set()
        if query_failures:
            reasons.add("sector_membership_query_failed")
            reasons.update(
                f"sector_membership_query_failed:{key}"
                for key in query_failures
            )

        invalid_codes: set[str] = set()
        gics1_members: dict[str, set[str]] = {}
        for capture in captures:
            if capture.level != "GICS1" or capture.canonical_key in query_failures:
                continue
            members, invalid = _normalize_members(capture.response)
            gics1_members.setdefault(capture.canonical_key, set()).update(members)
            invalid_codes.update(invalid)
        parent_union: set[str] = set().union(*gics1_members.values()) if gics1_members else set()

        candidates: list[_CatalogCandidate] = []
        empty_names: set[str] = set()
        conflicts: set[str] = set(colliding_keys)
        preclean_gics3_union: set[str] = set()
        for capture in captures:
            if capture.level != "GICS3" or capture.canonical_key in query_failures:
                continue
            members, malformed = _normalize_members(capture.response)
            preclean_gics3_union.update(members)
            outside_parent = members - parent_union
            filtered_members = members & parent_union
            invalid_codes.update(malformed)
            invalid_codes.update(outside_parent)
            local_reasons: set[str] = set()
            if malformed or outside_parent:
                local_reasons.add("invalid_security_code")
            if not filtered_members:
                empty_names.add(capture.canonical_key)
                conflicts.add(capture.canonical_key)
                continue
            if capture.canonical_key in colliding_keys:
                continue
            parents = [
                parent_id
                for parent_id, parent_members in gics1_members.items()
                if filtered_members <= parent_members
            ]
            if len(parents) != 1:
                conflicts.add(capture.canonical_key)
                continue
            parent_id = parents[0]
            candidates.append(
                _CatalogCandidate(
                    source_key=capture.canonical_key,
                    normalized_name=capture.normalized_name,
                    parent_id=parent_id,
                    parent_name=parent_id[len("GICS1") :].strip(),
                    members=frozenset(filtered_members),
                    local_reasons=frozenset(local_reasons),
                )
            )

        if parent_union != preclean_gics3_union:
            reasons.add("catalog_coverage_mismatch")

        reverse_membership: dict[str, list[str]] = {}
        for candidate in candidates:
            for code in candidate.members:
                reverse_membership.setdefault(code, []).append(candidate.sector_id)
        ambiguous_codes = {
            code for code, sector_ids in reverse_membership.items() if len(set(sector_ids)) > 1
        }
        ambiguous = tuple(
            AmbiguousGics3Membership(
                code=code,
                source_sector_ids=tuple(sorted(set(reverse_membership[code]))),
            )
            for code in sorted(ambiguous_codes)
        )

        sectors: list[SectorDefinition] = []
        for candidate in candidates:
            local_reasons = set(candidate.local_reasons)
            final_members = set(candidate.members) - ambiguous_codes
            if set(candidate.members) & ambiguous_codes:
                local_reasons.add("ambiguous_gics3_membership")
            if not final_members:
                empty_names.add(candidate.source_key)
                local_reasons.add("empty_gics3_sector")
            sectors.append(
                SectorDefinition.create(
                    source="qmt_gics",
                    level="GICS3",
                    name=candidate.normalized_name,
                    normalized_name=candidate.normalized_name,
                    parent_gics1_id=candidate.parent_id,
                    parent_gics1_name=candidate.parent_name,
                    members=tuple(sorted(final_members)),
                    eligible_for_entry=bool(final_members) and not local_reasons,
                    reason_codes=tuple(sorted(local_reasons)),
                )
            )

        if invalid_codes:
            reasons.add("invalid_security_code")
        if conflicts:
            reasons.add("parent_mapping_conflict")
        if ambiguous:
            reasons.add("ambiguous_gics3_membership")
        if empty_names:
            reasons.add("empty_gics3_sector")
        if not sectors and not reasons:
            reasons.add("sector_source_unavailable")

        return _catalog_result(
            source_service_id=source_service_id,
            captured_at=captured_at,
            sectors=tuple(sectors),
            ambiguous=ambiguous,
            invalid_codes=invalid_codes,
            empty_names=empty_names,
            conflicts=conflicts,
            reasons=reasons,
        )


def _q6(value: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT):
        return value.quantize(_Q6)


def _exact_decimal_half(value: Decimal) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("decimal half requires a finite value")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if coefficient % 2:
        coefficient *= 5
        exponent -= 1
    else:
        coefficient //= 2
    result_digits: list[int] = []
    if coefficient == 0:
        result_digits.append(0)
    else:
        while coefficient:
            coefficient, digit = divmod(coefficient, 10)
            result_digits.append(digit)
        result_digits.reverse()
    return Decimal((sign, tuple(result_digits), exponent))


def _decimal_midpoint(lower: Decimal, upper: Decimal) -> Decimal:
    lower_half = _exact_decimal_half(lower)
    upper_half = _exact_decimal_half(upper)
    with localcontext(_CALCULATION_CONTEXT):
        return lower_half + upper_half


def _usable_prices(
    tick: object,
    code: str,
    captured_at: datetime,
) -> tuple[Decimal, Decimal] | None:
    try:
        tick_code = tick.code
        quote_timestamp = tick.quote_timestamp
        batch_captured_at = tick.batch_captured_at
        last_price = tick.last_price
        previous_close = tick.previous_close
        session_volume = tick.session_volume
        stock_status = tick.stock_status
        tradable = tick.tradable
        source_fingerprint = tick.source_fingerprint
        if tick_code != code or type(tick_code) is not str:
            return None
        if not isinstance(batch_captured_at, datetime):
            return None
        if (
            batch_captured_at.tzinfo is None
            or batch_captured_at.utcoffset() is None
        ):
            return None
        if batch_captured_at != captured_at:
            return None
        if not isinstance(quote_timestamp, datetime):
            return None
        if quote_timestamp.tzinfo is None or quote_timestamp.utcoffset() is None:
            return None
        age = captured_at - quote_timestamp
        if age < timedelta(0) or age > _MAXIMUM_QUOTE_AGE:
            return None
        for value in (last_price, previous_close, session_volume):
            if type(value) is not Decimal or not value.is_finite() or value <= 0:
                return None
        if type(stock_status) is not int or stock_status != 3:
            return None
        if tradable is not True:
            return None
        if (
            type(source_fingerprint) is not str
            or _FINGERPRINT_PATTERN.fullmatch(source_fingerprint) is None
        ):
            return None
    except Exception:
        return None
    return last_price, previous_close


def _sector_stats(
    sector: SectorDefinition,
    ticks: Mapping[str, BreadthTick],
    captured_at: datetime,
) -> _BreadthStats:
    raw_returns: list[Decimal] = []
    usable_quote_count = 0
    advancing_count = 0
    with localcontext(_CALCULATION_CONTEXT):
        for code in sector.members:
            tick = ticks.get(code)
            prices = _usable_prices(tick, code, captured_at)
            if prices is None:
                continue
            last_price, previous_close = prices
            usable_quote_count += 1
            try:
                raw_return = (last_price - previous_close) / previous_close
            except DecimalException:
                continue
            raw_returns.append(raw_return)
            if last_price > previous_close:
                advancing_count += 1

        member_count = len(sector.members)
        usable_return_count = len(raw_returns)
        coverage_ratio = (
            Decimal(usable_quote_count) / Decimal(member_count)
            if member_count
            else Decimal("0")
        )
        coverage_score = _q6(coverage_ratio * Decimal("100"))
        advance_score = (
            _q6(
                Decimal(advancing_count)
                / Decimal(usable_return_count)
                * Decimal("100")
            )
            if usable_return_count
            else None
        )
        median_return: Decimal | None = None
        if raw_returns:
            ordered = sorted(raw_returns)
            middle = len(ordered) // 2
            median_return = (
                ordered[middle]
                if len(ordered) % 2
                else _decimal_midpoint(ordered[middle - 1], ordered[middle])
            )
    return _BreadthStats(
        sector=sector,
        member_count=member_count,
        usable_quote_count=usable_quote_count,
        usable_return_count=usable_return_count,
        advancing_count=advancing_count,
        coverage_ratio=coverage_ratio,
        coverage_score=coverage_score,
        advance_score=advance_score,
        median_return=median_return,
    )


def capture_sector_breadth(
    catalog: GicsCatalogSnapshot,
    ticks: Mapping[str, BreadthTick],
    *,
    captured_at: datetime,
) -> Mapping[str, SectorBreadthMetric]:
    if type(catalog) is not GicsCatalogSnapshot:
        raise TypeError("catalog must be GicsCatalogSnapshot")
    if not isinstance(ticks, Mapping):
        raise TypeError("ticks must be a mapping")
    normalized_captured_at = normalize_datetime(captured_at, "captured_at")
    stats = tuple(
        _sector_stats(sector, ticks, normalized_captured_at)
        for sector in sorted(catalog.sectors, key=lambda item: item.sector_id)
    )
    medians = tuple(
        item.median_return for item in stats if item.median_return is not None
    )
    metrics: dict[str, SectorBreadthMetric] = {}
    for item in stats:
        percentile: Decimal | None = None
        breadth_score: Decimal | None = None
        if item.median_return is not None:
            with localcontext(_CALCULATION_CONTEXT):
                less_count = sum(value < item.median_return for value in medians)
                equal_count = sum(value == item.median_return for value in medians)
                percentile = _q6(
                    Decimal("100")
                    * (
                        Decimal(less_count)
                        + Decimal("0.5") * Decimal(equal_count)
                    )
                    / Decimal(len(medians))
                )
                breadth_score = _q6(
                    Decimal("0.50") * item.advance_score
                    + Decimal("0.30") * percentile
                    + Decimal("0.20") * item.coverage_score
                )

        reasons: set[str] = set()
        if not catalog.eligible_for_entry:
            reasons.add("catalog_ineligible")
        if not item.sector.eligible_for_entry:
            reasons.add("catalog_sector_ineligible")
        if item.member_count == 0:
            reasons.add("empty_catalog_sector")
        if item.usable_return_count == 0:
            reasons.add("no_usable_return")
        if item.coverage_ratio < MINIMUM_QUOTE_COVERAGE_RATIO:
            reasons.add("quote_coverage_below_threshold")
        reason_values = tuple(sorted(reasons))
        with localcontext(_CALCULATION_CONTEXT):
            metric = SectorBreadthMetric(
                sector_id=item.sector.sector_id,
                captured_at=normalized_captured_at,
                catalog_member_count=item.member_count,
                usable_quote_count=item.usable_quote_count,
                usable_return_count=item.usable_return_count,
                advancing_count=item.advancing_count,
                missing_count=item.member_count - item.usable_quote_count,
                quote_coverage_ratio=item.coverage_ratio,
                quote_coverage_score=item.coverage_score,
                advance_ratio_score=item.advance_score,
                median_return_percentile=percentile,
                breadth_score=breadth_score,
                eligible_for_entry=not reason_values,
                reason_codes=reason_values,
            )
        metrics[metric.sector_id] = metric
    return MappingProxyType(metrics)
