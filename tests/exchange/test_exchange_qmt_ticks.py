"""锁定 ExchangeQMT.ticks() 的除零保护(新股上市首日等 lastClose=0 场景)。

历史实现中 ticks() 遗漏了 `lastClose == 0` 的保护:
lastClose 为 0 时 rate=(last-lastClose)/lastClose*100 会抛 ZeroDivisionError。
ticks() 被 /tv/quotes 高频轮询(自选组报价)按 market 分组调用, 一个标的除零会被
该 market 分组的 try/except 兜底吃掉 → 整组标的本轮报价全部失败(炸弹半径放大)。

xtdata.get_full_tick 是 native 模块, 这里 monkeypatch 掉, 不连真实数据源。
"""
from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange import Tick
import pytest


def _full_tick(last_price, last_close):
    """构造 xtdata.get_full_tick 的最小返回(单标的), key 为 QMT 代码。"""
    return {
        "600000.SH": {
            "lastPrice": last_price,
            "lastClose": last_close,
            "bidPrice": [last_price],
            "askPrice": [last_price],
            "high": last_price,
            "low": last_price,
            "open": last_price,
            "volume": 100.0,
        }
    }


def test_ticks_zero_last_close_does_not_raise(monkeypatch):
    """lastClose=0: 保留已知价格，未知涨跌幅不得当成真实的 0%。"""
    ex = exchange_qmt.ExchangeQMT()
    monkeypatch.setattr(
        exchange_qmt.xtdata, "get_full_tick", lambda codes: _full_tick(10.0, 0)
    )

    result = ex.ticks(["SH.600000"])

    assert "SH.600000" in result
    assert isinstance(result["SH.600000"], Tick)
    assert result["SH.600000"].last == 10.0
    assert result["SH.600000"].rate is None


def test_ticks_normal_last_close_computes_rate(monkeypatch):
    """lastClose 正常时 rate 仍按 (last-close)/close*100 计算(回归保护)。"""
    ex = exchange_qmt.ExchangeQMT()
    monkeypatch.setattr(
        exchange_qmt.xtdata, "get_full_tick", lambda codes: _full_tick(11.0, 10.0)
    )

    result = ex.ticks(["SH.600000"])

    assert abs(result["SH.600000"].rate - 10.0) < 1e-9


@pytest.mark.parametrize('last_close', [None, 'unknown', float('nan'), float('inf')])
def test_unusable_previous_close_keeps_price_without_inventing_change(monkeypatch, last_close):
    ex = exchange_qmt.ExchangeQMT()
    monkeypatch.setattr(exchange_qmt.xtdata, 'get_full_tick', lambda _: _full_tick(10., last_close))
    value = ex.ticks(['SH.600000'])['SH.600000']
    assert value.last == 10. and value.rate is None


def test_one_malformed_qmt_row_does_not_drop_the_healthy_quote(monkeypatch):
    ex = exchange_qmt.ExchangeQMT()
    payload = _full_tick(11., 10.)
    payload['000001.SZ'] = {'lastPrice': 20., 'lastClose': 19., 'bidPrice': []}
    monkeypatch.setattr(exchange_qmt.xtdata, 'get_full_tick', lambda _: payload)
    value = ex.ticks(['SH.600000', 'SZ.000001'])
    assert list(value) == ['SH.600000']
    assert value['SH.600000'].last == 11. and value['SH.600000'].rate == 10.
