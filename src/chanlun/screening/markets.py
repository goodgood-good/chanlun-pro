"""Market identities, provider time labels and non-A-share session boundaries."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import hashlib
import re

import pandas as pd
import numpy as np

from chanlun.market import Market
from chanlun.screening.rules import CN, FREQUENCIES, trading_context
from chanlun.screening.errors import MarketDataContractError


def validate_symbol(market, code):
    return (market in {m.value for m in Market} and isinstance(code, str)
            and 0 < len(code) <= 80 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", code) is not None
            and ".." not in code)


def storage_symbol(code, market="a"):
    if not validate_symbol(market, code):
        raise ValueError("标的代码或市场不合法")
    if market == "a" and re.fullmatch(r"(?:SH|SZ|BJ)\.\d{6}", code):
        return code
    return market + "_" + hashlib.sha256(code.encode()).hexdigest()[:32]


def watchlist_symbols():
    from chanlun.zixuan import ZiXuan
    watch = ZiXuan("a")
    found = {}
    for group in watch.zixuan_list:
        for row in watch.zx_stocks(group["name"]):
            market, code = row["market"], row["code"]
            if not validate_symbol(market, code):
                raise ValueError(f"关注组标的无效：{market}:{code}")
            found[(market, code)] = {"market": market, "code": code, "name": row["name"], "origin": "watchlist"}
    return list(found.values())


def holding_symbols():
    """Explicit manual holdings, kept separate from the all-group watchlist."""
    from chanlun.zixuan import MANUAL_HOLDING_ZX_GROUP, ZiXuan
    rows = ZiXuan("a").zx_stocks(MANUAL_HOLDING_ZX_GROUP)
    if any(not validate_symbol(row["market"], row["code"]) for row in rows):
        raise ValueError("持仓分组包含无效标的")
    return [{"market": row["market"], "code": row["code"], "name": row["name"], "origin": "watchlist"}
            for row in rows]


@lru_cache(maxsize=16)
def _calendar(name, year):
    import exchange_calendars as xcals
    return xcals.get_calendar(name, start=f"{year-2}-01-01", end=f"{year+1}-12-31")


def _calendar_name(market, code):
    name = {"us": "XNYS", "hk": "XHKG", "currency": "24/7", "currency_spot": "24/7"}.get(market)
    if market == "ny_futures" and code.startswith(("CO.", "NY.")):
        name = "CMES"
    if name is None:
        raise MarketDataContractError("SESSION_CALENDAR_UNAVAILABLE", f"{market}:{code} 尚无已配置的标的交易时段，保留在诊断中")
    return name


def _observation_minute(observed_at, frequency):
    if frequency not in FREQUENCIES:
        raise ValueError("unsupported screening frequency")
    if observed_at.tzinfo is None:
        raise ValueError("screening observation time must be timezone-aware")
    return int(observed_at.timestamp()) // 60 * 60


def market_context(market, code, observed_at, frequency, recent_sessions=5, max_anchor_sessions=20, *, completed_session=False):
    """Reuse a calendar calculation, returning independent symbol metadata.

    XNYS/XHKG calendars include DST, breaks, holidays and short sessions. The
    supported bars and session boundaries align to whole minutes, so seconds
    within one observation minute cannot change a completed-bar cutoff.
    """
    minute = _observation_minute(observed_at, frequency)
    if (type(recent_sessions) is not int or recent_sessions < 1
            or type(max_anchor_sessions) is not int or max_anchor_sessions < 1):
        raise ValueError("session windows must be positive integers")
    template = _market_context(_calendar_name(market, code), minute, frequency,
                               recent_sessions, max_anchor_sessions, completed_session)
    # Never expose cached lists to a symbol worker or evidence reader.
    return {**template, "market": market, "symbol": code,
            "trading_days": list(template["trading_days"]),
            "session_periods": list(template["session_periods"])}


@lru_cache(maxsize=256)
def _market_context(name, minute, frequency, recent_sessions, max_anchor_sessions, completed_session):
    now = pd.Timestamp(minute, unit="s", tz=CN)
    calendar = _calendar(name, now.year)
    schedule = calendar.schedule.loc[(now - pd.Timedelta(days=730)).date():now.date()]
    if completed_session and name != "24/7":
        closed_sessions = schedule.loc[schedule["close"] <= now]
        if closed_sessions.empty:
            raise ValueError("SESSION_CALENDAR_UNAVAILABLE: 尚无完整收盘的交易日")
        now = closed_sessions["close"].iloc[-1]
        schedule = closed_sessions
    periods, sessions = [], []
    step = int(frequency[:-1]) * 60
    for session, row in schedule.iterrows():
        opening, closing = row["open"], row["close"]
        if opening >= now:
            continue
        label = session.date().isoformat()
        if pd.notna(row["break_start"]):
            ranges = [(opening, row["break_start"]), (row["break_end"], closing)]
        else:
            ranges = [(opening, closing)]
        for start, end in ranges:
            start, end = int(start.timestamp()), min(int(end.timestamp()), int(now.timestamp()))
            if end >= start + step:
                periods.append((start, end - (end - start) % step))
                if not sessions or sessions[-1] != label:
                    sessions.append(label)
    if not periods or len(sessions) < max(recent_sessions, max_anchor_sessions):
        raise ValueError("SESSION_CALENDAR_UNAVAILABLE: 交易日历不足")
    cutoff = periods[-1][1]
    return {"frequency": frequency, "cutoff": cutoff,
            "recent_from": sessions[-recent_sessions], "anchor_from": sessions[-max_anchor_sessions],
            "history_from": sessions[-min(len(sessions), max(60, max_anchor_sessions+2))],
            "trading_days": tuple(sessions), "calendar_source": f"exchange_calendars:{name}",
            "calendar_coverage_start": sessions[0], "session_periods": tuple(periods),
            "session_timezone": str(calendar.tz), "session_calendar": name}


@lru_cache(maxsize=128)
def _a_cutoff(minute, frequency):
    return trading_context(datetime.fromtimestamp(minute, CN), frequency, 1, 1)["cutoff"]


def closed_bar_cutoff(market, code, observed_at, frequency):
    """Return only the verified clock, without copying a history context."""
    minute = _observation_minute(observed_at, frequency)
    if market == "a":
        return _a_cutoff(minute, frequency)
    return _market_context(_calendar_name(market, code), minute, frequency, 1, 1, False)["cutoff"]


def _session_mask(stamps, periods, step):
    """Find each bar's open-left, closed-right session without rescanning it."""
    ranges = np.asarray(periods)
    if (ranges.ndim == 2 and len(ranges) and ranges.shape[1] == 2 and ranges.dtype.kind == 'i'
            and np.all(ranges[:, 1] >= ranges[:, 0])
            and np.all(ranges[1:, 0] > ranges[:-1, 0])
            and np.all(ranges[1:, 0] >= ranges[:-1, 1])):
        values = stamps.to_numpy()
        # side='left' assigns a shared close/open boundary to the prior
        # session, preserving its final completed candle (including 24/7).
        indices = np.searchsorted(ranges[:, 0], values, side='left') - 1
        chosen = ranges[np.maximum(indices, 0)]
        return pd.Series((indices >= 0) & (values <= chosen[:, 1])
                         & ((values - chosen[:, 0]) % step == 0), index=stamps.index)
    # Preserve the existing union semantics for overlapping, unordered or
    # nonintegral ranges; optimization never silently changes the clock.
    mask = pd.Series(False, index=stamps.index)
    for start, finish in periods:
        mask |= (stamps > start) & (stamps <= finish) & ((stamps-start) % step == 0)
    return mask


