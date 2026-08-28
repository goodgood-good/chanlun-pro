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
from typing import Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    CN,
    PITMetadataSnapshot,
    SecurityMasterRecord,
    SectorMembershipChange,
    membership_changes_from_cninfo,
    normalize_qmt_a_share_code,
    qmt_native_code,
    qmt_factors_from_rows,
    sha256_json,
    snapshot_payload,
    sw1_sector_id,
)
from chanlun.decision_support.trading_system.backtest.pit_scope import (
    FULL_MARKET_MODE,
    MAX_UNCONFIRMED_REQUESTED_CODES,
    PIT_SCOPE_SCHEMA,
    SCOPED_SECTOR_CLOSURE_MODE,
    relevant_sector_ids,
    scope_source_hashes,
    sector_closure_codes,
    validate_scope_proof,
)


DEFAULT_START = date(2025, 5, 1)
DEFAULT_END = date(2026, 7, 24)
_CNINFO_STANDARD = "008003"
_CNINFO_HISTORY_URL = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
_CNINFO_TAXONOMY_URL = "https://webapi.cninfo.com.cn/api/stock/p_public0002"
_QMT_CURRENT_A = "\u6caa\u6df1A\u80a1"
_QMT_EXPIRED_A = "\u8fc7\u671f\u6caa\u6df1A\u80a1"
_MEMBERSHIP_INDEX_SCHEMA = "chanlun-cninfo-membership-index/v1"


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
    result.add_argument(
        "--output",
        type=Path,
        required=True,
        help="explicit profile-specific PIT snapshot target",
    )
    result.add_argument("--workers", type=_positive_int, default=2)
    result.add_argument(
        "--codes",
        help="explicit comma-separated normalized stock codes (for example SH.600000)",
    )
    result.add_argument(
        "--codes-file",
        type=Path,
        help="UTF-8 file containing one normalized requested stock code per line",
    )
    result.add_argument(
        "--membership-checkpoint-dir",
        type=Path,
        help=(
            "existing complete CNInfo checkpoint directory used to prove the "
            "historical SW1 closure; scoped capture never fills checkpoint gaps"
        ),
    )
    result.add_argument(
        "--membership-index",
        type=Path,
        help=(
            "immutable index emitted by an explicitly authorized full PIT "
            "capture; required for bounded sector-closure resolution"
        ),
    )
    result.add_argument(
        "--full-market",
        action="store_true",
        help="explicitly request a full-market metadata capture",
    )
    result.add_argument(
        "--confirm-large-scope",
        action="store_true",
        help="second authorization required for full market or more than 20 subjects",
    )
    result.add_argument("--force", action="store_true")
    result.add_argument(
        "--refresh-contracts",
        action="store_true",
        help="refresh QMT expired-contract and current-sector files first",
    )
    return result


def _normalized_requested_codes(args: argparse.Namespace) -> tuple[str, ...]:
    if args.full_market and (args.codes or args.codes_file is not None):
        raise ValueError("--full-market cannot be combined with --codes/--codes-file")
    if args.codes and args.codes_file is not None:
        raise ValueError("pass only one of --codes or --codes-file")
    if args.full_market:
        if not args.confirm_large_scope:
            raise ValueError("--full-market requires --confirm-large-scope")
        return ()
    if not args.codes and args.codes_file is None:
        raise ValueError(
            "bounded PIT scope required: pass --codes/--codes-file, or explicitly "
            "authorize --full-market --confirm-large-scope"
        )
    raw_values = (
        args.codes.split(",")
        if args.codes
        else args.codes_file.read_text(encoding="utf-8").splitlines()
    )
    values: list[str] = []
    for raw in raw_values:
        code = raw.strip().upper()
        if not code or code.startswith("#"):
            continue
        qmt_native_code(code)
        values.append(code)
    requested = tuple(sorted(set(values)))
    if not requested:
        raise ValueError("bounded PIT scope is empty")
    if len(requested) > MAX_UNCONFIRMED_REQUESTED_CODES and not args.confirm_large_scope:
        raise ValueError(
            f"{len(requested)} requested codes require --confirm-large-scope"
        )
    return requested


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
    # AkShare 自带巨潮资讯官方请求码算法。只在主线程计算一次，因为
    # py_mini_racer 在 Windows 下不是线程安全的。
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


