"""Immutable point-in-time ledger for QMT sector catalog captures.

QMT exposes a ``real_timetag`` argument for sector membership, but a provider
argument is not itself historical evidence.  The strategy therefore treats
each QMT catalog as current-only until it has actually been captured and
hash-chained.  A stored capture may be replayed only on its capture session and
only after ``captured_at``; it is never backfilled into an earlier decision.

The module also contains the pure audit used to detect a particularly strong
form of current-constituent backfill: a supposedly historical member whose
official listing date is later than the requested as-of date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.file_lock import interprocess_file_lock


QMT_SECTOR_LEDGER_SCHEMA = "chanlun-qmt-sector-capture-ledger"
QMT_SECTOR_ENTRY_SCHEMA = "chanlun-qmt-sector-capture"
QMT_HISTORICAL_AUDIT_SCHEMA = "chanlun-qmt-sector-history-audit"
QMT_SECTOR_RECEIPT_AUDIT_SCHEMA = "chanlun-qmt-sector-receipt-audit"
QMT_SECTOR_RECEIPT_SCHEMA = "chanlun-qmt-sector-daily-capture-receipt"
QMT_FORWARD_CAPTURE_READINESS_SCHEMA = (
    "chanlun-forward-sector-capture-readiness"
)
_CATALOG_SCHEMA_SOURCE = "qmt_gics3_components"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_A_SHARE = re.compile(r"^(SH|SZ|BJ)\.[0-9]{6}$")


def _require_sha256(value: object, label: str) -> str:
    text = str(value)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{label} must be a sha256 identity")
    return text


def _canonical_sectors(catalog: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw = catalog.get("sectors")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("QMT sector catalog is empty")
    sectors: list[dict[str, object]] = []
    identities: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("QMT sector catalog row is invalid")
        sector_id = str(item.get("sector_id") or "")
        name = str(item.get("name") or "").strip()
        source_key = str(item.get("source_key") or "").strip()
        members = item.get("member_codes")
        if not sector_id or not name or not source_key:
            raise ValueError("QMT sector identity is incomplete")
        if sector_id in identities:
            raise ValueError("QMT sector identities must be unique")
        if not isinstance(members, (list, tuple)) or any(
            not isinstance(value, str) or _A_SHARE.fullmatch(value) is None
            for value in members
        ):
            raise ValueError("QMT sector members must be normalized A-share codes")
        canonical_members = tuple(sorted(set(members)))
        if len(canonical_members) != len(members):
            raise ValueError("QMT sector members must be unique")
        identities.add(sector_id)
        sectors.append(
            {
                "sector_id": sector_id,
                "name": name,
                "source_key": source_key,
                "member_codes": canonical_members,
            }
        )
    # The provider adapter freezes catalog order by canonical QMT source key;
    # preserve that same order so its catalog revision remains authoritative.
    sectors.sort(key=lambda value: str(value["source_key"]))
    return tuple(sectors)


def qmt_sector_catalog_revision(catalog: Mapping[str, object]) -> str:
    """Recompute the authoritative identity from catalog semantics.

    Both the 09:10 capture ledger and the live screening gateway call this
    function.  A provider-supplied ``catalog_revision`` is therefore evidence
    only after it agrees with the actual sector names, source keys and member
    codes consumed by the caller.
    """

    return sha256_json(
        {
            "schema": "chanlun-qmt-gics3-catalog",
            "sectors": _canonical_sectors(catalog),
        }
    )


def catalog_capture_entry(
    catalog: Mapping[str, object],
    *,
    previous_entry_sha256: str | None,
) -> dict[str, object]:
    """Validate a current QMT catalog and turn it into one chained entry."""

    if catalog.get("source") != _CATALOG_SCHEMA_SOURCE:
        raise ValueError("only the QMT GICS3 catalog can enter this ledger")
    if catalog.get("point_in_time_scope") != "CURRENT_CAPTURE_ONLY":
        raise ValueError("QMT catalog must remain current-capture-only")
    try:
        captured_at = normalize_datetime(
            datetime.fromisoformat(str(catalog["captured_at"])),
            "catalog.captured_at",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("QMT catalog capture time is invalid") from exc
    catalog_revision = _require_sha256(
        catalog.get("catalog_revision"), "catalog_revision"
    )
    sectors = _canonical_sectors(catalog)
    expected_revision = qmt_sector_catalog_revision(catalog)
    if catalog_revision != expected_revision:
        raise ValueError("QMT catalog revision does not match its members")
    if previous_entry_sha256 is not None:
        _require_sha256(previous_entry_sha256, "previous_entry_sha256")
    stable: dict[str, object] = {
        "schema": QMT_SECTOR_ENTRY_SCHEMA,
        "captured_at": captured_at.isoformat(),
        "catalog_revision": catalog_revision,
        "point_in_time_scope": "CAPTURE_SESSION_AFTER_CAPTURE_ONLY",
        "previous_entry_sha256": previous_entry_sha256,
        "sectors": sectors,
    }
    return {**stable, "entry_sha256": sha256_json(stable)}


def _ledger_document(entries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": QMT_SECTOR_LEDGER_SCHEMA,
        "entries": tuple(dict(value) for value in entries),
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def validate_sector_ledger(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate document hash, chronological order and the complete hash chain."""

    if payload.get("schema") != QMT_SECTOR_LEDGER_SCHEMA:
        raise ValueError("unsupported QMT sector ledger schema")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, (list, tuple)):
        raise ValueError("QMT sector ledger entries are unavailable")
    stable = {"schema": QMT_SECTOR_LEDGER_SCHEMA, "entries": tuple(raw_entries)}
    if payload.get("content_sha256") != sha256_json(stable):
        raise ValueError("QMT sector ledger content hash changed")
    previous_hash: str | None = None
    previous_time: datetime | None = None
    validated: list[dict[str, object]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise ValueError("QMT sector ledger entry is invalid")
        if raw.get("schema") != QMT_SECTOR_ENTRY_SCHEMA:
            raise ValueError("unsupported QMT sector entry schema")
        stable_entry = {key: raw[key] for key in raw if key != "entry_sha256"}
        entry_hash = _require_sha256(raw.get("entry_sha256"), "entry_sha256")
        if entry_hash != sha256_json(stable_entry):
            raise ValueError("QMT sector entry hash changed")
        if raw.get("previous_entry_sha256") != previous_hash:
            raise ValueError("QMT sector entry hash chain is broken")
        try:
            captured = normalize_datetime(
                datetime.fromisoformat(str(raw["captured_at"])),
                "entry.captured_at",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("QMT sector entry capture time is invalid") from exc
        if previous_time is not None and captured <= previous_time:
            raise ValueError("QMT sector captures must be strictly chronological")
        # Reuse the catalog validator so a valid hash cannot hide malformed rows.
        canonical_entry = catalog_capture_entry(
            {
                "source": _CATALOG_SCHEMA_SOURCE,
                "captured_at": captured.isoformat(),
                "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
                "catalog_revision": raw.get("catalog_revision"),
                "sectors": list(raw.get("sectors") or ()),
            },
            previous_entry_sha256=previous_hash,
        )
        if canonical_entry["entry_sha256"] != entry_hash:
            raise ValueError("QMT sector entry contains unsupported fields")
        validated.append(canonical_entry)
        previous_hash = entry_hash
        previous_time = captured
    return _ledger_document(validated)


def load_sector_ledger(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("QMT sector ledger cannot be read") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("QMT sector ledger document is invalid")
    return validate_sector_ledger(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _ledger_file_bytes(document: Mapping[str, object]) -> bytes:
    """Serialize a validated ledger exactly as the atomic writer does.

    Historical receipts bind both the logical ledger content and the exact
    UTF-8 file bytes.  Because a ledger is an append-only hash chain, an old
    file can be reproduced from the corresponding prefix of the current
    validated document without retaining a full copy after every append.
    """

    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def audit_sector_capture_receipts(
    *,
    output: Path,
    receipt_dir: Path | None = None,
    required_capture_session: date | None = None,
) -> dict[str, object]:
    """Audit current immutable receipts against every hash-chained capture."""

    ledger_path = output.resolve()
    ledger = load_sector_ledger(ledger_path)
    entries = tuple(ledger["entries"])
    if required_capture_session is not None and (
        isinstance(required_capture_session, datetime)
        or not isinstance(required_capture_session, date)
    ):
        raise TypeError("required_capture_session must be a date")
    entries_by_sha = {str(value["entry_sha256"]): value for value in entries}
    captured_sessions = frozenset(
        datetime.fromisoformat(str(value["captured_at"])).date() for value in entries
    )
    required_capture_missing = (
        required_capture_session is not None
        and required_capture_session not in captured_sessions
    )
    receipts_path = (
        receipt_dir.resolve()
        if receipt_dir is not None
        else ledger_path.parent / "receipts"
    )
    current_file_sha256 = _sha256_file(ledger_path)
    covered: dict[str, str] = {}
    binding_sources: dict[str, str] = {}
    invalid: list[dict[str, str]] = []

    candidates = (
        tuple(sorted(receipts_path.rglob("*.json")))
        if receipts_path.is_dir()
        else ()
    )
    for path in candidates:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, Mapping):
                raise ValueError("receipt document is not an object")
            if receipt.get("schema") != QMT_SECTOR_RECEIPT_SCHEMA:
                raise ValueError("unsupported receipt schema")
            entry_sha256 = str(receipt.get("entry_sha256"))
            entry = entries_by_sha.get(entry_sha256)
            if entry is None:
                raise ValueError("receipt entry is absent from the ledger")
            if entry_sha256 in covered:
                raise ValueError("ledger entry has more than one receipt")
            captured = datetime.fromisoformat(str(entry["captured_at"]))
            if receipt.get("capture_session") != captured.date().isoformat():
                raise ValueError("receipt session differs from its ledger entry")
            if Path(str(receipt.get("receipt_path"))).resolve() != path.resolve():
                raise ValueError("receipt path identity changed")
            if Path(str(receipt.get("output"))).resolve() != ledger_path:
                raise ValueError("receipt points to a different sector ledger")
            capture_count = receipt.get("capture_count")
            if (
                isinstance(capture_count, bool)
                or not isinstance(capture_count, int)
                or capture_count < 1
                or capture_count > len(entries)
            ):
                raise ValueError("receipt capture count is outside the ledger chain")
            ledger_file_sha256 = _require_sha256(
                receipt.get("ledger_file_sha256"),
                "receipt.ledger_file_sha256",
            )
            if ledger_file_sha256 == current_file_sha256:
                bound_document = ledger
                binding_source = "CURRENT_LEDGER"
            else:
                bound_ledger = (
                    ledger_path.parent
                    / "archive"
                    / f"{ledger_file_sha256.removeprefix('sha256:')}.json"
                )
                if bound_ledger.is_file():
                    if _sha256_file(bound_ledger) != ledger_file_sha256:
                        raise ValueError("receipt-bound ledger file hash changed")
                    bound_document = load_sector_ledger(bound_ledger)
                    binding_source = "ARCHIVED_PREFIX"
                else:
                    # The current ledger has already validated the complete
                    # entry chain.  Rebuild the exact historical prefix named
                    # by the immutable receipt and prove its original file
                    # identity before accepting it as evidence.
                    bound_document = _ledger_document(entries[:capture_count])
                    reconstructed_sha256 = _sha256_bytes(
                        _ledger_file_bytes(bound_document)
                    )
                    if reconstructed_sha256 != ledger_file_sha256:
                        raise ValueError(
                            "receipt-bound ledger prefix cannot be reconstructed"
                        )
                    binding_source = "RECONSTRUCTED_PREFIX"
            bound_entries = tuple(bound_document["entries"])
            if not bound_entries or bound_entries[-1]["entry_sha256"] != entry_sha256:
                raise ValueError("receipt entry is not the bound ledger tail")
            if receipt.get("ledger_content_sha256") != bound_document["content_sha256"]:
                raise ValueError("receipt ledger content identity changed")
            if capture_count != len(bound_entries):
                raise ValueError("receipt capture count changed")
            if receipt.get("captured_at") != entry["captured_at"]:
                raise ValueError("receipt capture time changed")
            if receipt.get("catalog_revision") != entry["catalog_revision"]:
                raise ValueError("receipt catalog revision changed")
            if receipt.get("sector_count") != len(entry["sectors"]):
                raise ValueError("receipt sector count changed")
            if receipt.get("point_in_time_scope") != entry["point_in_time_scope"]:
                raise ValueError("receipt point-in-time scope changed")
            if receipt.get("historical_backfill_allowed") is not False:
                raise ValueError("receipt permits historical backfill")
            if receipt.get("real_account_accessed") is not False:
                raise ValueError("receipt claims real-account access")
            if receipt.get("real_order_transport_enabled") is not False:
                raise ValueError("receipt enables real-order transport")
            if receipt.get("live_status") != "LIVE_DISABLED":
                raise ValueError("receipt live status changed")
            covered[entry_sha256] = str(path.resolve())
            binding_sources[entry_sha256] = binding_source
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            invalid.append({"path": str(path.resolve()), "reason": str(exc)})

    missing = tuple(
        value for value in entries if str(value["entry_sha256"]) not in covered
    )
    covered_capture_dates = tuple(
        datetime.fromisoformat(str(entries_by_sha[value]["captured_at"])).date()
        for value in covered
    )
    status = (
        "INVALID_RECEIPTS_PRESENT"
        if invalid
        else "REQUIRED_CAPTURE_MISSING"
        if required_capture_missing
        else "REQUIRED_RECEIPT_GAPS"
        if missing
        else "COMPLETE"
    )
    return {
        "schema": QMT_SECTOR_RECEIPT_AUDIT_SCHEMA,
        "status": status,
        "ledger": str(ledger_path),
        "ledger_content_sha256": ledger["content_sha256"],
        "ledger_file_sha256": current_file_sha256,
        "entry_count": len(entries),
        "valid_receipt_count": len(covered),
        "covered_entry_count": len(covered),
        "covered_entry_sha256s": tuple(sorted(covered)),
        "covered_capture_sessions": tuple(
            sorted(value.isoformat() for value in covered_capture_dates)
        ),
        "current_ledger_receipt_count": sum(
            value == "CURRENT_LEDGER" for value in binding_sources.values()
        ),
        "archived_prefix_receipt_count": sum(
            value == "ARCHIVED_PREFIX" for value in binding_sources.values()
        ),
        "reconstructed_prefix_receipt_count": sum(
            value == "RECONSTRUCTED_PREFIX" for value in binding_sources.values()
        ),
        "missing_entry_count": len(missing),
        "missing_capture_sessions": tuple(
            str(value["captured_at"])[:10] for value in missing
        ),
        "missing_entry_sha256s": tuple(
            str(value["entry_sha256"]) for value in missing
        ),
        "required_capture_session": (
            None
            if required_capture_session is None
            else required_capture_session.isoformat()
        ),
        "required_capture_present": (
            None if required_capture_session is None else not required_capture_missing
        ),
        "required_capture_missing_sessions": (
            ()
            if not required_capture_missing or required_capture_session is None
            else (required_capture_session.isoformat(),)
        ),
        "required_missing_capture_sessions": tuple(
            str(value["captured_at"])[:10] for value in missing
        ),
        "invalid_receipt_count": len(invalid),
        "invalid_receipts": tuple(invalid),
        "historical_receipts_synthesized": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


def _append_sector_catalog_unlocked(
    path: Path,
    catalog: Mapping[str, object],
) -> dict[str, object]:
    """Append while the caller holds ``path``'s interprocess lock."""

    if path.exists():
        ledger = load_sector_ledger(path)
        entries = list(ledger["entries"])
    else:
        entries = []
    previous_hash = str(entries[-1]["entry_sha256"]) if entries else None
    entry = catalog_capture_entry(catalog, previous_entry_sha256=previous_hash)
    if entries:
        previous_time = datetime.fromisoformat(str(entries[-1]["captured_at"]))
        current_time = datetime.fromisoformat(str(entry["captured_at"]))
        if current_time <= previous_time:
            raise ValueError("new QMT sector capture must follow the ledger tail")
    entries.append(entry)
    document = _ledger_document(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return document


def append_sector_catalog(path: Path, catalog: Mapping[str, object]) -> dict[str, object]:
    """Atomically append one capture; never lose a concurrent ledger update."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    with interprocess_file_lock(lock_path):
        return _append_sector_catalog_unlocked(path, catalog)


def captured_catalog_at(
    ledger: Mapping[str, object],
    *,
    decision_time: datetime,
) -> dict[str, object] | None:
    """Return only a capture visible later on the same decision session."""

    validated = validate_sector_ledger(ledger)
    decision = normalize_datetime(decision_time, "decision_time")
    eligible = tuple(
        entry
        for entry in validated["entries"]
        if datetime.fromisoformat(str(entry["captured_at"])) <= decision
        and datetime.fromisoformat(str(entry["captured_at"])).date()
        == decision.date()
    )
    if not eligible:
        return None
    entry = eligible[-1]
    return {
        "source": _CATALOG_SCHEMA_SOURCE,
        "captured_at": entry["captured_at"],
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": entry["catalog_revision"],
        "sectors": list(entry["sectors"]),
        "ledger_entry_sha256": entry["entry_sha256"],
        "ledger_content_sha256": validated["content_sha256"],
    }


def audit_forward_sector_capture_readiness(
    *,
    output: Path,
    session: date,
    decision_time: datetime,
    receipt_dir: Path | None = None,
) -> dict[str, object]:
    """Prove the exact QMT sector input required by a forward decision.

    A calendar-day ledger row alone is insufficient.  The selected catalog
    must have existed no later than the decision close and the immutable
    receipt created by the daily Capture phase must bind that exact ledger
    entry.  Web readiness and the forward evaluator both consume this audit so
    a complete screening page cannot be mistaken for a complete daily archive.
    """

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    decision = normalize_datetime(decision_time, "decision_time")
    if decision.date() != session:
        raise ValueError("decision_time must belong to session")

    ledger_path = output.resolve()
    ledger = load_sector_ledger(ledger_path)
    catalog = captured_catalog_at(ledger, decision_time=decision)
    receipt_audit = audit_sector_capture_receipts(
        output=ledger_path,
        receipt_dir=receipt_dir,
        required_capture_session=session,
    )
    covered = frozenset(
        str(value) for value in receipt_audit["covered_entry_sha256s"]
    )
    catalog_entry_sha256 = (
        None if catalog is None else str(catalog["ledger_entry_sha256"])
    )
    receipt_proven = bool(
        catalog_entry_sha256 is not None and catalog_entry_sha256 in covered
    )
    if catalog is None:
        reason_code = "SAME_SESSION_SECTOR_CAPTURE_UNAVAILABLE_BEFORE_CLOSE"
    elif int(receipt_audit["invalid_receipt_count"]) > 0:
        reason_code = "SECTOR_CAPTURE_RECEIPT_AUDIT_INVALID"
    elif not receipt_proven:
        reason_code = "SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN"
    else:
        reason_code = "READY"
    ready = reason_code == "READY"
    return {
        "schema": QMT_FORWARD_CAPTURE_READINESS_SCHEMA,
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "reason_code": reason_code,
        "session": session.isoformat(),
        "decision_time": decision.isoformat(),
        "catalog": catalog,
        "catalog_entry_sha256": catalog_entry_sha256,
        "catalog_captured_at": (
            None if catalog is None else str(catalog["captured_at"])
        ),
        "receipt_proven": receipt_proven,
        "receipt_audit_status": str(receipt_audit["status"]),
        "receipt_audit": receipt_audit,
        "historical_backfill_allowed": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


@dataclass(frozen=True, slots=True)
class HistoricalSectorProbe:
    sector_key: str
    as_of: date
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.sector_key.strip():
            raise ValueError("historical QMT sector key is required")
        if self.members != tuple(sorted(set(self.members))):
            raise ValueError("historical QMT members must be unique and sorted")
        if any(_A_SHARE.fullmatch(value) is None for value in self.members):
            raise ValueError("historical QMT members must be normalized A-share codes")


def audit_historical_sector_probes(
    probes: Sequence[HistoricalSectorProbe],
    *,
    listed_from: Mapping[str, date],
) -> dict[str, object]:
    """Prove that a dated QMT response is not automatically point-in-time.

    An identical response across dates is a warning, not by itself proof of
    leakage.  A member listed after the requested date is direct proof and
    forces ``CURRENT_BACKFILL_PROVEN``.
    """

    if not probes:
        raise ValueError("at least one historical QMT sector probe is required")
    keys = tuple((value.sector_key, value.as_of) for value in probes)
    if len(keys) != len(set(keys)):
        raise ValueError("historical QMT probes must be unique")
    grouped: dict[str, list[HistoricalSectorProbe]] = {}
    future_members: list[dict[str, str]] = []
    missing_listing_dates: set[str] = set()
    rows: list[dict[str, object]] = []
    for probe in sorted(probes, key=lambda value: (value.sector_key, value.as_of)):
        grouped.setdefault(probe.sector_key, []).append(probe)
        member_sha256 = sha256_json(probe.members)
        rows.append(
            {
                "sector_key": probe.sector_key,
                "as_of": probe.as_of.isoformat(),
                "member_count": len(probe.members),
                "members_sha256": member_sha256,
            }
        )
        for member in probe.members:
            listed = listed_from.get(member)
            if listed is None:
                missing_listing_dates.add(member)
            elif listed > probe.as_of:
                future_members.append(
                    {
                        "sector_key": probe.sector_key,
                        "as_of": probe.as_of.isoformat(),
                        "member": member,
                        "listed_from": listed.isoformat(),
                    }
                )
    identical = tuple(
        sorted(
            sector
            for sector, values in grouped.items()
            if len({value.as_of for value in values}) >= 2
            and len({sha256_json(value.members) for value in values}) == 1
        )
    )
    status = (
        "CURRENT_BACKFILL_PROVEN"
        if future_members
        else "HISTORICAL_POINT_IN_TIME_UNVERIFIED"
    )
    stable: dict[str, object] = {
        "schema": QMT_HISTORICAL_AUDIT_SCHEMA,
        "status": status,
        "historical_point_in_time_eligible": False,
        "probe_rows": tuple(rows),
        "identical_member_sets_across_dates": identical,
        "future_listed_members": tuple(future_members),
        "missing_listing_date_members": tuple(sorted(missing_listing_dates)),
        "reason_codes": tuple(
            code
            for condition, code in (
                (bool(future_members), "FUTURE_LISTED_MEMBER_IN_HISTORICAL_RESPONSE"),
                (bool(identical), "IDENTICAL_QMT_MEMBER_SET_ACROSS_HISTORICAL_DATES"),
                (
                    True,
                    "NO_IMMUTABLE_CAPTURE_OR_EFFECTIVE_DATE_PROOF_FOR_PAST_RESPONSE",
                ),
            )
            if condition
        ),
    }
    return {**stable, "content_sha256": sha256_json(stable)}


__all__ = (
    "HistoricalSectorProbe",
    "QMT_HISTORICAL_AUDIT_SCHEMA",
    "QMT_FORWARD_CAPTURE_READINESS_SCHEMA",
    "QMT_SECTOR_ENTRY_SCHEMA",
    "QMT_SECTOR_LEDGER_SCHEMA",
    "QMT_SECTOR_RECEIPT_AUDIT_SCHEMA",
    "QMT_SECTOR_RECEIPT_SCHEMA",
    "append_sector_catalog",
    "audit_historical_sector_probes",
    "audit_forward_sector_capture_readiness",
    "audit_sector_capture_receipts",
    "captured_catalog_at",
    "catalog_capture_entry",
    "load_sector_ledger",
    "validate_sector_ledger",
)
