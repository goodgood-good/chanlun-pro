"""Verify native completed tails once, then reuse only identical checked input."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import lru_cache, wraps
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pandas as pd

from .cache import input_fingerprint
from .rules import CN, frame_quality, validate_frame


_root = ContextVar("completed_input_root", default=None)
_verify = ContextVar("verify_completed_input", default=True)
SCHEMA = "qmt-verified-completed-input-v1"


def _now():
    return time.time()


@contextmanager
def input_verification(enabled):
    """Explicit offline replay tools can analyze already frozen inputs."""
    token = _verify.set(enabled)
    try:
        yield
    finally:
        _verify.reset(token)


def completed_input_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        cache_root = kwargs.get("cache_root")
        token = _root.set(Path(cache_root) / "completed_inputs" if cache_root else None)
        try:
            return function(*args, **kwargs)
        finally:
            _root.reset(token)

    return wrapped


@lru_cache(maxsize=1)
def _contract():
    exchange = Path(__file__).resolve().parents[1] / "exchange"
    digest = hashlib.sha256()
    for path in [
        Path(__file__),
        *(
            exchange / name
            for name in (
                "exchange_qmt.py",
                "qmt_aggregate.py",
                "qmt_time_contract.py",
                "kline_precision.py",
                "price_basis.py",
            )
        ),
    ]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _receipt_path(code, frequency):
    from .markets import storage_symbol

    root = _root.get()
    return root / f"{storage_symbol(code)}_{frequency}.json" if root else None


def _key(frame, code, frequency, cutoff):
    day = datetime.fromtimestamp(cutoff, CN).date()
    window = frame.loc[frame.date.dt.tz_convert(CN).dt.date == day]
    return input_fingerprint(window, code, frequency)


def prepared_content_key(frame, code, frequency, cutoff):
    """Compare producer and consumer values independently of history length."""
    day = datetime.fromtimestamp(cutoff, CN).date()
    window = frame.loc[frame.date.dt.tz_convert(CN).dt.date == day].copy(deep=False)
    window.attrs = {
        key: frame.attrs.get(key)
        for key in (
            "price_basis_provider",
            "price_basis_revision",
            "structure_price_quantum",
        )
    }
    return input_fingerprint(window, code, frequency)


def _verified(path, key, cutoff):
    if path is None:
        return False
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        raw = wrapper["payload"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != wrapper["sha256"]:
            return False
        proof = json.loads(raw)
        return (
            proof["schema"] == SCHEMA
            and proof["contract"] == _contract()
            and proof["key"] == key
            and proof["cutoff"] == cutoff
            and cutoff <= proof["checked_at"] <= _now()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def preparation_needed(cache_root, code, frequency, cutoff):
    """Only a prefetch hint; the worker still verifies the actual input hash."""
    from .markets import storage_symbol

    path = (
        Path(cache_root)
        / "completed_inputs"
        / f"{storage_symbol(code)}_{frequency}.json"
    )
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        raw = wrapper["payload"].encode("utf-8")
        proof = json.loads(raw)
        return not (
            hashlib.sha256(raw).hexdigest() == wrapper["sha256"]
            and proof["schema"] == SCHEMA
            and proof["contract"] == _contract()
            and proof["cutoff"] == cutoff
            and cutoff <= proof["checked_at"] <= _now()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return True


def _remember(path, frame, code, frequency, cutoff):
    if path is None or _now() < cutoff:
        return
    raw = json.dumps(
        {
            "schema": SCHEMA,
            "contract": _contract(),
            "key": _key(frame, code, frequency, cutoff),
            "cutoff": cutoff,
            "checked_at": _now(),
        },
        sort_keys=True,
    )
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                {"payload": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _eligible(frame, frequency, context):
    return (
        _verify.get()
        and frequency in {"1m", "5m", "30m"}
        and context.get("market", "a") == "a"
        and frame is not None
        and not frame.empty
        and frame.attrs.get("price_basis_provider") == "qmt"
    )


def record_download(frame, code, frequency, context):
    """Reuse a history request already made by gap/stale-input repair."""
    if (
        _eligible(frame, frequency, context)
        and not frame_quality(frame, context)["errors"]
    ):
        _remember(
            _receipt_path(code, frequency), frame, code, frequency, context["cutoff"]
        )
    return frame


def verify_completed_input(exchange, frame, code, frequency, context, *, start, end):
    """Refresh only the cutoff day and retain the complete earlier history.

    A close label alone cannot prove a cached candle was finalized. Neither
    zero nor positive volume authorizes an unchecked cached tail. Receipts
    bind the exact day data, price basis and source implementation.
    """
    if (
        not _eligible(frame, frequency, context)
        or frame_quality(frame, context)["errors"]
    ):
        return frame, False
    cutoff = context["cutoff"]
    path = _receipt_path(code, frequency)
    if _verified(path, _key(frame, code, frequency, cutoff), cutoff):
        return frame, False
    prepared = context.get("_screening_tail_prepared") or {}
    if (
        prepared.get("schema") == SCHEMA
        and prepared.get("contract") == _contract()
        and prepared.get("code") == code
        and prepared.get("frequency") == frequency
        and prepared.get("cutoff") == cutoff
        and cutoff <= prepared.get("checked_at", 0) <= _now()
        and prepared.get("input_key")
        == prepared_content_key(frame, code, frequency, cutoff)
        and not any(
            datetime.fromtimestamp(bar["closed_at"], CN).date()
            == datetime.fromtimestamp(cutoff, CN).date()
            for bar in frame.attrs.get("history_repair", {}).get("bars", [])
        )
    ):
        _remember(path, frame, code, frequency, cutoff)
        return frame, False
    closed = datetime.fromtimestamp(cutoff, CN)
    failed = frame.copy()
    failed.attrs["_screening_closing_refresh_error"] = True
    if _now() < cutoff:
        return failed, True
    day = closed.date()
    try:
        native = getattr(type(exchange), "download_completed_window", None)
        if callable(native):
            if (
                exchange.download_completed_window(
                    [code],
                    "1m" if frequency == "1m" else "5m",
                    closed.strftime("%Y%m%d000000"),
                    closed.strftime("%Y%m%d%H%M%S"),
                )
                is not True
            ):
                return failed, True
        tail = exchange.klines(
            code,
            frequency,
            start_date=closed.strftime("%Y%m%d"),
            end_date=end,
            args={
                "exact_end": True,
                "local_only": True,
                **(
                    {"skip_download": True}
                    if callable(native)
                    else {"download_start_date": closed.strftime("%Y%m%d")}
                ),
            },
        )
        if tail is None or tail.empty or set(tail.columns) != set(frame.columns):
            return failed, True
        if "code" in tail and set(tail.code) != {code}:
            return failed, True
        if any(
            reason != "INSUFFICIENT_HISTORY" for reason in validate_frame(tail, context)
        ):
            return failed, True
        if not (tail.date.dt.tz_convert(CN).dt.date == day).all():
            return failed, True
        same_basis = all(
            tail.attrs.get(key) == frame.attrs.get(key)
            for key in ("price_basis_revision", "structure_price_quantum")
        )
        if not same_basis:
            merged = exchange.klines(
                code,
                frequency,
                start_date=start,
                end_date=end,
                args={"exact_end": True, "skip_download": True, "local_only": True},
            )
        else:
            prefix = frame.loc[frame.date.dt.tz_convert(CN).dt.date < day]
            tail = tail.copy()
            tail["date"] = tail.date.astype(frame.date.dtype)
            merged = pd.concat([prefix, tail], ignore_index=True)
            merged.attrs = dict(frame.attrs)
        if merged is None or frame_quality(merged, context)["errors"]:
            return failed, True
        merged.attrs["_screening_closing_refresh"] = {
            "source": "native_qmt",
            "closed_at": cutoff,
            "download_start": closed.strftime("%Y%m%d"),
            "before_close": float(frame.close.iloc[-1]),
            "before_volume": float(frame.volume.iloc[-1]),
            "after_close": float(merged.close.iloc[-1]),
            "after_volume": float(merged.volume.iloc[-1]),
        }
        _remember(path, merged, code, frequency, cutoff)
        return merged, True
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        return failed, True
