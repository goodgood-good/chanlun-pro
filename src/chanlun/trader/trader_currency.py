import datetime

from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun import utils
from chanlun.db import db
from chanlun import zixuan
from chanlun.backtesting.base import Operation, POSITION
from chanlun.backtesting.backtest_trader import BackTestTrader

"""
数字货币实盘交易器，使用 Binance 合约接口。
"""


class TraderCurrency(BackTestTrader):
    """Binance 合约实盘交易器，支持多空双向开平仓。"""

    def __init__(self, name, log=None):
        super().__init__(name=name, mode="online", market="currency", log=log)

        self.ex = ExchangeBinance()

        # 最大同时持仓数，超过则拒绝新开仓
        self.poss_max = 8
        # 杠杆倍数，用于计算开仓名义量
        self.leverage = 2

        self.zx = zixuan.ZiXuan("currency")

    def open_buy(self, code, opt: Operation, amount: float = None):
        """开多仓：按可用余额均分资金（留 2% 缓冲）并乘以杠杆倍数计算开仓量。"""
        try:
            positions = self.ex.positions()
            if len(positions) >= self.poss_max:
                utils.send_fs_msg(
                    "currency",
                    "数字货币交易提醒",
                    f"{code} open buy 下单失败，达到最大开仓数量",
                )
                return False
            balance = self.ex.balance()
            # 0.98 预留手续费/滑点缓冲，避免因余额不足被拒单
            open_usdt = balance["free"] / (self.poss_max - len(positions)) * 0.98
            ticks = self.ex.ticks([code])
            amount = (open_usdt / ticks[code].last) * self.leverage
            res = self.ex.order(code, "open_long", amount, {"leverage": self.leverage})
            if res is False:
                utils.send_fs_msg(
                    "currency", "数字货币交易提醒", f"{code} open buy 下单失败"
                )
                return False
            msg = f"开多仓 {code} 价格 {res['price']} 数量 {open_usdt} 原因 {opt.msg}"
            utils.send_fs_msg("currency", "数字货币交易提醒", msg)

            self.zx.add_stock("我的持仓", code, code)

            db.order_save(
                "currency",
                code,
                code,
                "open_long",
                res["price"],
                res["amount"],
                opt.msg,
                datetime.datetime.now(),
            )

            return {"price": res["price"], "amount": res["amount"]}
        except Exception as e:
            utils.send_fs_msg(
                "currency", "数字货币交易提醒", f"{code} open buy 异常: {str(e)}"
            )
            return False

    def open_sell(self, code, opt: Operation, amount: float = None):
        """开空仓：逻辑与 open_buy 对称，使用 open_short 方向。"""
        try:
            positions = self.ex.positions()
            if len(positions) >= self.poss_max:
                utils.send_fs_msg(
                    "currency",
                    "数字货币交易提醒",
                    f"{code} open sell 下单失败，达到最大开仓数量",
                )
                return False
            balance = self.ex.balance()
            open_usdt = balance["free"] / (self.poss_max - len(positions)) * 0.98

            ticks = self.ex.ticks([code])
            amount = (open_usdt / ticks[code].last) * self.leverage
            res = self.ex.order(code, "open_short", amount, {"leverage": self.leverage})
            if res is False:
                utils.send_fs_msg(
                    "currency", "数字货币交易提醒", f"{code} open sell 下单失败"
                )
                return False
            msg = f"开空仓 {code} 价格 {res['price']} 数量 {open_usdt} 原因 {opt.msg}"
            utils.send_fs_msg("currency", "数字货币交易提醒", msg)
            self.zx.add_stock("我的持仓", code, code)

            db.order_save(
                "currency",
                code,
                code,
                "open_short",
                res["price"],
                res["amount"],
                opt.msg,
                datetime.datetime.now(),
            )

            return {"price": res["price"], "amount": res["amount"]}
        except Exception as e:
            utils.send_fs_msg(
                "currency", "数字货币交易提醒", f"{code} open sell 异常: {str(e)}"
            )
            return False

    def close_buy(self, code, pos: POSITION, opt: Operation):
        """平多仓；若交易所无持仓则视为已平，直接返回原始价/量避免重复操作。"""
        try:
            hold_position = self.ex.positions(code)
            if len(hold_position) == 0:
                return {"price": pos.price, "amount": pos.amount}
            hold_position = hold_position[0]

            res = self.ex.order(code, "close_long", pos.amount)
            if res is False:
                utils.send_fs_msg("currency", "数字货币交易提醒", f"{code} 下单失败")
                return False
            msg = "平多仓 %s 价格 %s 数量 %s 盈亏 %s (%.2f%%) 原因 %s" % (
                code,
                res["price"],
                res["amount"],
                hold_position["unrealizedPnl"],
                hold_position["percentage"],
                opt.msg,
            )
            utils.send_fs_msg("currency", "数字货币交易提醒", msg)

            self.zx.del_stock("我的持仓", code)

            db.order_save(
                "currency",
                code,
                code,
                "close_long",
                res["price"],
                res["amount"],
                opt.msg,
                datetime.datetime.now(),
            )

            return {"price": res["price"], "amount": res["amount"]}
        except Exception as e:
            utils.send_fs_msg(
                "currency", "数字货币交易提醒", f"{code} close buy 异常: {str(e)}"
            )
            return False

    def close_sell(self, code, pos: POSITION, opt: Operation):
        """平空仓；若交易所无持仓则视为已平，直接返回原始价/量。"""
        try:
            hold_position = self.ex.positions(code)
            if len(hold_position) == 0:
                return {"price": pos.price, "amount": pos.amount}
            hold_position = hold_position[0]

            res = self.ex.order(code, "close_short", pos.amount)
            if res is False:
                utils.send_fs_msg("currency", "数字货币交易提醒", f"{code} 下单失败")
                return False
            msg = "平空仓 %s 价格 %s 数量 %s 盈亏 %s (%.2f%%) 原因 %s" % (
                code,
                res["price"],
                res["amount"],
                hold_position["unrealizedPnl"],
                hold_position["percentage"],
                opt.msg,
            )
            utils.send_fs_msg("currency", "数字货币交易提醒", msg)

            self.zx.del_stock("我的持仓", code)

            db.order_save(
                "currency",
                code,
                code,
                "close_short",
                res["price"],
                res["amount"],
                opt.msg,
                datetime.datetime.now(),
            )

            return {"price": res["price"], "amount": res["amount"]}
        except Exception as e:
            utils.send_fs_msg(
                "currency", "数字货币交易提醒", f"{code} close sell 异常: {str(e)}"
            )
            return False