def external_frame(code, frequency, context):
    from chanlun.exchange import get_exchange
    from chanlun.exchange.kline_completion import drop_unclosed_last_bar, normalize_completed_bar_labels
    exchange = get_exchange(Market(context["market"]))
    if getattr(exchange, "screening_time_contract_verified", None) is False:
        raise MarketDataContractError("BAR_TIME_LABEL_UNAVAILABLE", "行情源尚未核实分钟K线时间口径，暂不请求结构计算行情")
    end = datetime.fromtimestamp(context["cutoff"], CN)
    frame = exchange.klines(code, frequency, end_date=end.isoformat(), args={"timeout": 20})
    if frame is None or frame.empty:
        raise ValueError("行情源没有返回 K 线，请检查连接或数据权限")
    declared = frame.attrs.get("bar_time_label", getattr(exchange, "kline_time_label", None))
    if declared not in {"start", "end"}:
        raise MarketDataContractError("BAR_TIME_LABEL_UNAVAILABLE", "行情源未声明分钟 K 线起止时间口径")
    frame = drop_unclosed_last_bar(frame, frequency, time_label=declared, as_of=end)
    frame = normalize_completed_bar_labels(frame, frequency, time_label=declared).copy()
    frame.attrs.update(screening_market=context["market"], bar_time_label="end")
    if isinstance(frame.date.dtype, pd.DatetimeTZDtype):
        # Scan regular sessions only. Extended-hours bars cannot silently enter
        # a regular-session calendar, and missing regular bars remain errors.
        stamps = frame.date.astype("int64") // 1_000_000_000
        step = int(frequency[:-1]) * 60
        mask = _session_mask(stamps, context['session_periods'], step)
        frame = frame.loc[mask].copy()
    return frame