def _qmt_a_share_inventory() -> tuple[tuple[str, str], ...]:
    """Enumerate identities only; this must not read per-instrument details."""

    from xtquant import xtdata

    by_code: dict[str, str] = {}
    native_codes = sorted(
        set(xtdata.get_stock_list_in_sector(_QMT_CURRENT_A))
        | set(xtdata.get_stock_list_in_sector(_QMT_EXPIRED_A))
    )
    for native in native_codes:
        try:
            normalized = normalize_qmt_a_share_code(native)
        except ValueError:
            continue
        if normalized.startswith(("SH.", "SZ.", "BJ.")):
            by_code[normalized] = native
    if not by_code:
        raise RuntimeError("QMT A-share contract inventory is empty")
    return tuple(sorted(by_code.items()))


def _security_master(
    start: date,
    end: date,
    *,
    native_codes: Sequence[str],
) -> tuple[tuple[SecurityMasterRecord, ...], dict[str, object]]:
    from xtquant import xtdata
    import pandas as pd

    xtdata.enable_hello = False

    details: dict[str, Mapping[str, object]] = {}
    unreadable_details: list[str] = []
    invalid_expiry: list[str] = []
    invalid_open: list[str] = []
    for native in sorted(set(native_codes)):
        try:
            normalized = normalize_qmt_a_share_code(native)
        except ValueError:
            continue
        if not normalized.startswith(("SH.", "SZ.", "BJ.")):
            continue
        detail = xtdata.get_instrument_detail(native, iscomplete=False)
        if not isinstance(detail, Mapping):
            unreadable_details.append(native)
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

    if unreadable_details:
        raise RuntimeError(
            "QMT security detail is unavailable for scoped contracts: "
            + ",".join(unreadable_details[:10])
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
                # 它无法在本数据范围贡献 K 线或成交；认证范围内不保留猜测的到期日。
                    continue
                last_observed = sessions[-1]
                # 最后一根可用 K 线本身不是带生效日期的退市公告。后续覆盖率分母中
                # 仍保留该成员，仅在期末零值回收时使用测试结束时已知的状态。
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
        "detail_read_code_count": len(set(native_codes)),
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


def _strict_outside_range_identity_proof(
    *,
    code: str,
    native_code: str,
    detail: object,
    start: date,
    end: date,
) -> dict[str, object]:
    """Prove from instrument detail alone that an identity cannot enter replay."""

    if qmt_native_code(code) != native_code:
        raise RuntimeError(f"QMT identity mapping is inconsistent: {code}")
    if not isinstance(detail, Mapping):
        raise RuntimeError(f"QMT instrument detail is unreadable: {code}")
    open_text = str(detail.get("OpenDate") or "").strip()
    expiry_text = str(detail.get("ExpireDate") or "").strip()
    create_text = str(detail.get("CreateDate") or "").strip()
    raw_dates = {
        "OpenDate": open_text,
        "ExpireDate": expiry_text,
        "CreateDate": create_text,
    }

    def strict_date(value: str, label: str) -> date:
        try:
            parsed = datetime.strptime(value, "%Y%m%d").date()
        except ValueError as exc:
            raise RuntimeError(f"QMT {label} is invalid: {code}") from exc
        if not date(1990, 1, 1) <= parsed <= date(2100, 12, 31):
            raise RuntimeError(f"QMT {label} is outside the strict range: {code}")
        return parsed

    # A valid terminal date before replay is sufficient even when old expired
    # identities expose OpenDate=0.  No listing-start inference is required.
    if expiry_text not in {"", "0", "99999999"}:
        try:
            expiry = strict_date(expiry_text, "ExpireDate")
        except RuntimeError:
            expiry = None
        if expiry is not None and expiry < start:
            return {
                "code": code,
                "native_code": native_code,
                "listed_from": None,
                "listed_through": expiry.isoformat(),
                "created_on": None,
                "relation": "BEFORE_REPLAY_RANGE",
                "proof_basis": "EXPIRE_DATE_BEFORE_REPLAY",
                "raw_date_fields": raw_dates,
            }

    # Some newly created QMT identities temporarily carry the 1970/zero open
    # sentinel.  A valid later CreateDate proves the identity did not exist in
    # replay; it does not imply any K-line or listing-date backfill.
    if open_text in {"0", "19700101"} and create_text:
        try:
            created_on = strict_date(create_text, "CreateDate")
        except RuntimeError:
            created_on = None
        if created_on is not None and created_on > end:
            return {
                "code": code,
                "native_code": native_code,
                "listed_from": None,
                "listed_through": None,
                "created_on": created_on.isoformat(),
                "relation": "AFTER_REPLAY_RANGE",
                "proof_basis": "CREATE_DATE_AFTER_REPLAY_WITH_OPEN_PLACEHOLDER",
                "raw_date_fields": raw_dates,
            }

    listed_from = strict_date(open_text, "OpenDate")
    # A valid listing start after replay is independently sufficient.  QMT may
    # expose non-date expiry sentinels such as 10011011 for newly created
    # identities; bind that raw value, but never use it to negate OpenDate.
    if listed_from > end:
        return {
            "code": code,
            "native_code": native_code,
            "listed_from": listed_from.isoformat(),
            "listed_through": None,
            "created_on": None,
            "relation": "AFTER_REPLAY_RANGE",
            "proof_basis": "OPEN_DATE_AFTER_REPLAY",
            "raw_date_fields": raw_dates,
        }
    if expiry_text in {"", "0", "99999999"}:
        listed_through = None
    else:
        listed_through = strict_date(expiry_text, "ExpireDate")
        if listed_through < listed_from:
            raise RuntimeError(f"QMT listing interval is inverted: {code}")
    if listed_through is not None and listed_through < start:
        relation = "BEFORE_REPLAY_RANGE"
    elif listed_from > end:
        relation = "AFTER_REPLAY_RANGE"
    else:
        raise RuntimeError(
            "checkpoint-absent identity intersects the replay range: " + code
        )
    return {
        "code": code,
        "native_code": native_code,
        "listed_from": listed_from.isoformat(),
        "listed_through": (
            None if listed_through is None else listed_through.isoformat()
        ),
        "created_on": None,
        "relation": relation,
        "proof_basis": "STRICT_LISTING_INTERVAL_OUTSIDE_REPLAY",
        "raw_date_fields": raw_dates,
    }


def _certify_checkpoint_absent_identities(
    *,
    codes: Sequence[str],
    native_by_code: Mapping[str, str],
    start: date,
    end: date,
    detail_loader: Callable[..., object] | None = None,
) -> tuple[dict[str, object], ...]:
    """Read details only for absent identities; never infer from bars or network."""

    if detail_loader is None:
        from xtquant import xtdata

        detail_loader = xtdata.get_instrument_detail
    proofs: list[dict[str, object]] = []
    for code in sorted(set(codes)):
        try:
            native = native_by_code[code]
        except KeyError as exc:
            raise RuntimeError(f"QMT native identity is unavailable: {code}") from exc
        detail = detail_loader(native, iscomplete=False)
        proofs.append(
            _strict_outside_range_identity_proof(
                code=code,
                native_code=native,
                detail=detail,
                start=start,
                end=end,
            )
        )
    return tuple(proofs)


def _load_scoped_checkpoint_inventory(
    *,
    inventory_codes: Sequence[str],
    checkpoint_dir: Path,
    end: date,
) -> tuple[
    tuple[SectorMembershipChange, ...],
    tuple[Path, ...],
    tuple[str, ...],
]:
    """Load valid checkpoints and report truly absent identities separately."""

    root = checkpoint_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"membership checkpoint directory is missing: {root}")
    codes = tuple(sorted(set(inventory_codes)))
    paths_by_code = {
        code: root / f"{code.replace('.', '_')}.json" for code in codes
    }
    corrupt = tuple(
        code
        for code, path in paths_by_code.items()
        if path.exists() and not _valid_checkpoint(path, code=code, end=end)
    )
    if corrupt:
        raise RuntimeError(
            "historical SW1 closure cannot be certified: "
            f"{len(corrupt)} invalid CNInfo checkpoints ({','.join(corrupt[:10])})"
        )
    missing = tuple(
        code for code, path in paths_by_code.items() if not path.exists()
    )
    valid_paths = tuple(
        path for path in paths_by_code.values() if path.exists()
    )
    memberships: list[SectorMembershipChange] = []
    for code, path in paths_by_code.items():
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        memberships.extend(
            membership_changes_from_cninfo(
                code=code,
                records=raw["records"],
                not_after=end,
            )
        )
    return tuple(memberships), valid_paths, missing


