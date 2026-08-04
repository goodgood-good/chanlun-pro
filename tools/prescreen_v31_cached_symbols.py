#!/usr/bin/env python3
"""Batch-prescreen cached minute symbols for strict V3.1 entry evidence.

This is a read-only outer adapter.  It consumes the frozen 30m/5m/1m
structure output and deliberately keeps the L1 definition at a *completed
5m TrendType*.  Constituent units are never accepted as L1 evidence.

The result is a structural prescreen, not a portfolio backtest.  A symbol
without a point-in-time causal adjustment ledger may still be inspected on
raw prices, but its raw alignment count is not promoted to the admissible
legal-chain count.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable, Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    _causal_confirmed_structure_events,
    final_confirmed_structure_events,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    completed_l1_trend_fact,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    V31AlignmentDecision,
    align_v31_independent_entry_chains,
    confirmation_bar_fact,
    v31_alignment_contract,
)
from chanlun.exchange.kline_precision import resolve_structure_price_quantum
from chanlun.exchange.price_basis import (
    PriceBasisMetadata,
    attach_price_basis_metadata,
    build_price_basis_revision,
)
from tools.chanlun_v3_research_data import (
    BENCHMARK_SYMBOL,
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    DistributionEvent,
    aggregate_completed_bars,
    apply_causal_forward_adjustments,
    atomic_json,
    causal_adjustment_ledger,
    content_sha256,
    longest_complete_interval,
    read_cached_series,
    sha256_file,
)


DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/v31_cached_symbol_prescreen.json"
)
DEFAULT_SEED = Path(
    "audit/chanlun_live_integration/v31_independent_timeframe_structure.json"
)
DEFAULT_CORPORATE_ACTIONS = Path(
    "audit/chanlun_live_integration/qmt_etf_corporate_actions_v1.json"
)
_PROVIDER_SYMBOL = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")
_IMPLEMENTATION_DEPENDENCIES = (
    Path(__file__).resolve(),
    (PROJECT_ROOT / "tools/chanlun_v3_research_data.py").resolve(),
    (
        SOURCE_ROOT
        / "chanlun/decision_support/trading_system/backtest/fixed_year.py"
    ).resolve(),
    (
        SOURCE_ROOT
        / "chanlun/decision_support/trading_system/v3_timeframe_alignment.py"
    ).resolve(),
    (
        SOURCE_ROOT
        / "chanlun/decision_support/trading_system/v31_timeframe_alignment.py"
    ).resolve(),
    (SOURCE_ROOT / "chanlun/exchange/kline_precision.py").resolve(),
    (SOURCE_ROOT / "chanlun/exchange/price_basis.py").resolve(),
)


def provider_to_project_code(symbol: str) -> str:
    """Map a provider identity to the existing project's A-share identity."""

    normalized = symbol.strip().upper()
    matched = _PROVIDER_SYMBOL.fullmatch(normalized)
    if matched is None:
        raise ValueError(f"unsupported cached A-share symbol: {symbol!r}")
    return f"{matched.group('exchange')}.{matched.group('code')}"


