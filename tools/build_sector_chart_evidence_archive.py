#!/usr/bin/env python3
"""Build a causal, content-addressed chart archive for QMT GICS3 audit points.

This is an offline research-output command.  It reads only the local QMT
fixed-record cache and existing audited inputs; it never connects to an
account, sends an order, or changes the frozen strategy parameters.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
WEB_ROOT = PROJECT_ROOT / "web" / "chanlun_chart"
for value in (PROJECT_ROOT, SOURCE_ROOT, WEB_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.backtest.current_sector import (  # noqa: E402
    CurrentQmtGics3CompositeReplaySource,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    read_qmt_local_kline,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (  # noqa: E402
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    resolve_sector_higher_timeframe_gate,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (  # noqa: E402
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (  # noqa: E402
    DailyMarketBar,
    aggregate_completed_period_bars,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    QMT_SECTOR_LEDGER_SCHEMA,
    load_sector_ledger,
)
from cl_app.services.sector_chart_archive import (  # noqa: E402
    SECTOR_CHART_ARCHIVE_RELATIVE_PATH,
    SECTOR_CHART_ARCHIVE_SCHEMA,
    load_sector_chart_archive,
    load_sector_chart_frame,
    normalize_sector_chart_frame,
    sector_chart_frame_content_sha256,
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_ARTIFACT = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p_mwd_strength/"
    "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/"
    "fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_CATALOG = Path(
    ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)
DEFAULT_COVERAGE = Path(
    "audit/chanlun_live_integration/sector_first_full_market_1m_coverage.json"
)
_REQUIRED_INTERVALS = ("5", "30", "1D", "1W", "1M")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=PROJECT_ROOT)
    value.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument("--catalog-ledger", type=Path, default=DEFAULT_CATALOG)
    value.add_argument("--qmt-local-data-dir", type=Path)
    value.add_argument("--coverage-audit", type=Path, default=DEFAULT_COVERAGE)
    value.add_argument(
        "--output",
        type=Path,
        default=SECTOR_CHART_ARCHIVE_RELATIVE_PATH,
    )
    value.add_argument("--force", action="store_true")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _normal(value: object) -> object:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


def _assert_expected_projection(
    actual: object,
    expected: object,
    *,
    path: str = "sector_risk_warmup_evidence",
) -> None:
    """Require every artifact field to reproduce; permit additive diagnostics.

    The current risk evidence model may add non-decision diagnostic fields to
    ``mapping_supply``.  An older audited document cannot be rewritten to add
    them.  Treat the artifact as the required projection: no recorded value
    may differ or disappear, while genuinely additive fields are allowed.
    """

    actual = _normal(actual)
    expected = _normal(expected)
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ValueError(f"{path} changed type")
        for key, value in expected.items():
            if key not in actual:
                raise ValueError(f"{path}.{key} disappeared")
            _assert_expected_projection(
                actual[key], value, path=f"{path}.{key}"
            )
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError(f"{path} changed sequence length")
        for index, (actual_item, expected_item) in enumerate(
            zip(actual, expected)
        ):
            _assert_expected_projection(
                actual_item, expected_item, path=f"{path}[{index}]"
            )
        return
    if actual != expected:
        raise ValueError(f"{path} changed: {actual!r} != {expected!r}")


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            _normal(value), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)


def _load_artifact(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("research artifact must be a JSON object")
    reported = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if reported != sha256_json(stable):
        raise ValueError("research artifact content hash changed")
    if (
        payload.get("schema")
        != "chanlun-sector-first-full-market-research-backtest"
        or payload.get("live_status") != "LIVE_DISABLED"
        or payload.get("highest_status") != "RESEARCH_ONLY"
    ):
        raise ValueError("unsupported research artifact")
    return payload, "sha256:" + hashlib.sha256(raw).hexdigest()


def _all_values(value: object, key: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name == key and isinstance(item, str):
                found.append(item)
            found.extend(_all_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_all_values(item, key))
    return found


def _data_dir(args: argparse.Namespace, *, root: Path) -> Path:
    if args.qmt_local_data_dir is not None:
        candidate = args.qmt_local_data_dir
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve(strict=True)
    coverage = args.coverage_audit
    if not coverage.is_absolute():
        coverage = root / coverage
    payload = json.loads(coverage.read_text(encoding="utf-8"))
    candidates = {
        Path(value).resolve()
        for value in _all_values(payload, "local_data_dir")
        if Path(value).is_dir()
    }
    if len(candidates) != 1:
        raise ValueError("QMT local data directory is not uniquely discoverable")
    return candidates.pop()


def _bound_catalog(
    path: Path,
    *,
    expected_entry_sha256: str,
    expected_ledger_sha256: str,
) -> tuple[dict[str, Any], str]:
    ledger = load_sector_ledger(path)
    entries = tuple(ledger["entries"])
    matches = tuple(
        (index, dict(entry))
        for index, entry in enumerate(entries)
        if entry.get("entry_sha256") == expected_entry_sha256
    )
    if len(matches) != 1:
        raise ValueError("artifact catalog entry is not unique in current ledger")
    index, entry = matches[0]
    stable = {
        "schema": QMT_SECTOR_LEDGER_SCHEMA,
        "entries": tuple(entries[: index + 1]),
    }
    stable_json = _normal(stable)
    assert isinstance(stable_json, dict)
    document = {
        **stable_json,
        "content_sha256": sha256_json(stable_json),
    }
    payload = (
        json.dumps(_normal(document), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    bound_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
    if bound_sha256 != expected_ledger_sha256:
        raise ValueError("artifact catalog ledger prefix changed")
    return entry, bound_sha256


def _calendar_sessions(
    *,
    data_dir: Path,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[date, ...], str, dict[str, object]]:
    paths: dict[str, tuple[date, ...]] = {}
    audits: dict[str, object] = {}
    for code in ("SH.000001", "SH.000300"):
        frame, audit = read_qmt_local_kline(
            data_dir=data_dir,
            code=code,
            frequency="1d",
            start_at=start_at,
            end_at=end_at,
        )
        sessions = tuple(
            pd.to_datetime(frame["time"], unit="ms", utc=True)
            .dt.tz_convert(CN)
            .dt.date
        )
        if not sessions or sessions != tuple(sorted(set(sessions))):
            raise ValueError(f"market calendar is invalid for {code}")
        paths[code] = sessions
        audits[code] = {
            "source_sha256": audit.source_sha256,
            "selected_record_count": audit.selected_record_count,
            "first_at": audit.first_at,
            "last_at": audit.last_at,
        }
    if paths["SH.000001"] != paths["SH.000300"]:
        raise ValueError("SH.000001 and SH.000300 calendar paths diverge")
    sessions = paths["SH.000001"]
    revision = sha256_json(
        {
            "schema": "chanlun-sector-chart-market-calendar",
            "sessions": tuple(value.isoformat() for value in sessions),
            "sources": audits,
        }
    )
    return sessions, revision, audits


def _candidate_sector_evidence(
    artifact: Mapping[str, object],
    *,
    sector_id: str,
    review_as_of: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = tuple(
        dict(raw)
        for raw in artifact["candidate_audit"]
        if isinstance(raw, Mapping)
        and raw.get("sector_id") == sector_id
        and raw.get("decision_at") == review_as_of.isoformat()
        and raw.get("sector_risk_warmup_evidence") is not None
    )
    if not rows:
        raise ValueError("sector chart cutoff has no candidate evidence")
    evidence_identities = {
        json.dumps(
            _normal(row["sector_risk_warmup_evidence"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
    }
    gate_identities = {
        (row.get("sector_risk_gate"), tuple(row.get("sector_risk_blocker_codes") or ()))
        for row in rows
    }
    names = {row.get("sector_name") for row in rows}
    if len(evidence_identities) != 1 or len(gate_identities) != 1 or len(names) != 1:
        raise ValueError("sector candidates disagree at one chart cutoff")
    return rows[0], dict(rows[0]["sector_risk_warmup_evidence"])


def _actual_sector_evidence(resolution: object) -> dict[str, object]:
    evidence = resolution.evidence
    source_mode = resolution.source_mode
    return {
        "source_revision": evidence.source_revision,
        "monthly": evidence.monthly,
        "weekly": evidence.weekly,
        "daily": evidence.daily,
        "grade": evidence.grade,
        "period_diagnostics": tuple(
            value.document() for value in evidence.period_diagnostics
        ),
        "session_evidence": evidence.session_evidence.document(),
        "warmup": (
            None
            if evidence.warmup_evidence is None
            else evidence.warmup_evidence.document()
        ),
        "warmup_convergence": (
            None
            if evidence.warmup_convergence_evidence is None
            else evidence.warmup_convergence_evidence.document()
        ),
        "warmup_convergence_diagnostic": (
            None
            if evidence.warmup_convergence_evidence is None
            or getattr(
                evidence.warmup_convergence_evidence, "diagnostic", None
            )
            is None
            else evidence.warmup_convergence_evidence.diagnostic.document()
        ),
        "warmup_mapping_supply_diagnostic": (
            None
            if evidence.warmup_convergence_evidence is None
            or getattr(
                evidence.warmup_convergence_evidence,
                "mapping_supply_diagnostic",
                None,
            )
            is None
            else evidence.warmup_convergence_evidence.mapping_supply_diagnostic.document()
        ),
        "warmup_structure_lineage_diagnostic": (
            None
            if evidence.warmup_convergence_evidence is None
            or getattr(
                evidence.warmup_convergence_evidence,
                "structure_lineage_diagnostic",
                None,
            )
            is None
            else evidence.warmup_convergence_evidence.structure_lineage_diagnostic.document()
        ),
        "membership_mode": "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED",
        "source_mode": source_mode,
        "research_bridge_parameter_set_id": (
            sector_native_daily_research_bridge_contract()["parameter_set_id"]
            if source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
            else None
        ),
        "strict_same_5m_warmup": (
            None
            if resolution.strict_warmup_evidence is None
            else resolution.strict_warmup_evidence.document()
        ),
        "strict_same_5m_warmup_convergence": (
            None
            if resolution.strict_warmup_convergence_evidence is None
            else resolution.strict_warmup_convergence_evidence.document()
        ),
        "strict_same_5m_warmup_convergence_diagnostic": (
            None
            if resolution.strict_warmup_convergence_evidence is None
            or getattr(
                resolution.strict_warmup_convergence_evidence,
                "diagnostic",
                None,
            )
            is None
            else resolution.strict_warmup_convergence_evidence.diagnostic.document()
        ),
        "strict_same_5m_warmup_mapping_supply_diagnostic": (
            None
            if resolution.strict_warmup_convergence_evidence is None
            or getattr(
                resolution.strict_warmup_convergence_evidence,
                "mapping_supply_diagnostic",
                None,
            )
            is None
            else resolution.strict_warmup_convergence_evidence.mapping_supply_diagnostic.document()
        ),
        "strict_same_5m_warmup_structure_lineage_diagnostic": (
            None
            if resolution.strict_warmup_convergence_evidence is None
            or getattr(
                resolution.strict_warmup_convergence_evidence,
                "structure_lineage_diagnostic",
                None,
            )
            is None
            else resolution.strict_warmup_convergence_evidence.structure_lineage_diagnostic.document()
        ),
        "strict_same_5m_source_coverage": (
            None
            if resolution.strict_source_coverage_evidence is None
            else resolution.strict_source_coverage_evidence.document()
        ),
        "fallback_unavailable_reason_codes": (
            resolution.fallback_unavailable_reason_codes
        ),
    }


def _period_frame(
    daily_frame: pd.DataFrame,
    *,
    sessions: Sequence[date],
    observed_at: datetime,
    period: str,
) -> pd.DataFrame:
    daily_rows = tuple(
        DailyMarketBar(
            session=pd.Timestamp(row.date).date(),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
            known_at=pd.Timestamp(row.date).to_pydatetime(),
        )
        for row in daily_frame.itertuples(index=False)
    )
    completed = aggregate_completed_period_bars(
        daily_rows,
        trading_sessions=sessions,
        decision_time=observed_at,
        period=period,
        calendar_coverage_end=sessions[-1],
    )
    return pd.DataFrame(
        (
            {
                "date": row.known_at,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
            for row in completed
        ),
        columns=("date", "open", "high", "low", "close", "volume"),
    )


def _frame_metadata(
    *,
    archive_dir: Path,
    frames_dir: Path,
    stem: str,
    interval: str,
    frame: pd.DataFrame,
) -> dict[str, object]:
    normalized = normalize_sector_chart_frame(frame)
    path = frames_dir / f"{stem}.{interval}.parquet"
    _atomic_parquet(path, normalized)
    return {
        "interval": interval,
        "path": path.relative_to(archive_dir).as_posix(),
        "file_sha256": _sha256_file(path),
        "content_sha256": sector_chart_frame_content_sha256(normalized),
        "row_count": len(normalized),
        "first_at": (
            None
            if normalized.empty
            else pd.Timestamp(normalized.iloc[0]["date"]).isoformat()
        ),
        "last_at": (
            None
            if normalized.empty
            else pd.Timestamp(normalized.iloc[-1]["date"]).isoformat()
        ),
    }


def build(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve(strict=True)
    artifact_path = (
        args.artifact if args.artifact.is_absolute() else root / args.artifact
    ).resolve(strict=True)
    pit_path = (
        args.pit_snapshot
        if args.pit_snapshot.is_absolute()
        else root / args.pit_snapshot
    ).resolve(strict=True)
    catalog_path = (
        args.catalog_ledger
        if args.catalog_ledger.is_absolute()
        else root / args.catalog_ledger
    ).resolve(strict=True)
    output_path = (
        args.output if args.output.is_absolute() else root / args.output
    ).resolve()
    if output_path.name != "manifest.json":
        raise ValueError("sector chart archive output must be manifest.json")
    if output_path.exists() and not args.force:
        raise FileExistsError(
            "sector chart archive already exists; use --force after review"
        )
    artifact, artifact_file_sha256 = _load_artifact(artifact_path)
    input_hashes = dict(artifact["input_hashes"])
    if _sha256_file(pit_path) != input_hashes.get("pit_snapshot"):
        raise ValueError("PIT snapshot no longer matches the research artifact")
    catalog_entry, catalog_bound_sha256 = _bound_catalog(
        catalog_path,
        expected_entry_sha256=str(input_hashes["current_catalog_entry"]),
        expected_ledger_sha256=str(input_hashes["current_catalog_ledger"]),
    )
    data_dir = _data_dir(args, root=root)

    variant = dict(artifact["parameter_snapshots"])["research_variant"]
    warmup_start = date.fromisoformat(str(variant["warmup_start"]))
    requested_end = date.fromisoformat(str(variant["requested_end"]))
    start_at = datetime.combine(warmup_start, time(9, 30), tzinfo=CN)
    end_at = datetime.combine(requested_end, time(15), tzinfo=CN)
    sessions, calendar_revision, calendar_sources = _calendar_sessions(
        data_dir=data_dir, start_at=start_at, end_at=end_at
    )

    sectors = {
        str(raw["sector_id"]): {
            "name": str(raw["name"]),
            "members": tuple(sorted(set(map(str, raw["member_codes"])))),
        }
        for raw in catalog_entry["sectors"]
    }
    sector_audit = artifact["higher_timeframe_effectiveness_audit"]["subjects"][
        "sector"
    ]
    point_audits = tuple(
        sector_audit[key]
        for key in (
            "globally_deduplicated_point_audit",
            "globally_deduplicated_diagnostic_buy_point_audit",
            "warmup_non_monotonic_point_audit",
            "warmup_mapping_supply_point_audit",
        )
        if key in sector_audit
    )
    point_keys = sorted(
        {
            (str(point["source_symbol"]), int(point["review_as_of_unix"]))
            for point_audit in point_audits
            for point in point_audit["points"]
        },
        key=lambda value: (value[1], value[0]),
    )
    if not point_keys:
        raise ValueError("research artifact contains no sector audit points")
    required_sector_ids = {value[0] for value in point_keys}
    missing = required_sector_ids.difference(sectors)
    if missing:
        raise ValueError(f"sector audit points are absent from catalog: {sorted(missing)}")
    pit_index = PITMetadataIndex(load_snapshot(pit_path))
    member_codes = tuple(
        sorted(
            {
                code
                for sector_id in required_sector_ids
                for code in sectors[sector_id]["members"]
            }
        )
    )
    source = CurrentQmtGics3CompositeReplaySource(
        data_dir=data_dir,
        start_at=start_at,
        end_at=end_at,
        factors_by_code={
            code: pit_index.factors_for(code) for code in member_codes
        },
    )
    archive_dir = output_path.parent
    frames_dir = archive_dir / "frames"
    entries: list[dict[str, object]] = []
    for sector_id, review_as_of_unix in point_keys:
        observed_at = datetime.fromtimestamp(review_as_of_unix, tz=CN)
        candidate, expected_evidence = _candidate_sector_evidence(
            artifact, sector_id=sector_id, review_as_of=observed_at
        )
        members = sectors[sector_id]["members"]
        visible_sessions = tuple(
            value for value in sessions if value <= observed_at.date()
        )
        five = source.five_minute_prefix(
            sector_id=sector_id,
            member_codes=members,
            observed_at=observed_at,
        )
        daily = source.native_daily_prefix(
            sector_id=sector_id,
            member_codes=members,
            observed_at=observed_at,
        )
        resolution = resolve_sector_higher_timeframe_gate(
            sector_id=sector_id,
            sector_members=members,
            five_minute_frame=five,
            observed_at=observed_at,
            trading_sessions=visible_sessions,
            calendar_coverage_end=visible_sessions[-1],
            native_daily_loader=lambda daily=daily: daily,
        )
        actual_evidence = _actual_sector_evidence(resolution)
        try:
            _assert_expected_projection(actual_evidence, expected_evidence)
        except ValueError as exc:
            raise ValueError(
                f"sector risk evidence no longer reproduces for {sector_id} "
                f"at {observed_at.isoformat()}: {exc}"
            ) from exc
        if (
            resolution.evidence.gate != candidate["sector_risk_gate"]
            or list(resolution.evidence.reason_codes)
            != list(candidate["sector_risk_blocker_codes"])
        ):
            raise ValueError("sector gate/reason evidence changed")

        thirty = derive_qmt_sector_thirty_minute_frame(five)
        weekly = _period_frame(
            daily,
            sessions=visible_sessions,
            observed_at=observed_at,
            period="W",
        )
        monthly = _period_frame(
            daily,
            sessions=visible_sessions,
            observed_at=observed_at,
            period="M",
        )
        source_revision = str(resolution.evidence.source_revision)
        entry_id = sha256_json(
            {
                "schema": "chanlun-sector-chart-evidence-entry",
                "source_artifact_content_sha256": artifact["content_sha256"],
                "sector_id": sector_id,
                "review_as_of": observed_at,
                "source_revision": source_revision,
            }
        )
        stem = entry_id.removeprefix("sha256:")
        frame_values = {
            "5": five,
            "30": thirty,
            "1D": daily,
            "1W": weekly,
            "1M": monthly,
        }
        frames = {
            interval: _frame_metadata(
                archive_dir=archive_dir,
                frames_dir=frames_dir,
                stem=stem,
                interval=interval,
                frame=frame_values[interval],
            )
            for interval in _REQUIRED_INTERVALS
        }
        period_source_revisions = {
            str(value["period"]): str(value["source_revision"])
            for value in expected_evidence["period_diagnostics"]
        }
        entries.append(
            {
                "entry_id": entry_id,
                "sector_id": sector_id,
                "sector_name": str(sectors[sector_id]["name"]),
                "review_as_of": observed_at.isoformat(),
                "review_as_of_unix": review_as_of_unix,
                "source_mode": str(resolution.source_mode),
                "source_revision": source_revision,
                "gate": str(resolution.evidence.gate),
                "reason_codes": list(resolution.evidence.reason_codes),
                "period_source_revisions": period_source_revisions,
                "price_basis_revision": str(five.attrs["price_basis_revision"]),
                "five_minute_base_stream_revision": str(
                    thirty.attrs["source_base_stream_revision"]
                ),
                "native_daily_base_stream_revision": str(
                    daily.attrs["source_base_stream_revision"]
                ),
                "calendar_revision": calendar_revision,
                "calendar_session_count": len(visible_sessions),
                "member_count": len(members),
                "composite_member_count": len(
                    tuple(five.attrs["sector_composite_members"])
                ),
                "pricescale": 1_000_000,
                "frames": frames,
            }
        )

    source_artifact = {
        "relative_path": artifact_path.relative_to(root).as_posix(),
        "file_sha256": artifact_file_sha256,
        "content_sha256": str(artifact["content_sha256"]),
        "risk_audit_sha256": str(
            artifact["higher_timeframe_effectiveness_audit"]["audit_sha256"]
        ),
        "decision_source_aggregate_sha256": str(
            artifact["decision_source_snapshot"]["aggregate_sha256"]
        ),
    }
    stable: dict[str, object] = {
        "schema": SECTOR_CHART_ARCHIVE_SCHEMA,
        "live_status": "LIVE_DISABLED",
        "result_status": "RESEARCH_ONLY",
        "chart_role": "CAUSAL_HUMAN_REVIEW_EVIDENCE_ONLY",
        "point_marker_semantics": "TIME_ONLY_NO_PRICE_ANCHOR",
        "source_artifact": source_artifact,
        "input_hashes": {
            "pit_snapshot": _sha256_file(pit_path),
            "current_catalog_ledger": catalog_bound_sha256,
            "current_catalog_entry": str(catalog_entry["entry_sha256"]),
        },
        "qmt_transport": "LOCAL_FIXED_RECORD_READ_ONLY",
        "warmup_start": start_at.isoformat(),
        "source_range_end": end_at.isoformat(),
        "calendar_revision": calendar_revision,
        "calendar_sources": calendar_sources,
        "supported_intervals": list(_REQUIRED_INTERVALS),
        "entries": sorted(
            entries,
            key=lambda value: (
                int(value["review_as_of_unix"]), str(value["sector_id"])
            ),
        ),
    }
    stable_json = _normal(stable)
    assert isinstance(stable_json, dict)
    document = {
        **stable_json,
        "content_sha256": sha256_json(stable_json),
    }
    _atomic_json(output_path, document)
    archive = load_sector_chart_archive(
        root,
        expected_source_artifact_file_sha256=artifact_file_sha256,
        expected_source_artifact_content_sha256=str(artifact["content_sha256"]),
        expected_risk_audit_sha256=str(
            artifact["higher_timeframe_effectiveness_audit"]["audit_sha256"]
        ),
        expected_decision_source_sha256=str(
            artifact["decision_source_snapshot"]["aggregate_sha256"]
        ),
        expected_input_hashes=input_hashes,
    )
    total_rows = 0
    for entry in archive.entries_by_id.values():
        for interval in _REQUIRED_INTERVALS:
            _validated_entry, frame = load_sector_chart_frame(
                archive, entry_id=str(entry["entry_id"]), interval=interval
            )
            total_rows += len(frame)
    referenced_paths = {
        (archive.manifest_path.parent / str(metadata["path"])).resolve()
        for entry in archive.entries_by_id.values()
        for metadata in entry["frames"].values()
    }
    orphan_paths = tuple(
        path.resolve()
        for path in frames_dir.glob("*.parquet")
        if path.resolve() not in referenced_paths
    )
    for path in orphan_paths:
        path.relative_to(frames_dir.resolve())
        path.unlink()
    return {
        "schema": SECTOR_CHART_ARCHIVE_SCHEMA,
        "manifest": output_path.relative_to(root).as_posix(),
        "manifest_file_sha256": _sha256_file(output_path),
        "manifest_content_sha256": archive.content_sha256,
        "entry_count": len(entries),
        "sector_count": len({str(value["sector_id"]) for value in entries}),
        "frame_count": len(entries) * len(_REQUIRED_INTERVALS),
        "total_frame_rows": total_rows,
        "removed_orphan_frame_count": len(orphan_paths),
        "supported_intervals": list(_REQUIRED_INTERVALS),
        "live_status": "LIVE_DISABLED",
    }


def main(argv: Sequence[str] | None = None) -> int:
    result = build(parser().parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