def _load_complete_checkpoint_inventory(
    *,
    inventory_codes: Sequence[str],
    checkpoint_dir: Path,
    end: date,
) -> tuple[tuple[SectorMembershipChange, ...], tuple[Path, ...]]:
    """Load an already captured universe checkpoint tree without network repair."""

    memberships, paths, missing = _load_scoped_checkpoint_inventory(
        inventory_codes=inventory_codes,
        checkpoint_dir=checkpoint_dir,
        end=end,
    )
    if missing:
        examples = ",".join(missing[:10])
        raise RuntimeError(
            "historical SW1 closure cannot be certified: "
            f"{len(missing)} missing/invalid CNInfo checkpoints ({examples})"
        )
    return memberships, paths


def _membership_index_payload(
    *,
    memberships: Sequence[SectorMembershipChange],
    checkpoint_paths: Sequence[Path],
    checkpoint_root: Path,
    end: date,
) -> dict[str, object]:
    checkpoints: list[dict[str, str]] = []
    for path in sorted(checkpoint_paths, key=lambda value: value.name):
        raw = json.loads(path.read_text(encoding="utf-8"))
        code = str(raw.get("code") or "")
        qmt_native_code(code)
        checkpoints.append(
            {
                "code": code,
                "path": path.relative_to(checkpoint_root).as_posix(),
                "sha256": _sha256(path),
            }
        )
    membership_rows = [
        {
            "code": row.code,
            "sector_id": row.sector_id,
            "sector_name": row.sector_name,
            "industry_code": row.industry_code,
            "source_changed_on": row.source_changed_on.isoformat(),
            "known_at": row.known_at.isoformat(),
        }
        for row in sorted(
            memberships,
            key=lambda value: (value.code, value.known_at, value.sector_id),
        )
    ]
    core: dict[str, object] = {
        "schema": _MEMBERSHIP_INDEX_SCHEMA,
        "not_after": end.isoformat(),
        "checkpoint_count": len(checkpoints),
        "checkpoints": checkpoints,
        "memberships": membership_rows,
        "checkpoint_tree_sha256": sha256_json(checkpoints),
    }
    return {**core, "content_sha256": sha256_json(core)}


