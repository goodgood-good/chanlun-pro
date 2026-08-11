#!/usr/bin/env python3
"""Audit QMT historical sector membership and tick availability for strict strategy.

The command is intentionally diagnostic.  It asks QMT for several dated
sector member sets, cross-checks every returned member against the security
master listing date, and samples historical tick rows.  It never downloads or
fills missing data and never promotes a provider response merely because an
``as_of`` argument was accepted.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    HistoricalSectorProbe,
    audit_historical_sector_probes,
)
from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK  # noqa: E402
from tools.research_data import atomic_json, content_sha256  # noqa: E402


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/qmt_history_source_audit.json"
)
DEFAULT_DATES = (date(2019, 1, 2), date(2022, 1, 4), date(2026, 7, 24))
DEFAULT_SECTORS = (
    "GICS3商业银行",
    "GICS3软件",
    "GICS3饮料",
    "GICS3半导体产品与设备",
)
DEFAULT_TICK_CODES = ("510300.SH", "600519.SH")


def _dates(value: str) -> tuple[date, ...]:
    try:
        result = tuple(date.fromisoformat(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dates must use comma-separated YYYY-MM-DD") from exc
    if not result or result != tuple(sorted(set(result))):
        raise argparse.ArgumentTypeError("dates must be non-empty, unique and ordered")
    return result


def _values(value: str) -> tuple[str, ...]:
    result = tuple(item.strip() for item in value.split(",") if item.strip())
    if not result or len(result) != len(set(result)):
        raise argparse.ArgumentTypeError("values must be non-empty and unique")
    return result


def _normal_code(value: str) -> str:
    digits, market = value.strip().upper().split(".", 1)
    if market not in {"SH", "SZ", "BJ"} or len(digits) != 6 or not digits.isdigit():
        raise ValueError(f"unsupported QMT A-share code: {value!r}")
    return f"{market}.{digits}"


def _native_code(value: str) -> str:
    market, digits = value.split(".", 1)
    return f"{digits}.{market}"


def _timetag(value: date) -> int:
    closed = datetime.combine(value, time(15, 0), tzinfo=CN)
    return int(closed.timestamp() * 1000)


def _listing_date(detail: Mapping[str, object]) -> date | None:
    text = str(detail.get("OpenDate") or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def collect_qmt_history_audit(
    *,
    dates: Sequence[date],
    sectors: Sequence[str],
    tick_codes: Sequence[str],
) -> dict[str, object]:
    from xtquant import xtdata

    xtdata.enable_hello = False
    probes: list[HistoricalSectorProbe] = []
    native_members: set[str] = set()
    with _XTDATA_NATIVE_LOCK:
        available = set(xtdata.get_sector_list())
        missing_sectors = tuple(sorted(set(sectors) - available))
        if missing_sectors:
            raise RuntimeError(f"QMT sectors are unavailable: {missing_sectors!r}")
        for sector in sectors:
            for as_of in dates:
                response = xtdata.get_stock_list_in_sector(
                    sector,
                    real_timetag=_timetag(as_of),
                )
                if type(response) is not list:
                    raise RuntimeError(f"QMT sector response is invalid: {sector}/{as_of}")
                normalized: set[str] = set()
                for value in response:
                    try:
                        code = _normal_code(str(value))
                    except (TypeError, ValueError):
                        continue
                    normalized.add(code)
                    native_members.add(_native_code(code))
                probes.append(
                    HistoricalSectorProbe(
                        sector_key=sector,
                        as_of=as_of,
                        members=tuple(sorted(normalized)),
                    )
                )
        details = {
            _normal_code(native): xtdata.get_instrument_detail(native)
            for native in sorted(native_members)
        }
        tick_rows: list[dict[str, object]] = []
        for code in tick_codes:
            normalized = _normal_code(code)
            native = _native_code(normalized)
            for session in dates:
                result = xtdata.get_market_data(
                    field_list=[],
                    stock_list=[native],
                    period="tick",
                    start_time=session.strftime("%Y%m%d") + "093000",
                    end_time=session.strftime("%Y%m%d") + "150000",
                    count=-1,
                    dividend_type="none",
                    fill_data=False,
                )
                rows = result.get(native) if isinstance(result, Mapping) else None
                tick_rows.append(
                    {
                        "code": native,
                        "session": session.isoformat(),
                        "row_count": 0 if rows is None else len(rows),
                    }
                )

    listed_from = {
        code: listed
        for code, detail in details.items()
        if isinstance(detail, Mapping)
        and (listed := _listing_date(detail)) is not None
    }
    membership = audit_historical_sector_probes(
        probes,
        listed_from=listed_from,
    )
    tick_available = bool(tick_rows) and all(int(value["row_count"]) > 0 for value in tick_rows)
    stable: dict[str, object] = {
        "schema": "chanlun-qmt-history-source-audit",
        "requested_dates": tuple(value.isoformat() for value in dates),
        "requested_sectors": tuple(sectors),
        "membership_audit": membership,
        "listing_date_coverage": {
            "returned_unique_members": len(native_members),
            "members_with_listing_date": len(listed_from),
            "coverage": (
                "0"
                if not native_members
                else str(len(listed_from) / len(native_members))
            ),
        },
        "historical_tick_audit": {
            "rows": tuple(tick_rows),
            "all_requested_ranges_available": tick_available,
            "historical_quote_and_trade_gate": (
                "PASS" if tick_available else "FAIL_MISSING_HISTORICAL_TICKS"
            ),
            "missing_data_was_downloaded_or_filled": False,
        },
        "data_decision": "RESEARCH_ONLY",
        "full_system_backtest_eligible": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": content_sha256(stable)}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--dates",
        type=_dates,
        default=DEFAULT_DATES,
        help="comma-separated historical sessions",
    )
    value.add_argument(
        "--sectors",
        type=_values,
        default=DEFAULT_SECTORS,
        help="comma-separated QMT sector keys",
    )
    value.add_argument(
        "--tick-codes",
        type=_values,
        default=DEFAULT_TICK_CODES,
        help="comma-separated native QMT codes",
    )
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return value


def main() -> int:
    args = parser().parse_args()
    report = collect_qmt_history_audit(
        dates=args.dates,
        sectors=args.sectors,
        tick_codes=args.tick_codes,
    )
    atomic_json(args.output, report)
    print(
        json.dumps(
            {
                "complete": True,
                "output": str(args.output.resolve()),
                "membership_status": report["membership_audit"]["status"],
                "historical_tick_gate": report["historical_tick_audit"][
                    "historical_quote_and_trade_gate"
                ],
                "full_system_backtest_eligible": False,
                "content_sha256": report["content_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
