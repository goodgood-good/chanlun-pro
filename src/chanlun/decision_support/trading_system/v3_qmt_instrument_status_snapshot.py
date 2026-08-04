"""Immutable QMT instrument-status evidence for later forward decisions.

The snapshot is deliberately narrow: it records the symbols exposed by one
completed human-review screen, never the whole current market by implication.
It is captured after that screen and can therefore be used only by later
sessions.  A current or historical missing bar is never retroactively relabelled
as a suspension merely because a later QMT call reports one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.file_lock import (
    interprocess_file_lock,
)


CN = ZoneInfo("Asia/Shanghai")
QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA = (
    "chanlun-qmt-instrument-status-snapshot/v1"
)
QMT_INSTRUMENT_STATUS_SOURCE_METHOD = "QMT_GET_INSTRUMENT_DETAIL"
_NORMALIZED_A_SHARE = re.compile(r"^(SH|SZ|BJ)\.(\d{6})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CODES = frozenset(
    {
        "QMT_INSTRUMENT_DETAIL_UNAVAILABLE",
        "QMT_INSTRUMENT_STATUS_FACT_INVALID",
    }
)


def _native_code(symbol: str) -> str:
    match = _NORMALIZED_A_SHARE.fullmatch(symbol)
    if match is None:
        raise ValueError(f"invalid normalized A-share symbol: {symbol!r}")
    market, digits = match.groups()
    return f"{digits}.{market}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class QmtInstrumentStatusFact:
    symbol: str
    trading_day: date
    instrument_name: str
    instrument_status: int
    is_trading: bool

    def __post_init__(self) -> None:
        if (
            _NORMALIZED_A_SHARE.fullmatch(self.symbol) is None
            or type(self.trading_day) is not date
            or not self.instrument_name.strip()
            or type(self.instrument_status) is not int
            or self.instrument_status < 0
            or type(self.is_trading) is not bool
        ):
            raise ValueError("QMT instrument-status fact is invalid")

    @property
    def suspended(self) -> bool:
        return self.instrument_status >= 1

    @property
    def classification(self) -> Literal["NORMAL", "SUSPENDED"]:
        return "SUSPENDED" if self.suspended else "NORMAL"

    def document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "native_code": _native_code(self.symbol),
            "trading_day": self.trading_day.isoformat(),
            "instrument_name": self.instrument_name,
            "instrument_status": self.instrument_status,
            # ``IsTrading`` is a wall-clock observation and is retained only
            # as raw evidence.  Suspension classification uses the SDK's
            # InstrumentStatus field exclusively.
            "is_trading": self.is_trading,
            "suspended": self.suspended,
            "classification": self.classification,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> QmtInstrumentStatusFact:
        try:
            if (
                type(value["instrument_status"]) is not int
                or type(value["is_trading"]) is not bool
                or type(value["suspended"]) is not bool
            ):
                raise ValueError("QMT instrument-status scalar types are invalid")
            result = cls(
                symbol=str(value["symbol"]),
                trading_day=date.fromisoformat(str(value["trading_day"])),
                instrument_name=str(value["instrument_name"]),
                instrument_status=int(value["instrument_status"]),
                is_trading=bool(value["is_trading"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("QMT instrument-status fact is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("QMT instrument-status fact is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class QmtInstrumentStatusCaptureError:
    symbol: str
    reason_code: Literal[
        "QMT_INSTRUMENT_DETAIL_UNAVAILABLE",
        "QMT_INSTRUMENT_STATUS_FACT_INVALID",
    ]
    detail: str

    def __post_init__(self) -> None:
        if (
            _NORMALIZED_A_SHARE.fullmatch(self.symbol) is None
            or self.reason_code not in _ERROR_CODES
            or not self.detail.strip()
            or len(self.detail) > 240
        ):
            raise ValueError("QMT instrument-status capture error is invalid")

    def document(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> QmtInstrumentStatusCaptureError:
        try:
            result = cls(
                symbol=str(value["symbol"]),
                reason_code=str(value["reason_code"]),  # type: ignore[arg-type]
                detail=str(value["detail"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("QMT instrument-status error is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("QMT instrument-status error is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class QmtInstrumentStatusSnapshot:
    session: date
    started_at: datetime
    captured_at: datetime
    sector_catalog_entry_sha256: str
    source_screen_content_sha256: str
    requested_symbols: tuple[str, ...]
    facts: tuple[QmtInstrumentStatusFact, ...]
    errors: tuple[QmtInstrumentStatusCaptureError, ...]
    schema: str = QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "started_at",
            normalize_datetime(self.started_at, "started_at"),
        )
        object.__setattr__(
            self,
            "captured_at",
            normalize_datetime(self.captured_at, "captured_at"),
        )
        object.__setattr__(self, "requested_symbols", tuple(self.requested_symbols))
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "errors", tuple(self.errors))
        if type(self.session) is not date:
            raise ValueError("QMT instrument-status snapshot identity is invalid")
        market_close = datetime.combine(self.session, time(15), tzinfo=CN)
        if (
            self.started_at.date() != self.session
            or self.captured_at.date() != self.session
            or self.started_at < market_close
            or self.captured_at < self.started_at
            or _SHA256.fullmatch(self.sector_catalog_entry_sha256) is None
            or _SHA256.fullmatch(self.source_screen_content_sha256) is None
            or self.schema != QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("QMT instrument-status snapshot identity is invalid")
        if self.requested_symbols != tuple(sorted(set(self.requested_symbols))):
            raise ValueError("QMT instrument-status symbols must be unique and ordered")
        if any(
            _NORMALIZED_A_SHARE.fullmatch(value) is None
            for value in self.requested_symbols
        ):
            raise ValueError("QMT instrument-status requested symbol is invalid")
        fact_symbols = tuple(value.symbol for value in self.facts)
        error_symbols = tuple(value.symbol for value in self.errors)
        if (
            fact_symbols != tuple(sorted(set(fact_symbols)))
            or error_symbols != tuple(sorted(set(error_symbols)))
            or set(fact_symbols) & set(error_symbols)
            or set(fact_symbols) | set(error_symbols) != set(self.requested_symbols)
            or any(value.trading_day != self.session for value in self.facts)
        ):
            raise ValueError("QMT instrument-status coverage is inconsistent")

    @property
    def all_complete(self) -> bool:
        return not self.errors and len(self.facts) == len(self.requested_symbols)

    def _stable_document(self) -> dict[str, object]:
        status_counts = Counter(value.classification for value in self.facts)
        return {
            "schema": self.schema,
            "session": self.session.isoformat(),
            "started_at": self.started_at.isoformat(),
            "captured_at": self.captured_at.isoformat(),
            "sector_catalog_entry_sha256": self.sector_catalog_entry_sha256,
            "source_screen_content_sha256": self.source_screen_content_sha256,
            "requested_symbols": list(self.requested_symbols),
            "facts": [value.document() for value in self.facts],
            "errors": [value.document() for value in self.errors],
            "requested_symbol_count": len(self.requested_symbols),
            "complete_symbol_count": len(self.facts),
            "error_count": len(self.errors),
            "all_complete": self.all_complete,
            "status_counts": {
                key: status_counts.get(key, 0) for key in ("NORMAL", "SUSPENDED")
            },
            "source_method": QMT_INSTRUMENT_STATUS_SOURCE_METHOD,
            "status_interpretation": (
                "INSTRUMENT_STATUS_ZERO_NORMAL_POSITIVE_SUSPENDED"
            ),
            "point_in_time_scope": "SUBSEQUENT_SESSION_DECISIONS_ONLY",
            "same_session_decision_adjudication_allowed": False,
            "historical_backfill_allowed": False,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "result_status": "PAPER_OBSERVATION",
            "live_status": self.live_status,
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        return {**self._stable_document(), "content_sha256": self.content_sha256}

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> QmtInstrumentStatusSnapshot:
        try:
            if (
                not isinstance(value["requested_symbols"], list)
                or not isinstance(value["facts"], list)
                or not isinstance(value["errors"], list)
                or not isinstance(value["status_counts"], Mapping)
                or set(value["status_counts"]) != {"NORMAL", "SUSPENDED"}
                or any(
                    type(value["status_counts"][key]) is not int
                    or value["status_counts"][key] < 0
                    for key in ("NORMAL", "SUSPENDED")
                )
                or type(value["requested_symbol_count"]) is not int
                or type(value["complete_symbol_count"]) is not int
                or type(value["error_count"]) is not int
                or type(value["all_complete"]) is not bool
                or value["same_session_decision_adjudication_allowed"] is not False
                or value["historical_backfill_allowed"] is not False
                or value["tick_data_used"] is not False
                or value["real_account_accessed"] is not False
                or value["real_order_transport_enabled"] is not False
            ):
                raise ValueError("QMT instrument-status snapshot types are invalid")
            result = cls(
                session=date.fromisoformat(str(value["session"])),
                started_at=datetime.fromisoformat(str(value["started_at"])),
                captured_at=datetime.fromisoformat(str(value["captured_at"])),
                sector_catalog_entry_sha256=str(
                    value["sector_catalog_entry_sha256"]
                ),
                source_screen_content_sha256=str(
                    value["source_screen_content_sha256"]
                ),
                requested_symbols=tuple(str(item) for item in value["requested_symbols"]),
                facts=tuple(
                    QmtInstrumentStatusFact.from_document(item)
                    for item in value["facts"]
                    if isinstance(item, Mapping)
                ),
                errors=tuple(
                    QmtInstrumentStatusCaptureError.from_document(item)
                    for item in value["errors"]
                    if isinstance(item, Mapping)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("QMT instrument-status snapshot is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("QMT instrument-status snapshot is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class QmtInstrumentStatusCaptureResult:
    snapshot: QmtInstrumentStatusSnapshot
    reused: bool
    latest_path: Path
    object_path: Path
    latest_file_sha256: str
    object_file_sha256: str

    def evidence(self) -> dict[str, object]:
        document = self.snapshot.document()
        return {
            "schema": document["schema"],
            "session": document["session"],
            "captured_at": document["captured_at"],
            "sector_catalog_entry_sha256": document[
                "sector_catalog_entry_sha256"
            ],
            "source_screen_content_sha256": document[
                "source_screen_content_sha256"
            ],
            "content_sha256": document["content_sha256"],
            "latest_path": str(self.latest_path),
            "latest_file_sha256": self.latest_file_sha256,
            "object_path": str(self.object_path),
            "object_file_sha256": self.object_file_sha256,
            "reused": self.reused,
            "requested_symbol_count": document["requested_symbol_count"],
            "complete_symbol_count": document["complete_symbol_count"],
            "error_count": document["error_count"],
            "all_complete": document["all_complete"],
            "status_counts": document["status_counts"],
            "point_in_time_scope": document["point_in_time_scope"],
            "same_session_decision_adjudication_allowed": False,
            "historical_backfill_allowed": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }


def _default_detail_provider(symbol: str) -> Mapping[str, object]:
    from xtquant import xtdata

    value = xtdata.get_instrument_detail(_native_code(symbol), iscomplete=False)
    if not isinstance(value, Mapping):
        raise RuntimeError("QMT instrument detail is unavailable")
    return value


def _fact_from_detail(
    *,
    symbol: str,
    session: date,
    detail: Mapping[str, object],
) -> QmtInstrumentStatusFact:
    trading_day = datetime.strptime(
        str(detail.get("TradingDay") or ""),
        "%Y%m%d",
    ).date()
    status = detail.get("InstrumentStatus")
    raw_is_trading = detail.get("IsTrading")
    name = str(detail.get("InstrumentName") or "").strip()
    if (
        trading_day != session
        or type(status) is not int
        or int(status) < 0
        or (
            type(raw_is_trading) is not bool
            and not (type(raw_is_trading) is int and raw_is_trading in {0, 1})
        )
        or not name
    ):
        raise ValueError("QMT instrument detail does not prove same-session status")
    return QmtInstrumentStatusFact(
        symbol=symbol,
        trading_day=trading_day,
        instrument_name=name,
        instrument_status=int(status),
        is_trading=bool(raw_is_trading),
    )


def _load_snapshot(path: Path) -> QmtInstrumentStatusSnapshot | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            return None
        return QmtInstrumentStatusSnapshot.from_document(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def capture_qmt_instrument_status_snapshot(
    *,
    output: Path,
    session: date,
    sector_catalog_entry_sha256: str,
    source_screen_content_sha256: str,
    symbols: Sequence[str],
    detail_provider: Callable[[str], Mapping[str, object]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> QmtInstrumentStatusCaptureResult:
    """Capture one immutable, retry-safe candidate status snapshot.

    A complete snapshot for the same catalog, screen and requested symbol set
    is reused verbatim on restart.  Incomplete attempts remain immutable
    content-addressed objects and are retried; no attempt is silently upgraded.
    """

    output = Path(output).resolve()
    requested = tuple(sorted(set(symbols)))
    if any(_NORMALIZED_A_SHARE.fullmatch(value) is None for value in requested):
        raise ValueError("QMT instrument-status capture symbol is invalid")
    provider = detail_provider or _default_detail_provider
    now = clock or (lambda: datetime.now(CN))
    lock_path = output.with_suffix(output.suffix + ".lock")
    with interprocess_file_lock(lock_path):
        existing = _load_snapshot(output) if output.is_file() else None
        if (
            existing is not None
            and existing.all_complete
            and existing.session == session
            and existing.sector_catalog_entry_sha256
            == sector_catalog_entry_sha256
            and existing.source_screen_content_sha256
            == source_screen_content_sha256
            and existing.requested_symbols == requested
        ):
            object_path = (
                output.parent
                / "objects"
                / "qmt_instrument_status_snapshot"
                / f"{existing.content_sha256[7:]}.json"
            )
            if _load_snapshot(object_path) != existing:
                raise RuntimeError(
                    "immutable QMT instrument-status object is unavailable"
                )
            return QmtInstrumentStatusCaptureResult(
                snapshot=existing,
                reused=True,
                latest_path=output,
                object_path=object_path,
                latest_file_sha256=_sha256_file(output),
                object_file_sha256=_sha256_file(object_path),
            )

        started_at = normalize_datetime(now(), "status_capture_started_at")
        facts: list[QmtInstrumentStatusFact] = []
        errors: list[QmtInstrumentStatusCaptureError] = []
        for symbol in requested:
            try:
                detail = provider(symbol)
            except Exception as exc:
                errors.append(
                    QmtInstrumentStatusCaptureError(
                        symbol=symbol,
                        reason_code="QMT_INSTRUMENT_DETAIL_UNAVAILABLE",
                        detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                )
                continue
            try:
                if not isinstance(detail, Mapping):
                    raise ValueError("QMT instrument detail must be a mapping")
                facts.append(
                    _fact_from_detail(
                        symbol=symbol,
                        session=session,
                        detail=detail,
                    )
                )
            except (TypeError, ValueError) as exc:
                errors.append(
                    QmtInstrumentStatusCaptureError(
                        symbol=symbol,
                        reason_code="QMT_INSTRUMENT_STATUS_FACT_INVALID",
                        detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                )
        captured_at = normalize_datetime(now(), "status_captured_at")
        snapshot = QmtInstrumentStatusSnapshot(
            session=session,
            started_at=started_at,
            captured_at=captured_at,
            sector_catalog_entry_sha256=sector_catalog_entry_sha256,
            source_screen_content_sha256=source_screen_content_sha256,
            requested_symbols=requested,
            facts=tuple(sorted(facts, key=lambda value: value.symbol)),
            errors=tuple(sorted(errors, key=lambda value: value.symbol)),
        )
        document = snapshot.document()
        object_path = (
            output.parent
            / "objects"
            / "qmt_instrument_status_snapshot"
            / f"{snapshot.content_sha256[7:]}.json"
        )
        if object_path.is_file():
            if _load_snapshot(object_path) != snapshot:
                raise RuntimeError("QMT instrument-status object identity collision")
        else:
            _atomic_json(object_path, document)
        _atomic_json(output, document)
        return QmtInstrumentStatusCaptureResult(
            snapshot=snapshot,
            reused=False,
            latest_path=output,
            object_path=object_path,
            latest_file_sha256=_sha256_file(output),
            object_file_sha256=_sha256_file(object_path),
        )


__all__ = (
    "QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA",
    "QMT_INSTRUMENT_STATUS_SOURCE_METHOD",
    "QmtInstrumentStatusCaptureError",
    "QmtInstrumentStatusCaptureResult",
    "QmtInstrumentStatusFact",
    "QmtInstrumentStatusSnapshot",
    "capture_qmt_instrument_status_snapshot",
)
