"""锁定 ExchangeTDXUS._convert_dt 的时区换算正确性(pytz localize vs replace)。

bug: _convert_dt 用 `_dt.replace(tzinfo=pytz.timezone("Asia/Shanghai"))` 与
`_dt.replace(..., tzinfo=self.tz)`——pytz 的 DstTzInfo 对象直接塞进 tzinfo 会取
"第一个历史转换"的 LMT 偏移(Shanghai +8:06、US/Eastern -4:56),而非标准 +8:00 / EST/EDT。
后果:分钟级美股 K 线时间戳系统性早 6 分钟;日线收盘时刻带错误的 LMT 偏移。
正确写法是 `tz.localize(naive)`。
"""
import datetime
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

import pytz  # noqa: E402

from chanlun.exchange.exchange_tdx_us import ExchangeTDXUS  # noqa: E402

# @fun.singleton 把类包成工厂 function;__wrapped__ 取回原始类,__new__ 绕过 __init__。
_CLS = ExchangeTDXUS.__wrapped__


def _mk_ex():
    ex = _CLS.__new__(_CLS)
    ex.tz = pytz.timezone("US/Eastern")
    return ex


def test_convert_dt_intraday_no_six_minute_lmt_drift():
    """分钟级:通达信北京时间 09:30 → UTC 应为 01:30(非 LMT 致的 01:24)。"""
    ex = _mk_ex()
    naive = datetime.datetime(2024, 6, 3, 9, 30)  # 周一,美东夏令时
    got = ex._convert_dt(naive)
    got_utc = got.astimezone(pytz.utc)
    assert got_utc == datetime.datetime(2024, 6, 3, 1, 30, tzinfo=pytz.utc), (
        f"got_utc={got_utc}, expected 01:30 UTC (bug 会是 01:24)"
    )


def test_convert_dt_daily_close_uses_correct_eastern_offset():
    """日线 15:00 → 美东 16:00 收盘,偏移应为 EDT(-4h),非 pytz LMT(-4:56)。"""
    ex = _mk_ex()
    naive = datetime.datetime(2024, 6, 3, 15, 0)
    got = ex._convert_dt(naive)
    assert (got.hour, got.minute) == (16, 0), f"got {got}"
    assert got.utcoffset() == datetime.timedelta(hours=-4), (
        f"utcoffset={got.utcoffset()} (bug: LMT -4:56)"
    )