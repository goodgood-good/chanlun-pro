"""SSE disclosed fund trading halts, distinct from subscription/redemption stops.

Source field mapping: SSE's search_tradeTip_2021.js, suspension_fund,
stopTimeConversion: WH=whole day, 9=morning, 9h=09:30-10:30,
13=afternoon, 915=09:15-10:30. Only dated temporary trading halts
(dateSource 2/5, LSTP) are used; unknown records never excuse missing bars.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import time

import requests

SOURCE_URL = "https://query.sse.com.cn/sseQuery/commonSoaQuery.do"
SOURCE_PAGE = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/fund/"
FIELD_MAPPING = (
    "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_tradeTip_2021.js"
)


def _fetch(code, first, last):
    rows = []
    expected = None
    with requests.Session() as session:
        session.trust_env = False
        for page in range(1, 21):
            response = session.get(
                SOURCE_URL,
                params={
                    "isPagination": "true",
                    "sqlId": "SSE_PL_JYTS_TFPXX_JJ",
                    "secCode": code[3:],
                    "stopReason": "",
                    "order": "startStopDate|desc,secCode|asc",
                    "startDate": first.replace("-", ""),
                    "endDate": last.replace("-", ""),
                    "pageHelp.pageSize": 100,
                    "pageHelp.pageNo": page,
                    "pageHelp.beginPage": page,
                    "pageHelp.cacheSize": 1,
                    "pageHelp.endPage": page,
                },
                headers={"Referer": SOURCE_PAGE, "User-Agent": "Mozilla/5.0"},
                timeout=(4, 8),
            )
            response.raise_for_status()
            body = response.json()
            meta = body.get("pageHelp", {})
            total = meta.get("total")
            items = body.get("result")
            if (
                body.get("sqlId") != "SSE_PL_JYTS_TFPXX_JJ"
                or type(total) is not int
                or total < 0
                or meta.get("pageNo") != page
                or not isinstance(items, list)
                or expected is not None
                and total != expected
            ):
                raise ValueError("SSE fund suspension response is incomplete")
            expected = total
            rows.extend(items)
            if len(rows) == total:
                return rows
            if not items or len(rows) > total:
                break
    raise ValueError("SSE fund suspension pagination is incomplete")


def intervals_from_records(code, records, start, cutoff):
    from .sessions import CN

    slots = {
        "WH": ("09:15", "15:00"),
        "9": ("09:15", "11:30"),
        "9h": ("09:30", "10:30"),
        "915": ("09:15", "10:30"),
        "13": ("13:00", "15:00"),
    }
    intervals = set()
    for row in records:
        if not isinstance(row, dict) or row.get("secCode") != code[3:]:
            raise ValueError("SSE fund suspension identity mismatch")
        if (
            row.get("dateSource") not in ("2", "5")
            or row.get("startStopType") != "LSTP"
        ):
            continue
        day = row.get("startStopDate")
        slot = slots.get(row.get("stopTime"))
        if not slot or day != row.get("endStopDate"):
            continue
        if not isinstance(day, str) or len(day) != 8 or not day.isdigit():
            raise ValueError("SSE fund suspension date is invalid")
        date = datetime.strptime(day, "%Y%m%d").date().isoformat()
        began, ended = [
            int(
                datetime.fromisoformat(date + "T" + clock)
                .replace(tzinfo=CN)
                .timestamp()
            )
            for clock in slot
        ]
        if began <= cutoff and ended >= start:
            intervals.add((began, min(ended, cutoff)))
    return [list(pair) for pair in sorted(intervals)]


def suspension_evidence(code, start, cutoff, *, cache_root=None):
    from .sessions import CN, SCHEMA, _digest, _read_cache

    if cache_root is None:
        from chanlun import config

        cache_root = config.get_data_path() / "screening"
    path = Path(cache_root) / ("fund_suspensions_" + code + ".json")
    first = datetime.fromtimestamp(start, CN).date().isoformat()
    last = datetime.fromtimestamp(cutoff, CN).date().isoformat()
    data = _read_cache(path, first, cutoff, source_url=SOURCE_URL)
    if data is not None and (
        data.get("symbol") != code or data.get("query_through", "") < last
    ):
        data = None
    intervals = None
    if data is not None:
        try:
            intervals = intervals_from_records(code, data['records'], start, cutoff)
        except (ValueError, TypeError):
            # Old cache writers persisted records before checking their
            # identity/date contract. Refresh such a cache without using it.
            data = None
    if data is None:
        data = {
            "schema": SCHEMA,
            "source_url": SOURCE_URL,
            "symbol": code,
            "query_from": first,
            "query_through": last,
            "records": _fetch(code, first, last),
            "fetched_at": int(time.time()),
        }
        # Validate before publication: an invalid response must not poison
        # later checks for the whole cache lifetime.
        intervals = intervals_from_records(code, data['records'], start, cutoff)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({**data, "sha256": _digest(data)}, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            pass  # Verified fetched evidence is independent of optional storage.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    evidence = {
        "schema": SCHEMA,
        "symbol": code,
        "source_url": SOURCE_URL,
        "source_page": SOURCE_PAGE,
        "field_mapping_source": FIELD_MAPPING,
        "checked_through": cutoff,
        "intervals": intervals,
    }
    return {**evidence, "revision": _digest(evidence)}
