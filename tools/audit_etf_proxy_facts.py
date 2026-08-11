#!/usr/bin/env python3
"""Audit the sparse CSI300 ETF_PROXY facts without creating trading signals."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from chanlun.decision_support.trading_system.etf_proxy_facts import (
    DailyMarketBar,
    EtfProxyPitRepository,
    EtfTrackingMapping,
    FrozenStructureBar,
    build_benchmark_structure_risk_facts,
    build_higher_timeframe_risk_facts,
    latest_completed_bottom_fractal_anchor,
    load_qmt_corporate_action_ledger,
)


CN = ZoneInfo("Asia/Shanghai")
PIT_DATABASE = Path(".cache/chanlun_external_pit/etf_proxy_pit.sqlite3")
MARKET_DATABASE = Path(
    ".cache/chanlun_available_data/financial_data_query_bars.sqlite3"
)
ETF_DATABASES = {
    "SH.510300": (MARKET_DATABASE, "510300.SH"),
    "SZ.159919": (
        Path(".cache/chanlun_159919/financial_data_query_bars.sqlite3"),
        "159919.SZ",
    ),
}
QMT_CORPORATE_ACTION_SNAPSHOT = Path(
    "audit/chanlun_live_integration/qmt_etf_corporate_actions.json"
)
OUTPUT = Path("audit/chanlun_live_integration/etf_proxy_fact_audit.json")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _daily_index_rows() -> tuple[DailyMarketBar, ...]:
    uri = f"file:{MARKET_DATABASE.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = tuple(
            connection.execute(
                """
                SELECT bar_time, open, high, low, close, volume
                FROM bars
                WHERE symbol='000300.CSI'
                  AND period='P_Day1'
                  AND adj_type='S_Unsplit'
                ORDER BY bar_time
                """
            )
        )
    return tuple(
        DailyMarketBar(
            session=(session := date.fromisoformat(str(row[0])[:10])),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            known_at=datetime.combine(session, time(15, 0), tzinfo=CN),
        )
        for row in rows
    )


def _calendar() -> tuple[date, ...]:
    uri = f"file:{PIT_DATABASE.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return tuple(
            date.fromisoformat(row[0])
            for row in connection.execute(
                """
                SELECT calendar_date FROM trading_calendar
                WHERE is_trading_day='1' ORDER BY calendar_date
                """
            )
        )


def _etf_data_inventory() -> dict[str, object]:
    result: dict[str, object] = {}
    pit_uri = f"file:{PIT_DATABASE.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(pit_uri, uri=True) as pit:
        for symbol, (database, provider_symbol) in ETF_DATABASES.items():
            uri = f"file:{database.resolve().as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*), MIN(bar_time), MAX(bar_time)
                    FROM bars
                    WHERE symbol=? AND period='P_Min1'
                      AND adj_type='S_Unsplit'
                    """,
                    (provider_symbol,),
                ).fetchone()
            distributions = int(
                pit.execute(
                    "SELECT COUNT(*) FROM etf_distributions WHERE symbol=?",
                    (symbol,),
                ).fetchone()[0]
            )
            qmt_facts = load_qmt_corporate_action_ledger(
                QMT_CORPORATE_ACTION_SNAPSHOT,
                symbol=symbol,
            )
            qmt_ledger = qmt_facts.ledger
            blockers = [value.code for value in qmt_facts.blockers]
            if int(row[0]) == 0:
                blockers.append("ONE_MINUTE_SOURCE_MISSING")
            if distributions == 0 and qmt_ledger is None:
                blockers.append("POINT_IN_TIME_ETF_DISTRIBUTION_LEDGER_MISSING")
            result[symbol] = {
                "database": str(database.resolve()),
                "database_sha256": _file_hash(database),
                "one_minute_rows": int(row[0]),
                "one_minute_start": row[1],
                "one_minute_end": row[2],
                "distribution_event_rows": distributions,
                "qmt_corporate_action_snapshot": str(
                    QMT_CORPORATE_ACTION_SNAPSHOT.resolve()
                ),
                "qmt_corporate_action_grade": qmt_facts.grade,
                "qmt_corporate_action_event_rows": (
                    0 if qmt_ledger is None else len(qmt_ledger.events)
                ),
                "qmt_corporate_action_snapshot_sha256": (
                    None
                    if qmt_ledger is None
                    else qmt_ledger.snapshot_content_sha256
                ),
                "qmt_corporate_action_source_store_sha256": (
                    None if qmt_ledger is None else qmt_ledger.source_store_sha256
                ),
                "qmt_corporate_action_authority_attestation_id": (
                    None
                    if qmt_ledger is None
                    else qmt_ledger.authority_attestation_id
                ),
                "corporate_action_ledger_resolution": (
                    "PIT_SQLITE_AND_QMT_RESEARCH_LEDGER"
                    if distributions > 0 and qmt_ledger is not None
                    else (
                        "QMT_RESEARCH_LEDGER_ONLY"
                        if qmt_ledger is not None
                        else "UNRESOLVED"
                    )
                ),
                "tracked_index_high_timeframe_risk_status": (
                    "COMPUTED_PER_SNAPSHOT_SEE_HIGHER_TIMEFRAME_MARKET_RISK"
                ),
                "blockers": sorted(set(blockers)),
            }
    return result


