import datetime

from chanlun import utils
from chanlun import zixuan
from chanlun.db import db
from chanlun.exchange.exchange_tdx import ExchangeTDX
from chanlun.backtesting.backtest_trader import BackTestTrader
from chanlun.backtesting.base import Operation, POSITION

"""
A股信号通知交易器：使用通达信行情接口，不实际下单，
仅将信号写入持仓自选并通过飞书消息通知，用户自行决定是否手动操作。
"""


class TraderAStock(BackTestTrader):
    """A股模拟交易器（仅通知，不实际下单）。

    触发买卖信号时记录到数据库并发送飞书消息；
    实盘执行需用户根据消息手动操作。
    """

    def __init__(self, name, log=None):
        super().__init__(name=name, mode="online", market="a", log=log)

        self.ex = ExchangeTDX()

        self.zx = zixuan.ZiXuan("a")

    def open_buy(self, code, opt: Operation, amount: float = None):
        """模拟买入：发送飞书通知并记录订单，不实际下单。金额固定 50000，按 100 股取整。"""
        tick = self.ex.ticks([code])
        if code not in tick.keys():
            return False

        stock = self.ex.stock_info(code)
        if stock is None:
            return False

        balance = 50000
        price = tick[code].last
        amount = balance / price
        # A股最小交易单位 100 股，向下取整
        amount = amount - amount % 100

        msg = (
            f"股票买入 {code}-{stock['name']} 价格 {price} 数量 {amount} 原因 {opt.msg}"
        )
        utils.send_fs_msg("a", "沪深交易提醒", [msg])

        self.zx.add_stock("我的持仓", stock["code"], stock["name"])

        # 写入数据库，图表可据此标注买卖位置
        db.order_save(
            "a",
            code,
            stock["name"],
            "buy",
            price,
            amount,
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": price, "amount": amount}

    def open_sell(self, code, opt: Operation, amount: float = None):
        """A股不支持做空，直接返回 False。"""
        return False

    def close_buy(self, code, pos: POSITION, opt):
        """模拟平多：发送飞书通知并记录订单，不实际下单。"""
        tick = self.ex.ticks([code])
        if code not in tick.keys():
            return False
        stock = self.ex.stock_info(code)
        if stock is None:
            return False

        price = tick[code].last
        msg = f"股票卖出 {code}-{stock['name']} 价格 {price} 数量 {pos.amount} 原因 {opt.msg}"
        utils.send_fs_msg("a", "沪深交易提醒", [msg])

        self.zx.del_stock("我的持仓", stock["code"])

        db.order_save(
            "a",
            code,
            stock["name"],
            "sell",
            price,
            pos.amount,
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": price, "amount": pos.amount}

    def close_sell(self, code, pos: POSITION, opt):
        """A股不支持做空，直接返回 False。"""
        return False
