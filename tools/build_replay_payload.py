#!/usr/bin/env python3
"""Build and run strict strategy ReplayBatch payloads from frozen real-fact artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.bar_execution import (
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.replay_payload_builder import (
    build_replay_payload,
)
from tools.backtest_multisymbol_events import run_payload
from tools.research_data import (
    CN,
    normalize_completed_minute_sessions,
    read_cached_series,
)


DEFAULT_PRESCREENS = (
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_159919.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_159925.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510300.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510310.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510330.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510360.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510380.json"),
    Path("audit/chanlun_live_integration/cached_symbol_prescreen_510390.json"),
)
DEFAULT_MARKET_DATABASE = Path(
    ".cache/chanlun_csi300_broad_pool/financial_data_query_bars.sqlite3"
)
DEFAULT_CORPORATE_ACTIONS = Path(
    "audit/chanlun_live_integration/qmt_csi300_etf_corporate_actions.json"
)
DEFAULT_BUILD_OUTPUT = Path(
    "audit/chanlun_live_integration/strict_replay_payload_build.json"
)
DEFAULT_PAYLOAD_OUTPUT = Path(
    "audit/chanlun_live_integration/strict_replay_payload.json"
)
DEFAULT_REPLAY_OUTPUT = Path(
    "audit/chanlun_live_integration/strict_multisymbol_replay.json"
)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"), parse_float=str)
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _chain_symbols(
    prescreens: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, str], ...]:
    values: set[tuple[str, str]] = set()
    for prescreen in prescreens:
        reports = prescreen.get("symbol_reports", ())
        if not isinstance(reports, Sequence):
            continue
        for value in reports:
            if not isinstance(value, Mapping):
                continue
            if int(value.get("structurally_legal_chain_count", 0)) <= 0:
                continue
            project = str(value.get("project_code"))
            provider = str(value.get("provider_symbol"))
            values.add((project, provider))
    return tuple(sorted(values))


def _bars(
    database: Path,
    identities: tuple[tuple[str, str], ...],
) -> dict[str, tuple[HistoricalMinuteExecutionBar, ...]]:
    database_hash = _sha256(database)
    result: dict[str, tuple[HistoricalMinuteExecutionBar, ...]] = {}
    for project_symbol, provider_symbol in identities:
        raw = read_cached_series(
            database,
            symbol=provider_symbol,
            period="P_Min1",
        )
        normalized, _coverage = normalize_completed_minute_sessions(raw)
        rows: list[HistoricalMinuteExecutionBar] = []
        for sequence, row in normalized.iterrows():
            closed_at = row["date"].to_pydatetime()
            rows.append(
                HistoricalMinuteExecutionBar(
                    symbol=project_symbol,
                    opened_at=closed_at - timedelta(minutes=1),
                    closed_at=closed_at,
                    sequence=int(sequence),
                    raw_open=Decimal(str(row["open"])),
                    raw_high=Decimal(str(row["high"])),
                    raw_low=Decimal(str(row["low"])),
                    raw_close=Decimal(str(row["close"])),
                    raw_volume=Decimal(str(row["volume"])),
                    source_id=(
                        f"financial-data-query:{database_hash}:"
                        f"{provider_symbol}:{closed_at.isoformat()}"
                    ),
                )
            )
        result[project_symbol] = tuple(rows)
    return result


def _infer_started_at(prescreens: Sequence[Mapping[str, object]]) -> datetime:
    starts: list[datetime] = []
    for prescreen in prescreens:
        reports = prescreen.get("symbol_reports", ())
        if not isinstance(reports, Sequence):
            continue
        for value in reports:
            if not isinstance(value, Mapping) or value.get("source_start") is None:
                continue
            starts.append(
                datetime.fromisoformat(str(value["source_start"]) + "T09:30:00").replace(
                    tzinfo=CN
                )
            )
    if not starts:
        raise ValueError("cannot infer replay start from prescreen artifacts")
    return min(starts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prescreen", type=Path, action="append")
    parser.add_argument("--facts", type=Path)
    parser.add_argument(
        "--market-database",
        type=Path,
        default=DEFAULT_MARKET_DATABASE,
    )
    parser.add_argument(
        "--corporate-actions",
        type=Path,
        default=DEFAULT_CORPORATE_ACTIONS,
    )
    parser.add_argument("--initial-cash", default="1000000")
    parser.add_argument("--started-at")
    parser.add_argument("--build-output", type=Path, default=DEFAULT_BUILD_OUTPUT)
    parser.add_argument("--payload-output", type=Path, default=DEFAULT_PAYLOAD_OUTPUT)
    parser.add_argument("--replay-output", type=Path, default=DEFAULT_REPLAY_OUTPUT)
    args = parser.parse_args(argv)

    prescreen_paths = tuple(args.prescreen or DEFAULT_PRESCREENS)
    missing = tuple(path for path in prescreen_paths if not path.is_file())
    if missing:
        parser.error("missing prescreen artifact(s): " + ", ".join(map(str, missing)))
    prescreens = tuple(_load(path) for path in prescreen_paths)
    identities = _chain_symbols(prescreens)
    bars_by_symbol = _bars(args.market_database, identities) if identities else {}
    facts = None if args.facts is None else _load(args.facts)
    corporate = (
        _load(args.corporate_actions)
        if args.corporate_actions.is_file()
        else None
    )
    started = (
        _infer_started_at(prescreens)
        if args.started_at is None
        else datetime.fromisoformat(args.started_at).astimezone(CN)
    )
    built = build_replay_payload(
        prescreen_artifacts=prescreens,
        fact_ledger=facts,
        bars_by_symbol=bars_by_symbol,
        corporate_action_ledger=corporate,
        initial_cash=Decimal(args.initial_cash),
        started_at=started,
    )
    _write(args.payload_output, built.payload)
    payload_raw = args.payload_output.read_bytes()
    replay = run_payload(
        built.payload,
        input_sha256="sha256:" + hashlib.sha256(payload_raw).hexdigest(),
    )
    _write(args.replay_output, replay)
    summary = built.report()
    summary.pop("payload")
    summary.update(
        {
            "input_files": tuple(
                {
                    "path": str(path.resolve()),
                    "sha256": _sha256(path),
                }
                for path in (
                    *prescreen_paths,
                    *(() if args.facts is None else (args.facts,)),
                    *(
                        ()
                        if not args.corporate_actions.is_file()
                        else (args.corporate_actions,)
                    ),
                )
            ),
            "market_database": (
                None
                if not identities
                else {
                    "path": str(args.market_database.resolve()),
                    "sha256": _sha256(args.market_database),
                    "loaded_symbols": tuple(project for project, _provider in identities),
                }
            ),
            "payload_output": str(args.payload_output.resolve()),
            "payload_sha256": _sha256(args.payload_output),
            "replay_output": str(args.replay_output.resolve()),
            "replay_sha256": _sha256(args.replay_output),
        }
    )
    _write(args.build_output, summary)
    print(
        json.dumps(
            {
                "build_output": str(args.build_output.resolve()),
                "payload_output": str(args.payload_output.resolve()),
                "replay_output": str(args.replay_output.resolve()),
                "legal_chains": built.discovered_legal_chain_count,
                "entry_events": built.generated_entry_event_count,
                "structure_events": built.generated_structure_event_count,
                "empty_replay": built.empty_replay,
                "return_evaluation_allowed": built.return_evaluation_allowed,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
