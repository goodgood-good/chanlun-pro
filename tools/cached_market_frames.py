#!/usr/bin/env python3
"""读取缓存行情并构建严格结构共用的因果复权周期数据。

本模块只处理数据身份、公司行动与 OHLC 聚合，不生成中枢、背驰或买卖点。
所有信号消费者因此可以共用同一份行情准备逻辑，而不会引入第二套结构权威。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    FRAME_COLUMNS,
)
from chanlun.exchange.kline_precision import (  # noqa: E402
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (  # noqa: E402
    PriceBasisMetadata,
    attach_price_basis_metadata,
    build_price_basis_revision,
)
from tools.research_data import (  # noqa: E402
    DistributionEvent,
    aggregate_completed_bars,
    apply_causal_forward_adjustments,
    causal_adjustment_ledger,
    content_sha256,
    longest_complete_interval,
    read_cached_series,
    sha256_file,
)


_PROVIDER_SYMBOL = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")


def provider_to_project_code(symbol: str) -> str:
    """把数据提供方代码转换为项目统一的 A 股代码。"""

    normalized = symbol.strip().upper()
    matched = _PROVIDER_SYMBOL.fullmatch(normalized)
    if matched is None:
        raise ValueError(f"unsupported cached A-share symbol: {symbol!r}")
    return f"{matched.group('exchange')}.{matched.group('code')}"


def discover_minute_symbols(database: Path) -> tuple[dict[str, object], ...]:
    """只读列出缓存中的未复权一分钟序列。"""

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
    """读取带日期的 ETF 分红；没有账本时返回空元组，由调用方关闭正式资格。"""

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
    """只读加载并校验 QMT 生效日公司行动。"""

    if not snapshot.is_file():
        return ()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if payload.get("schema") != "chanlun-qmt-etf-corporate-actions":
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
    """从生效交易日起应用每个 QMT ``dr``，不改写更早数据。"""

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


def build_cached_market_frames(
    *,
    database: Path,
    pit_database: Path,
    corporate_actions: Path,
    benchmark_symbol: str,
    provider_symbol: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, object], dict[str, object]]:
    """构建同源的 1m/5m/30m 行情数据，不执行任何结构识别。"""

    project_code = provider_to_project_code(provider_symbol)
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
    distributions: tuple[DistributionEvent, ...] = ()
    if all_qmt_events:
        ledger: Sequence[object] = qmt_events
        adjusted_one = _apply_qmt_dr_adjustments(raw_one, qmt_events)
        adjustment = "causal-forward-effective-session-qmt-dr"
        adjustment_status = "QMT_EFFECTIVE_DATED_CAUSAL_ADJUSTMENT_AVAILABLE"
        adjustment_source = "QMT_CORPORATE_ACTION_SNAPSHOT"
        formally_eligible = True
    else:
        distributions = _optional_distributions(
            pit_database, project_code=project_code
        )
        if distributions:
            ledger = causal_adjustment_ledger(raw_one, distributions)
            adjusted_one = apply_causal_forward_adjustments(raw_one, ledger)
            adjustment = "causal-forward-cash-distribution"
            adjustment_status = "PIT_CAUSAL_ADJUSTMENT_AVAILABLE"
            adjustment_source = "LOCAL_PIT_CASH_DISTRIBUTION_LEDGER"
            formally_eligible = True
        else:
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


__all__ = [
    "_apply_qmt_dr_adjustments",
    "_events_in_interval",
    "build_cached_market_frames",
    "discover_minute_symbols",
    "provider_to_project_code",
]
