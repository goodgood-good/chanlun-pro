import os
import time
from datetime import datetime
from typing import Any, Dict

from chanlun.trader._ctp_state import CTPState

from openctp_ctp.thostmduserapi import (
    CThostFtdcInputOrderActionField,
    THOST_FTDC_AF_Delete,
)
from openctp_ctp.thosttraderapi import (
    THOST_FTDC_TC_GFD,  # 当日有效
    THOST_FTDC_VC_AV,  # 任意数量
    CThostFtdcInputOrderField,
    CThostFtdcQryInstrumentField,
    CThostFtdcQryInvestorPositionField,
    CThostFtdcQryOrderField,
    CThostFtdcQryTradeField,
    CThostFtdcQryTradingAccountField,
    CThostFtdcReqAuthenticateField,
    CThostFtdcReqUserLoginField,
    CThostFtdcSettlementInfoConfirmField,
    CThostFtdcTraderApi,
    THOST_FTDC_CC_Immediately,  # 立即触发
    THOST_FTDC_D_Buy,  # 买入
    THOST_FTDC_D_Sell,  # 卖出
    THOST_FTDC_HF_Speculation,  # 投机
    THOST_FTDC_OF_Close,  # 平仓
    THOST_FTDC_OF_Open,  # 开仓 (B2 follow-up: ApiStruct 迁移)
    THOST_FTDC_OPT_LimitPrice,  # 限价单
    THOST_FTDC_OST_AllTraded,  # 全部成交 (M3)
    THOST_FTDC_OST_Canceled,  # 已撤单 (M4 cancel_order 确认)
    THOST_FTDC_OST_PartTradedQueueing,  # 部分成交仍在队列 (M3)
)

# 报单/查询的回报等待超时 (秒). 取代原 time.sleep(1) 硬编码:
# 回报快时立即返回, 回报慢时也不超过 _CTP_CALLBACK_TIMEOUT 才放弃.
_CTP_CALLBACK_TIMEOUT = 3.0

from chanlun import utils
from chanlun.tools.log_util import LogUtil
from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION, Operation
from chanlun.persistence.db import db
from chanlun.exchange.exchange_ctp import MarketCTP


def _precheck_ctp_order(amount, price) -> bool:
    """下单前防御:数量须为正、价格须 >0,挡 NaN/负/零价穿透到券商真实下单。

    开仓 ``amount or 1`` 对 NaN 不兜底(``float('nan') or 1`` → NaN 仍穿透)、
    LimitPrice 直取 tick.last 不校验——信号层异常 amount 或行情快照异常时,会下出
    NaN/负量单或零价/负价限价单。此处统一前置拦截,异常即由调用方 return False + 告警。
    """
    try:
        return bool(amount == amount and amount > 0 and price == price and price > 0)
    except TypeError:
        return False


def _ctp_order_filled_amount(order) -> float:
    """从 OnRtnOrder 回报判定实际成交量 (M3)。

    返回 >0 表示已成交的手数 (AllTraded 全成 / PartTradedQueueing 部分成交);
    返回 0 表示未成交 / 被拒 / 已撤 → 调用方须 return False 不记本地持仓。

    VolumeTraded 是 CThostFtdcOrderField 的累计成交量字段; OrderStatus 仅做门控:
    只有 AllTraded / PartTradedQueueing 才承认成交, 其余 (NoTrade/Canceled/废单) 一律 0。

    注: 若柜台 "先回 NoTradeQueueing(Volume=0) 后回 AllTraded", wait_for_order 只等到
    第一个回报就返回, 这里可能取到 VolumeTraded=0 误判未成交。投产前须按设计 M3 灰度第 1 步
    在仿真确认时序, 若有此问题应升级为 OnRtnTrade 累计方案。
    """
    status = getattr(order, "OrderStatus", None)
    if status not in (THOST_FTDC_OST_AllTraded, THOST_FTDC_OST_PartTradedQueueing):
        return 0
    traded = getattr(order, "VolumeTraded", 0) or 0
    try:
        return traded if traded > 0 else 0
    except TypeError:
        return 0


