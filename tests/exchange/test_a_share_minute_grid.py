"""A-share market-data minute labels preserve both continuous-auction sessions."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


from chanlun.exchange.a_share_minute_grid import a_share_completed_one_minute_closes


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 30)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 30, hour, minute, second, tzinfo=CN)




def test_completed_session_is_exactly_240_exchange_aligned_closes() -> None:
    closes = a_share_completed_one_minute_closes(SESSION)

    assert len(closes) == 240
    assert closes[0] == _at(9, 31)
    assert closes[119] == _at(11, 30)
    assert closes[120] == _at(13, 1)
    assert closes[-1] == _at(15, 0)
