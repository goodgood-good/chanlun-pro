"""R6-#3: _daily_bsp_and_d3 用默认回看窗口拉日线, 未接收 build 的 start/end。

build(start,end) 只把窗口传给 small/big 的 _sig(), 而 _daily_bsp_and_d3(code,ex,dates)
内部 ex.klines(code,"d") 无 start_date → 落 get_start_date_by_frequency("d")=默认回看3年。
当 build 的 start 早于"今日-3年"(如 `fetch daily` 的 2022-2024 熊市验证窗)时, 日线只从
约(今日-3年)起, daily_bsp/d3_ok 缺失最老约1.5年 → 写入 pkl 的日线共振字段系统性偏移。
修复=build 透传 start/end 给 _daily_bsp_and_d3, 内部 klines 带 start_date/end_date。
"""
import pathlib
import re
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

import pandas as pd  # noqa: E402

from chanlun.recursive_bt.data import fetch  # noqa: E402


class _RecEx:
    def __init__(self):
        self.calls = []

    def klines(self, code, frequency, start_date=None, end_date=None, args=None):
        self.calls.append(
            {"code": code, "freq": frequency, "start_date": start_date, "end_date": end_date}
        )
        return pd.DataFrame()  # 空 df → 函数早退(len<100), 但 ex.klines 调用已被记录


def test_daily_bsp_forwards_start_end():
    ex = _RecEx()
    fetch._daily_bsp_and_d3("SH.600519", ex, [], start="2022-01-01", end="2024-12-31")
    assert len(ex.calls) == 1
    c = ex.calls[0]
    assert c["freq"] == "d"
    assert c["start_date"] == "2022-01-01"  # 修复前恒 None → 落默认回看3年
    assert c["end_date"] == "2024-12-31"


def test_build_passes_start_end_to_daily():
    # build 必须把自身 start/end 透传给 _daily_bsp_and_d3(源码接线守护)
    src = (_root / "src" / "chanlun" / "recursive_bt" / "data" / "fetch.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"= _daily_bsp_and_d3\(code, ex, dates([^)]*)\)", src)
    assert m is not None, "未找到 build 中 _daily_bsp_and_d3 调用"
    assert "start=start" in m.group(1)
    assert "end=end" in m.group(1)