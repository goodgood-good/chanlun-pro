"""Independent suspension records; missing quotes never prove a suspension.

The public disclosure feed supplies actual SUSPEND_START_TIME/END_TIME.
PREDICT_RESUME_DATE is deliberately not used to exempt future missing bars.
QMT's aligned suspendFlag also marks unavailable local history and cannot be
used as independent evidence. One replaceable feed cache is shared by scans.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import requests
import pandas as pd

CN = ZoneInfo("Asia/Shanghai")
SOURCE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_PAGE = "https://data.eastmoney.com/tfpxx/"
SCHEMA = "chanlun-screening-suspensions-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _read_cache(path, start, cutoff):
    try:
        data = json.loads(path.read_bytes())
        digest = data.pop("sha256")
        if (digest == _digest(data) and data["schema"] == SCHEMA
                and data["source_url"] == SOURCE_URL and data["query_from"] <= start
                and cutoff <= data["fetched_at"] <= time.time()
                and time.time() - data["fetched_at"] < 3600
                and isinstance(data["records"], list)):
            return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _fetch_records(start):
    records = []
    # Chinese public data is reachable directly; do not inherit a foreign
    # quote provider's proxy or credentials for this anonymous endpoint.
    with requests.Session() as session:
        session.trust_env = False
        params = {"reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE", "columns": "ALL",
                  "source": "WEB", "client": "WEB", "sortColumns": "SUSPEND_START_DATE",
                  "sortTypes": "-1", "pageSize": 500,
                  "filter": f'(MARKET="全部")(DATETIME=\'{start}\')'}
        pages = None
        for page in range(1, 21):
            response = session.get(SOURCE_URL, params={**params, "pageNumber": page}, timeout=(4, 8))
            response.raise_for_status()
            body = response.json()
            if body.get("success") is not True or not isinstance(body.get("result"), dict):
                raise ValueError("suspension feed did not return a complete result")
            result = body["result"]
            if type(result.get("pages")) is not int or not 1 <= result["pages"] <= 20:
                raise ValueError("suspension feed page count is invalid")
            if pages is not None and pages != result["pages"]:
                raise ValueError("suspension feed changed during pagination")
            pages = result["pages"]
            if not isinstance(result.get("data"), list):
                raise ValueError("suspension feed records are invalid")
            records.extend(result["data"])
            if page == pages:
                return records
    raise ValueError("suspension feed pagination is incomplete")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("suspension time is missing")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN)
    return int(stamp.timestamp())


def suspension_evidence(code: str, start: int, cutoff: int, *, cache_root=None) -> dict:
    """Resolve reported intervals for one symbol, retaining feed provenance."""
    if not re.fullmatch(r"(?:SH|SZ|BJ)\.\d{6}", code) or start > cutoff:
        raise ValueError("suspension request identity is invalid")
    first = datetime.fromtimestamp(start, CN).date().isoformat()
    if cache_root is None:
        from chanlun import config
        cache_root = config.get_data_path() / "screening"
    path = Path(cache_root) / "suspensions.json"
    data = _read_cache(path, first, cutoff)
    if data is None:
        data = {"schema": SCHEMA, "source_url": SOURCE_URL, "query_from": first,
                "records": _fetch_records(first), "fetched_at": int(time.time())}
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps({**data, "sha256": _digest(data)}, ensure_ascii=False,
                                       allow_nan=False), encoding="utf-8")
            os.replace(temp, path)
        except OSError:
            pass  # The optional feed cache cannot invalidate a fetched record.
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    vendor_code = code[3:] + "." + code[:2]
    intervals = set()
    for record in data["records"]:
        if not isinstance(record, dict) or record.get("SECUCODE") != vendor_code:
            continue
        began = _timestamp(record.get("SUSPEND_START_TIME"))
        ended = cutoff if record.get("SUSPEND_END_TIME") is None else _timestamp(record["SUSPEND_END_TIME"])
        if ended < began and began <= cutoff:
            raise ValueError("suspension interval ends before it starts")
        if began <= cutoff and ended >= start:
            intervals.add((began, min(ended, cutoff)))
    evidence = {"schema": SCHEMA, "symbol": code, "source_url": SOURCE_URL,
                "source_page": SOURCE_PAGE, "checked_through": cutoff,
                "intervals": [list(pair) for pair in sorted(intervals)]}
    return {**evidence, "revision": _digest(evidence)}


def exempt_closes(frame, context, expected):
    """Consume only explicit matching evidence; reject any conflict with trades."""
    evidence = frame.attrs.get("screening_suspensions")
    if evidence is None:
        return set()
    try:
        content = {k: v for k, v in evidence.items() if k != "revision"}
        codes = {context["symbol"]} if "symbol" in context else set(frame.code) if "code" in frame else set()
        if (evidence["schema"] != SCHEMA or evidence["source_url"] != SOURCE_URL
                or codes != {evidence["symbol"]} or evidence["revision"] != _digest(content)
                or type(evidence["checked_through"]) is not int
                or evidence["checked_through"] < context["cutoff"]
                or not isinstance(evidence["intervals"], list)):
            raise ValueError("suspension evidence identity is invalid")
        step = int(context["frequency"][:-1]) * 60
        dates = frame.date.astype(pd.DatetimeTZDtype(unit="ns", tz=CN)).astype("int64").to_numpy() // 1_000_000_000
        volume = frame.volume.to_numpy()
        output = set()
        for pair in evidence["intervals"]:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(x) is not int for x in pair):
                raise ValueError("suspension evidence interval is invalid")
            first, last = pair
            if first > last or last > evidence["checked_through"]:
                raise ValueError("suspension evidence interval is invalid")
            if ((dates - step >= first) & (dates <= last) & (volume > 0)).any():
                raise ValueError("suspension record conflicts with recorded trades")
            output.update(at for at in expected if first <= at - step and at <= last)
        return output
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("suspension evidence is invalid") from exc
