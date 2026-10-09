"""Closed, whole-session SZSE stock halts from the exchange's historical report.

CATALOGID=1798 is published by /disclosure/memo/. Its metadata declares
txtDmorjc (code/name), txtKsrq/txtZzrq (event-date range), tpkssj (halt),
fpkssj (resumption), and tpsj (duration/action). A resumption is often a
separate row with an empty halt date. The date filter is not interval overlap:
002036 on 2025-12-19 returns no row during its disclosed 12/18--12/25 halt.

Only explicit, already-closed opening-to-opening records are consumed.
Intraday/unknown times, orphan resumptions and open-ended halts never excuse
missing quotes. The bounded lookback helps pair a halt starting before the
requested data window; it does not claim exhaustive history beyond that bound.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time as daytime, timedelta
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import requests

SOURCE_URL = "https://www.szse.cn/api/report/ShowReport/data"
SOURCE_PAGE = "https://www.szse.cn/disclosure/memo/index.html"
CATALOG_ID = "1798"
PARSER_VERSION = "szse-stock-closed-open-to-open-v1"
LOOKBACK_DAYS = 365
MAX_PAGES = 20
_CODE = re.compile(r"SZ\.(?:00|30)\d{4}\Z")
_OPEN = re.compile(r"(\d{4}-\d{2}-\d{2})\s+开市\Z")
_DATE_PREFIX = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\s|$)")


def _request_window(code, start, cutoff):
    from .sessions import CN

    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError("SZSE stock suspension identity is invalid")
    if type(start) is not int or type(cutoff) is not int or start < 0 or start > cutoff:
        raise ValueError("SZSE stock suspension time range is invalid")
    first = datetime.fromtimestamp(start, CN).date()
    last = datetime.fromtimestamp(cutoff, CN).date()
    return first, last


def _field(value):
    if not isinstance(value, str):
        raise ValueError("SZSE suspension time field is invalid")
    value = value.strip()
    if not value:
        return None, False
    match = _DATE_PREFIX.match(value)
    if match is None:
        raise ValueError("SZSE suspension date is not independently identifiable")
    day = date.fromisoformat(match.group(1))
    return day, _OPEN.fullmatch(value) is not None


def _record(code, row):
    if not isinstance(row, dict) or row.get("zqdm") != code[3:]:
        raise ValueError("SZSE stock suspension identity mismatch")
    for key in ("zqjc", "tpsj", "tpyy"):
        if not isinstance(row.get(key), str):
            raise ValueError("SZSE stock suspension fields are invalid")
    first, first_open = _field(row.get("tpkssj"))
    last, last_open = _field(row.get("fpkssj"))
    if first is None and last is None:
        raise ValueError("SZSE stock suspension has no event date")
    if first is not None and last is not None and last < first:
        raise ValueError("SZSE stock suspension ends before it starts")
    return first, last, first_open, last_open


def _params(code, first, last, page):
    return {"SHOWTYPE": "JSON", "CATALOGID": CATALOG_ID, "TABKEY": "tab1",
            "PAGENO": str(page), "txtDmorjc": code[3:], "txtKsrq": first, "txtZzrq": last}


def _validate_page(code, first, last, page, body, expected, seen):
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise ValueError("SZSE stock suspension response is incomplete")
    report = body[0]
    meta, items = report.get("metadata"), report.get("data")
    if not isinstance(meta, dict) or not isinstance(items, list) or report.get("error") is not None:
        raise ValueError("SZSE stock suspension response is incomplete")
    total, size, count = meta.get("recordcount"), meta.get("pagesize"), meta.get("pagecount")
    if (meta.get("catalogid") != CATALOG_ID or meta.get("tabkey") != "tab1"
            or type(meta.get("pageno")) is not int or meta["pageno"] != page
            or any(type(value) is not int for value in (total, size, count))
            or total < 0 or not 1 <= size <= 100 or not 0 <= count <= MAX_PAGES
            or count != math.ceil(total / size) or len(items) > size
            or expected is not None and (total, size, count) != expected
            or page < count and len(items) != size):
        raise ValueError("SZSE stock suspension pagination is incomplete")
    begin, through = date.fromisoformat(first), date.fromisoformat(last)
    for row in items:
        began, ended, _, _ = _record(code, row)
        if not any(begin <= day <= through for day in (began, ended) if day is not None):
            raise ValueError("SZSE stock suspension response ignores the requested date range")
        key = json.dumps(row, sort_keys=True, ensure_ascii=True)
        if key in seen:
            raise ValueError("SZSE stock suspension pages repeat records")
        seen.add(key)
    return items, (total, size, count)


def _fetch(code, first, last):
    """Read one symbol's bounded event window, validating every returned page."""
    rows, pages, seen, expected = [], [], set(), None
    with requests.Session() as session:
        session.trust_env = False
        for page in range(1, MAX_PAGES + 1):
            params = _params(code, first, last, page)
            response = session.get(SOURCE_URL, params=params,
                                   headers={"Referer": SOURCE_PAGE, "User-Agent": "Mozilla/5.0"},
                                   timeout=(4, 8))
            response.raise_for_status()
            body = response.json()
            items, expected = _validate_page(code, first, last, page, body, expected, seen)
            rows.extend(items)
            pages.append({"params": params, "response": body})
            if page >= expected[2]:
                if len(rows) != expected[0]:
                    raise ValueError("SZSE stock suspension record count is incomplete")
                return {"records": rows, "source_pages": pages}
    raise ValueError("SZSE stock suspension pagination exceeds the bounded request")


