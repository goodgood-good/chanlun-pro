"""R1-C1: session 首根进行中 bar 必须被裁剪(drop_unclosed_last_bar 两副本口径一致)。

QMT 5m 为端点标签(09:35..11:30 / 13:05..15:00): 9:30/13:00 开盘扫描时末根
= 进行中的 09:35/13:05 bar, 其与前一根(昨 15:00 / 11:30)间隔 != 名义 5m。
该半成品 bar 不能进入 only-once 增量计算，否则会在 CL 中永久定格。

修复口径: 间隔异常时仅裁「标签在未来」的末根——已收盘 bar 的标签无论起点/
终点约定必然 <= now, 标签 > now 只可能是进行中 bar, 故绝不误删历史收盘 bar;
节假日跳空的历史 bar(标签在过去)保护不变。
"""
import pandas as pd
import pytest

from chanlun.trader.online_market_datas import _drop_unclosed_last_bar


def _df(dates):
    return pd.DataFrame({
        "date": [pd.Timestamp(d) for d in dates],
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
    })


IMPLS = [pytest.param(_drop_unclosed_last_bar, id="online_market_datas")]


@pytest.mark.parametrize("impl", IMPLS)
def test_lunch_session_first_forming_bar_dropped(impl):
    # 午后开盘: 末根 13:05(端点标签,未来=进行中), prev=11:30, 间隔 95min != 5m
    out = impl(
        _df(["2099-01-01 11:25:00", "2099-01-01 11:30:00",
             "2099-01-01 13:05:00"]),
        "5m",
        time_label="end",
        as_of=pd.Timestamp("2099-01-01 13:02:00"),
    )
    assert len(out) == 2
    assert pd.Timestamp(out["date"].iloc[-1]) == pd.Timestamp("2099-01-01 11:30:00")


@pytest.mark.parametrize("impl", IMPLS)
def test_overnight_session_first_forming_bar_dropped(impl):
    # 早盘开盘: 末根今 09:35(未来=进行中), prev=昨 15:00, 间隔 18.5h != 5m
    out = impl(
        _df(["2099-01-01 14:55:00", "2099-01-01 15:00:00",
             "2099-01-02 09:35:00"]),
        "5m",
        time_label="end",
        as_of=pd.Timestamp("2099-01-02 09:32:00"),
    )
    assert len(out) == 2
    assert pd.Timestamp(out["date"].iloc[-1]) == pd.Timestamp("2099-01-01 15:00:00")


@pytest.mark.parametrize("impl", IMPLS)
def test_holiday_gap_historical_bar_kept(impl):
    # 节假日跳空但全是历史收盘 bar(标签在过去)→ 保护不变, 不裁
    out = impl(_df(["2020-01-01 09:30:00", "2020-01-01 09:50:00"]), "5m")
    assert len(out) == 2


@pytest.mark.parametrize("impl", IMPLS)
def test_regular_forming_bar_still_dropped(impl):
    out = impl(
        _df(["2099-01-01 09:30:00", "2099-01-01 09:35:00"]),
        "5m",
        time_label="end",
        as_of=pd.Timestamp("2099-01-01 09:32:00"),
    )
    assert len(out) == 1


@pytest.mark.parametrize("impl", IMPLS)
def test_regular_closed_bar_still_kept(impl):
    out = impl(_df(["2020-01-01 09:30:00", "2020-01-01 09:35:00"]), "5m")
    assert len(out) == 2


@pytest.mark.parametrize("impl", IMPLS)
def test_aware_tz_session_first_dropped(impl):
    # aware 时间戳(美股/港股口径)同样裁 session 首根未来末根, 不抛 TypeError
    out = impl(
        _df([pd.Timestamp("2099-01-01 11:25:00", tz="Asia/Shanghai"),
             pd.Timestamp("2099-01-01 11:30:00", tz="Asia/Shanghai"),
             pd.Timestamp("2099-01-01 13:05:00", tz="Asia/Shanghai")]),
        "5m",
        time_label="end",
        as_of=pd.Timestamp("2099-01-01 13:02:00", tz="Asia/Shanghai"),
    )
    assert len(out) == 2
