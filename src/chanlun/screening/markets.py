"""Market identities, provider time labels and non-A-share session boundaries."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import hashlib
import re

import pandas as pd

from chanlun.market import Market
from chanlun.screening.rules import CN


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


@lru_cache(maxsize=16)
def _calendar(name, year):
    import exchange_calendars as xcals
    return xcals.get_calendar(name, start=f"{year-2}-01-01", end=f"{year+1}-12-31")


def market_context(market, code, observed_at, frequency, recent_sessions=5, max_anchor_sessions=20, *, completed_session=False):
    """Never project the Shanghai calendar onto another market.

    XNYS/XHKG calendars include DST, breaks, holidays and short sessions. Other
    instruments require an identified session calendar, rather than guessing
    that a weekday or every absent bar is a valid market session.
    """
    name = {"us": "XNYS", "hk": "XHKG", "currency": "24/7", "currency_spot": "24/7"}.get(market)
    if market == "ny_futures" and code.startswith(("CO.", "NY.")):
        name = "CMES"
    if name is None:
        raise ValueError(f"SESSION_CALENDAR_UNAVAILABLE: {market}:{code} 尚无已配置的标的交易时段，保留在诊断中")
    now = pd.Timestamp(observed_at)
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
    return {"market": market, "symbol": code, "frequency": frequency, "cutoff": cutoff,
            "recent_from": sessions[-recent_sessions], "anchor_from": sessions[-max_anchor_sessions],
            "history_from": sessions[-min(len(sessions), max(60, max_anchor_sessions+2))],
            "trading_days": sessions, "calendar_source": f"exchange_calendars:{name}",
            "calendar_coverage_start": sessions[0], "session_periods": periods,
            "session_timezone": str(calendar.tz), "session_calendar": name}


def external_frame(code, frequency, context):
    from chanlun.exchange import get_exchange
    from chanlun.exchange.kline_completion import drop_unclosed_last_bar, normalize_completed_bar_labels
    exchange = get_exchange(Market(context["market"]))
    end = datetime.fromtimestamp(context["cutoff"], CN)
    frame = exchange.klines(code, frequency, end_date=end.isoformat(), args={"timeout": 20})
    if frame is None or frame.empty:
        raise ValueError("行情源没有返回 K 线，请检查连接或数据权限")
    declared = frame.attrs.get("bar_time_label", getattr(exchange, "kline_time_label", None))
    if declared not in {"start", "end"}:
        raise ValueError("BAR_TIME_LABEL_UNAVAILABLE: 行情源未声明分钟 K 线起止时间口径")
    frame = drop_unclosed_last_bar(frame, frequency, time_label=declared, as_of=end)
    frame = normalize_completed_bar_labels(frame, frequency, time_label=declared).copy()
    frame.attrs.update(screening_market=context["market"], bar_time_label="end")
    if isinstance(frame.date.dtype, pd.DatetimeTZDtype):
        # Scan regular sessions only. Extended-hours bars cannot silently enter
        # a regular-session calendar, and missing regular bars remain errors.
        stamps = frame.date.astype("int64") // 1_000_000_000
        mask = pd.Series(False, index=frame.index)
        step = int(frequency[:-1]) * 60
        for start, finish in context["session_periods"]:
            mask |= (stamps > start) & (stamps <= finish) & ((stamps-start) % step == 0)
        frame = frame.loc[mask].copy()
    return frame