def discover_minute_symbols(database: Path) -> tuple[dict[str, object], ...]:
    """List locally cached, unsplit one-minute series without mutating SQLite."""

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT symbol, COUNT(*), MIN(bar_time), MAX(bar_time)
            FROM bars
            WHERE period='P_Min1' AND adj_type='S_Unsplit'
            GROUP BY symbol
            ORDER BY symbol
            """
        ).fetchall()
    output: list[dict[str, object]] = []
    for symbol, count, first, last in rows:
        try:
            project_code = provider_to_project_code(str(symbol))
        except ValueError:
            project_code = None
        output.append(
            {
                "provider_symbol": str(symbol),
                "project_code": project_code,
                "rows": int(count),
                "first": str(first),
                "last": str(last),
                "supported_a_share_identity": project_code is not None,
            }
        )
    return tuple(output)


def _optional_distributions(
    database: Path,
    *,
    project_code: str,
) -> tuple[DistributionEvent, ...]:
    """Return dated ETF distributions, or empty when no ledger exists.

    Empty means unknown, not "the symbol paid no distribution".  The caller
    must therefore fail closed for formal chain eligibility.
    """

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "etf_distributions" not in tables:
            return ()
        rows = connection.execute(
            """
            SELECT ex_date, cash_per_share, cumulative_cash_per_share, source
            FROM etf_distributions
            WHERE UPPER(symbol)=?
            ORDER BY ex_date
            """,
            (project_code.upper(),),
        ).fetchall()
    return tuple(
        DistributionEvent(
            ex_date=date.fromisoformat(str(row[0])),
            cash_per_share=Decimal(str(row[1])),
            cumulative_cash_per_share=Decimal(str(row[2])),
            source=str(row[3]),
        )
        for row in rows
    )


def _qmt_corporate_action_events(
    snapshot: Path,
    *,
    provider_symbol: str,
) -> tuple[dict[str, object], ...]:
    """Load hash-verified effective-dated QMT ``dr`` events read-only."""

    if not snapshot.is_file():
        return ()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if payload.get("schema") != "chanlun-qmt-etf-corporate-actions/v1":
        raise RuntimeError("QMT corporate-action snapshot schema is invalid")
    stable_envelope = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    if payload.get("content_sha256") != content_sha256(stable_envelope):
        raise RuntimeError("QMT corporate-action snapshot content hash is invalid")
    matching = tuple(
        item
        for item in payload.get("instruments", ())
        if isinstance(item, Mapping)
        and str(item.get("code", "")).upper() == provider_symbol.upper()
    )
    if len(matching) != 1:
        return ()
    instrument = matching[0]
    if (
        instrument.get("status") != "EFFECTIVE_DATED_EVENTS_AVAILABLE"
        or not str(instrument.get("causal_application", "")).startswith(
            "ALLOWED_FROM_EFFECTIVE_SESSION_ONLY"
        )
    ):
        return ()
    raw_events = instrument.get("events", ())
    if instrument.get("events_sha256") != content_sha256(raw_events):
        raise RuntimeError("QMT instrument event-ledger hash is invalid")
    output: list[dict[str, object]] = []
    for event in raw_events:
        if not isinstance(event, Mapping):
            raise RuntimeError("QMT corporate-action event is malformed")
        raw = event.get("raw")
        if not isinstance(raw, Mapping):
            raise RuntimeError("QMT corporate-action raw fields are missing")
        if (
            event.get("availability_policy")
            != "EFFECTIVE_SESSION_OPEN_RESEARCH_ASSUMPTION"
        ):
            raise RuntimeError("QMT corporate-action availability policy changed")
        effective_on = date.fromisoformat(str(event.get("effective_on")))
        divisor = Decimal(str(raw.get("dr")))
        if divisor <= 0:
            raise RuntimeError("QMT corporate-action dr must be positive")
        output.append(
            {
                "effective_on": effective_on,
                "raw_price_divisor": divisor,
                "availability_policy": event["availability_policy"],
            }
        )
    keys = tuple(item["effective_on"] for item in output)
    if not output or keys != tuple(sorted(set(keys))):
        raise RuntimeError("QMT corporate-action dates must be non-empty and unique")
    return tuple(output)


def _apply_qmt_dr_adjustments(
    frame: pd.DataFrame,
    events: Sequence[Mapping[str, object]],
) -> pd.DataFrame:
    """Apply each effective-session ``dr`` only from that session onward."""

    adjusted = frame.copy()
    multiplier = pd.Series(1.0, index=adjusted.index, dtype="float64")
    sessions = adjusted["date"].dt.date
    for event in events:
        effective_on = event["effective_on"]
        if not isinstance(effective_on, date):
            raise TypeError("QMT corporate-action effective date is invalid")
        divisor = float(Decimal(str(event["raw_price_divisor"])))
        multiplier.loc[sessions >= effective_on] *= divisor
    for field in ("open", "high", "low", "close"):
        adjusted[field] = adjusted[field] * multiplier
    return adjusted


def _events_in_interval(
    events: Sequence[Mapping[str, object]],
    *,
    start: date,
    end: date,
) -> tuple[Mapping[str, object], ...]:
    if start > end:
        raise ValueError("corporate-action interval is inverted")
    return tuple(
        event
        for event in events
        if start <= event["effective_on"] <= end
    )


def _attach_metadata(
    adjusted: pd.DataFrame,
    raw: pd.DataFrame,
    *,
    project_code: str,
    adjustment: str,
    adjustment_ledger_sha256: str,
) -> pd.DataFrame:
    if len(adjusted) != len(raw) or not adjusted["date"].equals(raw["date"]):
        raise ValueError("raw and adjusted cached frames are not aligned")
    frame = adjusted.copy()
    for field in ("open", "high", "low", "close"):
        frame[f"raw_{field}"] = raw[field].to_numpy(copy=True)
    frame.insert(0, "code", project_code)
    frame = frame.loc[:, list(FRAME_COLUMNS)]
    quantum = resolve_structure_price_quantum("a", project_code)
    if quantum is None:
        raise RuntimeError(f"structure price quantum is unavailable: {project_code}")
    provider = "financial-data-query+effective-dated-corporate-actions"
    revision = build_price_basis_revision(
        provider=provider,
        market="a",
        code=project_code,
        adjustment=adjustment,
        structure_price_quantum=quantum,
        adjustment_ledger=(
            {"effective_dated_adjustment_ledger_sha256": adjustment_ledger_sha256},
        ),
    )
    metadata = PriceBasisMetadata(
        structure_price_quantum=quantum,
        price_basis_revision=revision,
        provider=provider,
        adjustment=adjustment,
    )
    return attach_price_basis_metadata(frame, metadata)


def _build_frames(
    *,
    database: Path,
    pit_database: Path,
    corporate_actions: Path,
    benchmark_symbol: str,
    provider_symbol: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, object], dict[str, object]]:
    project_code = provider_to_project_code(provider_symbol)
    # Validate and freeze the symbol-specific event ledger before the heavier
    # minute normalization.  The instrument ledger hash is the authoritative
    # identity consumed by the price-basis revision.
    all_qmt_events = _qmt_corporate_action_events(
        corporate_actions, provider_symbol=provider_symbol
    )
    benchmark = read_cached_series(
        database, symbol=benchmark_symbol, period="P_Day1"
    )
    raw_source = read_cached_series(
        database, symbol=provider_symbol, period="P_Min1"
    )
    raw_one, interval = longest_complete_interval(raw_source, benchmark)
    qmt_events = _events_in_interval(
        all_qmt_events,
        start=interval["start"],
        end=interval["end"],
    )
    distributions = ()
    if all_qmt_events:
        ledger = qmt_events
        adjusted_one = _apply_qmt_dr_adjustments(raw_one, ledger)
        adjustment = "causal-forward-effective-session-qmt-dr-v1"
        adjustment_status = "QMT_EFFECTIVE_DATED_CAUSAL_ADJUSTMENT_AVAILABLE"
        adjustment_source = "QMT_CORPORATE_ACTION_SNAPSHOT"
        formally_eligible = True
    else:
        distributions = _optional_distributions(
            pit_database, project_code=project_code
        )
    if not all_qmt_events and distributions:
        ledger = causal_adjustment_ledger(raw_one, distributions)
        adjusted_one = apply_causal_forward_adjustments(raw_one, ledger)
        adjustment = "causal-forward-cash-distribution-v2"
        adjustment_status = "PIT_CAUSAL_ADJUSTMENT_AVAILABLE"
        adjustment_source = "LOCAL_PIT_CASH_DISTRIBUTION_LEDGER"
        formally_eligible = True
    elif not all_qmt_events:
        # Do not guess that a missing ledger means no corporate action.
        ledger = ()
        adjusted_one = raw_one.copy()
        adjustment = "raw-unadjusted-diagnostic-only"
        adjustment_status = "MISSING_PIT_CAUSAL_ADJUSTMENT_LEDGER"
        adjustment_source = "NONE"
        formally_eligible = False
    ledger_hash = content_sha256(tuple(ledger))
    output = {
        "1m": _attach_metadata(
            adjusted_one,
            raw_one,
            project_code=project_code,
            adjustment=adjustment,
            adjustment_ledger_sha256=ledger_hash,
        )
    }
    for minutes, frequency in ((5, "5m"), (30, "30m")):
        raw_aggregate = aggregate_completed_bars(raw_one, minutes=minutes)
        adjusted_aggregate = aggregate_completed_bars(
            adjusted_one, minutes=minutes
        )
        output[frequency] = _attach_metadata(
            adjusted_aggregate,
            raw_aggregate,
            project_code=project_code,
            adjustment=adjustment,
            adjustment_ledger_sha256=ledger_hash,
        )
    return output, interval, {
        "status": adjustment_status,
        "source": adjustment_source,
        "formal_chain_eligibility": formally_eligible,
        "events_in_selected_interval": len(ledger),
        "effective_dated_adjustment_ledger_sha256": ledger_hash,
        "dated_distribution_rows_for_symbol": len(distributions),
        "qmt_effective_dated_event_rows_for_symbol": len(qmt_events),
        "qmt_full_event_ledger_rows_for_symbol": len(all_qmt_events),
        "corporate_action_snapshot_sha256": (
            sha256_file(corporate_actions) if corporate_actions.is_file() else None
        ),
        "missing_data_was_inferred": False,
    }


def _counts(values: Iterable[object], field: str) -> dict[str, int]:
    return dict(
        sorted(Counter(str(getattr(value, field)) for value in values).items())
    )


def _candidate_alignment_decision_documents(
    decisions: Iterable[V31AlignmentDecision],
    *,
    l0_available_at: Mapping[str, datetime],
) -> tuple[dict[str, object], ...]:
    """Render one deterministic, time-traceable decision per L0 candidate."""

    values = tuple(decisions)
    decision_ids = tuple(item.l0_point_id for item in values)
    if len(decision_ids) != len(set(decision_ids)):
        raise RuntimeError("V3.1 alignment returned duplicate L0 decisions")
    if set(decision_ids) != set(l0_available_at):
        raise RuntimeError("V3.1 alignment decisions do not cover every L0 candidate")

    documents: list[dict[str, object]] = []
    for item in sorted(
        values,
        key=lambda value: (
            l0_available_at[value.l0_point_id],
            value.l0_point_id,
            value.window_start,
            value.window_end,
        ),
    ):
        available_at = l0_available_at[item.l0_point_id]
        document = item.document()
        document["l0_available_at"] = available_at
        document["alignment_decision_at"] = (
            item.chain.decision_at if item.chain is not None else available_at
        )
        documents.append(document)
    return tuple(documents)


def prescreen_symbol(
    *,
    database: Path,
    pit_database: Path,
    corporate_actions: Path,
    benchmark_symbol: str,
    provider_symbol: str,
) -> dict[str, object]:
    """Run all three frozen causal ledgers for one cached symbol."""

    project_code = provider_to_project_code(provider_symbol)
    frames, interval, adjustment = _build_frames(
        database=database,
        pit_database=pit_database,
        corporate_actions=corporate_actions,
        benchmark_symbol=benchmark_symbol,
        provider_symbol=provider_symbol,
    )
    ledgers = {}
    for frequency in ("30m", "5m"):
        print(
            json.dumps(
                {
                    "stage": "V31_CACHED_SYMBOL_CAUSAL_REPLAY",
                    "symbol": provider_symbol,
                    "frequency": frequency,
                    "rows": len(frames[frequency]),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        ledgers[frequency] = final_confirmed_structure_events(
            project_code, frequency, frames[frequency]
        )
    l0_points = tuple(
        point
        for point in ledgers["30m"].points
        if point.recursive_level == 0
        and point.point_type == "3buy"
        and point.center_ordinal == 1
    )
    quantum = Decimal(str(frames["5m"].attrs["structure_price_quantum"]))
    l1_trends = tuple(
        completed_l1_trend_fact(trend, price_quantum=quantum)
        for trend in ledgers["5m"].completed_trends
        if trend.structural_level == 0 and trend.complete
    )

    # A legal L2 locator can only become available after the 30m completion
    # return starts and no later than the associated L0 decision.  Restricting
    # causal checkpoints to that conservative superset does not change the
    # admissible chain set; it avoids replaying years of irrelevant 1m strict
    # recursive snapshots for a batch prescreen.
    center_by_id = {
        (fact.center_id, fact.structural_level): fact
        for fact in ledgers["30m"].center_completions
    }
    l2_visibility_windows = tuple(
        (
            center_by_id[(point.center_id, 0)].return_market_start,
            point.available_at,
        )
        for point in l0_points
        if point.center_id is not None
        and (point.center_id, 0) in center_by_id
        and center_by_id[(point.center_id, 0)].return_market_start
        <= point.available_at
    )
    print(
        json.dumps(
            {
                "stage": "V31_CACHED_SYMBOL_CAUSAL_REPLAY",
                "symbol": provider_symbol,
                "frequency": "1m",
                "rows": len(frames["1m"]),
                "visibility_windows": len(l2_visibility_windows),
                "visibility_scope": "L0_RETURN_START_THROUGH_L0_DECISION",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    # The public wrapper intentionally exposes the full ledger only.  The
    # underlying read-only causal adapter already supports visibility windows;
    # use it directly instead of changing any frozen structure/core interface.
    ledgers["1m"] = _causal_confirmed_structure_events(
        project_code,
        "1m",
        frames["1m"],
        visibility_windows=l2_visibility_windows,
    )
    l2_points = tuple(
        point
        for point in ledgers["1m"].points
        if point.recursive_level == 0 and point.point_type in {"1buy", "2buy"}
    )
    confirmation_bars = {
        point.point_id: confirmation_bar_fact(point, frames["1m"])
        for point in l2_points
    }
    decisions = align_v31_independent_entry_chains(
        l0_points=l0_points,
        l0_center_completions=ledgers["30m"].center_completions,
        l1_trends=l1_trends,
        l2_points=l2_points,
        confirmation_bars=confirmation_bars,
        l0_price_quantum=Decimal(
            str(frames["30m"].attrs["structure_price_quantum"])
        ),
    )
    candidate_decisions = _candidate_alignment_decision_documents(
        decisions,
        l0_available_at={point.point_id: point.available_at for point in l0_points},
    )
    raw_chains = tuple(item.chain for item in decisions if item.chain is not None)
    rejection_counts = Counter(
        reason for item in decisions for reason in item.reason_codes
    )
    formally_eligible = bool(adjustment["formal_chain_eligibility"])
    legal_chain_count = len(raw_chains) if formally_eligible else 0
    return {
        "provider_symbol": provider_symbol,
        "project_code": project_code,
        "evaluation_source": "FRESH_FROZEN_CAUSAL_REPLAY",
        "source_start": interval["start"],
        "source_end": interval["end"],
        "source_sessions": interval["sessions"],
        "rows_by_frequency": {
            frequency: len(frames[frequency])
            for frequency in ("1m", "5m", "30m")
        },
        "price_basis_revision": frames["5m"].attrs["price_basis_revision"],
        "adjustment_gate": adjustment,
        "l0_first_center_third_buy_count": len(l0_points),
        "l0_all_point_counts": _counts(
            (
                value
                for value in ledgers["30m"].points
                if value.recursive_level == 0
            ),
            "point_type",
        ),
        "l1_evidence_kind": "COMPLETED_TREND",
        "l1_completed_trend_count": len(l1_trends),
        "l1_completed_trend_counts_by_direction": _counts(
            l1_trends, "direction"
        ),
        "l1_constituent_unit_accepted": False,
        "l2_first_or_second_buy_count": len(l2_points),
        "l2_locator_replay_scope": "L0_RETURN_START_THROUGH_L0_DECISION",
        "l2_visibility_window_count": len(l2_visibility_windows),
        "l2_locator_counts_by_type": _counts(l2_points, "point_type"),
        "l2_confirmation_bar_count": len(confirmation_bars),
        "raw_price_structurally_aligned_chain_count": len(raw_chains),
        "structurally_legal_chain_count": legal_chain_count,
        "alignment_rejection_counts": dict(sorted(rejection_counts.items())),
        "candidate_alignment_decisions": candidate_decisions,
        "aligned_entry_chains": tuple(
            item.document() for item in raw_chains
        ) if formally_eligible else (),
        "decision": (
            "STRUCTURALLY_LEGAL_CHAINS_AVAILABLE"
            if legal_chain_count
            else "STRUCTURALLY_LEGAL_CHAIN_ZERO"
            if formally_eligible
            else "RAW_DIAGNOSTIC_ONLY_MISSING_PIT_CAUSAL_ADJUSTMENT"
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def _seed_report(
    seed: Mapping[str, object],
    *,
    market_hash: str,
    pit_hash: str,
) -> dict[str, object] | None:
    """Convert the existing 510300 causal artifact into one batch row."""

    if (
        seed.get("market_database_sha256") != market_hash
        or seed.get("pit_database_sha256") != pit_hash
        or seed.get("l1_evidence_kind") != "COMPLETED_TREND"
        or seed.get("mapping") != {"L0": "30m", "L1": "5m", "L2": "1m"}
        or seed.get("alignment_contract")
        != v31_alignment_contract().document()
        or seed.get("alignment_parameter_set_id")
        != v31_alignment_contract().parameter_set_id
    ):
        return None
    l1_ledger = seed.get("l1_completed_trend_ledger", ())
    directions = Counter(
        str(item.get("direction"))
        for item in l1_ledger
        if isinstance(item, Mapping)
    )
    legal_count = int(seed.get("aligned_entry_chain_count", 0))
    return {
        "provider_symbol": "510300.SH",
        "project_code": "SH.510300",
        "evaluation_source": "HASH_VERIFIED_SINGLE_SYMBOL_V31_CAUSAL_ARTIFACT",
        "source_start": seed.get("source_start"),
        "source_end": seed.get("source_end"),
        "source_sessions": seed.get("source_sessions"),
        "rows_by_frequency": seed.get("rows_by_frequency"),
        "price_basis_revision": (
            l1_ledger[0].get("price_basis_revision")
            if isinstance(l1_ledger, Sequence) and l1_ledger
            else None
        ),
        "adjustment_gate": {
            "status": "PIT_CAUSAL_ADJUSTMENT_AVAILABLE",
            "formal_chain_eligibility": True,
            "events_in_selected_interval": seed.get(
                "adjustment_events_applied", 0
            ),
            "dated_distribution_rows_for_symbol": None,
            "missing_data_was_inferred": False,
        },
        "l0_first_center_third_buy_count": int(
            seed.get("l0_first_center_third_buy_count", 0)
        ),
        "l0_all_point_counts": None,
        "l1_evidence_kind": "COMPLETED_TREND",
        "l1_completed_trend_count": int(
            seed.get("l1_completed_trend_count", 0)
        ),
        "l1_completed_trend_counts_by_direction": dict(sorted(directions.items())),
        "l1_constituent_unit_accepted": False,
        "l2_first_or_second_buy_count": int(
            seed.get("l2_first_or_second_buy_count", 0)
        ),
        "l2_locator_counts_by_type": None,
        "l2_confirmation_bar_count": int(
            seed.get("l2_confirmation_bar_fact_count", 0)
        ),
        "raw_price_structurally_aligned_chain_count": legal_count,
        "structurally_legal_chain_count": legal_count,
        "alignment_rejection_counts": seed.get(
            "alignment_rejection_counts", {}
        ),
        "candidate_alignment_decisions": seed.get(
            "candidate_alignment_decisions",
            seed.get("alignment_decisions", ()),
        ),
        "aligned_entry_chains": seed.get("aligned_entry_chains", ()),
        "decision": (
            "STRUCTURALLY_LEGAL_CHAINS_AVAILABLE"
            if legal_count
            else "STRUCTURALLY_LEGAL_CHAIN_ZERO"
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def _implementation_manifest(
    paths: Sequence[Path] = _IMPLEMENTATION_DEPENDENCIES,
) -> tuple[dict[str, str], ...]:
    manifest: list[dict[str, str]] = []
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            label = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            label = path.as_posix()
        manifest.append({"path": label, "sha256": sha256_file(path)})
    return tuple(sorted(manifest, key=lambda item: item["path"]))


def _implementation_sha256(
    paths: Sequence[Path] = _IMPLEMENTATION_DEPENDENCIES,
) -> str:
    return content_sha256(_implementation_manifest(paths))


def _prior_artifact_matches_inputs(
    candidate: Mapping[str, object],
    *,
    market_hash: str,
    pit_hash: str,
    implementation_hash: str,
    corporate_action_hash: str | None,
    benchmark_symbol: str,
) -> bool:
    """Require every decision-affecting cache identity before report reuse."""

    return bool(
        candidate.get("market_database_sha256") == market_hash
        and candidate.get("pit_database_sha256") == pit_hash
        and candidate.get("implementation_sha256") == implementation_hash
        and candidate.get("corporate_action_snapshot_sha256")
        == corporate_action_hash
        and candidate.get("benchmark_symbol") == benchmark_symbol
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-prescreen locally cached A-share minute symbols using strict "
            "30m L0, completed 5m L1 trends, and 1m L2 locators."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_MARKET_DATABASE)
    parser.add_argument("--pit-database", type=Path, default=DEFAULT_PIT_DATABASE)
    parser.add_argument(
        "--corporate-actions", type=Path, default=DEFAULT_CORPORATE_ACTIONS
    )
    parser.add_argument("--benchmark-symbol", default=BENCHMARK_SYMBOL)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-artifact", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-seed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.database.is_file() or not args.pit_database.is_file():
        raise FileNotFoundError("market and PIT databases are required")
    available = discover_minute_symbols(args.database)
    available_ids = {
        str(item["provider_symbol"])
        for item in available
        if item["supported_a_share_identity"]
    }
    requested = tuple(
        dict.fromkeys(
            symbol.strip().upper()
            for symbol in (args.symbols or sorted(available_ids))
        )
    )
    unknown = tuple(symbol for symbol in requested if symbol not in available_ids)
    if unknown:
        raise ValueError(f"requested minute series are not cached: {unknown!r}")
    if args.max_symbols is not None:
        if args.max_symbols <= 0:
            raise ValueError("--max-symbols must be positive")
        requested = requested[: args.max_symbols]

    market_hash = sha256_file(args.database)
    pit_hash = sha256_file(args.pit_database)
    implementation_hash = _implementation_sha256()
    implementation_manifest = _implementation_manifest()
    corporate_action_hash = (
        sha256_file(args.corporate_actions)
        if args.corporate_actions.is_file()
        else None
    )
    prior = None
    if args.output.is_file() and not args.force:
        candidate = json.loads(args.output.read_text(encoding="utf-8"))
        if _prior_artifact_matches_inputs(
            candidate,
            market_hash=market_hash,
            pit_hash=pit_hash,
            implementation_hash=implementation_hash,
            corporate_action_hash=corporate_action_hash,
            benchmark_symbol=args.benchmark_symbol,
        ):
            prior = candidate
    prior_reports = {
        str(item["provider_symbol"]): item
        for item in (prior or {}).get("symbol_reports", ())
    }
    seed_report = None
    # Legacy single-symbol artifacts did not bind their price-basis revision
    # to the effective-dated event-ledger hash.  Once the QMT snapshot exists,
    # replay afresh instead of importing that weaker basis identity.
    if (
        args.seed_artifact.is_file()
        and not args.no_seed
        and corporate_action_hash is None
    ):
        seed = json.loads(args.seed_artifact.read_text(encoding="utf-8"))
        seed_report = _seed_report(
            seed, market_hash=market_hash, pit_hash=pit_hash
        )

    reports: list[dict[str, object]] = []
    for symbol in requested:
        if symbol in prior_reports:
            report = dict(prior_reports[symbol])
            report["batch_cache_reused"] = True
        elif seed_report is not None and symbol == "510300.SH":
            report = dict(seed_report)
            report["batch_cache_reused"] = True
        else:
            try:
                report = prescreen_symbol(
                    database=args.database,
                    pit_database=args.pit_database,
                    corporate_actions=args.corporate_actions,
                    benchmark_symbol=args.benchmark_symbol,
                    provider_symbol=symbol,
                )
                report["batch_cache_reused"] = False
            except Exception as exc:  # retain other symbols' safe diagnostics
                report = {
                    "provider_symbol": symbol,
                    "project_code": provider_to_project_code(symbol),
                    "decision": "PRESCREEN_ERROR",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "structurally_legal_chain_count": 0,
                    "highest_status": "RESEARCH_ONLY",
                    "live_status": "LIVE_DISABLED",
                    "batch_cache_reused": False,
                }
        reports.append(report)
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "decision": report["decision"],
                    "l0_candidates": report.get(
                        "l0_first_center_third_buy_count"
                    ),
                    "l1_complete_trends": report.get(
                        "l1_completed_trend_count"
                    ),
                    "l2_locators": report.get(
                        "l2_first_or_second_buy_count"
                    ),
                    "legal_chains": report.get(
                        "structurally_legal_chain_count"
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    result: dict[str, object] = {
        "schema": "chanlun-v31-cached-symbol-prescreen/v1",
        "generated_at": datetime.now(CN),
        "scope": "STRUCTURAL_PRESCREEN_NOT_PORTFOLIO_BACKTEST",
        "mapping": {"L0": "30m", "L1": "5m", "L2": "1m"},
        "alignment_contract": v31_alignment_contract().document(),
        "alignment_parameter_set_id": (
            v31_alignment_contract().parameter_set_id
        ),
        "l1_evidence_kind": "COMPLETED_TREND",
        "l1_constituent_unit_accepted": False,
        "available_minute_series": available,
        "requested_symbols": requested,
        "benchmark_symbol": args.benchmark_symbol,
        "symbol_reports": tuple(reports),
        "totals": {
            "symbols": len(reports),
            "symbols_with_legal_chains": sum(
                int(item.get("structurally_legal_chain_count", 0)) > 0
                for item in reports
            ),
            "legal_chains": sum(
                int(item.get("structurally_legal_chain_count", 0))
                for item in reports
            ),
            "prescreen_errors": sum(
                item.get("decision") == "PRESCREEN_ERROR" for item in reports
            ),
        },
        "market_database_sha256": market_hash,
        "pit_database_sha256": pit_hash,
        "implementation_sha256": implementation_hash,
        "implementation_manifest": implementation_manifest,
        "corporate_action_snapshot_sha256": corporate_action_hash,
        "frozen_core_modified": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    result["content_sha256"] = content_sha256(result)
    atomic_json(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "totals": result["totals"],
                "content_sha256": result["content_sha256"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
