import datetime

from chanlun.exchange.exchange_futu import ExchangeFutu
from chanlun import utils
from chanlun.persistence.db import db
from chanlun import zixuan
from chanlun.trading.base import Operation, POSITION
from chanlun.trading.backtest_trader import BackTestTrader

"""
港股实盘交易器，使用富途（Futu OpenD）交易接口。
"""


class TraderHKStock(BackTestTrader):
    """港股实盘交易器，通过富途 API 下单。"""

    def __init__(self, name, log=None):
        super().__init__(name=name, mode="online", market="hk", log=log)

        self.ex = ExchangeFutu()

        # 最大同时持仓数，资金按仓位数均分
        self.b_space = 3

        self.zx = zixuan.ZiXuan("hk")

    def open_buy(self, code, opt: Operation, amount: float = None):
        """买入开多；按剩余仓位均分可用保证金，并对齐港股 lot_size（最小交易单位）。"""
        positions = self.ex.positions()
        if len(positions) >= self.b_space:
            return False
        if self._broker_already_holds(code):
            self._safe_alert("hk", "港股交易提醒", f"{code} 券商已持仓, 跳过重复开仓")
            return False
        stock_info = self.ex.stock_info(code)
        if stock_info is None:
            return False
        can_tv = self.ex.can_trade_val(code)
        if can_tv is None:
            return False
        max_amount = can_tv["max_margin_buy"] / (self.b_space - len(positions))
        # 港股必须按 lot_size 取整，否则富途拒单
        max_amount = max_amount - (max_amount % stock_info["lot_size"])

        if max_amount <= 0:
            return False
        order = self.ex.order(code, "buy", max_amount)
        if order is False:
            utils.send_fs_msg(
                "hk", "港股交易提醒", f"{code} 下单失败 买入数量 {max_amount}"
            )
            return False
        if order.get("dealt_amount", 0) <= 0:
            # D9-#1: 券商受理但未成交(dealt_amount=0, 富途市价单挂单/被拒仍 ret==RET_OK):
            # 不记本地仓, 否则 execute 记 now_pos_rate>0 而 amount=0 的幽灵仓 -> run 跳过
            # amount==0 仓 -> 券商后续成交=裸持无止损。镜像平仓侧 N4。请人工核查挂单待撤。
            self._safe_alert(
                "hk", "港股交易提醒[开仓未成交]",
                f"{code} 开多下单成交量为0(可能挂单未成交), 已放弃本地记仓, 请人工核查挂单",
            )
            return False
        msg = f"股票买入 {code}-{stock_info['name']} 价格 {order['dealt_avg_price']} 数量 {order['dealt_amount']} 原因 {opt.msg}"

        utils.send_fs_msg("hk", "港股交易提醒", msg)

        self.zx.add_stock("我的持仓", stock_info["code"], stock_info["name"])

        db.order_save(
            "hk",
            code,
            stock_info["name"],
            "buy",
            order["dealt_avg_price"],
            order["dealt_amount"],
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": order["dealt_avg_price"], "amount": order["dealt_amount"]}

    def open_sell(self, code, opt: Operation, amount: float = None):
        """卖空开空；使用 max_margin_short 计算可用融券额度，同样对齐 lot_size。"""
        positions = self.ex.positions()
        if len(positions) >= self.b_space:
            return False
        if self._broker_already_holds(code):
            self._safe_alert("hk", "港股交易提醒", f"{code} 券商已持仓, 跳过重复开仓")
            return False
        stock_info = self.ex.stock_info(code)
        if stock_info is None:
            return False
        can_tv = self.ex.can_trade_val(code)
        if can_tv is None:
            return False
        max_amount = can_tv["max_margin_short"] / (self.b_space - len(positions))
        max_amount = max_amount - (max_amount % stock_info["lot_size"])
        if max_amount <= 0:
            return False
        order = self.ex.order(code, "sell", max_amount)
        if order is False:
            utils.send_fs_msg(
                "hk", "港股交易提醒", f"{code} 下单失败 卖出数量 {max_amount}"
            )
            return False
        if order.get("dealt_amount", 0) <= 0:
            # D9-#1: 券商受理但未成交(dealt_amount=0): 不记本地仓, 防 now_pos_rate>0/amount=0
            # 幽灵仓 -> 券商后续成交=裸持无止损。镜像平仓侧 N4。请人工核查挂单待撤。
            self._safe_alert(
                "hk", "港股交易提醒[开仓未成交]",
                f"{code} 开空下单成交量为0(可能挂单未成交), 已放弃本地记仓, 请人工核查挂单",
            )
            return False
        msg = f"股票卖空 {code}-{stock_info['name']} 价格 {order['dealt_avg_price']} 数量 {order['dealt_amount']} 原因 {opt.msg}"

        utils.send_fs_msg("hk", "港股交易提醒", msg)

        self.zx.add_stock("我的持仓", stock_info["code"], stock_info["name"])

        db.order_save(
            "hk",
            code,
            stock_info["name"],
            "sell",
            order["dealt_avg_price"],
            order["dealt_amount"],
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": order["dealt_avg_price"], "amount": order["dealt_amount"]}

    def close_buy(self, code, pos: POSITION, opt: Operation):
        """平多仓；券商确认无持仓时视为已平，查询失败则不平 (M1)。"""
        # M1: 区分"确认已平"(查询成功且空) 与"查询失败"(不得据此清本地仓)
        status, positions = self.query_broker_position(code)
        if status == "fail":
            self._safe_alert("hk", "港股交易提醒", f"{code} 平多前持仓查询失败, 暂不平仓")
            return False
        if len(positions) == 0:
            return {"price": pos.price, "amount": pos.amount}

        stock_info = self.ex.stock_info(code)
        if stock_info is None:
            return False

        order = self.ex.order(code, "sell", pos.amount)
        if order is False:
            # M2: 平仓失败是高危 (该止损没止住), 升级告警 + 通道失败不掩盖
            self._safe_alert(
                "hk",
                "港股交易提醒[平仓失败-高危]",
                f"{code} 平仓卖出失败, 持仓未平! 需人工介入 数量 {pos.amount}",
            )
            return False
        msg = "股票卖出 %s-%s 价格 %s 数量 %s 盈亏 %s (%.2f%%) 原因 %s" % (
            code,
            stock_info["name"],
            order["dealt_avg_price"],
            order["dealt_amount"],
            positions[0]["profit_val"],
            positions[0]["profit"],
            opt.msg,
        )
        utils.send_fs_msg("hk", "港股交易提醒", msg)

        self.zx.del_stock("我的持仓", stock_info["code"])

        db.order_save(
            "hk",
            code,
            stock_info["name"],
            "sell",
            order["dealt_avg_price"],
            order["dealt_amount"],
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": order["dealt_avg_price"], "amount": order["dealt_amount"]}

    def close_sell(self, code, pos: POSITION, opt: Operation):
        """平空仓（买入回补）；券商确认无持仓时视为已平，查询失败则不平 (M1)。"""
        # M1: 区分"确认已平"(查询成功且空) 与"查询失败"(不得据此清本地仓)
        status, positions = self.query_broker_position(code)
        if status == "fail":
            self._safe_alert("hk", "港股交易提醒", f"{code} 平空前持仓查询失败, 暂不平仓")
            return False
        if len(positions) == 0:
            return {"price": pos.price, "amount": pos.amount}

        stock_info = self.ex.stock_info(code)
        if stock_info is None:
            return False

        order = self.ex.order(code, "buy", pos.amount)
        if order is False:
            # M2: 平仓失败是高危 (该止损没止住), 升级告警 + 通道失败不掩盖
            self._safe_alert(
                "hk",
                "港股交易提醒[平仓失败-高危]",
                f"{code} 平空买入失败, 持仓未平! 需人工介入 数量 {pos.amount}",
            )
            return False
        msg = "股票平空 %s-%s 价格 %s 数量 %s 盈亏 %s (%.2f%%) 原因 %s" % (
            code,
            stock_info["name"],
            order["dealt_avg_price"],
            order["dealt_amount"],
            positions[0]["profit_val"],
            positions[0]["profit"],
            opt.msg,
        )
        utils.send_fs_msg("hk", "港股交易提醒", msg)

        self.zx.del_stock("我的持仓", stock_info["code"])

        db.order_save(
            "hk",
            code,
            stock_info["name"],
            "buy",
            order["dealt_avg_price"],
            order["dealt_amount"],
            opt.msg,
            datetime.datetime.now(),
        )

        return {"price": order["dealt_avg_price"], "amount": order["dealt_amount"]}