def _mapping(symbol: str, first_session: date, last_session: date) -> EtfTrackingMapping:
    # The user supplied the tracking relationship for this research task, but
    # no authoritative historical methodology/publication record is cached.
    # Keep it explicitly non-authoritative so evaluate/report layers cannot
    # promote these rows to FULL_SYSTEM_ELIGIBLE.
    first = datetime.combine(first_session, time(0, 0), tzinfo=CN)
    last = datetime.combine(last_session, time(23, 59, 59), tzinfo=CN)
    return EtfTrackingMapping(
        symbol=symbol,
        tracked_index="CSI.000300",
        known_at=first,
        effective_from=first,
        valid_until=last,
        evidence_ids=(f"USER_RESEARCH_DECLARATION:{symbol}:TRACKS_CSI300",),
        authoritative=False,
    )


def _blocker_payload(values: object) -> list[dict[str, str]]:
    return [
        {"field": value.field, "code": value.code, "detail": value.detail}
        for value in values
    ]


def _load_completed_30m_bars(path: Path | None) -> tuple[FrozenStructureBar, ...]:
    if path is None:
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("bars") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("benchmark 30m JSON must be a list or an object with bars")
    bars: list[FrozenStructureBar] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"benchmark 30m row {index} must be an object")
        completed = record.get("completed")
        if not isinstance(completed, bool):
            raise ValueError(f"benchmark 30m row {index} requires boolean completed")
        bars.append(
            FrozenStructureBar(
                end_at=datetime.fromisoformat(str(record["end_at"])),
                open=Decimal(str(record["open"])),
                high=Decimal(str(record["high"])),
                low=Decimal(str(record["low"])),
                close=Decimal(str(record["close"])),
                volume=Decimal(str(record["volume"])),
                completed=completed,
            )
        )
    ends = tuple(value.end_at for value in bars)
    if ends != tuple(sorted(set(ends))):
        raise ValueError("benchmark 30m JSON bars must be unique and chronological")
    return tuple(bars)