class MyTraderCallback(CThostFtdcTraderApi):
    """CTP交易回调"""

    def __init__(self, trader: Any) -> None:
        super().__init__()
        self.trader = trader
        self.connected: bool = False
        self.logged_in: bool = False
        self.authenticated: bool = False  # 添加认证状态
        self.front_id: int | None = None
        self.session_id: int | None = None
        # B2: 线程安全状态容器, 替代原 self.order_ref / orders / positions
        # CTP 回调线程与主线程都通过 self.state.xxx() 访问, 内部加锁
        self.state = CTPState()

    def OnFrontConnected(self):
        print("交易服务器连接成功")
        self.connected = True

        # 新版 CTP 要求先 AppID/AuthCode 认证后才能登录；旧柜台无此字段可直接登录
        if self.trader.ex.app_id:
            req = CThostFtdcReqAuthenticateField()
            req.BrokerID = self.trader.ex.broker_id
            req.UserID = self.trader.ex.user_id
            req.AppID = self.trader.ex.app_id
            req.AuthCode = self.trader.ex.auth_code
            self.ReqAuthenticate(req, 0)
        else:
            self._login()

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        """认证响应"""
        if pRspInfo and pRspInfo.ErrorID == 0:
            print("交易账户认证成功")
            self.authenticated = True
            self._login()
        else:
            print(f"交易账户认证失败：{pRspInfo.ErrorMsg if pRspInfo else '未知错误'}")

    def _login(self):
        """执行登录"""
        req = CThostFtdcReqUserLoginField()
        req.BrokerID = self.trader.ex.broker_id
        req.UserID = self.trader.ex.user_id
        req.Password = self.trader.ex.password
        self.ReqUserLogin(req, 0)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID == 0:
            self.logged_in = True
            self.front_id = pRspUserLogin.FrontID
            self.session_id = pRspUserLogin.SessionID
            print("交易服务器登录成功")
        else:
            print(f"交易服务器登录失败：{pRspInfo.ErrorMsg}")

    def OnRtnOrder(self, pOrder):
        """委托回报 (CTP callback 线程, B2 改走 state.set_order)"""
        print(f"委托回报: {pOrder.InstrumentID} {pOrder.OrderStatus}")
        self.state.set_order(pOrder.OrderRef, pOrder)

    def OnRtnTrade(self, pTrade):
        """成交回报"""
        print(
            f"成交回报: {pTrade.InstrumentID} 价格:{pTrade.Price} 数量:{pTrade.Volume}"
        )

    def OnRspQryInvestorPosition(
        self, pInvestorPosition, pRspInfo, nRequestID, bIsLast
    ):
        """持仓查询回报 (CTP callback 线程).

        B2: set_position 加锁写入.
        B2 follow-up: bIsLast=True 时唤醒等待中的主线程, 取代 time.sleep(1).
        """
        if pInvestorPosition:
            key = f"{pInvestorPosition.InstrumentID}_{pInvestorPosition.PosiDirection}"
            self.state.set_position(key, pInvestorPosition)
        if bIsLast:
            self.state.mark_position_query_done()


