"""Dated SSE stock trading halts, including earlier halts of the same stock.

The exchange's search_tradeTip_2021.js supplies the query and temporary-slot
mapping. Its stock query returns overlapping intervals; endStopDate is the
last suspended date, not the predicted resumption date. Unclosed/unknown
records never exempt missing prices.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time
import uuid

import requests

SOURCE_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SOURCE_PAGE = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/"
FIELD_MAPPING = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_tradeTip_2021.js"
SQL_ID = "GW_PL_JYTS_TFPXX"


def _date(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError("SSE stock suspension date is invalid")
    return datetime.strptime(value, "%Y%m%d").date()


def _fetch(code, first, last):
    began, through = [datetime.fromisoformat(day).date() for day in (first, last)]
    rows = {}
    if (through - began).days > 366 * 5:
        raise ValueError("SSE stock suspension query range is too large")
    with requests.Session() as session:
        session.trust_env = False
        while began <= through:
            ended = min(through, began + timedelta(days=83))
            expected = None
            received = 0
            seen = set()
            for page in range(1, 21):
                response = session.get(
                    SOURCE_URL,
                    params={"isPagination": "true", "sqlId": SQL_ID,
                            "pageHelp.pageSize": 100, "pageHelp.pageNo": page,
                            "productCode": code[3:], "keyWords": "",
                            "startStopDate": began.strftime("%Y%m%d"),
                            "endStopDate": ended.strftime("%Y%m%d")},
                    headers={"Referer": SOURCE_PAGE, "User-Agent": "Mozilla/5.0"},
                    timeout=(4, 8),
                )
                response.raise_for_status()
                body = response.json()
                if (not isinstance(body, dict) or body.get("actionErrors")
                        or body.get("fieldErrors") or not isinstance(body.get("pageHelp"), dict)):
                    raise ValueError("SSE stock suspension query failed")
                meta = body.get("pageHelp", {})
                total, items = meta.get("total"), body.get("result")
                if (body.get("sqlId") != SQL_ID or type(total) is not int or total < 0
                        or meta.get("pageNo") != page or not isinstance(items, list)
                        or expected is not None and total != expected):
                    raise ValueError("SSE stock suspension response is incomplete")
                expected = total
                for row in items:
                    if not isinstance(row, dict) or row.get("productCode") != code[3:]:
                        raise ValueError("SSE stock suspension identity mismatch")
                    start = _date(row.get("startStopDate"))
                    end = _date(row["endStopDate"]) if row.get("endStopDate") else None
                    if start > ended or end is not None and (end < began or end < start):
                        raise ValueError("SSE stock suspension query coverage mismatch")
                    key = json.dumps(row, sort_keys=True, ensure_ascii=True)
                    if key in seen:
                        raise ValueError("SSE stock suspension pagination repeated a record")
                    seen.add(key)
                    rows[key] = row
                received += len(items)
                if received == total:
                    break
                if not items or received > total or page == 20:
                    raise ValueError("SSE stock suspension pagination is incomplete")
            began = ended + timedelta(days=1)
    return list(rows.values())


def intervals_from_records(code, records, start, cutoff):
    from .sessions import CN

    slots = {"WH": ("09:15", "15:00"), "930": ("09:30", "10:30"),
             "AM": ("09:15", "11:30"), "PM": ("13:00", "15:00")}
    intervals = set()
    for row in records:
        if not isinstance(row, dict) or row.get("productCode") != code[3:]:
            raise ValueError("SSE stock suspension identity mismatch")
        began = _date(row.get("startStopDate"))
        ended = _date(row["endStopDate"]) if row.get("endStopDate") else None
        if ended is not None and ended < began:
            raise ValueError("SSE stock suspension interval is reversed")
        if row.get("controlType") != "TR" or ended is None:
            continue
        if row.get("type") == "LSTP" and began == ended:
            slot = slots.get(row.get("stopTime"))
        elif row.get("type") == "LXTP" and row.get("stopTime") in ("", "WH"):
            slot = ("09:15", "15:00")
        else:
            slot = None
        if not slot:
            continue
        first, last = [int(datetime.fromisoformat(day.isoformat() + "T" + clock)
                           .replace(tzinfo=CN).timestamp())
                       for day, clock in zip((began, ended), slot)]
        if first <= cutoff and last >= start:
            intervals.add((first, min(last, cutoff)))
    return [list(pair) for pair in sorted(intervals)]


def suspension_evidence(code, start, cutoff, *, cache_root=None):
    from .sessions import CN, SCHEMA, _digest, _read_cache

    if cache_root is None:
        from chanlun import config
        cache_root = config.get_data_path() / "screening"
    first = datetime.fromtimestamp(start, CN).date().isoformat()
    last = datetime.fromtimestamp(cutoff, CN).date().isoformat()
    path = Path(cache_root) / ("sse_stock_suspensions_" + code + ".json")
    data = _read_cache(path, first, cutoff, source_url=SOURCE_URL)
    if data is not None and (data.get("symbol") != code or data.get("query_through", "") < last):
        data = None
    if data is not None:
        try:
            intervals = intervals_from_records(code, data["records"], start, cutoff)
        except (TypeError, ValueError):
            data = None
    if data is None:
        data = {"schema": SCHEMA, "source_url": SOURCE_URL, "symbol": code,
                "query_from": first, "query_through": last,
                "records": _fetch(code, first, last), "fetched_at": int(time.time())}
        intervals = intervals_from_records(code, data["records"], start, cutoff)
        temporary = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({**data, "sha256": _digest(data)},
                                           ensure_ascii=False, allow_nan=False), encoding="utf8")
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    proof = {"schema": SCHEMA, "symbol": code, "source_url": SOURCE_URL,
             "source_page": SOURCE_PAGE, "field_mapping_source": FIELD_MAPPING,
             "checked_through": cutoff, "intervals": intervals}
    return {**proof, "revision": _digest(proof)}