def _validate_cached_pages(data, code):
    if data.get("catalog_id") != CATALOG_ID or data.get("source_page") != SOURCE_PAGE:
        raise ValueError("SZSE stock suspension cache source is incompatible")
    pages = data.get("source_pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError("SZSE stock suspension cache has no complete source pages")
    rows, seen, expected = [], set(), None
    for index, saved in enumerate(pages, 1):
        if not isinstance(saved, dict) or saved.get("params") != _params(code, data["query_from"], data["query_through"], index):
            raise ValueError("SZSE stock suspension cached query does not match its coverage")
        items, expected = _validate_page(code, data["query_from"], data["query_through"], index,
                                         saved.get("response"), expected, seen)
        rows.extend(items)
    if len(pages) != max(1, expected[2]) or len(rows) != expected[0] or rows != data["records"]:
        raise ValueError("SZSE stock suspension cached records differ from their source pages")


def intervals_from_records(code, records, start, cutoff):
    """Return inclusive epoch intervals covering suspended dates, never resume day."""
    from .sessions import CN

    _request_window(code, start, cutoff)
    if not isinstance(records, list):
        raise ValueError("SZSE stock suspension records are invalid")
    events = defaultdict(list)
    uncertain = []
    for row in records:
        began, ended, began_open, ended_open = _record(code, row)
        event_day = began if began is not None else ended
        action = row["tpsj"].strip()
        if began is not None and ended is not None and began_open and ended_open and action == "1天":
            kind = "closed"
        elif began is not None and ended is None and began_open and action == "停牌":
            kind = "start"
        elif began is None and ended is not None and ended_open and action == "取消停牌":
            kind = "resume"
        else:
            kind = "unknown"
        if kind == "unknown":
            boundaries = sorted({day for day in (began, ended) if day is not None})
            uncertain.append((boundaries[0], boundaries[-1]))
            for boundary in boundaries:
                events[boundary].append(("unknown", None, None))
        else:
            events[event_day].append((kind, began, ended))
    uncertain.extend((day, day) for day, values in events.items() if len(values) != 1)
    output, pending = set(), None

    def add_interval(began, ended):
        if began is None or ended is None or ended <= began:
            return
        if any(low <= ended and high >= began for low, high in uncertain):
            return
        if any(began < day < ended for day in events):
            return
        # Midnight bounds encode the declared whole suspended dates, not an
        # invented intraday trading clock. The resumption date is excluded.
        left = int(datetime.combine(began, daytime.min, tzinfo=CN).timestamp())
        resume = int(datetime.combine(ended, daytime.min, tzinfo=CN).timestamp())
        right = resume - 1
        # A future/planned resumption does not prove a closed historical halt.
        resume_open = int(datetime.combine(ended, daytime(9, 30), tzinfo=CN).timestamp())
        if resume_open <= cutoff and left <= cutoff and right >= start:
            output.add((left, right))

    for day in sorted(events):
        values = events[day]
        # Several distinct same-day actions or an unknown intraday event make
        # pairing ambiguous. Do not join separate halt/resume rows across it.
        if len(values) != 1:
            pending = None
            continue
        kind, began, ended = values[0]
        if kind == "closed":
            pending = None
            add_interval(began, ended)
        elif kind == "start":
            pending = began
        elif kind == "resume":
            add_interval(pending, ended)
            pending = None
        else:
            pending = None
    return [list(pair) for pair in sorted(output)]


def suspension_evidence(code, start, cutoff, *, cache_root=None):
    from .sessions import SCHEMA, _digest, _read_cache

    first, last = _request_window(code, start, cutoff)
    query_from = (first - timedelta(days=LOOKBACK_DAYS)).isoformat()
    query_through = last.isoformat()
    if cache_root is None:
        from chanlun import config
        cache_root = config.get_data_path() / "screening"
    path = Path(cache_root) / ("szse_stock_suspensions_" + code + ".json")
    data = _read_cache(path, query_from, cutoff, source_url=SOURCE_URL)
    if data is not None and (data.get("symbol") != code or data.get("parser_version") != PARSER_VERSION
                             or data.get("query_through", "") < query_through):
        data = None
    intervals = None
    if data is not None:
        try:
            _validate_cached_pages(data, code)
            intervals = intervals_from_records(code, data["records"], start, cutoff)
        except (ValueError, TypeError, KeyError):
            data = None
    if data is None:
        fetched = _fetch(code, query_from, query_through)
        data = {"schema": SCHEMA, "source_url": SOURCE_URL, "source_page": SOURCE_PAGE,
                "catalog_id": CATALOG_ID, "parser_version": PARSER_VERSION, "symbol": code,
                "query_from": query_from, "query_through": query_through,
                "records": fetched["records"], "source_pages": fetched["source_pages"],
                "fetched_at": int(time.time())}
        _validate_cached_pages(data, code)
        intervals = intervals_from_records(code, data["records"], start, cutoff)
        temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({**data, "sha256": _digest(data)},
                                            ensure_ascii=False, allow_nan=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    evidence = {"schema": SCHEMA, "symbol": code, "source_url": SOURCE_URL,
                "source_page": SOURCE_PAGE, "catalog_id": CATALOG_ID, "parser_version": PARSER_VERSION,
                "query_from": data["query_from"], "query_through": data["query_through"],
                "checked_through": cutoff, "intervals": intervals, "records": data["records"],
                "source_records_revision": _digest(data)}
    return {**evidence, "revision": _digest(evidence)}
