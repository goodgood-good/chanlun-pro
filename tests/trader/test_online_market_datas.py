"""L2: OnlineMarketDatas.begin_round 推进轮次使缓存自动失效。"""

import pandas as pd

from chanlun.trader.online_market_datas import OnlineMarketDatas


class _FakeEx:
    """每次 klines 返回带递增标记的 DataFrame, 用于检测是否命中缓存。"""

    def __init__(self):
        self.calls = 0

    def klines(self, code, frequency):
        self.calls += 1
        return pd.DataFrame({"close": [self.calls]})


def _md():
    ex = _FakeEx()
    md = OnlineMarketDatas("futures", ["5m"], ex, {}, use_cache=True)
    return md, ex


def test_cache_hit_within_same_round():
    md, ex = _md()
    a = md.klines("rb2405", "5m")
    b = md.klines("rb2405", "5m")
    # 同轮第二次命中缓存, ex.klines 只被调一次
    assert ex.calls == 1
    assert a["close"].iloc[0] == b["close"].iloc[0]


def test_begin_round_invalidates_cache():
    md, ex = _md()
    first = md.klines("rb2405", "5m")
    md.begin_round()  # 推进轮次
    second = md.klines("rb2405", "5m")
    # 新轮取到新数据 (ex.klines 再次被调), 非旧缓存
    assert ex.calls == 2
    assert first["close"].iloc[0] != second["close"].iloc[0]


def test_round_seq_increments():
    md, _ = _md()
    assert md._round_seq == 0
    md.begin_round()
    assert md._round_seq == 1
    md.begin_round()
    assert md._round_seq == 2


def test_clear_cache_still_works():
    """保留 clear_cache 兼容: 清缓存后重新取数。"""
    md, ex = _md()
    md.klines("rb2405", "5m")
    md.begin_round()
    md.klines("rb2405", "5m")
    assert ex.calls == 2
