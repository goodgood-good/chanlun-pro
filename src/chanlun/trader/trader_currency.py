import datetime

from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun import utils
from chanlun.persistence.db import db
from chanlun import zixuan
from chanlun.trading.base import Operation, POSITION
from chanlun.trading.backtest_trader import BackTestTrader

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
            price = ticks[code].last
            amount = (open_usdt / price) * self.leverage
            # M5: 开仓前统一风控 (默认阈值 None → 恒通过, 行为不变)
            ok, reason = self.risk_precheck(code, opt, price, amount, open_usdt)
            if not ok:
                self._safe_alert(
                    "currency", "数字货币交易提醒", f"{code} 风控拦截: {reason}"
                )
                return False
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
            price = ticks[code].last
            amount = (open_usdt / price) * self.leverage
            # M5: 开仓前统一风控 (默认阈值 None → 恒通过, 行为不变)
            ok, reason = self.risk_precheck(code, opt, price, amount, open_usdt)
            if not ok:
                self._safe_alert(
                    "currency", "数字货币交易提醒", f"{code} 风控拦截: {reason}"
                )
                return False
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
        """平多仓；券商确认无持仓时视为已平，查询失败则不平 (M1)。"""
        try:
            # M1: 区分"确认已平"(查询成功且空) 与"查询失败"(不得据此清本地仓)
            status, hold_position = self.query_broker_position(code)
            if status == "fail":
                self._safe_alert(
                    "currency", "数字货币交易提醒", f"{code} 平多前持仓查询失败, 暂不平仓"
                )
                return False
            # 审计 D1-LOW-2: 按 side 过滤多仓(ccxt positions 的 side="long"), 防同 code 同时
            # 持多空时误取到空仓 dict 而对不存在的多仓下 close_long(取首元素不校验 side)。
            hold_position = [p for p in hold_position if p.get("side") == "long"]
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
            # M2: 平仓失败是高危 (该止损没止住), 升级告警 + 落地日志, 不掩盖原异常
            self._safe_alert(
                "currency",
                "数字货币交易提醒[平仓失败-高危]",
                f"{code} close buy 异常, 持仓未平! 需人工介入: {str(e)}",
                exc=e,
            )
            return False

    def close_sell(self, code, pos: POSITION, opt: Operation):
        """平空仓；券商确认无持仓时视为已平，查询失败则不平 (M1)。"""
        try:
            # M1: 区分"确认已平"(查询成功且空) 与"查询失败"(不得据此清本地仓)
            status, hold_position = self.query_broker_position(code)
            if status == "fail":
                self._safe_alert(
                    "currency", "数字货币交易提醒", f"{code} 平空前持仓查询失败, 暂不平仓"
                )
                return False
            # 审计 D1-LOW-2: 按 side 过滤空仓(ccxt positions 的 side="short")
            hold_position = [p for p in hold_position if p.get("side") == "short"]
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
            # M2: 平仓失败是高危 (该止损没止住), 升级告警 + 落地日志, 不掩盖原异常
            self._safe_alert(
                "currency",
                "数字货币交易提醒[平仓失败-高危]",
                f"{code} close sell 异常, 持仓未平! 需人工介入: {str(e)}",
                exc=e,
            )
            return False
