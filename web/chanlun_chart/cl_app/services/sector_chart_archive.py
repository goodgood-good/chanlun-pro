"""Verified read-only QMT sector chart evidence for causal audit links.

The backtest's sector risk facts are built from a synthetic ``qmt-gics3``
composite.  That symbol is not an exchange instrument and must never fall
through to the live QMT chart path.  This module serves only an offline,
content-addressed archive whose manifest is bound to the canonical research
artifact and whose bars are capped at the audited observation time.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd

from chanlun.decision_support.fingerprints import sha256_json


SECTOR_CHART_ARCHIVE_SCHEMA = "chanlun-v3-sector-chart-evidence-archive/v1"
SECTOR_CHART_FRAME_SCHEMA = "chanlun-v3-sector-chart-frame/v1"
SECTOR_CHART_ARCHIVE_RELATIVE_PATH = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p_mwd_strength/"
    "sector_chart_evidence_archive/manifest.json"
)
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECTOR_RE = re.compile(r"^qmt-gics3:[0-9a-f]{64}$")
_INTERVALS = frozenset({"5", "30", "1D", "1W", "1M"})
_REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class SectorChartArchiveUnavailable(ValueError):
    """The optional presentation archive is absent, stale, or malformed."""


@dataclass(frozen=True, slots=True)
class SectorChartArchive:
    root: Path
    manifest_path: Path
    file_sha256: str
    content_sha256: str
    document: dict[str, Any]
    entries_by_id: dict[str, dict[str, Any]]
    entries_by_key: dict[tuple[str, int], dict[str, Any]]

    def summary(self) -> dict[str, object]:
        return {
            "schema": SECTOR_CHART_ARCHIVE_SCHEMA,
            "status": "VERIFIED",
            "manifest_relative_path": self.manifest_path.relative_to(
                self.root
            ).as_posix(),
            "manifest_file_sha256": self.file_sha256,
            "manifest_content_sha256": self.content_sha256,
            "entry_count": len(self.entries_by_id),
            "supported_intervals": list(
                self.document["supported_intervals"]
            ),
            "source_artifact": dict(self.document["source_artifact"]),
            "presentation_overlay_not_in_risk_audit_hash": True,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SectorChartArchiveUnavailable(
                "sector chart archive contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_MANIFEST_BYTES:
            raise OSError("manifest size is invalid")
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SectorChartArchiveUnavailable(
                    f"invalid JSON constant: {value}"
                )
            ),
        )
    except SectorChartArchiveUnavailable:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart archive manifest cannot be read"
        ) from exc
    if not isinstance(value, dict):
        raise SectorChartArchiveUnavailable(
            "sector chart archive manifest must be an object"
        )
    return value, "sha256:" + hashlib.sha256(raw).hexdigest()


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise SectorChartArchiveUnavailable(f"{label} is not a sha256 identity")
    return value


def _text(value: object, label: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
    ):
        raise SectorChartArchiveUnavailable(f"{label} is invalid")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise SectorChartArchiveUnavailable(f"{label} is invalid")
    return value


def _resolved_root(root: str | Path) -> Path:
    try:
        value = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart archive root is unavailable"
        ) from exc
    if not value.is_dir():
        raise SectorChartArchiveUnavailable(
            "sector chart archive root is unavailable"
        )
    return value


def _manifest_path(root: Path) -> Path:
    try:
        path = (root / SECTOR_CHART_ARCHIVE_RELATIVE_PATH).resolve(strict=True)
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart archive manifest is unavailable"
        ) from exc
    if not path.is_file():
        raise SectorChartArchiveUnavailable(
            "sector chart archive manifest is unavailable"
        )
    return path


def _safe_frame_path(archive: SectorChartArchive, relative: object) -> Path:
    text = _text(relative, "sector chart frame path", limit=260)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SectorChartArchiveUnavailable("sector chart frame path escapes archive")
    try:
        path = (archive.manifest_path.parent / candidate).resolve(strict=True)
        path.relative_to(archive.manifest_path.parent)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart frame is unavailable"
        ) from exc
    if not path.is_file():
        raise SectorChartArchiveUnavailable("sector chart frame is unavailable")
    return path


def _validate_frame_metadata(value: object, *, interval: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SectorChartArchiveUnavailable("sector chart frame metadata is invalid")
    row = dict(value)
    if row.get("interval") != interval:
        raise SectorChartArchiveUnavailable("sector chart frame interval changed")
    _text(row.get("path"), "sector chart frame path", limit=260)
    _hash(row.get("file_sha256"), "sector chart frame file hash")
    _hash(row.get("content_sha256"), "sector chart frame content hash")
    count = _integer(row.get("row_count"), "sector chart frame row count")
    first = row.get("first_at")
    last = row.get("last_at")
    if count == 0:
        if first is not None or last is not None:
            raise SectorChartArchiveUnavailable(
                "empty sector chart frame has time bounds"
            )
    else:
        _text(first, "sector chart frame first time", limit=64)
        _text(last, "sector chart frame last time", limit=64)
    return row


def load_sector_chart_archive(
    root: str | Path,
    *,
    expected_source_artifact_file_sha256: str | None = None,
    expected_source_artifact_content_sha256: str | None = None,
    expected_risk_audit_sha256: str | None = None,
    expected_decision_source_sha256: str | None = None,
    expected_input_hashes: Mapping[str, object] | None = None,
    expected_manifest_content_sha256: str | None = None,
) -> SectorChartArchive:
    """Load and validate the manifest without reading every Parquet frame."""

    root_path = _resolved_root(root)
    manifest_path = _manifest_path(root_path)
    document, file_sha256 = _load_json(manifest_path)
    if document.get("schema") != SECTOR_CHART_ARCHIVE_SCHEMA:
        raise SectorChartArchiveUnavailable(
            "unsupported sector chart archive schema"
        )
    content_sha256 = _hash(
        document.get("content_sha256"), "sector chart archive content hash"
    )
    stable = dict(document)
    stable.pop("content_sha256", None)
    if sha256_json(stable) != content_sha256:
        raise SectorChartArchiveUnavailable(
            "sector chart archive content hash changed"
        )
    if (
        expected_manifest_content_sha256 is not None
        and content_sha256 != expected_manifest_content_sha256
    ):
        raise SectorChartArchiveUnavailable(
            "sector chart archive changed after chart-lock validation"
        )

    source = document.get("source_artifact")
    if not isinstance(source, Mapping):
        raise SectorChartArchiveUnavailable(
            "sector chart source-artifact binding is missing"
        )
    expected = {
        "file_sha256": expected_source_artifact_file_sha256,
        "content_sha256": expected_source_artifact_content_sha256,
        "risk_audit_sha256": expected_risk_audit_sha256,
        "decision_source_aggregate_sha256": expected_decision_source_sha256,
    }
    for key, wanted in expected.items():
        actual = _hash(source.get(key), f"source_artifact.{key}")
        if wanted is not None and actual != wanted:
            raise SectorChartArchiveUnavailable(
                f"sector chart archive is stale for source_artifact.{key}"
            )
    relative = _text(
        source.get("relative_path"), "source artifact relative path", limit=260
    )
    try:
        artifact_path = (root_path / relative).resolve(strict=True)
        artifact_path.relative_to(root_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart source artifact is unavailable"
        ) from exc
    if _sha256_file(artifact_path) != source["file_sha256"]:
        raise SectorChartArchiveUnavailable(
            "sector chart source artifact file hash changed"
        )

    inputs = document.get("input_hashes")
    if not isinstance(inputs, Mapping):
        raise SectorChartArchiveUnavailable(
            "sector chart input bindings are missing"
        )
    for key in ("pit_snapshot", "current_catalog_ledger", "current_catalog_entry"):
        actual = _hash(inputs.get(key), f"input_hashes.{key}")
        if expected_input_hashes is not None and actual != expected_input_hashes.get(
            key
        ):
            raise SectorChartArchiveUnavailable(
                f"sector chart archive is stale for input_hashes.{key}"
            )

    intervals = document.get("supported_intervals")
    if (
        not isinstance(intervals, list)
        or not intervals
        or len(intervals) != len(set(intervals))
        or any(value not in _INTERVALS for value in intervals)
    ):
        raise SectorChartArchiveUnavailable(
            "sector chart supported intervals are invalid"
        )
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SectorChartArchiveUnavailable("sector chart archive has no entries")
    entries_by_id: dict[str, dict[str, Any]] = {}
    entries_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise SectorChartArchiveUnavailable("sector chart entry is invalid")
        entry = dict(raw)
        entry_id = _hash(entry.get("entry_id"), "sector chart entry ID")
        sector_id = _text(entry.get("sector_id"), "sector chart sector ID")
        if _SECTOR_RE.fullmatch(sector_id) is None:
            raise SectorChartArchiveUnavailable("sector chart sector ID is invalid")
        _text(entry.get("sector_name"), "sector chart sector name", limit=160)
        cutoff = _integer(
            entry.get("review_as_of_unix"),
            "sector chart review cutoff",
            positive=True,
        )
        if entry.get("review_as_of") is None:
            raise SectorChartArchiveUnavailable(
                "sector chart review cutoff text is missing"
            )
        _text(entry.get("review_as_of"), "sector chart review cutoff", limit=64)
        _hash(entry.get("source_revision"), "sector chart source revision")
        _hash(entry.get("price_basis_revision"), "sector chart price basis")
        frames = entry.get("frames")
        if not isinstance(frames, Mapping) or not frames:
            raise SectorChartArchiveUnavailable("sector chart frames are missing")
        normalized_frames = {
            str(interval): _validate_frame_metadata(value, interval=str(interval))
            for interval, value in frames.items()
        }
        if set(normalized_frames) != set(intervals):
            raise SectorChartArchiveUnavailable(
                "sector chart entry interval coverage is incomplete"
            )
        entry["frames"] = normalized_frames
        key = (sector_id, cutoff)
        if entry_id in entries_by_id or key in entries_by_key:
            raise SectorChartArchiveUnavailable(
                "sector chart entry identity is duplicated"
            )
        entries_by_id[entry_id] = entry
        entries_by_key[key] = entry
    return SectorChartArchive(
        root=root_path,
        manifest_path=manifest_path,
        file_sha256=file_sha256,
        content_sha256=content_sha256,
        document=document,
        entries_by_id=entries_by_id,
        entries_by_key=entries_by_key,
    )


def sector_chart_entry(
    archive: SectorChartArchive,
    *,
    sector_id: str,
    review_as_of: int,
    interval: str,
    verify_file: bool = True,
) -> dict[str, Any]:
    entry = archive.entries_by_key.get((sector_id, review_as_of))
    if entry is None:
        raise SectorChartArchiveUnavailable(
            "sector chart point has no exact archive prefix"
        )
    frame = entry["frames"].get(interval)
    if frame is None:
        raise SectorChartArchiveUnavailable(
            "sector chart point interval is not archived"
        )
    if verify_file:
        path = _safe_frame_path(archive, frame["path"])
        if _sha256_file(path) != frame["file_sha256"]:
            raise SectorChartArchiveUnavailable(
                "sector chart frame file hash changed"
            )
    return entry


def normalize_sector_chart_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a strict chronological OHLCV frame suitable for the archive."""

    if not isinstance(frame, pd.DataFrame):
        raise SectorChartArchiveUnavailable("sector chart frame is not tabular")
    optional = ("member_mask",) if "member_mask" in frame.columns else ()
    missing = set(_REQUIRED_COLUMNS).difference(frame.columns)
    if missing:
        raise SectorChartArchiveUnavailable(
            "sector chart frame is missing required columns"
        )
    result = frame.loc[:, [*_REQUIRED_COLUMNS, *optional]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    if result["date"].dt.tz is None:
        raise SectorChartArchiveUnavailable(
            "sector chart frame timestamps must be timezone-aware"
        )
    result["date"] = result["date"].dt.tz_convert("Asia/Shanghai")
    if result["date"].duplicated().any() or not result["date"].is_monotonic_increasing:
        raise SectorChartArchiveUnavailable(
            "sector chart frame timestamps are not strictly chronological"
        )
    for field in ("open", "high", "low", "close", "volume"):
        result[field] = pd.to_numeric(result[field], errors="raise").astype(float)
    numeric = result.loc[:, ["open", "high", "low", "close", "volume"]]
    prices = numeric.loc[:, ["open", "high", "low", "close"]]
    invalid = (
        ~numeric.map(math.isfinite).all(axis=1)
        | (prices <= 0).any(axis=1)
        | (numeric["volume"] < 0)
        | (numeric["high"] < prices.max(axis=1))
        | (numeric["low"] > prices.min(axis=1))
    )
    if bool(invalid.any()):
        raise SectorChartArchiveUnavailable(
            "sector chart frame contains invalid OHLCV"
        )
    if "member_mask" in result:
        masks = tuple(result["member_mask"])
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int,))
            and not hasattr(value, "__index__")
            for value in masks
        ):
            raise SectorChartArchiveUnavailable(
                "sector chart member masks are invalid"
            )
        result["member_mask"] = tuple(int(value) for value in masks)
    return result.reset_index(drop=True)


