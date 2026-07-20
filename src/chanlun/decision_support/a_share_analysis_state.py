"""Pure, read-only A-share Chanlun analysis state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.core.cl import CL
from chanlun.recursive_bt.engine.engine import (
    CL_CFG,
    collect_branch_signals,
    latest_prev_day_close,
)


_CN = ZoneInfo("Asia/Shanghai")
_SESSIONS = (((9, 30), (11, 30)), ((13, 0), (15, 0)))


def _market_timestamp(value: object, field_name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a datetime") from exc
    if pd.isna(timestamp):
        raise ValueError(f"{field_name} must be a datetime")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(_CN)
    else:
        timestamp = timestamp.tz_convert(_CN)
    return timestamp


def _normalized_cutoff(value: object) -> pd.Timestamp:
    timestamp = _market_timestamp(value, "bar_closed_at")
    original = pd.Timestamp(value)
    if original.tzinfo is None:
        raise ValueError("bar_closed_at must be timezone-aware")
    return timestamp


def _physical_closed_at(
    label_at: pd.Timestamp,
    *,
    minutes: int,
    time_label: str,
) -> pd.Timestamp | None:
    duration = pd.Timedelta(minutes=minutes)
    started_at = label_at if time_label == "start" else label_at - duration
    closed_at = started_at + duration
    midnight = label_at.normalize()
    step_seconds = minutes * 60
    for (open_hour, open_minute), (close_hour, close_minute) in _SESSIONS:
        session_open = midnight + pd.Timedelta(
            hours=open_hour,
            minutes=open_minute,
        )
        session_close = midnight + pd.Timedelta(
            hours=close_hour,
            minutes=close_minute,
        )
        elapsed = (started_at - session_open).total_seconds()
        if (
            started_at >= session_open
            and closed_at <= session_close
            and elapsed >= 0
            and elapsed % step_seconds == 0
        ):
            return closed_at
    return None


def _closed_physical_bars(
    frame: object,
    *,
    frequency: str,
    time_label: str,
    cutoff: pd.Timestamp,
) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("quote klines must return a pandas DataFrame")
    if "date" not in frame.columns:
        raise ValueError("quote klines must contain date")
    minutes = {"5m": 5, "30m": 30}.get(frequency)
    if minutes is None:
        raise ValueError("physical minute frequency must be 5m or 30m")
    if time_label not in {"start", "end"}:
        raise ValueError("kline_time_label must be start or end")
    labels = [
        _market_timestamp(value, "kline.date") for value in frame["date"]
    ]
    if any(right <= left for left, right in zip(labels, labels[1:])):
        raise ValueError("quote klines must be strictly chronological")
    keep = []
    for position, label_at in enumerate(labels):
        closed_at = _physical_closed_at(
            label_at,
            minutes=minutes,
            time_label=time_label,
        )
        if closed_at is not None and closed_at <= cutoff:
            keep.append(position)
    result = frame.iloc[keep].copy()
    if keep:
        result.loc[:, "date"] = [labels[position] for position in keep]
    return result.reset_index(drop=True)


def _closed_daily_bars(frame: object, *, cutoff: pd.Timestamp) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("quote klines must return a pandas DataFrame")
    if "date" not in frame.columns:
        raise ValueError("quote klines must contain date")
    labels = [
        _market_timestamp(value, "daily kline.date") for value in frame["date"]
    ]
    if any(right <= left for left, right in zip(labels, labels[1:])):
        raise ValueError("daily quote klines must be strictly chronological")
    keep = [
        position
        for position, label_at in enumerate(labels)
        if label_at.date() < cutoff.date()
    ]
    result = frame.iloc[keep].copy()
    if keep:
        result.loc[:, "date"] = [labels[position] for position in keep]
    return result.reset_index(drop=True)


class AResearchQuoteSource(Protocol):
    """Narrow quote capability required by one research symbol state."""

    kline_time_label: str

    def klines(self, code: str, frequency: str, **kwargs): ...

    def stock_info(self, code: str): ...


@dataclass(slots=True)
class AResearchSymbolState:
    """Own only quote-backed analysis state for one symbol."""

    code: str
    _quote: AResearchQuoteSource = field(repr=False)
    kline_time_label: str = field(init=False)
    op_level: str = field(default="5m", init=False)
    big_level: str = field(default="30m", init=False)
    mid_level: str = field(default="", init=False)
    cd_op: object = field(init=False)
    cd_mid: None = field(default=None, init=False)
    cd_big: object = field(init=False)
    cdd: object = field(init=False)
    last_op: pd.Timestamp | None = field(default=None, init=False)
    last5: pd.Timestamp | None = field(default=None, init=False)
    last30: pd.Timestamp | None = field(default=None, init=False)
    lastd: pd.Timestamp | None = field(default=None, init=False)
    last_open: float = field(default=0.0, init=False)
    last_px: float = field(default=0.0, init=False)
    prev_close: float = field(default=0.0, init=False)
    d3_until: pd.Timestamp | None = field(default=None, init=False)
    seen: set[tuple[object, int, str, object]] = field(
        default_factory=set,
        init=False,
    )
    signal_freshness: pd.Timedelta = field(
        default=pd.Timedelta(minutes=200),
        init=False,
    )

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or not self.code
            or self.code != self.code.strip()
        ):
            raise ValueError("code must be a non-empty stripped string")
        label = self._quote.kline_time_label
        if label not in {"start", "end"}:
            raise ValueError("kline_time_label must be start or end")
        self.kline_time_label = label
        self.cd_op = CL(self.code, "5m", dict(CL_CFG))
        self.cd_big = CL(self.code, "30m", dict(CL_CFG))
        self.cdd = CL(self.code, "d", dict(CL_CFG))

    @staticmethod
    def _feed_unseen(
        cd: object,
        frame: pd.DataFrame,
        last: pd.Timestamp | None,
    ) -> pd.Timestamp | None:
        if frame.empty:
            return last
        new = frame if last is None else frame.loc[frame["date"] > last]
        if new.empty:
            return last
        processor = getattr(cd, "process_klines", None)
        if not callable(processor):
            raise TypeError("CL state must expose process_klines")
        processor(new.reset_index(drop=True))
        return _market_timestamp(frame["date"].iloc[-1], "kline.date")

    def _refresh_daily(self, frame: pd.DataFrame) -> None:
        before = self.lastd
        self.lastd = self._feed_unseen(self.cdd, frame, self.lastd)
        if self.lastd == before:
            return
        for signal in collect_branch_signals(self.cdd, use_xd=False):
            if getattr(signal, "bs_type", None) != "3buy":
                continue
            until = _market_timestamp(signal.date, "daily signal.date") + pd.Timedelta(
                days=11
            )
            if self.d3_until is None or until > self.d3_until:
                self.d3_until = until

    def _update_previous_close(
        self,
        frame: pd.DataFrame,
        *,
        cutoff: pd.Timestamp,
    ) -> None:
        candidate = 0.0
        if not frame.empty:
            candidate = float(latest_prev_day_close(frame))
            prior_positions = [
                position
                for position, value in enumerate(frame["date"])
                if _market_timestamp(value, "kline.date").date() < cutoff.date()
            ]
            if prior_positions:
                candidate = float(frame["close"].iloc[prior_positions[-1]])
        self.prev_close = (
            candidate
            if math.isfinite(candidate) and candidate > 0
            else 0.0
        )

    def refresh_at(self, bar_closed_at: datetime) -> list[object]:
        """Fetch and feed only physical bars closed at the supplied cutoff."""

        cutoff = _normalized_cutoff(bar_closed_at)
        frame5 = _closed_physical_bars(
            self._quote.klines(self.code, "5m"),
            frequency="5m",
            time_label=self.kline_time_label,
            cutoff=cutoff,
        )
        frame30 = _closed_physical_bars(
            self._quote.klines(self.code, "30m"),
            frequency="30m",
            time_label=self.kline_time_label,
            cutoff=cutoff,
        )
        framed = _closed_daily_bars(
            self._quote.klines(self.code, "d"),
            cutoff=cutoff,
        )
        self.last5 = self._feed_unseen(self.cd_op, frame5, self.last5)
        self.last_op = self.last5
        self.last30 = self._feed_unseen(self.cd_big, frame30, self.last30)
        self._refresh_daily(framed)
        if not frame5.empty:
            self.last_open = float(frame5["open"].iloc[-1])
            self.last_px = float(frame5["close"].iloc[-1])
        self._update_previous_close(frame5, cutoff=cutoff)
        observed: list[object] = []
        latest_closed = (
            None
            if self.last5 is None
            else _physical_closed_at(
                self.last5,
                minutes=5,
                time_label=self.kline_time_label,
            )
        )
        for signal in collect_branch_signals(
            self.cd_op,
            use_xd=False,
            annotate_nest=True,
        ):
            key = (
                getattr(signal, "date", None),
                int(getattr(signal, "level", 0) or 0),
                str(getattr(signal, "bs_type", "")),
                getattr(signal, "price", None),
            )
            if key in self.seen:
                continue
            self.seen.add(key)
            if latest_closed is None:
                continue
            signal_at = _market_timestamp(signal.date, "signal.date")
            age = latest_closed - signal_at
            if pd.Timedelta(0) <= age <= self.signal_freshness:
                observed.append(signal)
        return observed

    def big_dir(self) -> str:
        bis_getter = getattr(self.cd_big, "get_bis", None)
        if not callable(bis_getter):
            raise TypeError("30m CL must expose get_bis")
        bis = tuple(bis_getter())
        if not bis:
            return "neutral"
        return "up" if getattr(bis[-1], "type", None) == "up" else "down"

    def mid_dir(self) -> str:
        return ""

    def in_d3(self) -> bool:
        if self.d3_until is None or self.last5 is None:
            return False
        return self.last5 <= self.d3_until