class CTPTrader(BackTestTrader):
    """CTP期货交易实现"""

    def __init__(self, name, log=None):
        super().__init__(name=name, mode="online", market="futures", log=log)
        self.ex = MarketCTP()

        # 最大持仓数量
        self.max_pos = 3

        # 单笔持仓最长持有天数 (自然日); 超过则风控强平 (H1)。None 表示不启用时间止损。
        self.max_hold_days = 5

        # 创建临时目录
        self.temp_path = os.path.expanduser("~/.ctp/ctp")
        os.makedirs(self.temp_path, exist_ok=True)

        # 初始化交易接口
        self.trader_api = MyTraderCallback(self)
        self.trader_api.CreateTrader(os.path.join(self.temp_path, "td"))
        self.trader_api.RegisterFront(self.ex.td_front)
        self.trader_api.Init()

        # 等待连接和登录
        self._wait_ready()

    def _wait_ready(self):
        """等待接口就绪"""
        for _ in range(10):
            if self.trader_api.connected:
                break
            time.sleep(1)

        if not self.trader_api.connected:
            raise Exception("交易服务器连接失败")

        # 等待认证和登录完成
        for _ in range(10):
            if self.trader_api.logged_in:
                break
            time.sleep(1)

        if not self.trader_api.logged_in:
            raise Exception("交易服务器登录失败")

    def close(self):
        """关闭接口"""
        if self.trader_api:
            self.trader_api.Release()

    def open_buy(self, code, opt: Operation, amount: float = None):
        """开多仓"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 检查持仓数量 (B2 follow-up: ApiStruct → CThostFtdc + Event 等待)
        qry_req = CThostFtdcQryInvestorPositionField()
        qry_req.BrokerID = self.ex.broker_id
        qry_req.InvestorID = self.ex.user_id
        qry_req.InstrumentID = code
        self.trader_api.state.prepare_position_query()
        self.trader_api.ReqQryInvestorPosition(
            qry_req, self.trader_api.state.next_request_id()
        )
        if not self.trader_api.state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT):
            return False

        if self.trader_api.state.get_position_count() >= self.max_pos:
            return False

        # 下单前防御:挡 NaN/负数量、非正价格穿透到券商(amount=None/0 仍走 `or 1` 兜底1手)
        if not _precheck_ctp_order(amount or 1, tick[code].last):
            LogUtil.warning(f"CTP open_buy 拒单:异常 amount={amount}、price={tick[code].last}")
            return False
        # M5: 开仓前统一风控 (默认阈值 None → 恒通过, 行为不变)
        _amt = amount or 1
        _ok, _reason = self.risk_precheck(
            code, opt, tick[code].last, _amt, tick[code].last * _amt
        )
        if not _ok:
            LogUtil.warning(f"CTP open_buy 风控拦截 code={code}: {_reason}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒", [f"开多风控拦截 {code}: {_reason}"]
            )
            return False
        # M4: 开仓前清理本标的存活挂单, 避免重启/上轮残留挂单导致重复建仓
        for _ref, _o in self.trader_api.state.get_alive_orders(code):
            self.cancel_order(_ref)
        # 下单
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = THOST_FTDC_D_Buy
        req.CombOffsetFlag = THOST_FTDC_OF_Open
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = tick[code].last
        req.VolumeTotalOriginal = amount or 1
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        # 等待订单回报 (B2 follow-up: Event 取代 time.sleep)
        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            # 超时未收到回报: 可能在途, 主动撤单兜底避免幽灵挂单 (见 M4), 记为失败
            self.cancel_order(order_ref)
            LogUtil.warning(f"CTP open_buy 回报超时, 已发撤单 ref={order_ref} code={code}")
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        # M3: 仅 OrderStatus 为 AllTraded/PartTradedQueueing 且有实际成交量才算成功;
        # 被拒/废单/未成交一律 return False, 不记本地持仓 (否则持仓与柜台失同步)
        filled = _ctp_order_filled_amount(order)
        if filled <= 0:
            status = getattr(order, "OrderStatus", "?")
            status_msg = getattr(order, "StatusMsg", "")
            LogUtil.warning(
                f"CTP open_buy 未成交 code={code} ref={order_ref} "
                f"OrderStatus={status} msg={status_msg}"
            )
            utils.send_fs_msg(
                "futures_trader",
                "期货交易提醒",
                [f"开多未成交/被拒 {code} 状态={status} {status_msg}"],
            )
            return False

        # 记录订单 (用实际成交量 filled, 非请求量)
        db.order_save(
            "futures",
            code,
            code,
            "buy",
            tick[code].last,
            filled,
            opt.msg,
            datetime.now(),
        )

        msg = f"期货开多 {code} 价格 {tick[code].last} 数量 {filled} 原因 {opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": filled}

    def open_sell(self, code, opt: Operation, amount: float = None):
        """开空仓"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 检查持仓数量
        qry_req = CThostFtdcQryInvestorPositionField()
        qry_req.BrokerID = self.ex.broker_id
        qry_req.InvestorID = self.ex.user_id
        qry_req.InstrumentID = code
        self.trader_api.state.prepare_position_query()
        self.trader_api.ReqQryInvestorPosition(
            qry_req, self.trader_api.state.next_request_id()
        )
        if not self.trader_api.state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT):
            return False

        if self.trader_api.state.get_position_count() >= self.max_pos:
            return False

        # 下单前防御:挡 NaN/负数量、非正价格穿透到券商(amount=None/0 仍走 `or 1` 兜底1手)
        if not _precheck_ctp_order(amount or 1, tick[code].last):
            LogUtil.warning(f"CTP open_sell 拒单:异常 amount={amount}、price={tick[code].last}")
            return False
        # M5: 开仓前统一风控 (默认阈值 None → 恒通过, 行为不变)
        _amt = amount or 1
        _ok, _reason = self.risk_precheck(
            code, opt, tick[code].last, _amt, tick[code].last * _amt
        )
        if not _ok:
            LogUtil.warning(f"CTP open_sell 风控拦截 code={code}: {_reason}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒", [f"开空风控拦截 {code}: {_reason}"]
            )
            return False
        # M4: 开仓前清理本标的存活挂单, 避免重启/上轮残留挂单导致重复建仓
        for _ref, _o in self.trader_api.state.get_alive_orders(code):
            self.cancel_order(_ref)
        # 下单
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = THOST_FTDC_D_Sell  # 卖出开仓
        req.CombOffsetFlag = THOST_FTDC_OF_Open  # 开仓
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = tick[code].last
        req.VolumeTotalOriginal = amount or 1
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            # 超时未收到回报: 可能在途, 主动撤单兜底避免幽灵挂单 (见 M4), 记为失败
            self.cancel_order(order_ref)
            LogUtil.warning(f"CTP open_sell 回报超时, 已发撤单 ref={order_ref} code={code}")
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        # M3: 仅成交才算成功; 被拒/废单/未成交一律 return False, 不记本地持仓
        filled = _ctp_order_filled_amount(order)
        if filled <= 0:
            status = getattr(order, "OrderStatus", "?")
            status_msg = getattr(order, "StatusMsg", "")
            LogUtil.warning(
                f"CTP open_sell 未成交 code={code} ref={order_ref} "
                f"OrderStatus={status} msg={status_msg}"
            )
            utils.send_fs_msg(
                "futures_trader",
                "期货交易提醒",
                [f"开空未成交/被拒 {code} 状态={status} {status_msg}"],
            )
            return False

        db.order_save(
            "futures",
            code,
            code,
            "sell",
            tick[code].last,
            filled,
            opt.msg,
            datetime.now(),
        )

        msg = f"期货开空 {code} 价格 {tick[code].last} 数量 {filled} 原因 {opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": filled}

    def close_buy(self, code, pos: POSITION, opt):
        """平多仓"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 下单前防御:挡 NaN/负持仓量、非正价格穿透到券商(pos.amount 异常=数据问题)
        if not _precheck_ctp_order(pos.amount, tick[code].last):
            LogUtil.warning(f"CTP close_buy 拒单:异常 pos.amount={pos.amount}、price={tick[code].last}")
            return False
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = THOST_FTDC_D_Sell
        req.CombOffsetFlag = THOST_FTDC_OF_Close
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = tick[code].last
        req.VolumeTotalOriginal = pos.amount
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            # 平仓回报超时: 撤单兜底, 记为失败 (execute 不会清本地仓, 下轮重试)
            self.cancel_order(order_ref)
            LogUtil.warning(f"CTP close_buy 回报超时, 已发撤单 ref={order_ref} code={code}")
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        # M3: 平仓必须真成交才算成功; 未成交/被拒时告警 (该平没平掉是高危) 且 return False,
        # 让 execute 不把本地仓清零 (避免裸持失管)
        filled = _ctp_order_filled_amount(order)
        if filled <= 0:
            status = getattr(order, "OrderStatus", "?")
            status_msg = getattr(order, "StatusMsg", "")
            LogUtil.warning(
                f"CTP close_buy 未成交(平多失败) code={code} ref={order_ref} "
                f"OrderStatus={status} msg={status_msg}"
            )
            utils.send_fs_msg(
                "futures_trader",
                "期货交易提醒",
                [f"平多未成交/被拒(持仓未平!) {code} 状态={status} {status_msg}"],
            )
            return False

        db.order_save(
            "futures",
            code,
            code,
            "sell",
            tick[code].last,
            filled,
            opt.msg,
            datetime.now(),
        )

        msg = f"期货平多 {code} 价格 {tick[code].last} 数量 {filled} 原因 {opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": filled}

    def close_sell(self, code, pos: POSITION, opt):
        """平空仓"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 下单前防御:挡 NaN/负持仓量、非正价格穿透到券商(pos.amount 异常=数据问题)
        if not _precheck_ctp_order(pos.amount, tick[code].last):
            LogUtil.warning(f"CTP close_sell 拒单:异常 pos.amount={pos.amount}、price={tick[code].last}")
            return False
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = THOST_FTDC_D_Buy  # 买入平仓
        req.CombOffsetFlag = THOST_FTDC_OF_Close  # 平仓
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = tick[code].last
        req.VolumeTotalOriginal = pos.amount
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            # 平仓回报超时: 撤单兜底, 记为失败 (execute 不会清本地仓, 下轮重试)
            self.cancel_order(order_ref)
            LogUtil.warning(f"CTP close_sell 回报超时, 已发撤单 ref={order_ref} code={code}")
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        # M3: 平仓必须真成交才算成功; 未成交/被拒时告警 (该平没平掉是高危) 且 return False
        filled = _ctp_order_filled_amount(order)
        if filled <= 0:
            status = getattr(order, "OrderStatus", "?")
            status_msg = getattr(order, "StatusMsg", "")
            LogUtil.warning(
                f"CTP close_sell 未成交(平空失败) code={code} ref={order_ref} "
                f"OrderStatus={status} msg={status_msg}"
            )
            utils.send_fs_msg(
                "futures_trader",
                "期货交易提醒",
                [f"平空未成交/被拒(持仓未平!) {code} 状态={status} {status_msg}"],
            )
            return False

        db.order_save(
            "futures",
            code,
            code,
            "buy",
            tick[code].last,
            filled,
            opt.msg,
            datetime.now(),
        )

        msg = f"期货平空 {code} 价格 {tick[code].last} 数量 {filled} 原因 {opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": filled}

    def lock_position(self, code: str, pos: POSITION, opt: Operation):
        """锁仓操作
        当持有多仓时开等量空仓，或持有空仓时开等量多仓
        """
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 查询当前持仓
        qry_req = CThostFtdcQryInvestorPositionField()
        qry_req.BrokerID = self.ex.broker_id
        qry_req.InvestorID = self.ex.user_id
        qry_req.InstrumentID = code
        self.trader_api.state.prepare_position_query()
        self.trader_api.ReqQryInvestorPosition(
            qry_req, self.trader_api.state.next_request_id()
        )
        if not self.trader_api.state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT):
            return False

        # 根据持仓方向决定锁仓方向
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        # H2: POSITION 无 direction 字段, 方向经 mmd 子串判 ("buy" in mmd / "sell" in mmd),
        # 与 force_close 判向口径一致
        if "buy" in pos.mmd:
            # 持有多仓，开空仓锁仓
            req.Direction = THOST_FTDC_D_Sell  # 开空仓
            direction = "sell"
        else:
            # 持有空仓，开多仓锁仓
            req.Direction = THOST_FTDC_D_Buy  # 开多仓
            direction = "buy"
        req.CombOffsetFlag = THOST_FTDC_OF_Open
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = tick[code].last
        req.VolumeTotalOriginal = pos.amount  # 锁仓数量等于持仓数量
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        db.order_save(
            "futures",
            code,
            code,
            direction,
            tick[code].last,
            pos.amount,
            f"锁仓:{opt.msg}",
            datetime.now(),
        )

        msg = f"期货锁仓 {code} 方向:{direction} 价格:{tick[code].last} 数量:{pos.amount} 原因:{opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": pos.amount}

    def force_close(self, code: str, pos: POSITION, opt: Operation):
        """强制平仓
        使用对手价强平，提高成交概率
        """
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref)

        # 根据持仓方向决定平仓方向和价格
        # H2: POSITION 无 direction 字段, 方向经 mmd 子串判 ("buy" in mmd / "sell" in mmd)
        if "buy" in pos.mmd:
            # 平多仓，用卖一价
            direction = THOST_FTDC_D_Sell
            price = tick[code].sell1
        else:
            # 平空仓，用买一价
            direction = THOST_FTDC_D_Buy
            price = tick[code].buy1

        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = direction
        req.CombOffsetFlag = THOST_FTDC_OF_Close
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = price
        req.VolumeTotalOriginal = pos.amount
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref

        result = self.trader_api.ReqOrderInsert(req, 0)
        if result != 0:
            return False

        if not self.trader_api.state.wait_for_order(order_ref, _CTP_CALLBACK_TIMEOUT):
            return False
        order = self.trader_api.state.get_order(order_ref)
        if not order:
            return False

        direction_str = "sell" if direction == THOST_FTDC_D_Sell else "buy"
        db.order_save(
            "futures",
            code,
            code,
            direction_str,
            price,
            pos.amount,
            f"强平:{opt.msg}",
            datetime.now(),
        )

        msg = f"期货强平 {code} 方向:{direction_str} 价格:{price} 数量:{pos.amount} 原因:{opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": price, "amount": pos.amount}

    def force_close_all(self, opt: Operation):
        """
        强制平掉所有持仓

        示例：
        #强平单个持仓
        opt = Operation(code, "close", "risk", 0, {}, "风控强平")
        trader.force_close(code, position, opt)

        # 强平所有持仓
        opt = Operation("ALL", "close", "risk", 0, {}, "风控全部强平")
        trader.force_close_all(opt)
        """
        # 查询所有持仓
        req = CThostFtdcQryInvestorPositionField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        self.trader_api.state.prepare_position_query()
        self.trader_api.ReqQryInvestorPosition(
            req, self.trader_api.state.next_request_id()
        )
        if not self.trader_api.state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT):
            return []

        results = []
        for key, pos_info in self.trader_api.state.get_positions_snapshot().items():
            code = pos_info.InstrumentID
            direction = "buy" if pos_info.PosiDirection == "2" else "sell"
            amount = pos_info.Position

            # H2: POSITION 无 direction 字段; 方向经 mmd 子串传递 ("buy" in mmd / "sell" in mmd),
            # 与 force_close / lock_position 判向口径一致; type 仅用于日志可读性
            pos = POSITION(
                code=code,
                mmd=direction,
                type="做多" if direction == "buy" else "做空",
                price=pos_info.OpenPrice,
                amount=amount,
            )

            result = self.force_close(code, pos, opt)
            if result:
                results.append(result)
            time.sleep(0.1)  # 避免请求太快

        return results

    def confirm_settlement(self):
        """确认结算单"""
        req = CThostFtdcSettlementInfoConfirmField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        self.trader_api.ReqSettlementInfoConfirm(req, self.trader_api.state.next_request_id())

    def query_instrument(self, code=""):
        """查询合约"""
        req = CThostFtdcQryInstrumentField()
        if code:
            req.InstrumentID = code
        self.trader_api.ReqQryInstrument(req, self.trader_api.state.next_request_id())

    def cancel_order(self, order_ref: str):
        """撤单 (M4: 发出后短等待 OnRtnOrder 把状态刷成 Canceled 以确认撤单结果)。"""
        order = self.trader_api.state.get_order(order_ref)
        if order is None:
            return False
        req = CThostFtdcInputOrderActionField()
        req.InstrumentID = order.InstrumentID
        req.OrderRef = order_ref
        req.FrontID = self.trader_api.front_id
        req.SessionID = self.trader_api.session_id
        req.ActionFlag = THOST_FTDC_AF_Delete
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id

        sent = (
            self.trader_api.ReqOrderAction(
                req, self.trader_api.state.next_request_id()
            )
            == 0
        )
        if not sent:
            return False
        # M4: 短等待撤单回报 (OnRtnOrder 会把 OrderStatus 刷成 Canceled)
        for _ in range(int(_CTP_CALLBACK_TIMEOUT * 10)):
            o = self.trader_api.state.get_order(order_ref)
            if o is not None and getattr(o, "OrderStatus", None) == THOST_FTDC_OST_Canceled:
                return True
            time.sleep(0.1)
        return sent  # 超时仍返回发送成功 (撤单请求已发, 状态未及确认)

    def OnRspSettlementInfoConfirm(
        self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast
    ):
        """结算单确认响应"""
        if pRspInfo and pRspInfo.ErrorID == 0:
            print("结算单确认成功")
        else:
            print(f"结算单确认失败：{pRspInfo.ErrorMsg if pRspInfo else '未知错误'}")

    def OnRspQryInstrument(self, pInstrument, pRspInfo, nRequestID, bIsLast):
        """合约查询响应"""
        if pInstrument:
            instrument_info = {
                "code": pInstrument.InstrumentID,
                "exchange_id": pInstrument.ExchangeID,
                "product_id": pInstrument.ProductID,
                "price_tick": pInstrument.PriceTick,
                "volume_multiple": pInstrument.VolumeMultiple,
                "max_market_order_volume": pInstrument.MaxMarketOrderVolume,
                "min_market_order_volume": pInstrument.MinMarketOrderVolume,
            }
            print(f"合约信息: {instrument_info}")

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        """报单录入请求响应"""
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"报单失败：{pRspInfo.ErrorMsg}")

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        """报单录入错误回报"""
        if pRspInfo:
            print(f"报单错误：{pRspInfo.ErrorMsg}")

    def OnRspOrderAction(self, pInputOrderAction, pRspInfo, nRequestID, bIsLast):
        """报单操作请求响应"""
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"撤单失败：{pRspInfo.ErrorMsg}")

    def OnErrRtnOrderAction(self, pOrderAction, pRspInfo):
        """报单操作错误回报"""
        if pRspInfo:
            print(f"撤单错误：{pRspInfo.ErrorMsg}")

    def query_trading_account(self):
        """查询资金账户"""
        req = CThostFtdcQryTradingAccountField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        self.trader_api.ReqQryTradingAccount(req, self.trader_api.state.next_request_id())

    def query_orders(self, code=""):
        """查询委托"""
        req = CThostFtdcQryOrderField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        if code:
            req.InstrumentID = code
        self.trader_api.ReqQryOrder(req, self.trader_api.state.next_request_id())

    def query_trades(self, code=""):
        """查询成交"""
        req = CThostFtdcQryTradeField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        if code:
            req.InstrumentID = code
        self.trader_api.ReqQryTrade(req, self.trader_api.state.next_request_id())

    def get_position(self, code: str) -> Dict:
        """获取单个合约的持仓"""
        positions = {}
        for key, pos in self.trader_api.state.get_positions_snapshot().items():
            if pos.InstrumentID == code:
                direction = "buy" if pos.PosiDirection == "2" else "sell"
                positions[direction] = {
                    "code": pos.InstrumentID,
                    "direction": direction,
                    "volume": pos.Position,
                    "price": pos.OpenPrice,
                    "margin": pos.UseMargin,
                    "profit": pos.PositionProfit,
                }
        return positions

    def get_all_positions(self) -> Dict:
        """获取所有持仓"""
        positions = {}
        for key, pos in self.trader_api.state.get_positions_snapshot().items():
            code = pos.InstrumentID
            direction = "buy" if pos.PosiDirection == "2" else "sell"
            if code not in positions:
                positions[code] = {}
            positions[code][direction] = {
                "code": code,
                "direction": direction,
                "volume": pos.Position,
                "price": pos.OpenPrice,
                "margin": pos.UseMargin,
                "profit": pos.PositionProfit,
            }
        return positions

    def save_to_pkl(self, key: str):
        """落盘前快照当前 order_ref, 供重启恢复 (H3-c)。"""
        self._ctp_order_ref_snapshot = self.trader_api.state.order_ref
        return super().save_to_pkl(key)

    def load_from_pkl(self, key: str, save_infos: dict = None):
        """恢复持仓后, 把 order_ref 推到持久化值 + 安全余量, 避免与历史 ref 撞号 (H3-c)。"""
        ok = super().load_from_pkl(key, save_infos)
        ref = getattr(self, "_ctp_order_ref_snapshot", None)
        # 恢复 order_ref, 并预留安全余量, 避免与本会话柜台已用 ref 撞号
        if ref:
            self.trader_api.state.restore_order_ref(int(ref) + 1000)
        return ok

    def get_positions(self) -> list:
        """以券商实时持仓为准, 组装 POSITION 列表供风控循环使用 (H1).

        方向经 mmd 子串传递 ("buy"/"sell"), 与 force_close/lock_position 判向一致。
        open_datetime/loss_price 券商不提供, 优先用本地 self.positions 同 code 回填,
        以便 check_position_time / check_stop_loss 可用。
        """
        # 先查询券商最新持仓
        qry_req = CThostFtdcQryInvestorPositionField()
        qry_req.BrokerID = self.ex.broker_id
        qry_req.InvestorID = self.ex.user_id
        self.trader_api.state.prepare_position_query()
        self.trader_api.ReqQryInvestorPosition(
            qry_req, self.trader_api.state.next_request_id()
        )
        if not self.trader_api.state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT):
            # 查询失败: 不臆造空列表 (会让风控误判无仓), 退回本地持仓快照
            return [p for p in self.positions.values() if p.amount != 0]

        # 建本地 code→pos 索引, 用于回填 open_datetime / loss_price
        local_by_code = {}
        for _p in self.positions.values():
            if _p.amount != 0:
                local_by_code.setdefault(_p.code, _p)

        result = []
        for key, pos_info in self.trader_api.state.get_positions_snapshot().items():
            if pos_info.Position == 0:
                continue
            code = pos_info.InstrumentID
            direction = "buy" if pos_info.PosiDirection == "2" else "sell"
            local = local_by_code.get(code)
            pos = POSITION(
                code=code,
                mmd=direction,
                type="做多" if direction == "buy" else "做空",
                price=pos_info.OpenPrice,
                amount=pos_info.Position,
                loss_price=(local.loss_price if local else None),
                open_datetime=(local.open_datetime if local else None),
            )
            result.append(pos)
        return result

    def check_stop_loss(self, pos: POSITION) -> bool:
        """固定价止损: 多头价跌破 loss_price / 空头价涨破 loss_price 返回 True (H1)。

        loss_price 为 None/0 (券商查询无本地回填) 时不触发, 交由策略级 ATR 止损处理。
        """
        if pos.loss_price is None or pos.loss_price == 0:
            return False
        tick = self.ex.ticks([pos.code])
        if pos.code not in tick:
            return False
        price = tick[pos.code].last
        if not (price == price and price > 0):  # NaN/非正价不判
            return False
        if "buy" in pos.mmd:
            return price < pos.loss_price
        if "sell" in pos.mmd:
            return price > pos.loss_price
        return False

    def check_position_time(self, pos: POSITION) -> bool:
        """持仓时间超 max_hold_days 返回 True (H1)。open_datetime 缺失时不触发。"""
        if self.max_hold_days is None or pos.open_datetime is None:
            return False
        delta = datetime.now() - pos.open_datetime
        return delta.days >= self.max_hold_days


if __name__ == "__main__":
    trader = CTPTrader("ctp_trader")

    try:
        # 确认结算单
        trader.confirm_settlement()

        # 查询账户资金
        trader.query_trading_account()
        time.sleep(1)

        # 查询持仓
        code = "rb2401"
        positions = trader.get_position(code)
        print(f"持仓信息: {positions}")

        # 开仓测试
        opt = Operation(code, "buy", "test", 0, {}, "测试买入")
        trade_res = trader.open_buy(code, opt, 1)
        print(f"开仓结果: {trade_res}")

        # 等待成交
        time.sleep(5)

        # 查询委托和成交
        trader.query_orders(code)
        trader.query_trades(code)

    except Exception as e:
        print(f"发生错误: {str(e)}")
    finally:
        trader.close()
