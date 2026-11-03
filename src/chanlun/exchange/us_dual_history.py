"""Provider-complete US history windows; prices are never spliced across feeds."""

from pathlib import Path
import json
import os
import threading
import time
import hashlib
from uuid import uuid4

import pandas as pd
import numpy as np

from chanlun.exchange.quote_capacity import (
    worker_quote_cancelled,
    QuoteConnectionCancelled,
)
from chanlun.exchange.kline_completion import (
    drop_unclosed_last_bar,
    normalize_completed_bar_labels,
)
from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
from .errors import (
    SourcePreparationPending,
    USmartHistoryPending,
    USmartAPIError,
    LongbridgeConnectionLimitError,
)
from .longbridge_history_limit import LongbridgeHistoryBusy
from .usmart_rate_limit import USmartRateLimitTimeout, USmartRateLimitCancelled


class USDualSourcePending(SourcePreparationPending):
    retryable = False
    code = "us_dual_source_pending"
    retry_after = 3

    def __init__(self, reasons=None):
        self.reasons = dict(reasons or {})
        token = hashlib.sha256(
            json.dumps(self.reasons, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        super().__init__(
            {"progress_token": token, "retry_after": 3}, code="us_dual_source_pending"
        )

    def __reduce__(self):
        return type(self), (self.reasons,), self.__dict__


class DualUSHistory:
    kline_time_label = "end"
    supports_canonical_minute_history = True

    def __init__(
        self,
        native,
        longbridge_factory,
        *,
        primary="longbridge",
        root=None,
        now=time.time,
    ):
        if primary not in {"longbridge", "usmart"}:
            raise ValueError("Unknown primary US source")
        self.native, self.factory, self.primary = native, longbridge_factory, primary
        self.longbridge = None
        self.now = now
        self._root = None if root is None else Path(root)
        self._lock = threading.RLock()

    @property
    def root(self):
        from chanlun import config

        return self._root or config.get_data_path() / "quote_budget" / "us_dual"

    def support_frequencys(self):
        return self.native.support_frequencys()

    def _book(self, update=None):
        folder = self.root
        folder.mkdir(parents=True, exist_ok=True)
        with interprocess_file_lock(folder / "routes.lock", timeout_seconds=0.5):
            path = folder / "routes.json"
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("schema") != "us-dual-routes-v1":
                    saved = {}
            except (OSError, ValueError, AttributeError):
                saved = {}
            state = {
                "schema": "us-dual-routes-v1",
                "symbols": saved.get("symbols", {})
                if isinstance(saved.get("symbols", {}), dict)
                else {},
                "providers": saved.get("providers", {})
                if isinstance(saved.get("providers", {}), dict)
                else {},
            }
            if update is not None:
                update(state)
                temporary = folder / (uuid4().hex + ".tmp")
                try:
                    temporary.write_text(
                        json.dumps(state, ensure_ascii=False, allow_nan=False),
                        encoding="utf-8",
                    )
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return state

    def _selected(self, code):
        row = self._book()["symbols"].get(code, {})
        if not isinstance(row, dict):
            return self.primary
        selected = row.get("provider")
        until = row.get("until", 0)
        return (
            selected
            if selected in {"longbridge", "usmart"}
            and type(until) in (int, float)
            and until > self.now()
            else self.primary
        )

    def _record(
        self, provider, code, frequency, *, error=None, seconds=0, promote=True
    ):
        now = self.now()

        def update(state):
            state["providers"][provider] = {
                "last_checked": now,
                "last_code": code,
                "last_frequency": frequency,
                "status": "ready" if error is None else "pending",
                "error_type": error,
                "seconds": round(seconds, 4),
            }
            if error is None and promote:
                previous = state["symbols"].get(code, {})
                if not isinstance(previous, dict):
                    previous = {}
                switched = previous.get("provider") not in {None, provider}
                state["symbols"][code] = {
                    "provider": provider,
                    "until": now + 300,
                    "changed_at": now
                    if switched or not previous
                    else previous.get("changed_at", now),
                }
            if len(state["symbols"]) > 256:
                valid = {
                    k: v
                    for k, v in state["symbols"].items()
                    if isinstance(v, dict) and type(v.get("until")) in (int, float)
                }
                state["symbols"] = dict(
                    sorted(
                        valid.items(), key=lambda item: item[1]["until"], reverse=True
                    )[:256]
                )

        self._book(update)

    def _provider(self, name):
        if name == "usmart":
            return self.native
        with self._lock:
            if self.longbridge is None:
                self.longbridge = self.factory()
            return self.longbridge

    @staticmethod
    def _cutoff(value):
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        if value is None:
            return now
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("America/New_York")
        return min(now, stamp)

    def _read(self, name, code, frequency, start_date, end_date, args):
        source = self._provider(name)
        cutoff = self._cutoff(end_date)
        start = None if start_date is None else pd.Timestamp(start_date)
        if start is not None and start.tzinfo is None:
            start = start.tz_localize("America/New_York")
        minutes = (
            int(frequency[:-1])
            if frequency.endswith("m") and frequency[:-1].isdigit()
            else None
        )
        label = getattr(
            source, "kline_time_label", "end" if name == "usmart" else "start"
        )
        native_start = (
            start - pd.Timedelta(minutes=minutes)
            if start is not None and label == "start" and minutes
            else start
        )
        frame = source.klines(
            code,
            frequency,
            start_date=None if native_start is None else native_start.isoformat(),
            end_date=end_date,
            args=args,
        )
        if (
            frame is None
            or frame.empty
            or frame.attrs.get("fetch_incomplete")
            or frame.attrs.get("price_basis_provider") != name
        ):
            raise ValueError("Source did not provide a verified complete price basis")
        if minutes:
            frame = drop_unclosed_last_bar(
                frame, frequency, time_label=label, as_of=cutoff
            )
            frame = normalize_completed_bar_labels(frame, frequency, time_label=label)
        if (
            frame.empty
            or frame.date.isna().any()
            or not frame.date.is_unique
            or not frame.date.is_monotonic_increasing
        ):
            raise ValueError("Source history clock is invalid")
        frame = frame.loc[
            (frame.date <= cutoff) & (True if start is None else frame.date >= start)
        ].copy()
        if frame.empty:
            raise ValueError("Source window is empty")
        values = frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
        if (
            not np.isfinite(values).all()
            or (frame.volume < 0).any()
            or not frame.code.eq(code).all()
            or not frame.frequency.eq(frequency).all()
            or (frame.low > frame[["open", "close"]].min(axis=1)).any()
            or (frame.high < frame[["open", "close"]].max(axis=1)).any()
        ):
            raise ValueError("Source frame values or identity are invalid")
        if frequency in {"1m", "5m", "30m"}:
            from chanlun.screening.markets import market_context

            expected = market_context("us", code, cutoff.to_pydatetime(), frequency)[
                "cutoff"
            ]
            actual = int(frame.date.max().timestamp())
            if actual < expected and 0 <= self.now() - expected < minutes * 60:
                # The latest completed bar can still be arriving. Keep the
                # whole-window fallback, but do not classify a fresh missing
                # tail as a permanent provider failure. Historical omissions
                # and clocks ahead of the requested market cutoff stay errors.
                facts = {"provider": name, "code": code, "frequency": frequency,
                         "expected_cutoff": expected, "observed_cutoff": actual}
                token = hashlib.sha256(json.dumps(
                    facts, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()
                raise SourcePreparationPending(
                    {**facts, "progress_token": token, "retry_after": 3,
                     "reason": "completed_clock_not_reached"},
                    code="us_completed_clock_pending",
                )
            if actual != expected:
                raise ValueError("Source did not reach the completed market clock")
        frame.attrs["bar_time_label"] = "end" if minutes else label
        frame.attrs["_screening_us_route"] = {
            "provider": name,
            "configured_sources": ["longbridge", "usmart"],
        }
        return frame

    def klines(self, code, frequency, start_date=None, end_date=None, args=None):
        query = dict(args or {})
        pinned = query.pop("_history_provider", None)
        if pinned is not None and pinned not in {"longbridge", "usmart"}:
            raise ValueError("Unknown pinned US history provider")
        if worker_quote_cancelled():
            raise QuoteConnectionCancelled("US source request cancelled")
        try:
            selected = pinned or self._selected(code)
        except InterprocessLockTimeout as exc:
            self._wait_for_route_book(exc)
        order = (
            [selected]
            if pinned
            else [selected, "usmart" if selected == "longbridge" else "longbridge"]
        )
        reasons = {}
        transient = False
        for name in order:
            if worker_quote_cancelled():
                raise QuoteConnectionCancelled("US source request cancelled")
            began = time.monotonic()
            try:
                frame = self._read(name, code, frequency, start_date, end_date, query)
            except Exception as exc:
                if worker_quote_cancelled():
                    raise QuoteConnectionCancelled(
                        "US source request cancelled"
                    ) from exc
                preparation = getattr(exc, "preparation", {})
                reasons[name] = {
                    "code": getattr(exc, "code", None) or type(exc).__name__,
                    "progress_token": preparation.get("progress_token")
                    if isinstance(preparation, dict)
                    else None,
                }
                transient |= isinstance(
                    exc,
                    (
                        TimeoutError,
                        USmartHistoryPending,
                        QuoteConnectionCancelled,
                        LongbridgeHistoryBusy,
                        LongbridgeConnectionLimitError,
                        USmartRateLimitTimeout,
                        USmartRateLimitCancelled,
                    ),
                )
                try:
                    self._record(
                        name, code, frequency, error=type(exc).__name__,
                        seconds=time.monotonic() - began,
                    )
                except InterprocessLockTimeout as busy:
                    # Shared routing metadata contention is a capacity wait,
                    # not a new provider failure or permission to fetch more.
                    self._wait_for_route_book(busy)
            else:
                try:
                    self._record(
                        name, code, frequency, seconds=time.monotonic() - began,
                        promote=pinned is None,
                    )
                except InterprocessLockTimeout:
                    # The book is a preference/health hint. Actual window and
                    # peer identity are bound by the validated frame metadata.
                    # Do not discard a successful window or fetch another one.
                    pass
                if worker_quote_cancelled():
                    raise QuoteConnectionCancelled("US source request cancelled")
                return frame
        if transient:
            raise USDualSourcePending(reasons)
        raise USmartAPIError(
            "两条美股数据链路均未提供可验证窗口，请查看来源状态",
            code="us_dual_sources_unavailable",
        )

    @staticmethod
    def _wait_for_route_book(error):
        if worker_quote_cancelled():
            raise QuoteConnectionCancelled("US source request cancelled") from error
        raise USDualSourcePending({"routing": {"code": "route_book_busy", "progress_token": None}}) from error
