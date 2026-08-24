"""锁定 ExchangeQMT.ticks() 的除零保护(新股上市首日等 lastClose=0 场景)。

历史实现中 ticks() 遗漏了 `lastClose == 0` 的保护:
lastClose 为 0 时 rate=(last-lastClose)/lastClose*100 会抛 ZeroDivisionError。
ticks() 被 /tv/quotes 高频轮询(自选组报价)按 market 分组调用, 一个标的除零会被
该 market 分组的 try/except 兜底吃掉 → 整组标的本轮报价全部失败(炸弹半径放大)。

xtdata.get_full_tick 是 native 模块, 这里 monkeypatch 掉, 不连真实数据源。
"""
from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange import Tick


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
    """新股首日 lastClose=0: ticks() 不应抛 ZeroDivisionError, rate 记为 0。"""
    ex = exchange_qmt.ExchangeQMT()
    monkeypatch.setattr(
        exchange_qmt.xtdata, "get_full_tick", lambda codes: _full_tick(10.0, 0)
    )

    result = ex.ticks(["SH.600000"])

    assert "SH.600000" in result
    assert isinstance(result["SH.600000"], Tick)
    assert result["SH.600000"].rate == 0


def test_ticks_normal_last_close_computes_rate(monkeypatch):
    """lastClose 正常时 rate 仍按 (last-close)/close*100 计算(回归保护)。"""
    ex = exchange_qmt.ExchangeQMT()
    monkeypatch.setattr(
        exchange_qmt.xtdata, "get_full_tick", lambda codes: _full_tick(11.0, 10.0)
    )

    result = ex.ticks(["SH.600000"])

    assert abs(result["SH.600000"].rate - 10.0) < 1e-9
