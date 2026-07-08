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
    # 末根在未来 -> 仍在进行 -> 丢弃
    out = _drop_unclosed_last_bar(_df(["2099-01-01 09:30:00", "2099-01-01 09:35:00"]), "5m")
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


def test_drop_short_kept():
    assert len(_drop_unclosed_last_bar(_df(["2099-01-01 09:35:00"]), "5m")) == 1


class _FakeEx:
    def __init__(self, df):
        self._df = df

    def klines(self, code, frequency):
        return self._df


class _CaptureFdb:
    def __init__(self):
        self.captured = None

    def get_web_cl_data(self, market, code, frequency, cl_config, klines):
        self.captured = klines
        return object()


def _make_om(df):
    om = OnlineMarketDatas.__new__(OnlineMarketDatas)
    om.market = "a"
    om.frequencys = ["5m"]
    om.cl_config = {}
    om.use_cache = False
    om._round_seq = 0
    om.cache_klines = {}
    om.ex = _FakeEx(df)
    om.fdb = _CaptureFdb()
    return om


def test_get_cl_data_feeds_closed_bars_only():
    """get_cl_data 喂给缠论的 K 线已丢进行中末根(实盘信号不吃未收盘 bar)。"""
    df = _df(["2099-01-01 09:30:00", "2099-01-01 09:35:00", "2099-01-01 09:40:00"])
    om = _make_om(df)
    om.get_cl_data("X", "5m")
    fed = om.fdb.captured
    assert fed is not None
    assert len(fed) == 2  # 3 根中末根(未来=进行中)被丢
    assert pd.Timestamp(fed["date"].iloc[-1]) == pd.Timestamp("2099-01-01 09:35:00")


def test_last_k_info_keeps_live_bar():
    """last_k_info(当前价)保持实时末根, 不受 D2-F4 影响(止损需实时价)。"""
    df = _df(["2099-01-01 09:30:00", "2099-01-01 09:35:00", "2099-01-01 09:40:00"])
    om = _make_om(df)
    info = om.last_k_info("X")
    assert pd.Timestamp(info["date"]) == pd.Timestamp("2099-01-01 09:40:00")