def sector_chart_frame_content_sha256(frame: pd.DataFrame) -> str:
    normalized = normalize_sector_chart_frame(frame)
    has_mask = "member_mask" in normalized.columns
    rows = []
    for row in normalized.itertuples(index=False):
        values: list[object] = [
            pd.Timestamp(row.date).isoformat(),
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            float(row.volume),
        ]
        if has_mask:
            values.append(int(row.member_mask))
        rows.append(values)
    return sha256_json(
        {
            "schema": SECTOR_CHART_FRAME_SCHEMA,
            "columns": [
                *_REQUIRED_COLUMNS,
                *(("member_mask",) if has_mask else ()),
            ],
            "rows": rows,
        }
    )


def load_sector_chart_frame(
    archive: SectorChartArchive,
    *,
    entry_id: str,
    interval: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    entry = archive.entries_by_id.get(entry_id)
    if entry is None:
        raise SectorChartArchiveUnavailable("sector chart archive entry is unknown")
    metadata = entry["frames"].get(interval)
    if metadata is None:
        raise SectorChartArchiveUnavailable("sector chart interval is unavailable")
    path = _safe_frame_path(archive, metadata["path"])
    if _sha256_file(path) != metadata["file_sha256"]:
        raise SectorChartArchiveUnavailable("sector chart frame file hash changed")
    try:
        frame = normalize_sector_chart_frame(pd.read_parquet(path))
    except SectorChartArchiveUnavailable:
        raise
    except Exception as exc:
        raise SectorChartArchiveUnavailable(
            "sector chart frame cannot be decoded"
        ) from exc
    if (
        len(frame) != metadata["row_count"]
        or sector_chart_frame_content_sha256(frame) != metadata["content_sha256"]
    ):
        raise SectorChartArchiveUnavailable("sector chart frame content changed")
    if frame.empty:
        actual_first = actual_last = None
    else:
        actual_first = pd.Timestamp(frame.iloc[0]["date"]).isoformat()
        actual_last = pd.Timestamp(frame.iloc[-1]["date"]).isoformat()
        if int(pd.Timestamp(frame.iloc[-1]["date"]).timestamp()) > int(
            entry["review_as_of_unix"]
        ):
            raise SectorChartArchiveUnavailable(
                "sector chart frame exceeds its causal cutoff"
            )
    if actual_first != metadata["first_at"] or actual_last != metadata["last_at"]:
        raise SectorChartArchiveUnavailable("sector chart frame bounds changed")
    return entry, frame


def sector_chart_history_payload(
    archive: SectorChartArchive,
    *,
    entry_id: str,
    interval: str,
    from_ts: int,
    to_ts: int,
) -> dict[str, object]:
    entry, frame = load_sector_chart_frame(
        archive, entry_id=entry_id, interval=interval
    )
    cutoff = int(entry["review_as_of_unix"])
    upper = cutoff if to_ts <= 0 else min(to_ts, cutoff)
    if from_ts > upper:
        return {"s": "no_data"}
    seconds = frame["date"].map(lambda value: int(pd.Timestamp(value).timestamp()))
    selected = frame[(seconds >= max(0, from_ts)) & (seconds <= upper)]
    if selected.empty:
        return {"s": "no_data"}
    times = [int(pd.Timestamp(value).timestamp()) for value in selected["date"]]
    return {
        "s": "ok",
        "t": times,
        "o": selected["open"].astype(float).tolist(),
        "h": selected["high"].astype(float).tolist(),
        "l": selected["low"].astype(float).tolist(),
        "c": selected["close"].astype(float).tolist(),
        "v": selected["volume"].astype(float).tolist(),
    }


def sector_chart_symbol_info(
    archive: SectorChartArchive,
    *,
    entry_id: str,
    interval: str,
) -> dict[str, object]:
    entry = archive.entries_by_id.get(entry_id)
    if entry is None or interval not in entry["frames"]:
        raise SectorChartArchiveUnavailable("sector chart symbol is unavailable")
    sector_id = str(entry["sector_id"])
    return {
        "name": sector_id,
        "ticker": f"a:{sector_id}",
        "full_name": f"a:{sector_id}",
        "description": f"{entry['sector_name']}（回测同源 QMT 板块合成）",
        "exchange": "a",
        "listed_exchange": "a",
        "type": "index",
        "session": "0930-1130,1300-1500",
        "timezone": "Asia/Shanghai",
        "minmov": 1,
        "pricescale": int(entry.get("pricescale", 1_000_000)),
        "visible_plots_set": "ohlcv",
        "supported_resolutions": [interval],
        "intraday_multipliers": [interval] if interval.isdigit() else [],
        "has_intraday": interval.isdigit(),
        "has_seconds": False,
        "has_daily": interval == "1D",
        "has_weekly_and_monthly": interval in {"1W", "1M"},
        "has_empty_bars": False,
        "volume_precision": 0,
        "data_status": "endofday",
        "sector": str(entry["sector_name"]),
        "industry": str(entry["sector_name"]),
    }


__all__ = (
    "SECTOR_CHART_ARCHIVE_RELATIVE_PATH",
    "SECTOR_CHART_ARCHIVE_SCHEMA",
    "SECTOR_CHART_FRAME_SCHEMA",
    "SectorChartArchive",
    "SectorChartArchiveUnavailable",
    "load_sector_chart_archive",
    "load_sector_chart_frame",
    "normalize_sector_chart_frame",
    "sector_chart_entry",
    "sector_chart_frame_content_sha256",
    "sector_chart_history_payload",
    "sector_chart_symbol_info",
)
