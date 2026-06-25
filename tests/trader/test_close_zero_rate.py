"""L1: close_buy / close_sell now_pos_rate==0 时防御性判零, 返回 amount=0 不抛 ZeroDivision。"""

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION, Operation


def _trader():
    # market='a' 避免 futures 合约配置依赖; mode 任意 (L1 在两分支前)
    return BackTestTrader("t", mode="trade", market="a")


def test_close_buy_zero_pos_rate_no_zerodivision():
    tr = _trader()
    pos = POSITION(code="SH.600000", mmd="1buy", amount=100, price=10.0)
    pos.now_pos_rate = 0  # 触发判零分支
    # loss_price 非 0 → price=loss_price, 不需要 datas
    opt = Operation("SH.600000", "close", "1buy", loss_price=9.0, pos_rate=1)
    res = tr.close_buy("SH.600000", pos, opt)
    assert res["amount"] == 0
    assert res["price"] == 9.0


def test_close_sell_zero_pos_rate_no_zerodivision():
    tr = _trader()
    pos = POSITION(code="SH.600000", mmd="1sell", amount=100, price=10.0)
    pos.now_pos_rate = 0
    opt = Operation("SH.600000", "close", "1sell", loss_price=11.0, pos_rate=1)
    res = tr.close_sell("SH.600000", pos, opt)
    assert res["amount"] == 0
    assert res["price"] == 11.0


def test_close_buy_normal_pos_rate_unchanged():
    """now_pos_rate>0 正常路径计算不变 (回归: L1 不改正常行为)。"""
    tr = _trader()
    pos = POSITION(code="SH.600000", mmd="1buy", amount=100, price=10.0)
    pos.now_pos_rate = 1.0
    opt = Operation("SH.600000", "close", "1buy", loss_price=9.0, pos_rate=1.0)
    res = tr.close_buy("SH.600000", pos, opt)
    # amount = 100 / 1.0 * 1.0 = 100, A 股取整百 → 100
    assert res["amount"] == 100
