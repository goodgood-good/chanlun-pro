"""D2-F4: 实盘缠论信号只认已收盘 bar。

OnlineMarketDatas.get_cl_data 喂给缠论前须丢弃仍在进行(未收盘)的末根 bar,
与回测/paper/live_monitor(均 drop_unclosed_last_bar)口径一致——否则在未收盘 bar 上
算出买卖点过早真实下单, 下一 tick 该 bar 反向则买点消失但仓已开(回测网测不到)。
last_k_info(当前价, 止损用)保持实时末根不受影响。
"""
import pandas as pd

from chanlun.trader.online_market_datas import OnlineMarketDatas, _drop_unclosed_last_bar


def _df(dates):
    return pd.DataFrame({
        "date": [pd.Timestamp(d) for d in dates],
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
    })


def test_drop_forming_last_bar_dropped():
    # 起点标签：09:37 时 09:30 已收盘，09:35 仍在进行。
    out = _drop_unclosed_last_bar(
        _df(["2099-01-01 09:30:00", "2099-01-01 09:35:00"]),
        "5m",
        as_of=pd.Timestamp("2099-01-01 09:37:00"),
    )
    assert len(out) == 1
    assert pd.Timestamp(out["date"].iloc[-1]) == pd.Timestamp("2099-01-01 09:30:00")


def test_drop_closed_last_bar_kept():
    # 末根在过去 -> 已收盘 -> 保留
    assert len(_drop_unclosed_last_bar(_df(["2020-01-01 09:30:00", "2020-01-01 09:35:00"]), "5m")) == 2


def test_drop_daily_kept():
    assert len(_drop_unclosed_last_bar(_df(["2099-01-01", "2099-01-02"]), "d")) == 2


def test_drop_irregular_interval_kept():
    # 间隔 != 名义 5m(节假日跳空)且末根标签在过去(已收盘)-> 不裁剪, 防误删历史 bar。
    # 末根标签在未来(session 首根进行中)的裁剪见 test_drop_unclosed_session_first_bar.py。
    assert len(_drop_unclosed_last_bar(_df(["2020-01-01 09:30:00", "2020-01-01 09:50:00"]), "5m")) == 2


def test_single_forming_bar_is_dropped():
    assert len(
        _drop_unclosed_last_bar(
            _df(["2099-01-01 09:35:00"]),
            "5m",
            as_of=pd.Timestamp("2099-01-01 09:37:00"),
        )
    ) == 0


class _FakeEx:
    def __init__(self, df):
        self._df = df

    def klines(self, code, frequency):
        return self._df


def _make_om(df):
    om = OnlineMarketDatas.__new__(OnlineMarketDatas)
    om.market = "a"
    om.frequencys = ["5m"]
    om.cl_config = {}
    om.use_cache = False
    om._round_seq = 0
    om.cache_klines = {}
    om.ex = _FakeEx(df)
    return om


def test_last_k_info_keeps_live_bar():
    """last_k_info(当前价)保持实时末根, 不受 D2-F4 影响(止损需实时价)。"""
    df = _df(["2099-01-01 09:30:00", "2099-01-01 09:35:00", "2099-01-01 09:40:00"])
    om = _make_om(df)
    info = om.last_k_info("X")
    assert pd.Timestamp(info["date"]) == pd.Timestamp("2099-01-01 09:40:00")

def test_drop_forming_last_bar_dropped_seconds():
    # D9-#2: 秒级(10s)末根在未来 -> 仍在进行 -> 丢弃 (_freq_minutes 曾对秒级返 None 不裁剪,
    # 期货实盘 frequencys=["10s"] 会在未收盘 bar 上算缠论 -> 过早下单, D2-F4 被击穿)。
    out = _drop_unclosed_last_bar(
        _df(["2099-01-01 09:30:00", "2099-01-01 09:30:10"]),
        "10s",
        as_of=pd.Timestamp("2099-01-01 09:30:15"),
    )
    assert len(out) == 1
    assert pd.Timestamp(out["date"].iloc[-1]) == pd.Timestamp("2099-01-01 09:30:00")


def test_drop_closed_last_bar_kept_seconds():
    # 秒级(30s)已收盘末根(过去)保留, 不误删。
    assert len(_drop_unclosed_last_bar(_df(["2020-01-01 09:30:00", "2020-01-01 09:30:30"]), "30s")) == 2


def test_freq_minutes_parses_seconds():
    # 秒级须解析为分数分钟(step 精确), 分钟级不变, 日/周级仍 None。
    from chanlun.exchange.kline_completion import frequency_to_minutes

    assert frequency_to_minutes("10s") == 10 / 60.0
    assert frequency_to_minutes("30s") == 0.5
    assert frequency_to_minutes("300s") == 5.0
    assert frequency_to_minutes("5m") == 5
    assert frequency_to_minutes("d") is None
