"""Fresh official fund halt facts, read without blocking the monitor loop."""

from datetime import datetime, time as day_time
from pathlib import Path
from threading import Lock, Thread
import json
import os
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .sessions import _digest, SCHEMA
from .fund_suspensions import SOURCE_URL, _fetch, intervals_from_records

CN = ZoneInfo("Asia/Shanghai")
MAX_AGE = 90
_lock = Lock()
_inflight = set()
_retry_at = {}


def _root(root=None):
    if root is not None:
        return Path(root)
    from chanlun import config

    return config.get_data_path()


def _read(path, code, day, now):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        digest = data.pop("sha256")
        if (
            digest != _digest(data)
            or data.get("schema") != SCHEMA
            or data.get("symbol") != code
            or data.get("source_url") != SOURCE_URL
            or data.get("query_through") != day
            or type(data.get("fetched_at")) not in (int, float)
            or not 0 <= now - data["fetched_at"] <= MAX_AGE
            or not isinstance(data.get("records"), list)
        ):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _refresh(root, code, day, key, fetcher):
    try:
        records = fetcher(code, day, day)
        end = int(
            datetime.combine(
                datetime.fromisoformat(day).date(), day_time(23, 59, 59), CN
            ).timestamp()
        )
        intervals_from_records(code, records, 0, end)
        data = {
            "schema": SCHEMA,
            "source_url": SOURCE_URL,
            "symbol": code,
            "query_from": day,
            "query_through": day,
            "records": records,
            "fetched_at": int(time.time()),
        }
        folder = root / "market_status"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (code + "_" + day + ".json")
        temporary = folder / (uuid4().hex + ".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {**data, "sha256": _digest(data)},
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        pass  # Unknown status never excuses missing prices.
    finally:
        with _lock:
            _inflight.discard(key)
            _retry_at[key] = time.monotonic() + 30


def fund_halt(code, observed, *, cutoff=None, root=None, refresh=True):
    if not isinstance(code, str) or not code.startswith("SH.5") or len(code) != 9:
        return None
    observed = observed.astimezone(CN)
    now = observed.timestamp()
    day = observed.date().isoformat()
    root = _root(root)
    dedicated = root / "market_status" / (code + "_" + day + ".json")
    legacy = root / "screening" / ("fund_suspensions_" + code + ".json")
    data = _read(dedicated, code, day, now) or _read(legacy, code, day, now)
    if refresh and (data is None or now - data["fetched_at"] > 45):
        key = (str(root.resolve()), code, day)
        with _lock:
            if (
                key not in _inflight
                and len(_inflight) < 8
                and time.monotonic() >= _retry_at.get(key, 0)
            ):
                _inflight.add(key)
                Thread(
                    target=_refresh,
                    args=(root, code, day, key, _fetch),
                    name="FundSuspensionStatus",
                    daemon=True,
                ).start()
    if data is None:
        return None
    end = int(datetime.combine(observed.date(), day_time(23, 59, 59), CN).timestamp())
    try:
        intervals = intervals_from_records(code, data["records"], 0, end)
    except (ValueError, TypeError):
        return None
    for first, last in intervals:
        active = first <= now < last
        waiting = cutoff is not None and first <= cutoff <= last and now >= last
        if active or waiting:
            return {
                "code": code,
                "market": "a",
                "reason": "TRADING_SUSPENDED",
                "from": first,
                "until": last,
                "source_url": SOURCE_URL,
                "fetched_at": data["fetched_at"],
                "state": "suspended" if active else "awaiting_first_closed_bar",
                "resume_label": datetime.fromtimestamp(last, CN).strftime("%H:%M"),
            }
    return None