def audit(
    *,
    benchmark_completed_30m_bars: tuple[FrozenStructureBar, ...] = (),
    benchmark_30m_source: str | None = None,
) -> dict[str, object]:
    repository = EtfProxyPitRepository(PIT_DATABASE)
    sessions = repository.available_membership_sessions()
    index_rows = _daily_index_rows()
    calendar = _calendar()
    mappings = {
        symbol: _mapping(symbol, sessions[0], sessions[-1])
        for symbol in ("SH.510300", "SZ.159919")
    }
    results: list[dict[str, object]] = []
    blocker_counts: Counter[str] = Counter()
    resolved_strength = 0
    for session in sessions:
        decision = datetime.combine(session, time(15, 0), tzinfo=CN)
        anchor = latest_completed_bottom_fractal_anchor(
            index_rows,
            decision_time=decision,
            symbol="CSI.000300",
        )
        strength = repository.build_basket_strength_facts(
            decision_time=decision,
            anchor_session=anchor.anchor_session,
        )
        if strength.snapshot.resolved:
            resolved_strength += 1
        benchmark_structure = build_benchmark_structure_risk_facts(
            index_rows,
            trading_sessions=calendar,
            calendar_coverage_end=calendar[-1],
            decision_time=decision,
            completed_30m_bars=benchmark_completed_30m_bars,
            symbol="CSI.000300",
        )
        risk = build_higher_timeframe_risk_facts(
            index_rows,
            trading_sessions=calendar,
            calendar_coverage_end=calendar[-1],
            decision_time=decision,
            structure_states=tuple(
                value.fact for value in benchmark_structure.states
            ),
            snapshot_id=f"risk:CSI.000300:{session.isoformat()}",
        )
        selections: dict[str, object] = {}
        for symbol, mapping in mappings.items():
            selection = repository.build_selection_facts(
                mapping,
                decision_time=decision,
                reviewer="etf-proxy-research-adapter",
                signature="RESEARCH_ONLY/LIVE_DISABLED",
            )
            blocker_counts.update(value.code for value in selection.blockers)
            selections[symbol] = {
                "grade": selection.grade,
                "snapshot_id": (
                    None if selection.snapshot is None else selection.snapshot.snapshot_id
                ),
                "basket_mapping_id": (
                    None if selection.basket is None else selection.basket.mapping_id
                ),
                "blockers": _blocker_payload(selection.blockers),
            }
        blocker_counts.update(value.code for value in anchor.blockers)
        blocker_counts.update(value.code for value in strength.blockers)
        blocker_counts.update(value.code for value in risk.blockers)
        for value in benchmark_structure.states:
            blocker_counts.update(item.code for item in value.blockers)
        results.append(
            {
                "decision_time": decision.isoformat(),
                "selection": selections,
                "broad_market_anchor": {
                    "anchor_session": anchor.anchor_session,
                    "confirmation_time": anchor.confirmation_time,
                    "middle_session": anchor.fractal_middle_session,
                    "value": anchor.fractal_value,
                    "stroke_mode": anchor.stroke_mode,
                    "source_revision": anchor.source_revision,
                    "blockers": _blocker_payload(anchor.blockers),
                    "warnings": _blocker_payload(anchor.warnings),
                },
                "basket_strength": {
                    "grade": strength.grade,
                    "resolved": strength.snapshot.resolved,
                    "strength": strength.snapshot.strength,
                    "member_count": strength.snapshot.member_count,
                    "snapshot_id": strength.snapshot.snapshot_id,
                    "blockers": _blocker_payload(strength.blockers),
                },
                "higher_timeframe_market_risk": {
                    "gate": risk.gate,
                    "states": {
                        value.fact.period: {
                            "state": value.fact.state,
                            "mapping_unique": value.fact.mapping_unique,
                            "mapped_center_id": value.fact.mapped_center_id,
                            "active_top_interval": value.active_top_interval,
                            "mapping_candidate_ids": value.mapping_candidate_ids,
                            "blockers": _blocker_payload(value.blockers),
                            "warnings": _blocker_payload(value.warnings),
                            "source_revision": value.fact.source_revision,
                        }
                        for value in benchmark_structure.states
                    },
                    "ma5": {period: value for period, value in risk.ma5},
                    "completed_period_counts": {
                        period: len(values) for period, values in risk.period_bars
                    },
                    "completed_30m_prefix_count": (
                        benchmark_structure.completed_30m_prefix_count
                    ),
                    "blockers": _blocker_payload(risk.blockers),
                },
            }
        )

    payload: dict[str, object] = {
        "schema": "chanlun-etf-proxy-fact-audit",
        "generated_at": datetime.now(CN),
        "selection_path": "ETF_PROXY",
        "symbols": ["SH.510300", "SZ.159919"],
        "tracked_index": "CSI.000300",
        "pit_database_sha256": _file_hash(PIT_DATABASE),
        "market_database_sha256": _file_hash(MARKET_DATABASE),
        "benchmark_30m_input": {
            "source": benchmark_30m_source or "NOT_SUPPLIED",
            "row_count": len(benchmark_completed_30m_bars),
            "start": (
                None
                if not benchmark_completed_30m_bars
                else benchmark_completed_30m_bars[0].end_at
            ),
            "end": (
                None
                if not benchmark_completed_30m_bars
                else benchmark_completed_30m_bars[-1].end_at
            ),
            "status": (
                "EXPLICIT_COMPLETED_FROZEN_STRUCTURE_BARS"
                if benchmark_completed_30m_bars
                else "MISSING_EXPLICIT_D_TO_30M_GATE"
            ),
        },
        "qmt_corporate_action_snapshot_sha256": _file_hash(
            QMT_CORPORATE_ACTION_SNAPSHOT
        ),
        "etf_data_inventory": _etf_data_inventory(),
        "membership_snapshot_count": len(sessions),
        "membership_scope": "EXACT_STORED_SESSION_ONLY_NO_FORWARD_FILL",
        "resolved_basket_strength_snapshots": resolved_strength,
        "high_timeframe_structure_state_status": (
            "CANONICAL_STRICT_PREFIX_ADAPTER_EXECUTED_WITH_D_TO_30M_GATE"
        ),
        "data_grade": "RESEARCH_ONLY",
        "full_system_candidate_gate_certified": False,
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "snapshots": results,
        "live_status": "LIVE_DISABLED",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload["content_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-30m-json",
        type=Path,
        help=(
            "JSON list (or {bars:[...]}) of completed CSI300 30m "
            "FrozenStructureBar records"
        ),
    )
    arguments = parser.parse_args()
    benchmark_30m = _load_completed_30m_bars(arguments.benchmark_30m_json)
    payload = audit(
        benchmark_completed_30m_bars=benchmark_30m,
        benchmark_30m_source=(
            None
            if arguments.benchmark_30m_json is None
            else str(arguments.benchmark_30m_json.resolve())
        ),
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(OUTPUT.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(OUTPUT)
    print(
        json.dumps(
            {
                "output": str(OUTPUT.resolve()),
                "membership_snapshot_count": payload["membership_snapshot_count"],
                "resolved_basket_strength_snapshots": payload[
                    "resolved_basket_strength_snapshots"
                ],
                "data_grade": payload["data_grade"],
                "full_system_candidate_gate_certified": payload[
                    "full_system_candidate_gate_certified"
                ],
                "blocker_counts": payload["blocker_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
