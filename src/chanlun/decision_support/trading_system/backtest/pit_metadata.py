"""Point-in-time metadata used by the certified fixed-year replay.

The module deliberately separates enumeration from admissibility.  A security
may be discovered from a contract master captured after the requested period,
but it cannot participate before its listing date or after its expiry date.
Likewise, an industry change is usable only after the source change date has
finished.  This makes a later, survivor-free archive safe to replay without
letting its future rows affect an earlier prefix.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.models import (
    CorporateActionAt,
)


CN = ZoneInfo("Asia/Shanghai")
PIT_METADATA_SCHEMA = "chanlun-qmt-pit-metadata"
LEGACY_PIT_METADATA_SCHEMAS = frozenset({"chanlun-qmt-pit-metadata/v1"})
SW_STANDARD_CODE = "008003"
_NATIVE_CODE = re.compile(r"^(?P<digits>[0-9]{6})\.(?P<market>SH|SZ|BJ)$")
_NORMALIZED_CODE = re.compile(r"^(?P<market>SH|SZ|BJ)\.(?P<digits>[0-9]{6})$")
_SW_CODE = re.compile(r"^S(?P<level_one>[0-9]{2})[0-9]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def normalize_qmt_a_share_code(value: str) -> str:
    match = _NATIVE_CODE.fullmatch(value.strip().upper())
    if match is None:
        raise ValueError(f"invalid QMT A-share code: {value!r}")
    return f"{match.group('market')}.{match.group('digits')}"


def qmt_native_code(value: str) -> str:
    match = _NORMALIZED_CODE.fullmatch(value.strip().upper())
    if match is None:
        raise ValueError(f"invalid normalized A-share code: {value!r}")
    return f"{match.group('digits')}.{match.group('market')}"


def _iso_date(value: object, label: str) -> date:
    text = str(value)
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc


def _known_after_session(session: date) -> datetime:
    """Use a source date only after that calendar day has completed."""

    return datetime.combine(session + timedelta(days=1), time.min, tzinfo=CN)


def sw1_sector_id(industry_code: str) -> str:
    match = _SW_CODE.fullmatch(industry_code.strip().upper())
    if match is None:
        raise ValueError(f"invalid SW industry code: {industry_code!r}")
    return f"qmt-sw1:S{match.group('level_one')}"


@dataclass(frozen=True, slots=True)
class SecurityMasterRecord:
    code: str
    name: str
    listed_from: date
    listed_through: date | None

    def __post_init__(self) -> None:
        if _NORMALIZED_CODE.fullmatch(self.code) is None:
            raise ValueError("security master code must be normalized")
        if not self.name.strip():
            raise ValueError("security master name is required")
        if self.listed_from < date(1990, 1, 1):
            raise ValueError("security listing date contains a pre-market sentinel")
        if self.listed_through is not None and self.listed_through < self.listed_from:
            raise ValueError("security expiry precedes listing")

    def listed_on(self, session: date) -> bool:
        return self.listed_from <= session and (
            self.listed_through is None or session <= self.listed_through
        )

    def intersects(self, start: date, end: date) -> bool:
        return self.listed_from <= end and (
            self.listed_through is None or self.listed_through >= start
        )

@dataclass(frozen=True, slots=True)
class SectorMembershipChange:
    code: str
    sector_id: str
    sector_name: str
    industry_code: str
    source_changed_on: date
    known_at: datetime

    def __post_init__(self) -> None:
        if _NORMALIZED_CODE.fullmatch(self.code) is None:
            raise ValueError("membership code must be normalized")
        if self.sector_id != sw1_sector_id(self.industry_code):
            raise ValueError("membership sector does not match industry code")
        if not self.sector_name.strip():
            raise ValueError("membership sector name is required")
        known = normalize_datetime(self.known_at, "known_at")
        if known < _known_after_session(self.source_changed_on):
            raise ValueError("membership became known before its source day closed")
        object.__setattr__(self, "known_at", known)


@dataclass(frozen=True, slots=True)
class QmtFactorAt:
    code: str
    effective_on: date
    interest: Decimal
    stock_bonus: Decimal
    stock_gift: Decimal
    allot_num: Decimal
    allot_price: Decimal
    gugai: Decimal
    raw_price_divisor: Decimal

    def __post_init__(self) -> None:
        if _NORMALIZED_CODE.fullmatch(self.code) is None:
            raise ValueError("factor code must be normalized")
        non_negative = (
            self.interest,
            self.stock_bonus,
            self.stock_gift,
            self.allot_num,
            self.allot_price,
            self.gugai,
        )
        if any(value < 0 or not value.is_finite() for value in non_negative):
            raise ValueError("QMT factor economics must be finite and non-negative")
        if not self.raw_price_divisor.is_finite() or self.raw_price_divisor <= 0:
            raise ValueError("QMT raw price divisor must be positive")

    @property
    def effective_at(self) -> datetime:
        return datetime.combine(self.effective_on, time(9, 30), tzinfo=CN)

    @property
    def share_multiplier(self) -> Decimal:
        return Decimal("1") + self.stock_bonus + self.stock_gift + self.allot_num

    def corporate_action(self) -> CorporateActionAt:
        if self.allot_num > 0:
            action_type = "rights"
        elif self.share_multiplier != Decimal("1"):
            action_type = "split"
        else:
            action_type = "cash_dividend"
        return CorporateActionAt(
            code=self.code,
            effective_at=self.effective_at,
    # 除权日经济信息会在开盘集合竞价前公开；使用集合竞价边界是保守做法，
    # 且绝不会改变前一交易日状态。
            known_at=self.effective_at,
            action_type=action_type,
            cash_per_share=self.interest,
            share_multiplier=self.share_multiplier,
            subscription_cost_per_share=self.allot_num * self.allot_price,
            raw_price_divisor=self.raw_price_divisor,
        )


def membership_changes_from_cninfo(
    *,
    code: str,
    records: Sequence[Mapping[str, object]],
    not_after: date,
) -> tuple[SectorMembershipChange, ...]:
    """Parse only effective SW rows; other standards are never strategy inputs."""

    normalized = code.strip().upper()
    if _NORMALIZED_CODE.fullmatch(normalized) is None:
        raise ValueError("CNInfo membership code must be normalized")
    output: list[SectorMembershipChange] = []
    seen: dict[date, tuple[str, str]] = {}
    for raw in records:
        if str(raw.get("F001V") or raw.get("classification_standard_code") or "") != SW_STANDARD_CODE:
            continue
        raw_date = raw.get("VARYDATE", raw.get("changed_on"))
        try:
            changed_on = date.fromisoformat(str(raw_date)[:10])
        except ValueError as exc:
            raise ValueError(f"invalid CNInfo change date for {normalized}") from exc
        if changed_on > not_after:
            continue
        industry_code = str(
            raw.get("F003V") or raw.get("industry_code") or ""
        ).strip().upper()
        sector_name = str(
            raw.get("F004V") or raw.get("sector_name") or ""
        ).strip()
        identity = (industry_code, sector_name)
        prior = seen.get(changed_on)
        if prior is not None and prior != identity:
            raise ValueError(
                f"conflicting CNInfo SW memberships for {normalized} on {changed_on}"
            )
        seen[changed_on] = identity
        output.append(
            SectorMembershipChange(
                code=normalized,
                sector_id=sw1_sector_id(industry_code),
                sector_name=sector_name,
                industry_code=industry_code,
                source_changed_on=changed_on,
                known_at=_known_after_session(changed_on),
            )
        )
    return tuple(
        sorted(
            set(output),
            key=lambda row: (row.known_at, row.sector_id, row.industry_code),
        )
    )


def qmt_factors_from_rows(
    *,
    code: str,
    rows: Sequence[Mapping[str, object]],
    not_before: date,
    not_after: date,
) -> tuple[QmtFactorAt, ...]:
    normalized = code.strip().upper()
    if _NORMALIZED_CODE.fullmatch(normalized) is None:
        raise ValueError("factor code must be normalized")
    output: list[QmtFactorAt] = []
    for raw in rows:
        raw_date = raw.get("effective_on", raw.get("date"))
        effective_on = _iso_date(raw_date, "factor effective date")
        if not not_before <= effective_on <= not_after:
            continue
        output.append(
            QmtFactorAt(
                code=normalized,
                effective_on=effective_on,
                interest=_decimal(raw.get("interest", 0), "interest"),
                stock_bonus=_decimal(raw.get("stockBonus", 0), "stockBonus"),
                stock_gift=_decimal(raw.get("stockGift", 0), "stockGift"),
                allot_num=_decimal(raw.get("allotNum", 0), "allotNum"),
                allot_price=_decimal(raw.get("allotPrice", 0), "allotPrice"),
                gugai=_decimal(raw.get("gugai", 0), "gugai"),
                raw_price_divisor=_decimal(raw.get("dr"), "dr"),
            )
        )
    keys = tuple((row.code, row.effective_on) for row in output)
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate QMT factor date for {normalized}")
    return tuple(sorted(output, key=lambda row: row.effective_on))


@dataclass(frozen=True, slots=True)
class PITMetadataSnapshot:
    source_start: date
    source_end: date
    captured_at: datetime
    securities: tuple[SecurityMasterRecord, ...]
    memberships: tuple[SectorMembershipChange, ...]
    factors: tuple[QmtFactorAt, ...]
    qmt_sw1_sector_names: tuple[tuple[str, str], ...]
    source_hashes: tuple[tuple[str, str], ...]
    schema: str = PIT_METADATA_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PIT_METADATA_SCHEMA:
            raise ValueError("unsupported PIT metadata schema")
        if self.source_start > self.source_end:
            raise ValueError("PIT source range is inverted")
        captured = normalize_datetime(self.captured_at, "captured_at")
        object.__setattr__(self, "captured_at", captured)
        codes = tuple(row.code for row in self.securities)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("security master must be unique and sorted")
        known_codes = set(codes)
        if any(row.code not in known_codes for row in self.memberships):
            raise ValueError("membership references an unknown security")
        if any(row.code not in known_codes for row in self.factors):
            raise ValueError("factor references an unknown security")
        membership_order = tuple(
            (row.code, row.known_at, row.sector_id) for row in self.memberships
        )
        if membership_order != tuple(sorted(set(membership_order))):
            raise ValueError("memberships must be unique and sorted")
        factor_order = tuple((row.code, row.effective_on) for row in self.factors)
        if factor_order != tuple(sorted(set(factor_order))):
            raise ValueError("factors must be unique and sorted")
        sector_ids = tuple(sector_id for sector_id, _name in self.qmt_sw1_sector_names)
        if sector_ids != tuple(sorted(set(sector_ids))):
            raise ValueError("QMT SW1 sectors must be unique and sorted")
        hash_names = tuple(name for name, _digest in self.source_hashes)
        if hash_names != tuple(sorted(set(hash_names))):
            raise ValueError("source hashes must be unique and sorted")
        if any(_SHA256.fullmatch(digest) is None for _name, digest in self.source_hashes):
            raise ValueError("PIT source hashes must be sha256 fingerprints")

    def security(self, code: str) -> SecurityMasterRecord:
        matches = tuple(row for row in self.securities if row.code == code)
        if len(matches) != 1:
            raise KeyError(f"security master row is not unique: {code}")
        return matches[0]

    def memberships_for(self, code: str) -> tuple[SectorMembershipChange, ...]:
        return tuple(row for row in self.memberships if row.code == code)

    def factors_for(self, code: str) -> tuple[QmtFactorAt, ...]:
        return tuple(row for row in self.factors if row.code == code)

    def membership_at(
        self,
        code: str,
        observed_at: datetime,
    ) -> SectorMembershipChange | None:
        observed = normalize_datetime(observed_at, "observed_at")
        master = self.security(code)
        if not master.listed_on(observed.date()):
            return None
        available = tuple(
            row
            for row in self.memberships_for(code)
            if row.known_at <= observed
        )
        return None if not available else available[-1]


class PITMetadataIndex:
    """O(1) identity and O(log n) effective-date lookup for one snapshot."""

    def __init__(self, snapshot: PITMetadataSnapshot) -> None:
        self.snapshot = snapshot
        self._securities = {row.code: row for row in snapshot.securities}
        memberships: dict[str, list[SectorMembershipChange]] = defaultdict(list)
        factors: dict[str, list[QmtFactorAt]] = defaultdict(list)
        for row in snapshot.memberships:
            memberships[row.code].append(row)
        for row in snapshot.factors:
            factors[row.code].append(row)
        self._memberships = {
            code: tuple(rows) for code, rows in memberships.items()
        }
        self._membership_times = {
            code: tuple(row.known_at for row in rows)
            for code, rows in self._memberships.items()
        }
        self._factors = {code: tuple(rows) for code, rows in factors.items()}

    def security(self, code: str) -> SecurityMasterRecord:
        try:
            return self._securities[code]
        except KeyError as exc:
            raise KeyError(f"security master row is unavailable: {code}") from exc

    def memberships_for(self, code: str) -> tuple[SectorMembershipChange, ...]:
        return self._memberships.get(code, ())

    def factors_for(self, code: str) -> tuple[QmtFactorAt, ...]:
        return self._factors.get(code, ())

    def membership_at(
        self,
        code: str,
        observed_at: datetime,
    ) -> SectorMembershipChange | None:
        observed = normalize_datetime(observed_at, "observed_at")
        master = self.security(code)
        if not master.listed_on(observed.date()):
            return None
        rows = self._memberships.get(code, ())
        position = bisect_right(self._membership_times.get(code, ()), observed)
        return None if position == 0 else rows[position - 1]


def _json_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _master_payload(row: SecurityMasterRecord) -> dict[str, object]:
    return {
        "code": row.code,
        "name": row.name,
        "listed_from": row.listed_from.isoformat(),
        "listed_through": (
            None if row.listed_through is None else row.listed_through.isoformat()
        ),
    }


def _membership_payload(row: SectorMembershipChange) -> dict[str, object]:
    return {
        "code": row.code,
        "sector_id": row.sector_id,
        "sector_name": row.sector_name,
        "industry_code": row.industry_code,
        "source_changed_on": row.source_changed_on.isoformat(),
        "known_at": row.known_at.isoformat(),
    }


def _factor_payload(row: QmtFactorAt) -> dict[str, object]:
    return {
        "code": row.code,
        "effective_on": row.effective_on.isoformat(),
        "interest": str(row.interest),
        "stock_bonus": str(row.stock_bonus),
        "stock_gift": str(row.stock_gift),
        "allot_num": str(row.allot_num),
        "allot_price": str(row.allot_price),
        "gugai": str(row.gugai),
        "raw_price_divisor": str(row.raw_price_divisor),
    }


def snapshot_payload(
    snapshot: PITMetadataSnapshot,
    *,
    audit: Mapping[str, object] | None = None,
) -> dict[str, object]:
    core: dict[str, object] = {
        "schema": snapshot.schema,
        "source_start": snapshot.source_start.isoformat(),
        "source_end": snapshot.source_end.isoformat(),
        "captured_at": snapshot.captured_at.isoformat(),
        "securities": [_master_payload(row) for row in snapshot.securities],
        "memberships": [_membership_payload(row) for row in snapshot.memberships],
        "factors": [_factor_payload(row) for row in snapshot.factors],
        "qmt_sw1_sector_names": [
            {"sector_id": sector_id, "name": name}
            for sector_id, name in snapshot.qmt_sw1_sector_names
        ],
        "source_hashes": [
            {"name": name, "sha256": digest}
            for name, digest in snapshot.source_hashes
        ],
    }
    core["content_sha256"] = _json_hash(core)
    if audit is not None:
        core["audit"] = dict(audit)
    return core


def snapshot_from_payload(payload: Mapping[str, object]) -> PITMetadataSnapshot:
    expected_hash = payload.get("content_sha256")
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "audit"}
    }
    if not isinstance(expected_hash, str) or _json_hash(canonical) != expected_hash:
        raise ValueError("PIT metadata content hash mismatch")
    source_schema = str(payload.get("schema") or "")
    if source_schema not in {PIT_METADATA_SCHEMA, *LEGACY_PIT_METADATA_SCHEMAS}:
        raise ValueError("unsupported PIT metadata schema")
    raw_securities = payload.get("securities")
    raw_memberships = payload.get("memberships")
    raw_factors = payload.get("factors")
    raw_sectors = payload.get("qmt_sw1_sector_names")
    raw_hashes = payload.get("source_hashes")
    if any(
        not isinstance(value, list)
        for value in (
            raw_securities,
            raw_memberships,
            raw_factors,
            raw_sectors,
            raw_hashes,
        )
    ):
        raise ValueError("PIT metadata arrays are malformed")
    securities = tuple(
        SecurityMasterRecord(
            code=str(row["code"]),
            name=str(row["name"]),
            listed_from=_iso_date(row["listed_from"], "listed_from"),
            listed_through=(
                None
                if row.get("listed_through") is None
                else _iso_date(row["listed_through"], "listed_through")
            ),
        )
        for row in raw_securities
        if isinstance(row, Mapping)
    )
    memberships = tuple(
        SectorMembershipChange(
            code=str(row["code"]),
            sector_id=str(row["sector_id"]),
            sector_name=str(row["sector_name"]),
            industry_code=str(row["industry_code"]),
            source_changed_on=_iso_date(
                row["source_changed_on"], "source_changed_on"
            ),
            known_at=datetime.fromisoformat(str(row["known_at"])),
        )
        for row in raw_memberships
        if isinstance(row, Mapping)
    )
    factors = tuple(
        QmtFactorAt(
            code=str(row["code"]),
            effective_on=_iso_date(row["effective_on"], "effective_on"),
            interest=_decimal(row["interest"], "interest"),
            stock_bonus=_decimal(row["stock_bonus"], "stock_bonus"),
            stock_gift=_decimal(row["stock_gift"], "stock_gift"),
            allot_num=_decimal(row["allot_num"], "allot_num"),
            allot_price=_decimal(row["allot_price"], "allot_price"),
            gugai=_decimal(row["gugai"], "gugai"),
            raw_price_divisor=_decimal(
                row["raw_price_divisor"], "raw_price_divisor"
            ),
        )
        for row in raw_factors
        if isinstance(row, Mapping)
    )
    sectors = tuple(
        (str(row["sector_id"]), str(row["name"]))
        for row in raw_sectors
        if isinstance(row, Mapping)
    )
    hashes = tuple(
        (str(row["name"]), str(row["sha256"]))
        for row in raw_hashes
        if isinstance(row, Mapping)
    )
    return PITMetadataSnapshot(
        # v1 used the same content contract and was already bound by the hash
        # above.  Normalize only the in-memory marker; never rewrite the
        # immutable source artifact used by the audit report.
        schema=PIT_METADATA_SCHEMA,
        source_start=_iso_date(payload.get("source_start"), "source_start"),
        source_end=_iso_date(payload.get("source_end"), "source_end"),
        captured_at=datetime.fromisoformat(str(payload.get("captured_at"))),
        securities=securities,
        memberships=memberships,
        factors=factors,
        qmt_sw1_sector_names=sectors,
        source_hashes=hashes,
    )


def load_snapshot(path: Path) -> PITMetadataSnapshot:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("PIT metadata file must contain a JSON object")
    return snapshot_from_payload(raw)


def sha256_json(value: object) -> str:
    return _json_hash(value)


__all__ = (
    "LEGACY_PIT_METADATA_SCHEMAS",
    "CN",
    "PIT_METADATA_SCHEMA",
    "PITMetadataSnapshot",
    "PITMetadataIndex",
    "QmtFactorAt",
    "SW_STANDARD_CODE",
    "SecurityMasterRecord",
    "SectorMembershipChange",
    "load_snapshot",
    "membership_changes_from_cninfo",
    "normalize_qmt_a_share_code",
    "qmt_factors_from_rows",
    "qmt_native_code",
    "sha256_json",
    "snapshot_from_payload",
    "snapshot_payload",
    "sw1_sector_id",
)
