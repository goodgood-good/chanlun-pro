#!/usr/bin/env python3
"""Capture the immutable point-in-time metadata ledger for a QMT replay."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    CN,
    PITMetadataSnapshot,
    SecurityMasterRecord,
    membership_changes_from_cninfo,
    normalize_qmt_a_share_code,
    qmt_factors_from_rows,
    sha256_json,
    snapshot_payload,
    sw1_sector_id,
)


DEFAULT_START = date(2025, 5, 1)
DEFAULT_END = date(2026, 7, 24)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
_CNINFO_STANDARD = "008003"
_CNINFO_HISTORY_URL = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
_CNINFO_TAXONOMY_URL = "https://webapi.cninfo.com.cn/api/stock/p_public0002"
_QMT_CURRENT_A = "\u6caa\u6df1A\u80a1"
_QMT_EXPIRED_A = "\u8fc7\u671f\u6caa\u6df1A\u80a1"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    result.add_argument("--end", type=_parse_date, default=DEFAULT_END)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--workers", type=_positive_int, default=6)
    result.add_argument("--force", action="store_true")
    result.add_argument(
        "--refresh-contracts",
        action="store_true",
        help="refresh QMT expired-contract and current-sector files first",
    )
    return result


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _tree_hash(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _cninfo_headers() -> dict[str, str]:
    # AkShare ships the official CNInfo request-code algorithm.  Evaluate it
    # once in the main thread; py_mini_racer is not thread-safe on Windows.
    from akshare.stock.stock_industry_cninfo import _get_file_content_ths
    import py_mini_racer

    runtime = py_mini_racer.MiniRacer()
    runtime.eval(_get_file_content_ths("cninfo.js"))
    request_code = str(runtime.call("getResCode1"))
    return {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Content-Length": "0",
        "Host": "webapi.cninfo.com.cn",
        "Accept-Enckey": request_code,
        "Origin": "https://webapi.cninfo.com.cn",
        "Pragma": "no-cache",
        "Referer": "https://webapi.cninfo.com.cn/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }


def _request_json(
    *,
    method: str,
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
) -> Mapping[str, object]:
    import requests

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = requests.request(
                method,
                url,
                params=dict(params),
                headers=dict(headers),
                timeout=(10, 30),
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, Mapping) or not isinstance(
                value.get("records"), list
            ):
                raise ValueError("CNInfo response has no records array")
            return value
        except Exception as exc:  # pragma: no cover - live retry path
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"CNInfo request failed: {last_error}")


def _taxonomy(headers: Mapping[str, str]) -> tuple[dict[str, str], list[object]]:
    payload = _request_json(
        method="GET",
        url=_CNINFO_TAXONOMY_URL,
        params={"indcode": "", "indtype": _CNINFO_STANDARD, "format": "json"},
        headers=headers,
    )
    records = list(payload["records"])
    level_one: dict[str, str] = {}
    for raw in records:
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("SORTCODE") or "").strip().upper()
        parent = str(raw.get("PARENTCODE") or "").strip().upper()
        name = str(raw.get("SORTNAME") or "").strip()
        if len(code) == 3 and code.startswith("S") and parent == "S" and name:
            level_one[sw1_sector_id(code)] = name
    if len(level_one) < 25:
        raise RuntimeError("CNInfo SW1 taxonomy is incomplete")
    return dict(sorted(level_one.items())), records


def _security_master(
    start: date,
    end: date,
) -> tuple[tuple[SecurityMasterRecord, ...], dict[str, object]]:
    from xtquant import xtdata
    import pandas as pd

    native_codes = sorted(
        set(xtdata.get_stock_list_in_sector(_QMT_CURRENT_A))
        | set(xtdata.get_stock_list_in_sector(_QMT_EXPIRED_A))
    )
    details: dict[str, Mapping[str, object]] = {}
    invalid_expiry: list[str] = []
    invalid_open: list[str] = []
    for native in native_codes:
        try:
            normalized = normalize_qmt_a_share_code(native)
        except ValueError:
            continue
        if not normalized.startswith(("SH.", "SZ.", "BJ.")):
            continue
        detail = xtdata.get_instrument_detail(native, iscomplete=False)
        if not isinstance(detail, Mapping):
            continue
        details[native] = detail
        open_text = str(detail.get("OpenDate") or "").strip()
        try:
            parsed_open = datetime.strptime(open_text, "%Y%m%d").date()
            if not date(1990, 1, 1) <= parsed_open <= date(2100, 12, 31):
                raise ValueError
        except ValueError:
            invalid_open.append(native)
        expiry = str(detail.get("ExpireDate") or "").strip()
        if expiry not in {"", "0", "99999999"}:
            try:
                parsed = datetime.strptime(expiry, "%Y%m%d").date()
                if not date(1990, 1, 1) <= parsed <= date(2100, 12, 31):
                    raise ValueError
            except ValueError:
                invalid_expiry.append(native)

    evidence_codes = tuple(
        sorted(
            set(invalid_expiry)
            | set(invalid_open)
        )
    )
    observed_sessions: dict[str, tuple[date, ...]] = {}
    if evidence_codes:
        raw = xtdata.get_market_data(
            field_list=["time"],
            stock_list=list(evidence_codes),
            period="1d",
            start_time=start.strftime("%Y%m%d"),
            end_time=end.strftime("%Y%m%d"),
            count=-1,
            dividend_type="none",
            fill_data=False,
        )
        matrix = raw.get("time") if isinstance(raw, Mapping) else None
        for native in evidence_codes:
            values = (
                []
                if matrix is None or native not in matrix.index
                else [
                    value
                    for value in matrix.loc[native].tolist()
                    if value == value and float(value) > 0
                ]
            )
            observed_sessions[native] = tuple(
                sorted(
                    {
                        pd.to_datetime(value, unit="ms", utc=True)
                        .tz_convert(CN)
                        .date()
                        for value in values
                    }
                )
            )

    records: list[SecurityMasterRecord] = []
    pseudo_contracts: list[str] = []
    inferred_active: list[str] = []
    inferred_expired: list[dict[str, str]] = []
    for native, detail in sorted(details.items()):
        open_text = str(detail.get("OpenDate") or "").strip()
        sessions = observed_sessions.get(native, ())
        if native in invalid_open:
            if not sessions:
                pseudo_contracts.append(native)
                continue
            listed_from = sessions[0]
        else:
            try:
                listed_from = datetime.strptime(open_text, "%Y%m%d").date()
            except ValueError as exc:
                raise RuntimeError(f"invalid QMT OpenDate for {native}") from exc
        expiry_text = str(detail.get("ExpireDate") or "").strip()
        if native in invalid_expiry:
            status = int(detail.get("InstrumentStatus") or 0)
            name = str(detail.get("InstrumentName") or "").strip()
            visibly_expired = status == 3 or "\u9000\u5e02" in name or name.endswith("\u9000")
            if visibly_expired:
                if not sessions:
                    # It cannot contribute a bar or be filled in this source
                    # range.  Keep no guessed expiry in the certified scope.
                    continue
                last_observed = sessions[-1]
                # The last available bar is not itself an effective-dated
                # delisting notice.  Keep the member in subsequent coverage
                # denominators and use the end-of-test known status only for
                # terminal zero recovery.
                listed_through = None
                inferred_expired.append(
                    {"code": native, "last_observed_session": str(last_observed)}
                )
            else:
                listed_through = None
                inferred_active.append(native)
        elif expiry_text in {"", "0", "99999999"}:
            listed_through = None
        else:
            listed_through = datetime.strptime(expiry_text, "%Y%m%d").date()
        row = SecurityMasterRecord(
            code=normalize_qmt_a_share_code(native),
            name=str(detail.get("InstrumentName") or "").strip(),
            listed_from=listed_from,
            listed_through=listed_through,
        )
        if row.intersects(start, end):
            records.append(row)
    output = tuple(sorted(records, key=lambda row: row.code))
    if not output:
        raise RuntimeError("QMT point-in-time security master is empty")
    return output, {
        "raw_contract_count": len(details),
        "malformed_open_date_count": len(invalid_open),
        "malformed_open_date_without_observed_bars": pseudo_contracts,
        "malformed_expiry_count": len(invalid_expiry),
        "malformed_expiry_active_from_observed_bars": len(inferred_active),
        "malformed_expiry_delisted_from_last_bar": inferred_expired,
    }


def _qmt_current_sw1(
    taxonomy: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    from xtquant import xtdata

    available = set(xtdata.get_sector_list())
    output: dict[str, tuple[str, ...]] = {}
    for sector_id, name in sorted(taxonomy.items()):
        source_key = "SW1" + name
        if source_key not in available:
            raise RuntimeError(f"QMT SW1 sector is missing: {source_key}")
        members: set[str] = set()
        for value in xtdata.get_stock_list_in_sector(source_key, real_timetag=-1):
            try:
                members.add(normalize_qmt_a_share_code(value))
            except ValueError:
                continue
        output[sector_id] = tuple(sorted(members))
    return output


def _membership_checkpoint(
    *,
    code: str,
    end: date,
    headers: Mapping[str, str],
    target: Path,
) -> Path:
    digits = code.split(".", 1)[1]
    payload = _request_json(
        method="POST",
        url=_CNINFO_HISTORY_URL,
        params={
            "scode": digits,
            "sdate": "1990-01-01",
            "edate": end.isoformat(),
        },
        headers=headers,
    )
    records = payload["records"]
    _atomic_json(
        target,
        {
            "schema": "cninfo-p_stock2110-checkpoint",
            "code": code,
            "not_after": end.isoformat(),
            "records": records,
            "records_sha256": sha256_json(records),
        },
    )
    return target


def _valid_checkpoint(path: Path, *, code: str, end: date) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return (
            isinstance(raw, Mapping)
            and raw.get("schema") == "cninfo-p_stock2110-checkpoint"
            and raw.get("code") == code
            and raw.get("not_after") == end.isoformat()
            and isinstance(raw.get("records"), list)
            and raw.get("records_sha256") == sha256_json(raw["records"])
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _capture_memberships(
    *,
    securities: Sequence[SecurityMasterRecord],
    end: date,
    headers: Mapping[str, str],
    source_dir: Path,
    workers: int,
    force: bool,
) -> tuple[tuple[object, ...], tuple[Path, ...]]:
    checkpoint_dir = source_dir / "cninfo_memberships"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        row.code: checkpoint_dir / f"{row.code.replace('.', '_')}.json"
        for row in securities
    }
    pending = [
        row.code
        for row in securities
        if force or not _valid_checkpoint(paths[row.code], code=row.code, end=end)
    ]
    started = time.perf_counter()
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _membership_checkpoint,
                    code=code,
                    end=end,
                    headers=headers,
                    target=paths[code],
                ): code
                for code in pending
            }
            for ordinal, future in enumerate(as_completed(futures), start=1):
                code = futures[future]
                future.result()
                if ordinal % 100 == 0 or ordinal == len(pending):
                    print(
                        json.dumps(
                            {
                                "stage": "cninfo_memberships",
                                "finished": ordinal,
                                "pending": len(pending),
                                "latest_code": code,
                                "seconds": round(time.perf_counter() - started, 1),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    memberships: list[object] = []
    for code, path in sorted(paths.items()):
        raw = json.loads(path.read_text(encoding="utf-8"))
        memberships.extend(
            membership_changes_from_cninfo(
                code=code,
                records=raw["records"],
                not_after=end,
            )
        )
    return tuple(memberships), tuple(paths.values())


def _capture_factors(
    *,
    securities: Sequence[SecurityMasterRecord],
    start: date,
    end: date,
) -> tuple[tuple[object, ...], list[dict[str, object]]]:
    from xtquant import xtdata

    output: list[object] = []
    raw_ledger: list[dict[str, object]] = []
    for row in securities:
        native = f"{row.code.split('.', 1)[1]}.{row.code.split('.', 1)[0]}"
        frame = xtdata.get_divid_factors(
            native,
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
        )
        records: list[dict[str, object]] = []
        if hasattr(frame, "iterrows"):
            for index, item in frame.iterrows():
                record = {
                    "effective_on": str(index),
                    **{
                        key: float(item[key])
                        for key in (
                            "interest",
                            "stockBonus",
                            "stockGift",
                            "allotNum",
                            "allotPrice",
                            "gugai",
                            "dr",
                        )
                    },
                }
                records.append(record)
                raw_ledger.append({"code": row.code, **record})
        output.extend(
            qmt_factors_from_rows(
                code=row.code,
                rows=records,
                not_before=start,
                not_after=end,
            )
        )
    return tuple(output), raw_ledger


def _crosscheck(
    *,
    snapshot: PITMetadataSnapshot,
    current: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    observed_at = datetime.combine(snapshot.source_end, datetime.max.time(), tzinfo=CN)
    current_by_code: dict[str, set[str]] = {}
    for sector_id, members in current.items():
        for code in members:
            current_by_code.setdefault(code, set()).add(sector_id)
    active = tuple(
        row
        for row in snapshot.securities
        if row.listed_on(snapshot.source_end)
        and "\u9000\u5e02" not in row.name
        and not row.name.endswith("\u9000")
    )
    matched = missing = mismatched = duplicate = 0
    examples: list[dict[str, object]] = []
    for master in active:
        expected = snapshot.membership_at(master.code, observed_at)
        actual = current_by_code.get(master.code, set())
        if len(actual) > 1:
            duplicate += 1
        if expected is None or not actual:
            missing += 1
        elif expected.sector_id in actual:
            matched += 1
        else:
            mismatched += 1
            if len(examples) < 30:
                examples.append(
                    {
                        "code": master.code,
                        "cninfo": expected.sector_id,
                        "qmt": sorted(actual),
                    }
                )
    return {
        "active_security_count": len(active),
        "matched": matched,
        "missing": missing,
        "mismatched": mismatched,
        "duplicate_qmt_memberships": duplicate,
        "mismatch_examples": examples,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.start > args.end:
        raise ValueError("start cannot follow end")
    if args.workers > 12:
        raise ValueError("workers cannot exceed 12")
    output = args.output.resolve()
    source_dir = output.parent / (output.stem + "_sources")
    source_dir.mkdir(parents=True, exist_ok=True)
    from xtquant import xtdata

    xtdata.enable_hello = False
    if args.refresh_contracts:
        xtdata.download_history_contracts(incrementally=False)
        xtdata.download_sector_data()
    captured_at = datetime.now().astimezone(CN)
    headers = _cninfo_headers()
    taxonomy, raw_taxonomy = _taxonomy(headers)
    securities, master_audit = _security_master(args.start, args.end)
    current_sw1 = _qmt_current_sw1(taxonomy)
    memberships, membership_paths = _capture_memberships(
        securities=securities,
        end=args.end,
        headers=headers,
        source_dir=source_dir,
        workers=args.workers,
        force=args.force,
    )
    factors, raw_factors = _capture_factors(
        securities=securities,
        start=args.start,
        end=args.end,
    )
    master_payload = [
        {
            "code": row.code,
            "name": row.name,
            "listed_from": row.listed_from.isoformat(),
            "listed_through": (
                None
                if row.listed_through is None
                else row.listed_through.isoformat()
            ),
        }
        for row in securities
    ]
    source_hashes = tuple(
        sorted(
            (
                ("qmt_security_master", sha256_json(master_payload)),
                ("cninfo_sw1_taxonomy", sha256_json(raw_taxonomy)),
                (
                    "qmt_current_sw1_crosscheck",
                    sha256_json(
                        {key: list(value) for key, value in current_sw1.items()}
                    ),
                ),
                (
                    "cninfo_membership_checkpoint_tree",
                    _tree_hash(membership_paths, root=source_dir),
                ),
                ("qmt_corporate_action_ledger", sha256_json(raw_factors)),
            )
        )
    )
    snapshot = PITMetadataSnapshot(
        source_start=args.start,
        source_end=args.end,
        captured_at=captured_at,
        securities=tuple(securities),
        memberships=tuple(
            sorted(
                memberships,
                key=lambda row: (row.code, row.known_at, row.sector_id),
            )
        ),
        factors=tuple(
            sorted(factors, key=lambda row: (row.code, row.effective_on))
        ),
        qmt_sw1_sector_names=tuple(sorted(taxonomy.items())),
        source_hashes=source_hashes,
    )
    rights = sum(row.allot_num > 0 for row in snapshot.factors)
    reforms = sum(row.gugai > 0 for row in snapshot.factors)
    crosscheck = _crosscheck(snapshot=snapshot, current=current_sw1)
    audit = {
        "security_count": len(snapshot.securities),
        "membership_change_count": len(snapshot.memberships),
        "classified_security_count": len(
            {row.code for row in snapshot.memberships}
        ),
        "factor_count": len(snapshot.factors),
        "rights_event_count": rights,
        "share_reform_event_count": reforms,
        "qmt_cninfo_crosscheck": crosscheck,
        "certifiable_action_contract": rights == 0 and reforms == 0,
        "security_master_anomalies": master_audit,
    }
    _atomic_json(output, snapshot_payload(snapshot, audit=audit))
    print(
        json.dumps(
            {
                "complete": True,
                "output": str(output),
                "content_sha256": _sha256(output),
                **audit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
