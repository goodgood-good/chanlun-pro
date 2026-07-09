"""R3-C4: 交易所工厂 NY_FUTURES 分支缺 else 兜底。

_build_exchange 中唯 Market.NY_FUTURES 分支没有末尾 else raise(其余 7 个市场都有)。
EXCHANGE_NY_FUTURES 配成不支持值时会穿透到 return g_exchange_obj[market.value] 抛
KeyError(晦涩), 应与其它市场一致抛"不支持的纽约期货交易所"清晰异常。
"""

import pytest

import chanlun.exchange as exmod
from chanlun import config
from chanlun.market import Market


def test_ny_futures_unsupported_raises_clear_exception(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_NY_FUTURES", "bogus_not_supported")
    # 确保未缓存, 逼真正走构建分支
    exmod.g_exchange_obj.pop(Market.NY_FUTURES.value, None)
    with pytest.raises(Exception) as ei:
        exmod._build_exchange(Market.NY_FUTURES)
    # 旧代码穿透后 return g_exchange_obj["ny_futures"] → KeyError('ny_futures')
    assert not isinstance(ei.value, KeyError)
    assert "不支持" in str(ei.value)
    assert "bogus_not_supported" in str(ei.value)


def test_ny_futures_db_still_builds(monkeypatch):
    # 兜底 else 不能误伤合法 db 配置
    monkeypatch.setattr(config, "EXCHANGE_NY_FUTURES", "db")
    exmod.g_exchange_obj.pop(Market.NY_FUTURES.value, None)
    exmod._build_exchange(Market.NY_FUTURES)
    assert Market.NY_FUTURES.value in exmod.g_exchange_obj
    exmod.g_exchange_obj.pop(Market.NY_FUTURES.value, None)