#!/usr/bin/env python3
"""Build the frozen QMT research approximation for sector-first replay.

The output is intentionally not a signed three-program adjudication.  It is a
point-in-time, reproducible proxy that keeps the individual-stock backtest at
``RESEARCH_ONLY / LIVE_DISABLED`` while still allowing a non-empty research
simulation when the external financial-data service is unavailable.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    PITMetadataSnapshot,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
    read_qmt_local_kline,
    read_qmt_local_pershare,
    resolve_qmt_local_data_dir,
)
from chanlun.decision_support.trading_system.v3_research_approximation import (  # noqa: E402
    ResearchApproximationEvent,
    ResearchApproximationLedger,
    ResearchApproximationObservation,
    ResearchApproximationParameters,
    evaluate_sector_research_approximation,
)
from chanlun.decision_support.trading_system.v3_sector_first_scope import (  # noqa: E402
    build_sector_first_scope,
)
from chanlun.decision_support.trading_system.v3_sector_first_trigger_plan import (  # noqa: E402
    SectorFirstTriggerLedger,
)


DEFAULT_ROOT = Path("audit/chanlun_trading_system_backtest/sector_first_full_market")
DEFAULT_SCOPE = Path("audit/chanlun_live_integration/v3_sector_first_full_market_scope.json")
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument(
        "--trigger-ledger",
        type=Path,
        default=DEFAULT_ROOT / "sector_first_trigger_ledger.pkl",
    )
    value.add_argument("--output-dir", type=Path, default=DEFAULT_ROOT)
    value.add_argument("--qmt-local-data-dir", type=Path)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode(
            "utf-8"
        ),
    )


def _monthly_checkpoints(
    ledger: SectorFirstTriggerLedger,
) -> tuple[datetime, ...]:
    """Use the final completed sector bar in every calendar month.

    The research program is an independent monthly snapshot.  Sector
    technical eligibility is joined again at the exact entry timestamp; it
    must not leak into, or suppress, the independent fundamental/relative-
    value classification here.
    """

    output: dict[tuple[int, int], datetime] = {}
    for event in ledger.events:
        key = (event.observed_at.year, event.observed_at.month)
        output[key] = event.observed_at
    return tuple(output[key] for key in sorted(output))


def _canonical_scope_hash(
    *,
    scope_document: Mapping[str, object],
    snapshot: PITMetadataSnapshot,
) -> tuple[str, tuple[str, ...]]:
    """Rebuild the strategy scope independently of report-only metadata.

    ``audit_v3_sector_first_scope.py`` deliberately appends the current-GICS
    diagnostic and source-file provenance to its JSON report.  Those fields
    change the report hash, but they do not change the frozen strategy scope
    consumed by the trigger ledger.  Reconstructing the scope from the PIT
    snapshot proves the symbols and dates are identical without weakening the
    trigger/scope identity gate.
    """

    start = date.fromisoformat(str(scope_document["requested_start"]))
    end = date.fromisoformat(str(scope_document["requested_end"]))
    canonical = build_sector_first_scope(
        snapshot,
        requested_start=start,
        requested_end=end,
    )
    reported = tuple(sorted(str(value) for value in scope_document["selected_symbols"]))
    if reported != canonical.selected_symbols:
        raise ValueError("reported sector scope symbols differ from the PIT reconstruction")
    return canonical.content_sha256, canonical.selected_symbols


def _trigger_event_at(
    ledger: SectorFirstTriggerLedger,
    observed_at: datetime,
):
    matches = tuple(value for value in ledger.events if value.observed_at == observed_at)
    if len(matches) != 1:
        raise ValueError("monthly research checkpoint is absent from trigger ledger")
    return matches[0]


def _decimal(value: object | None) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value))


def _positive_decimal(value: object | None) -> Decimal | None:
    """Return a positive valuation input or preserve it as explicitly missing."""

    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _observation(
    *,
    code: str,
    sector_id: str,
    observed_at: datetime,
    daily: pd.DataFrame,
    daily_sha256: str,
    finance: tuple[object, ...],
    finance_sha256: str,
    parameters: ResearchApproximationParameters,
) -> ResearchApproximationObservation:
    visible_daily = daily[
        daily["session"].map(
            lambda session: datetime.combine(session, time(15, 0), tzinfo=observed_at.tzinfo)
            <= observed_at
        )
    ]
    lookback = visible_daily.tail(parameters.liquidity_lookback_sessions)
    daily_complete = len(lookback) == parameters.liquidity_lookback_sessions
    last_close = _decimal(visible_daily.iloc[-1]["close"]) if not visible_daily.empty else None
    amounts = tuple(
        Decimal(str(value))
        for value in lookback.get("amount", pd.Series(dtype="float64"))
        if pd.notna(value) and float(value) > 0
    )
    median_amount = None
    if daily_complete and len(amounts) == parameters.liquidity_lookback_sessions:
        ordered = sorted(amounts)
        middle = len(ordered) // 2
        median_amount = (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    visible_finance = tuple(
        value for value in finance if getattr(value, "known_at") <= observed_at
    )
    latest = visible_finance[-1] if visible_finance else None
    source_revision = sha256_json(
        {
            "schema": "chanlun-v3-qmt-research-observation-source/v1",
            "code": code,
            "observed_at": observed_at,
            "daily_source_sha256": daily_sha256,
            "daily_last_session": (
                None
                if visible_daily.empty
                else visible_daily.iloc[-1]["session"].isoformat()
            ),
            "finance_source_sha256": finance_sha256,
            "finance_record_ordinal": (
                None if latest is None else getattr(latest, "source_record_ordinal")
            ),
        }
    )
    return ResearchApproximationObservation(
        symbol=code,
        sector_id=sector_id,
        observed_at=observed_at,
        last_completed_daily_close=last_close,
        median_daily_amount_20=median_amount,
        book_value_per_share=(
            None
            if latest is None
            else _positive_decimal(getattr(latest, "get")("book_value_per_share"))
        ),
        roe=None if latest is None else _decimal(getattr(latest, "get")("roe")),
        revenue_yoy=(
            None if latest is None else _decimal(getattr(latest, "get")("revenue_yoy"))
        ),
        parent_profit_yoy=(
            None
            if latest is None
            else _decimal(getattr(latest, "get")("parent_profit_yoy"))
        ),
        daily_known_at=(
            None
            if visible_daily.empty
            else datetime.combine(
                visible_daily.iloc[-1]["session"],
                time(15, 0),
                tzinfo=observed_at.tzinfo,
            )
        ),
        finance_known_at=None if latest is None else getattr(latest, "known_at"),
        source_revision=source_revision,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.qmt_local_data_dir is not None:
        os.environ[QMT_LOCAL_DATA_ENV] = str(args.qmt_local_data_dir.resolve())
    data_dir = resolve_qmt_local_data_dir()
    if data_dir is None:
        raise RuntimeError(f"{QMT_LOCAL_DATA_ENV} is required")
    scope_path = args.scope.resolve()
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    snapshot_path = args.pit_snapshot.resolve()
    snapshot = load_snapshot(snapshot_path)
    canonical_scope_sha256, codes = _canonical_scope_hash(
        scope_document=scope,
        snapshot=snapshot,
    )
    index = PITMetadataIndex(snapshot)
    trigger_path = args.trigger_ledger.resolve()
    trigger = pickle.loads(trigger_path.read_bytes())
    if not isinstance(trigger, SectorFirstTriggerLedger):
        raise ValueError("sector trigger ledger checkpoint is invalid")
    if trigger.sector_scope_sha256 != canonical_scope_sha256:
        raise ValueError("research proxy and sector trigger use different scopes")
    checkpoints = _monthly_checkpoints(trigger)
    if not checkpoints:
        raise RuntimeError("sector trigger ledger has no monthly checkpoints")
    parameters = ResearchApproximationParameters()
    read_start = checkpoints[0] - timedelta(days=90)
    read_end = checkpoints[-1]
    daily_by_code: dict[str, pd.DataFrame] = {}
    finance_by_code: dict[str, tuple[object, ...]] = {}
    source_by_code: dict[str, tuple[str, str]] = {}
    for ordinal, code in enumerate(codes, start=1):
        daily, daily_audit = read_qmt_local_kline(
            data_dir=data_dir,
            code=code,
            frequency="1d",
            start_at=read_start,
            end_at=read_end,
        )
        if not daily.empty:
            daily = daily.copy()
            daily["session"] = pd.to_datetime(
                daily["time"], unit="ms", utc=True
            ).dt.tz_convert("Asia/Shanghai").dt.date
        else:
            daily["session"] = pd.Series(dtype="object")
        finance, finance_audit = read_qmt_local_pershare(
            data_dir=data_dir,
            code=code,
        )
        daily_by_code[code] = daily
        finance_by_code[code] = tuple(finance)
        source_by_code[code] = (
            daily_audit.source_sha256,
            finance_audit.source_sha256,
        )
        if ordinal % 500 == 0 or ordinal == len(codes):
            print(f"loaded research inputs {ordinal}/{len(codes)}", flush=True)

    events: list[ResearchApproximationEvent] = []
    for observed_at in checkpoints:
        # Assert that the frozen checkpoint really belongs to the sector
        # ledger, but do not reuse its contemporaneous technical ranking as a
        # fundamental input.  The exact sector trigger is applied later at
        # every candidate timestamp.
        _trigger_event_at(trigger, observed_at)
        grouped: dict[str, list[ResearchApproximationObservation]] = {}
        for code in codes:
            security = index.security(code)
            if not security.listed_on(observed_at.date()):
                continue
            membership = index.membership_at(code, observed_at)
            if membership is None:
                continue
            daily_sha, finance_sha = source_by_code[code]
            grouped.setdefault(membership.sector_id, []).append(
                _observation(
                    code=code,
                    sector_id=membership.sector_id,
                    observed_at=observed_at,
                    daily=daily_by_code[code],
                    daily_sha256=daily_sha,
                    finance=finance_by_code[code],
                    finance_sha256=finance_sha,
                    parameters=parameters,
                )
            )
        decisions = tuple(
            sorted(
                (
                    decision
                    for sector_id, rows in sorted(grouped.items())
                    for decision in evaluate_sector_research_approximation(
                        rows,
                        sector_triggered=True,
                        parameters=parameters,
                    )
                ),
                key=lambda value: value.symbol,
            )
        )
        events.append(ResearchApproximationEvent(observed_at, decisions))
        print(
            f"research checkpoint {observed_at.isoformat()} "
            f"accepted={sum(value.accepted for value in decisions)}/{len(decisions)}",
            flush=True,
        )

    ledger = ResearchApproximationLedger(
        parameters=parameters,
        sector_scope_sha256=canonical_scope_sha256,
        pit_snapshot_sha256=_sha256_file(snapshot_path),
        trigger_ledger_sha256=_sha256_file(trigger_path),
        events=tuple(events),
    )
    output_dir = args.output_dir.resolve()
    checkpoint_path = output_dir / "research_approximation_ledger.pkl"
    _atomic_bytes(checkpoint_path, pickle.dumps(ledger, protocol=pickle.HIGHEST_PROTOCOL))
    rows = []
    for event in ledger.events:
        rejection_counts = Counter(
            reason
            for value in event.decisions
            if not value.accepted
            for reason in value.reason_codes
        )
        accepted_by_sector = Counter(
            value.sector_id for value in event.decisions if value.accepted
        )
        rows.append(
            {
                "observed_at": event.observed_at.isoformat(),
                "evaluated_symbol_count": len(event.decisions),
                "accepted_symbol_count": sum(value.accepted for value in event.decisions),
                "accepted_by_sector": dict(sorted(accepted_by_sector.items())),
                "rejection_counts": dict(sorted(rejection_counts.items())),
            }
        )
    document: dict[str, object] = {
        "schema": ledger.schema,
        "parameter_snapshot": asdict(parameters),
        "parameter_set_id": parameters.parameter_set_id,
        "sector_scope_sha256": ledger.sector_scope_sha256,
        "pit_snapshot_sha256": ledger.pit_snapshot_sha256,
        "trigger_ledger_sha256": ledger.trigger_ledger_sha256,
        "event_count": len(ledger.events),
        "ever_accepted_symbol_count": len(ledger.ever_accepted_symbols),
        "ever_accepted_symbols_sha256": sha256_json(ledger.ever_accepted_symbols),
        "events": rows,
        "strict_three_program_authority": False,
        "approximation_disclosure": (
            "liquidity substitutes for unavailable point-in-time market cap; "
            "sector opportunity and peer thresholds are frozen engineering proxies"
        ),
        "data_grade": ledger.data_grade,
        "highest_status": ledger.highest_status,
        "live_status": ledger.live_status,
        "checkpoint_file": str(checkpoint_path),
        "checkpoint_file_sha256": _sha256_file(checkpoint_path),
    }
    document["content_sha256"] = sha256_json(document)
    _atomic_json(output_dir / "research_approximation_ledger.json", document)
    print(json.dumps(document, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
