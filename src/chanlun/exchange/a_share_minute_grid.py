"""A 股连续竞价一分钟 K 线的收盘时间网格。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo



_CN = ZoneInfo("Asia/Shanghai")


def a_share_completed_one_minute_closes(
    session: date,
    *,
    timezone: tzinfo | None = _CN,
) -> tuple[datetime, ...]:
    """返回一个 A 股连续竞价交易日的 240 个 QMT 结束标签。"""

    morning = datetime.combine(session, time(9, 31), tzinfo=timezone)
    afternoon = datetime.combine(session, time(13, 1), tzinfo=timezone)
    return (
        *tuple(morning + timedelta(minutes=index) for index in range(120)),
        *tuple(afternoon + timedelta(minutes=index) for index in range(120)),
    )