def _load_membership_index(
    *,
    path: Path,
    checkpoint_dir: Path,
    end: date,
) -> tuple[
    tuple[str, ...],
    tuple[SectorMembershipChange, ...],
    dict[str, tuple[Path, str]],
    str,
]:
    try:
        raw = json.loads(path.resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"membership index is unreadable: {path}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("membership index is malformed")
    core = {key: value for key, value in raw.items() if key != "content_sha256"}
    if (
        raw.get("schema") != _MEMBERSHIP_INDEX_SCHEMA
        or raw.get("not_after") != end.isoformat()
        or raw.get("content_sha256") != sha256_json(core)
    ):
        raise ValueError("membership index identity/content proof is invalid")
    raw_checkpoints = raw.get("checkpoints")
    raw_memberships = raw.get("memberships")
    if not isinstance(raw_checkpoints, list) or not isinstance(raw_memberships, list):
        raise ValueError("membership index arrays are malformed")
    root = checkpoint_dir.resolve()
    checkpoints: dict[str, tuple[Path, str]] = {}
    for item in raw_checkpoints:
        if not isinstance(item, Mapping):
            raise ValueError("membership index checkpoint is malformed")
        code = str(item.get("code") or "")
        qmt_native_code(code)
        expected_relative = f"{code.replace('.', '_')}.json"
        if item.get("path") != expected_relative or code in checkpoints:
            raise ValueError("membership index checkpoint identity is invalid")
        digest = str(item.get("sha256") or "")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("membership index checkpoint hash is invalid")
        checkpoints[code] = (root / expected_relative, digest)
    codes = tuple(sorted(checkpoints))
    if int(raw.get("checkpoint_count") or -1) != len(codes):
        raise ValueError("membership index checkpoint count is invalid")
    if raw.get("checkpoint_tree_sha256") != sha256_json(raw_checkpoints):
        raise ValueError("membership index checkpoint tree proof is invalid")
    memberships: list[SectorMembershipChange] = []
    try:
        for item in raw_memberships:
            if not isinstance(item, Mapping):
                raise ValueError
            code = str(item["code"])
            if code not in checkpoints:
                raise ValueError
            memberships.append(
                SectorMembershipChange(
                    code=code,
                    sector_id=str(item["sector_id"]),
                    sector_name=str(item["sector_name"]),
                    industry_code=str(item["industry_code"]),
                    source_changed_on=date.fromisoformat(
                        str(item["source_changed_on"])
                    ),
                    known_at=datetime.fromisoformat(str(item["known_at"])),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("membership index history is malformed") from exc
    return (
        codes,
        tuple(memberships),
        checkpoints,
        str(raw["checkpoint_tree_sha256"]),
    )


def _verify_indexed_closure_checkpoints(
    *,
    codes: Sequence[str],
    checkpoints: Mapping[str, tuple[Path, str]],
    end: date,
) -> tuple[tuple[SectorMembershipChange, ...], tuple[Path, ...]]:
    memberships: list[SectorMembershipChange] = []
    paths: list[Path] = []
    for code in sorted(set(codes)):
        try:
            path, digest = checkpoints[code]
        except KeyError as exc:
            raise RuntimeError(f"membership index has no checkpoint: {code}") from exc
        if (
            not _valid_checkpoint(path, code=code, end=end)
            or _sha256(path) != digest
        ):
            raise RuntimeError(f"indexed membership checkpoint is invalid: {code}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        memberships.extend(
            membership_changes_from_cninfo(
                code=code,
                records=raw["records"],
                not_after=end,
            )
        )
        paths.append(path)
    return tuple(memberships), tuple(paths)


def _sector_names_from_memberships(
    memberships: Sequence[SectorMembershipChange],
) -> dict[str, str]:
    latest: dict[str, tuple[datetime, str]] = {}
    for raw in memberships:
        sector_id = str(raw.sector_id)
        candidate = (raw.known_at, str(raw.sector_name))
        if sector_id not in latest or candidate[0] >= latest[sector_id][0]:
            latest[sector_id] = candidate
    return {
        sector_id: name
        for sector_id, (_known_at, name) in sorted(latest.items())
    }


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

    xtdata.enable_hello = False

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
    requested_codes = _normalized_requested_codes(args)
    if args.start > args.end:
        raise ValueError("start cannot follow end")
    if args.workers > 12:
        raise ValueError("workers cannot exceed 12")
    if not args.full_market and args.membership_checkpoint_dir is None:
        raise ValueError(
            "scoped PIT capture requires --membership-checkpoint-dir; "
            "checkpoint gaps are never repaired implicitly"
        )
    if not args.full_market and args.membership_index is None:
        raise ValueError(
            "scoped PIT capture requires an immutable --membership-index; "
            "create it only through an explicitly authorized full PIT capture"
        )
    if args.full_market and (
        (args.membership_checkpoint_dir is None)
        != (args.membership_index is None)
    ):
        raise ValueError(
            "full-market checkpoint reuse requires both "
            "--membership-checkpoint-dir and --membership-index"
        )
    if args.full_market and args.force and args.membership_index is not None:
        raise ValueError("--force cannot be combined with immutable checkpoint reuse")
    if not args.full_market and args.refresh_contracts:
        raise ValueError("--refresh-contracts is forbidden for scoped PIT capture")
    output = args.output.resolve()
    source_dir = output.parent / (output.stem + "_sources")
    captured_at = datetime.now().astimezone(CN)
    if not args.full_market:
        (
            inventory_codes,
            indexed_memberships,
            indexed_checkpoints,
            checkpoint_tree_hash,
        ) = _load_membership_index(
            path=args.membership_index,
            checkpoint_dir=args.membership_checkpoint_dir,
            end=args.end,
        )
        missing_requested = tuple(sorted(set(requested_codes) - set(inventory_codes)))
        if missing_requested:
            raise ValueError(
                "requested codes are absent from immutable membership index: "
                + ",".join(missing_requested)
            )
        closure_candidate_codes, selected_sector_ids = sector_closure_codes(
            indexed_memberships,
            requested_codes=requested_codes,
            start=args.start,
            end=args.end,
        )
        all_memberships, membership_paths = _verify_indexed_closure_checkpoints(
            codes=closure_candidate_codes,
            checkpoints=indexed_checkpoints,
            end=args.end,
        )
        indexed_closure_memberships = tuple(
            sorted(
                (
                    row
                    for row in indexed_memberships
                    if row.code in set(closure_candidate_codes)
                ),
                key=lambda row: (row.code, row.known_at, row.sector_id),
            )
        )
        verified_closure_memberships = tuple(
            sorted(
                all_memberships,
                key=lambda row: (row.code, row.known_at, row.sector_id),
            )
        )
        if verified_closure_memberships != indexed_closure_memberships:
            raise RuntimeError("membership index does not match closure checkpoints")
        inventory_hash = sha256_json(list(inventory_codes))
        native_by_code = {
            code: qmt_native_code(code) for code in closure_candidate_codes
        }

    if args.full_market:
        from xtquant import xtdata

        xtdata.enable_hello = False
        if args.refresh_contracts:
            xtdata.download_history_contracts(incrementally=False)
            xtdata.download_sector_data()
        inventory = _qmt_a_share_inventory()
        inventory_codes = tuple(code for code, _native in inventory)
        native_by_code = dict(inventory)
        inventory_hash = sha256_json(list(inventory_codes))
        headers = _cninfo_headers()
        taxonomy, raw_taxonomy = _taxonomy(headers)
        securities, master_audit = _security_master(
            args.start,
            args.end,
            native_codes=tuple(native_by_code.values()),
        )
        current_sw1 = _qmt_current_sw1(taxonomy)
        requested_codes = tuple(row.code for row in securities)
        if args.membership_index is not None:
            (
                indexed_codes,
                indexed_memberships,
                indexed_checkpoints,
                _indexed_tree_hash,
            ) = _load_membership_index(
                path=args.membership_index,
                checkpoint_dir=args.membership_checkpoint_dir,
                end=args.end,
            )
            if indexed_codes != requested_codes:
                missing = tuple(sorted(set(requested_codes) - set(indexed_codes)))
                stale = tuple(sorted(set(indexed_codes) - set(requested_codes)))
                raise RuntimeError(
                    "immutable membership index does not match the full-market "
                    f"security master (missing={len(missing)}, stale={len(stale)}): "
                    + ",".join((*missing[:5], *stale[:5]))
                )
            memberships, membership_paths = _verify_indexed_closure_checkpoints(
                codes=indexed_codes,
                checkpoints=indexed_checkpoints,
                end=args.end,
            )
            normalized_indexed_memberships = tuple(
                sorted(
                    indexed_memberships,
                    key=lambda row: (row.code, row.known_at, row.sector_id),
                )
            )
            normalized_verified_memberships = tuple(
                sorted(
                    memberships,
                    key=lambda row: (row.code, row.known_at, row.sector_id),
                )
            )
            if normalized_verified_memberships != normalized_indexed_memberships:
                raise RuntimeError(
                    "membership index does not match its verified checkpoints"
                )
            checkpoint_root = args.membership_checkpoint_dir.resolve()
            membership_source = {
                "mode": "IMMUTABLE_INDEX_REUSE",
                "membership_index": str(args.membership_index.resolve()),
                "checkpoint_directory": str(checkpoint_root),
            }
        else:
            source_dir.mkdir(parents=True, exist_ok=True)
            memberships, membership_paths = _capture_memberships(
                securities=securities,
                end=args.end,
                headers=headers,
                source_dir=source_dir,
                workers=args.workers,
                force=args.force,
            )
            checkpoint_root = source_dir
            membership_index_path = source_dir / "membership_index.json"
            _atomic_json(
                membership_index_path,
                _membership_index_payload(
                    memberships=memberships,
                    checkpoint_paths=membership_paths,
                    checkpoint_root=source_dir / "cninfo_memberships",
                    end=args.end,
                ),
            )
            membership_source = {
                "mode": "FULL_NETWORK_CAPTURE",
                "membership_index": str(membership_index_path.resolve()),
                "checkpoint_directory": str(
                    (source_dir / "cninfo_memberships").resolve()
                ),
            }
        closure_codes = requested_codes
        closure_candidate_codes = closure_codes
        checkpoint_absent_codes: tuple[str, ...] = ()
        outside_range_proofs: tuple[dict[str, object], ...] = ()
        detail_read_codes = inventory_codes
        selected_sector_ids = relevant_sector_ids(
            memberships,
            codes=requested_codes,
            start=args.start,
            end=args.end,
        )
        checkpoint_tree_hash = _tree_hash(
            membership_paths,
            root=checkpoint_root,
        )
        scope_mode = FULL_MARKET_MODE
        taxonomy_hash = sha256_json(raw_taxonomy)
        current_sw1_hash = sha256_json(
            {key: list(value) for key, value in current_sw1.items()}
        )
        checkpoint_hash_name = "cninfo_membership_checkpoint_tree"
    else:
        checkpoint_absent_codes: tuple[str, ...] = ()
        outside_range_proofs: tuple[dict[str, object], ...] = ()
        securities, master_audit = _security_master(
            args.start,
            args.end,
            native_codes=tuple(
                native_by_code[code] for code in closure_candidate_codes
            ),
        )
        materialized_codes = tuple(row.code for row in securities)
        missing_requested_details = tuple(
            sorted(set(requested_codes) - set(materialized_codes))
        )
        if missing_requested_details:
            raise RuntimeError(
                "requested codes do not intersect the certified replay range: "
                + ",".join(missing_requested_details)
            )
        closure_codes = materialized_codes
        detail_read_codes = tuple(sorted(closure_candidate_codes))
        closure_set = set(closure_codes)
        memberships = tuple(
            row for row in all_memberships if row.code in closure_set
        )
        taxonomy = _sector_names_from_memberships(memberships)
        missing_sector_names = tuple(
            sorted(set(selected_sector_ids) - set(taxonomy))
        )
        if missing_sector_names:
            raise RuntimeError(
                "historical sector closure has no sector names: "
                + ",".join(missing_sector_names)
            )
        current_sw1 = {}
        checkpoint_root = args.membership_checkpoint_dir.resolve()
        membership_source = {
            "mode": "IMMUTABLE_INDEX_REUSE",
            "membership_index": str(args.membership_index.resolve()),
            "checkpoint_directory": str(checkpoint_root),
        }
        scope_mode = SCOPED_SECTOR_CLOSURE_MODE
        taxonomy_hash = sha256_json(
            {key: value for key, value in sorted(taxonomy.items())}
        )
        current_sw1_hash = None
        checkpoint_hash_name = "cninfo_membership_universe_checkpoint_tree"

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
    scope = {
        "schema": PIT_SCOPE_SCHEMA,
        "mode": scope_mode,
        "requested_codes": list(requested_codes),
        "requested_code_count": len(requested_codes),
        "selected_sector_ids": list(selected_sector_ids),
        "selected_sector_count": len(selected_sector_ids),
        "closure_codes": list(closure_codes),
        "closure_code_count": len(closure_codes),
        "closure_candidate_codes": list(closure_candidate_codes),
        "closure_candidate_code_count": len(closure_candidate_codes),
        "excluded_closure_candidate_codes": list(
            sorted(set(closure_candidate_codes) - set(closure_codes))
        ),
        "sector_closure_complete": True,
        "enumerated_contract_code_count": (
            len(closure_codes) if args.full_market else len(inventory_codes)
        ),
        "enumerated_contract_codes_sha256": inventory_hash,
        "membership_checkpoint_count": (
            len(membership_paths) if args.full_market else len(inventory_codes)
        ),
        "membership_checkpoint_tree_sha256": checkpoint_tree_hash,
        "missing_checkpoint_codes": [],
        "checkpoint_absent_identity_codes": list(checkpoint_absent_codes),
        "uncertified_checkpoint_absent_identity_codes": [],
        "excluded_identity_codes": [
            str(row["code"]) for row in outside_range_proofs
        ],
        "excluded_identity_count": len(outside_range_proofs),
        "certified_outside_range_identity_count": len(outside_range_proofs),
        "certified_outside_range_intervals": list(outside_range_proofs),
        "certified_outside_range_intervals_sha256": sha256_json(
            list(outside_range_proofs)
        ),
        "detail_read_codes": list(detail_read_codes),
        "detail_read_code_count": (
            int(master_audit["detail_read_code_count"])
            + len(outside_range_proofs)
        ),
        "factor_read_code_count": len(securities),
        "large_scope_confirmed": bool(args.confirm_large_scope),
    }
    base_hashes: list[tuple[str, str]] = [
        ("qmt_security_master", sha256_json(master_payload)),
        ("qmt_a_share_contract_inventory", inventory_hash),
        ("cninfo_sw1_taxonomy", taxonomy_hash),
        (checkpoint_hash_name, checkpoint_tree_hash),
        ("qmt_corporate_action_ledger", sha256_json(raw_factors)),
    ]
    if current_sw1_hash is not None:
        base_hashes.append(("qmt_current_sw1_crosscheck", current_sw1_hash))
    if not args.full_market:
        base_hashes.append(
            (
                "cninfo_membership_closure_checkpoint_tree",
                _tree_hash(membership_paths, root=checkpoint_root),
            )
        )
    source_hashes = tuple(
        sorted(
            (*base_hashes, *scope_source_hashes(scope))
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
    proof_failures = validate_scope_proof(
        snapshot=snapshot,
        scope=scope,
        replay_codes=requested_codes,
    )
    if proof_failures:
        raise RuntimeError(
            "generated PIT scope proof is inconsistent: "
            + ",".join(proof_failures)
        )
    rights = sum(row.allot_num > 0 for row in snapshot.factors)
    reforms = sum(row.gugai > 0 for row in snapshot.factors)
    crosscheck = (
        _crosscheck(snapshot=snapshot, current=current_sw1)
        if args.full_market
        else {
            "status": "NOT_RUN_FOR_SCOPED_CAPTURE",
            "reason": "no non-closure QMT sector membership reads",
        }
    )
    audit = {
        "scope": scope,
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
        "membership_checkpoint_source": membership_source,
    }
    _atomic_json(output, snapshot_payload(snapshot, audit=audit))
    console_scope = {
        "schema": scope["schema"],
        "mode": scope["mode"],
        "requested_codes": scope["requested_codes"],
        "requested_code_count": scope["requested_code_count"],
        "selected_sector_ids": scope["selected_sector_ids"],
        "selected_sector_count": scope["selected_sector_count"],
        "closure_code_count": scope["closure_code_count"],
        "closure_candidate_code_count": scope["closure_candidate_code_count"],
        "membership_checkpoint_count": scope["membership_checkpoint_count"],
        "certified_outside_range_identity_count": scope[
            "certified_outside_range_identity_count"
        ],
        "detail_read_code_count": scope["detail_read_code_count"],
        "factor_read_code_count": scope["factor_read_code_count"],
        "large_scope_confirmed": scope["large_scope_confirmed"],
    }
    print(
        json.dumps(
            {
                "complete": True,
                "output": str(output),
                "content_sha256": _sha256(output),
                "scope": console_scope,
                "security_count": audit["security_count"],
                "membership_change_count": audit["membership_change_count"],
                "factor_count": audit["factor_count"],
                "certifiable_action_contract": audit[
                    "certifiable_action_contract"
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
