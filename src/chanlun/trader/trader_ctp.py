import copy
import os
import threading
import time
from datetime import datetime
from functools import wraps
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
    THOST_FTDC_OF_Close,  # 平仓 (平昨 / 不分今昨的交易所)
    THOST_FTDC_OF_CloseToday,  # 平今 (D1-HIGH-2: SHFE/INE 当日仓必须平今)
    THOST_FTDC_OF_Open,  # 开仓 (B2 follow-up: ApiStruct 迁移)
    THOST_FTDC_OPT_LimitPrice,  # 限价单
    THOST_FTDC_OST_AllTraded,  # 全部成交 (M3)
    THOST_FTDC_OST_Canceled,  # 已撤单 (M4 cancel_order 确认)
    THOST_FTDC_OST_NoTradeNotQueueing,  # 未成交且不再排队（拒单终态）
    THOST_FTDC_OST_PartTradedNotQueueing,  # 部分成交且余量不再排队
    THOST_FTDC_OST_PartTradedQueueing,  # 部分成交仍在队列 (M3)
)

# 报单/查询的回报等待超时 (秒). 取代原 time.sleep(1) 硬编码:
# 回报快时立即返回, 回报慢时也不超过 _CTP_CALLBACK_TIMEOUT 才放弃.
_CTP_CALLBACK_TIMEOUT = 3.0

_CTP_TERMINAL_ORDER_STATUSES = frozenset(
    {
        THOST_FTDC_OST_AllTraded,
        THOST_FTDC_OST_PartTradedNotQueueing,
        THOST_FTDC_OST_NoTradeNotQueueing,
        THOST_FTDC_OST_Canceled,
    }
)
_NO_POSITION_RECONCILIATION = object()

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

    返回 >0 表示已成交的手数 (AllTraded 全成 / PartTraded* 部分成交);
    返回 0 表示未成交 / 被拒；Canceled 仅表示余量已撤，若 VolumeTraded>0
    仍是撤单前累计真实成交，必须记账。

    VolumeTraded 是 CThostFtdcOrderField 的累计成交量字段; OrderStatus 仅做门控:
    AllTraded / PartTradedQueueing / PartTradedNotQueueing / Canceled 承认累计成交，
    其余 NoTrade/废单为 0。NoTradeQueueing 是活动 GFD 单，CTPState 会继续等待。
    """
    status = getattr(order, "OrderStatus", None)
    if status not in (
        THOST_FTDC_OST_AllTraded,
        THOST_FTDC_OST_PartTradedQueueing,
        THOST_FTDC_OST_PartTradedNotQueueing,
        THOST_FTDC_OST_Canceled,
    ):
        return 0
    traded = getattr(order, "VolumeTraded", 0) or 0
    try:
        return traded if traded > 0 else 0
    except TypeError:
        return 0


def _ctp_order_is_terminal(order) -> bool:
    return getattr(order, "OrderStatus", None) in _CTP_TERMINAL_ORDER_STATUSES


class _RejectedCTPOrder:
    """把异步插单拒绝转换成 CTPState 可结算的零成交终态。"""

    def __init__(self, input_order, error_msg) -> None:
        self.OrderRef = getattr(input_order, "OrderRef", "")
        self.InstrumentID = getattr(input_order, "InstrumentID", "")
        self.OrderStatus = THOST_FTDC_OST_NoTradeNotQueueing
        self.VolumeTraded = 0
        self.StatusMsg = str(error_msg or "CTP 异步插单拒绝")


def _ctp_code_operation_guard(busy_result):
    """同 code 操作从方法入口到结算 return 全程非阻塞互斥。"""

    def decorate(method):
        @wraps(method)
        def guarded(self, code, *args, **kwargs):
            state = self.trader_api.state
            is_ready = getattr(state, "is_reconciliation_ready", None)
            if callable(is_ready) and not is_ready():
                reason = getattr(state, "get_reconciliation_reason", lambda: "")()
                LogUtil.warning(
                    f"CTP {method.__name__} 恢复屏障熔断: code={code} reason={reason}"
                )
                return busy_result
            acquire = getattr(state, "acquire_order_operation", None)
            release = getattr(state, "release_order_operation", None)
            if not callable(acquire) or not callable(release):
                return method(self, code, *args, **kwargs)
            if not acquire(code):
                LogUtil.warning(
                    f"CTP {method.__name__} 并发熔断: code={code} 已有交易操作进行中"
                )
                return busy_result
            try:
                return method(self, code, *args, **kwargs)
            finally:
                release(code)

        return guarded

    return decorate


def _plan_close_offsets(exchange_id, amount, position, yd_position):
    """规划 CTP 平仓 offset, 返回 [(offset_flag, qty), ...](qty 之和 == amount)。审计 D1-HIGH-2。

    SHFE(上期所)/INE(能源中心)强制区分平今/平昨: 当日仓必须 CloseToday、昨仓 Close,
    用错 offset 柜台直接拒单 → H1 止损/强平对上期所当日仓失效。其它交易所(中金 CFFEX /
    大商 DCE / 郑商 CZCE)平仓不分今昨, 单 Close。

    today = position - yd_position(当日新开仓); 优先平今(日内策略当日仓为主)。position 信息
    缺失/为 0(快照取不到)时保守退回单 Close(不漏平, 与原 OF_Close 行为一致)。
    """
    amount = int(amount)
    if amount <= 0:
        return []
    if exchange_id not in ("SHFE", "INE") or not position:
        return [(THOST_FTDC_OF_Close, amount)]
    yd = max(0, int(yd_position or 0))
    today = max(0, int(position) - yd)
    plan = []
    close_today = min(amount, today)
    if close_today > 0:
        plan.append((THOST_FTDC_OF_CloseToday, close_today))
    rem = amount - close_today
    close_yd = min(rem, yd)
    if close_yd > 0:
        plan.append((THOST_FTDC_OF_Close, close_yd))
    rem -= close_yd
    if rem > 0:  # 兜底: position 与 amount 不符, 剩余用 Close(保守, 不漏平)
        plan.append((THOST_FTDC_OF_Close, rem))
    return plan


class MyTraderCallback(CThostFtdcTraderApi):
    """CTP交易回调"""

    def __init__(self, trader: Any) -> None:
        super().__init__()
        self.trader = trader
        self.connected: bool = False
        self.logged_in: bool = False
        self.authenticated: bool = False  # 添加认证状态
        self._has_logged_in_once: bool = False
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

    def OnFrontDisconnected(self, nReason):
        """断线后旧会话订单身份和回报完整性均不再可信，重新对账前禁止交易。"""
        self.connected = False
        self.logged_in = False
        self.authenticated = False
        self.state.require_reconciliation(f"front disconnected reason={nReason}")
        LogUtil.warning(f"CTP 交易前置断线 reason={nReason}，已启用恢复屏障")

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
            is_reconnect = self._has_logged_in_once
            self._has_logged_in_once = True
            self.logged_in = True
            self.front_id = pRspUserLogin.FrontID
            self.session_id = pRspUserLogin.SessionID
            self.state.require_reconciliation("login/reconnect requires reconciliation")
            print("交易服务器登录成功")
            if is_reconnect:
                schedule = getattr(self.trader, "schedule_reconcile_recovery", None)
                if callable(schedule):
                    schedule()
        else:
            print(f"交易服务器登录失败：{pRspInfo.ErrorMsg}")

    def OnRtnOrder(self, pOrder):
        """委托回报 (CTP callback 线程, B2 改走 state.set_order)"""
        print(f"委托回报: {pOrder.InstrumentID} {pOrder.OrderStatus}")
        self.state.set_order(pOrder.OrderRef, pOrder)
        self._persist_durable_state()

    def OnRtnTrade(self, pTrade):
        """成交回报"""
        print(
            f"成交回报: {pTrade.InstrumentID} 价格:{pTrade.Price} 数量:{pTrade.Volume}"
        )
        if self.state.set_trade(pTrade):
            self._persist_durable_state()

    def _persist_durable_state(self) -> None:
        """回调状态同步落盘；失败时维持进程但立即熔断后续交易。"""
        persist = getattr(self.trader, "_persist_ctp_state", None)
        if not callable(persist):
            return
        try:
            persist()
        except Exception as exc:
            self.state.require_reconciliation(f"CTP callback persistence failed: {exc}")
            LogUtil.warning(f"CTP 回调状态落盘失败，已启用恢复屏障: {exc}")

    def _mark_order_insert_rejected(self, input_order, rsp_info) -> None:
        """将 SDK 异步插单拒绝写成零成交终态，解除主线程等待。"""
        if input_order is None:
            return
        order_ref = getattr(input_order, "OrderRef", "")
        if not order_ref:
            return
        error_msg = getattr(rsp_info, "ErrorMsg", "CTP 异步插单拒绝")
        self.state.set_order(
            order_ref, _RejectedCTPOrder(input_order, error_msg)
        )
        self._persist_durable_state()
        LogUtil.warning(f"CTP 报单异步拒绝 ref={order_ref}: {error_msg}")

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        """报单录入请求响应；非零错误是不会产生正常 OnRtnOrder 的终态。"""
        if pRspInfo and getattr(pRspInfo, "ErrorID", 0) != 0:
            self._mark_order_insert_rejected(pInputOrder, pRspInfo)

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        """交易所异步报单错误回报。"""
        self._mark_order_insert_rejected(pInputOrder, pRspInfo)

    def OnRspQryInvestorPosition(
        self, pInvestorPosition, pRspInfo, nRequestID, bIsLast
    ):
        """持仓查询回报 (CTP callback 线程).

        B2: set_position 加锁写入.
        B2 follow-up: bIsLast=True 时唤醒等待中的主线程, 取代 time.sleep(1).
        """
        if pRspInfo and getattr(pRspInfo, "ErrorID", 0) != 0:
            self.state.mark_position_query_failed(nRequestID)
            LogUtil.warning(
                f"CTP 持仓查询失败 request_id={nRequestID}: "
                f"{getattr(pRspInfo, 'ErrorMsg', '')}"
            )
            return
        if pInvestorPosition:
            key = f"{pInvestorPosition.InstrumentID}_{pInvestorPosition.PosiDirection}"
            self.state.set_position(key, pInvestorPosition, request_id=nRequestID)
        if bIsLast:
            self.state.mark_position_query_done(nRequestID)

    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        """权威委托查询回报；按 request_id 隔离迟到响应。"""
        if pRspInfo and getattr(pRspInfo, "ErrorID", 0) != 0:
            self.state.mark_order_query_failed(nRequestID)
            LogUtil.warning(
                f"CTP 委托查询失败 request_id={nRequestID}: "
                f"{getattr(pRspInfo, 'ErrorMsg', '')}"
            )
            return
        if pOrder is not None:
            ref = str(getattr(pOrder, "OrderRef", "") or "")
            if ref:
                self.state.set_queried_order(ref, pOrder, nRequestID)
        if bIsLast:
            self.state.mark_order_query_done(nRequestID)

    def OnRspQryTrade(self, pTrade, pRspInfo, nRequestID, bIsLast):
        """权威成交查询回报；成交键幂等，重复回放不会重复入账。"""
        if pRspInfo and getattr(pRspInfo, "ErrorID", 0) != 0:
            self.state.mark_trade_query_failed(nRequestID)
            LogUtil.warning(
                f"CTP 成交查询失败 request_id={nRequestID}: "
                f"{getattr(pRspInfo, 'ErrorMsg', '')}"
            )
            return
        if pTrade is not None:
            self.state.set_queried_trade(pTrade, nRequestID)
        if bIsLast:
            self.state.mark_trade_query_done(nRequestID)


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

        self._reconcile_schedule_lock = threading.Lock()
        self._reconcile_thread = None
        self._reconcile_retry_delay = 1.0

        # 初始化交易接口
        self.trader_api = MyTraderCallback(self)
        self.trader_api.state.require_reconciliation(
            "startup requires authoritative reconciliation"
        )
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

    def _execute_position_query(
        self, request, reconcile_scope=_NO_POSITION_RECONCILIATION
    ) -> bool:
        """在全局互斥窗口内执行一次持仓查询，防共享 Event/epoch 串扰。"""
        state = self.trader_api.state
        acquire = getattr(state, "acquire_position_query", None)
        release = getattr(state, "release_position_query", None)
        locked = callable(acquire) and callable(release)
        if locked:
            acquire()
        request_id = state.next_request_id()
        completed = False
        try:
            if reconcile_scope is _NO_POSITION_RECONCILIATION:
                state.prepare_position_query(request_id=request_id)
            else:
                state.begin_position_query(
                    scope_code=reconcile_scope, request_id=request_id
                )
            result = self.trader_api.ReqQryInvestorPosition(
                request, request_id
            )
            if result != 0:
                return False
            completed = state.wait_for_position_query(_CTP_CALLBACK_TIMEOUT)
            return completed
        finally:
            if not completed:
                abort = getattr(state, "abort_position_query", None)
                if callable(abort):
                    abort(request_id)
            if locked:
                release()

    def query_broker_position(self, code):
        """N2: CTP 覆写基类(基类走 self.ex.positions, 对 MarketCTP=raise → reconcile 恒 no-op)。

        走 trader_api 真查询该 code 持仓, 返回 ("ok", [Position!=0 持仓]) 或查询失败 ("fail", None),
        使 reconcile_positions / _broker_already_holds 对 CTP 生效。
        """
        try:
            qry_req = CThostFtdcQryInvestorPositionField()
            qry_req.BrokerID = self.ex.broker_id
            qry_req.InvestorID = self.ex.user_id
            qry_req.InstrumentID = code
            if not self._execute_position_query(qry_req, reconcile_scope=code):
                return ("fail", None)
            snap = self.trader_api.state.get_positions_snapshot()
            held = [
                _p
                for _p in snap.values()
                if getattr(_p, "InstrumentID", None) == code
                and getattr(_p, "Position", 0) != 0
            ]
            return ("ok", held)
        except Exception as e:
            if self.log:
                self.log(f"{code} CTP 持仓查询失败: {e}")
            return ("fail", None)

    def _cancel_alive_orders_before_submit(self, code: str, action: str) -> bool:
        """新报单前清理同合约活动单；任一撤单未确认即熔断本轮。"""
        state = self.trader_api.state
        get_quarantine = getattr(
            state, "get_order_reconciliation_required", None
        )
        quarantined = get_quarantine(code) if callable(get_quarantine) else {}
        acknowledge = getattr(state, "acknowledge_order_reconciliation", None)
        mark_quarantine = getattr(state, "mark_order_reconciliation_required", None)

        # 先处理已到终态的隔离项：最终累计量未超过当前调用链已入账量，可安全解隔离。
        for order_ref, accounted in list(quarantined.items()):
            latest = state.get_order(order_ref)
            if not _ctp_order_is_terminal(latest):
                continue
            if _ctp_order_filled_amount(latest) <= accounted and callable(acknowledge):
                acknowledge(code, order_ref)
                quarantined.pop(order_ref, None)

        # quarantine 只能阻止新单，不能阻止继续撤仍活动的旧 GFD 单。
        for order_ref, old_order in state.get_alive_orders(code):
            accounted = quarantined.get(order_ref)
            confirmed = self.cancel_order(order_ref)
            latest = state.get_order(order_ref) or old_order
            filled = _ctp_order_filled_amount(latest)
            if confirmed and _ctp_order_is_terminal(latest):
                if accounted is not None and filled <= accounted:
                    if callable(acknowledge):
                        acknowledge(code, order_ref)
                    quarantined.pop(order_ref, None)
                    continue
                if accounted is None and filled <= 0:
                    continue
                required = accounted if accounted is not None else 0
                reason = f"旧活动单撤前存在未入账成交={filled - required}"
            else:
                required = accounted if accounted is not None else 0
                reason = "旧活动单撤单未确认"
            if callable(mark_quarantine):
                mark_quarantine(code, order_ref, required)
            quarantined[order_ref] = required
            LogUtil.warning(
                f"CTP {action} 拒绝新单: {reason} code={code} ref={order_ref}"
            )

        quarantined = get_quarantine(code) if callable(get_quarantine) else quarantined
        if quarantined:
            reason = f"存在待权威对账订单 {quarantined}"
            LogUtil.warning(f"CTP {action} 拒绝新单: {reason} code={code}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒", [f"{action} 已熔断：{reason} {code}"]
            )
            return False
        return True

    def _broker_direction_matches_local(self, code: str, direction: str) -> bool:
        """仅当券商已有仓与本地同方向数量完全一致时，允许部分开仓补单。"""
        wanted_posi_direction = "2" if direction == "buy" else "3"
        broker_amount = 0.0
        for position in self.trader_api.state.get_positions_snapshot().values():
            if getattr(position, "InstrumentID", None) != code:
                continue
            amount = float(getattr(position, "Position", 0) or 0)
            if amount == 0:
                continue
            if getattr(position, "PosiDirection", None) != wanted_posi_direction:
                return False
            broker_amount += amount
        local_amount = 0.0
        for position in getattr(self, "positions", {}).values():
            if getattr(position, "code", None) != code:
                continue
            mmd = str(getattr(position, "mmd", "") or "")
            if direction in mmd:
                local_amount += float(getattr(position, "amount", 0) or 0)
        return broker_amount > 0 and abs(broker_amount - local_amount) <= 1e-9

    @_ctp_code_operation_guard(False)
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
        if not self._execute_position_query(qry_req, reconcile_scope=code):
            return False

        if self.trader_api.state.get_position_count() >= self.max_pos:
            return False

        # N1: 同 code 券商已持仓则不重复开(与 HK/currency/futures 对齐), 复用上面已查持仓快照。
        # 防崩溃/丢盘重启后 self.positions 空但券商有仓时, 下一 tick 对同合约二次真单致持仓翻倍。
        broker_holds_code = any(
            getattr(_p, "InstrumentID", None) == code and getattr(_p, "Position", 0) != 0
            for _p in self.trader_api.state.get_positions_snapshot().values()
        )
        if broker_holds_code and not self._broker_direction_matches_local(code, "buy"):
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
        if not self._cancel_alive_orders_before_submit(code, "open_buy"):
            return False
        # 下单
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref, code)
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
            self.trader_api.state.discard_order_wait(order_ref)
            return False
        self.trader_api.state.mark_order_submitted(order_ref)

        order = self._wait_for_order_settlement(order_ref, code, "open_buy")
        if not order:
            return False

        # 必须在部分成交撤余单/超时撤单完成后重读最新累计成交量。
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

        return {
            "price": tick[code].last,
            "amount": filled,
            "requested_amount": _amt,
        }

    @_ctp_code_operation_guard(False)
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
        if not self._execute_position_query(qry_req, reconcile_scope=code):
            return False

        if self.trader_api.state.get_position_count() >= self.max_pos:
            return False

        # N1: 同 code 券商已持仓则不重复开(与 HK/currency/futures 对齐), 复用上面已查持仓快照。
        # 防崩溃/丢盘重启后 self.positions 空但券商有仓时, 下一 tick 对同合约二次真单致持仓翻倍。
        broker_holds_code = any(
            getattr(_p, "InstrumentID", None) == code and getattr(_p, "Position", 0) != 0
            for _p in self.trader_api.state.get_positions_snapshot().values()
        )
        if broker_holds_code and not self._broker_direction_matches_local(code, "sell"):
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
        if not self._cancel_alive_orders_before_submit(code, "open_sell"):
            return False
        # 下单
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref, code)
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
            self.trader_api.state.discard_order_wait(order_ref)
            return False
        self.trader_api.state.mark_order_submitted(order_ref)

        order = self._wait_for_order_settlement(order_ref, code, "open_sell")
        if not order:
            return False

        # 必须在部分成交撤余单/超时撤单完成后重读最新累计成交量。
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

        return {
            "price": tick[code].last,
            "amount": filled,
            "requested_amount": _amt,
        }

    def _ctp_pos_meta(self, code, posi_direction):
        """从持仓快照取 (exchange_id, position, yd_position) 供平今平昨规划(审计 D1-HIGH-2)。

        取不到返回 ("",0,0) → _plan_close_offsets 退回单 Close(与原 OF_Close 行为一致)。
        posi_direction: 多仓 "2" / 空仓 "3"(CThostFtdcInvestorPositionField.PosiDirection)。
        注: 风控强平路径(reboot 先调 get_positions 全量查询)快照新鲜, SHFE 拆腿可靠;
        信号驱动平仓若快照陈旧则退回单 Close(不更差)。
        """
        try:
            snap = self.trader_api.state.get_positions_snapshot()
            info = snap.get(f"{code}_{posi_direction}")
        except Exception:
            info = None
        if info is None:
            return ("", 0, 0)
        return (
            str(getattr(info, "ExchangeID", "") or ""),
            int(getattr(info, "Position", 0) or 0),
            int(getattr(info, "YdPosition", 0) or 0),
        )

    @_ctp_code_operation_guard(0)
    def _send_close_leg(self, code, direction_flag, qty, offset_flag, price):
        """发一笔平仓腿(指定 offset+qty), 返回实际成交量(0=失败)。封装 precheck/register/
        build/insert/wait/settle_part_traded/M3 成交判定。供平今平昨计划循环调用(审计 D1-HIGH-2)。"""
        if qty <= 0:
            return 0
        if not _precheck_ctp_order(qty, price):
            LogUtil.warning(f"CTP 平仓腿拒单: 异常 qty={qty} price={price} code={code}")
            return 0
        if not self._cancel_alive_orders_before_submit(
            code, f"平仓腿 offset={offset_flag}"
        ):
            return 0
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref, code)
        req = CThostFtdcInputOrderField()
        req.InstrumentID = code
        req.OrderPriceType = THOST_FTDC_OPT_LimitPrice
        req.Direction = direction_flag
        req.CombOffsetFlag = offset_flag
        req.CombHedgeFlag = THOST_FTDC_HF_Speculation
        req.LimitPrice = price
        req.VolumeTotalOriginal = qty
        req.TimeCondition = THOST_FTDC_TC_GFD
        req.VolumeCondition = THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = THOST_FTDC_CC_Immediately
        req.OrderRef = order_ref
        if self.trader_api.ReqOrderInsert(req, 0) != 0:
            self.trader_api.state.discard_order_wait(order_ref)
            return 0
        self.trader_api.state.mark_order_submitted(order_ref)
        order = self._wait_for_order_settlement(
            order_ref, code, f"平仓腿 offset={offset_flag}"
        )
        if not order:
            return 0
        filled = _ctp_order_filled_amount(order)
        return filled

    @_ctp_code_operation_guard(False)
    def close_buy(self, code, pos: POSITION, opt):
        """平多仓(D1-HIGH-2: SHFE/INE 按平今平昨拆腿; 其它交易所单 Close)。"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False
        price = tick[code].last
        # 下单前防御:挡 NaN/负持仓量、非正价格穿透到券商
        if not _precheck_ctp_order(pos.amount, price):
            LogUtil.warning(f"CTP close_buy 拒单:异常 pos.amount={pos.amount}、price={price}")
            return False
        exch, position, yd = self._ctp_pos_meta(code, "2")  # 多仓 PosiDirection=2
        total = 0
        for offset_flag, qty in _plan_close_offsets(exch, pos.amount, position, yd):
            total += self._send_close_leg(code, THOST_FTDC_D_Sell, qty, offset_flag, price)
            if self.trader_api.state.get_alive_orders(code):
                LogUtil.warning(
                    f"CTP close_buy 中止后续平仓腿: 尚有未终结订单 code={code}"
                )
                break
        if total <= 0:
            # M3: 该平没平掉是高危, 告警 + return False 让 execute 不清本地仓(避免裸持失管)
            LogUtil.warning(f"CTP close_buy 未成交(平多失败) code={code}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒", [f"平多未成交/被拒(持仓未平!) {code}"]
            )
            return False
        db.order_save("futures", code, code, "sell", price, total, opt.msg, datetime.now())
        utils.send_fs_msg(
            "futures_trader", "期货交易提醒",
            [f"期货平多 {code} 价格 {price} 数量 {total} 原因 {opt.msg}"],
        )
        return {"price": price, "amount": total}

    @_ctp_code_operation_guard(False)
    def close_sell(self, code, pos: POSITION, opt):
        """平空仓"""
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 下单前防御:挡 NaN/负持仓量、非正价格穿透到券商(pos.amount 异常=数据问题)
        if not _precheck_ctp_order(pos.amount, tick[code].last):
            LogUtil.warning(f"CTP close_sell 拒单:异常 pos.amount={pos.amount}、price={tick[code].last}")
            return False
        price = tick[code].last
        exch, position, yd = self._ctp_pos_meta(code, "3")  # 空仓 PosiDirection=3
        total = 0
        for offset_flag, qty in _plan_close_offsets(exch, pos.amount, position, yd):
            total += self._send_close_leg(code, THOST_FTDC_D_Buy, qty, offset_flag, price)
            if self.trader_api.state.get_alive_orders(code):
                LogUtil.warning(
                    f"CTP close_sell 中止后续平仓腿: 尚有未终结订单 code={code}"
                )
                break
        if total <= 0:
            # M3: 该平没平掉是高危, 告警 + return False 让 execute 不清本地仓
            LogUtil.warning(f"CTP close_sell 未成交(平空失败) code={code}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒", [f"平空未成交/被拒(持仓未平!) {code}"]
            )
            return False
        db.order_save("futures", code, code, "buy", price, total, opt.msg, datetime.now())
        utils.send_fs_msg(
            "futures_trader", "期货交易提醒",
            [f"期货平空 {code} 价格 {price} 数量 {total} 原因 {opt.msg}"],
        )
        return {"price": price, "amount": total}

    @_ctp_code_operation_guard(False)
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
        if not self._execute_position_query(qry_req, reconcile_scope=code):
            return False

        if not self._cancel_alive_orders_before_submit(code, "lock_position"):
            return False

        # 根据持仓方向决定锁仓方向
        order_ref = self.trader_api.state.next_order_ref()
        self.trader_api.state.register_order_wait(order_ref, code)
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
            self.trader_api.state.discard_order_wait(order_ref)
            return False
        self.trader_api.state.mark_order_submitted(order_ref)

        order = self._wait_for_order_settlement(order_ref, code, "lock_position")
        if not order:
            return False
        filled = _ctp_order_filled_amount(order)
        if filled <= 0:
            status = getattr(order, "OrderStatus", "?")
            LogUtil.warning(
                f"CTP lock_position 未成交 code={code} ref={order_ref} "
                f"OrderStatus={status}"
            )
            utils.send_fs_msg(
                "futures_trader",
                "期货交易提醒",
                [f"锁仓未成交/被拒 {code} 状态={status}"],
            )
            return False

        db.order_save(
            "futures",
            code,
            code,
            direction,
            tick[code].last,
            filled,
            f"锁仓:{opt.msg}",
            datetime.now(),
        )

        msg = f"期货锁仓 {code} 方向:{direction} 价格:{tick[code].last} 数量:{filled} 原因:{opt.msg}"
        utils.send_fs_msg("futures_trader", "期货交易提醒", [msg])

        return {"price": tick[code].last, "amount": filled}

    def _sync_positions_after_force_close(
        self, code: str, pos: POSITION, filled_amount: float
    ):
        """force_close 后按实际成交量扣减本地账本，完全平仓才归档。

        force_close 只向柜台发平仓单 + db.order_save, 从不碰 self.positions;而
        execute() 开仓守卫(backtest_trader.py:917-923)读 self.positions[open_uid]
        的 amount/now_pos_rate 判是否已满仓而静默 return True。强平后旧条目残留
        amount!=0/now_pos_rate>=1 会永久拉黑同 code+方向重开, 且 get_positions()
        按 code 回填(:988-990 amount!=0 过滤)会拿僵尸 loss_price/open_datetime
        污染同 code 新仓。部分成交若直接清零又会丢掉未平余仓，因此按 filled_amount
        逐条扣减；只有 amount 归零才写 close 字段并归档，随后立即落盘。
        """
        want = "buy" if "buy" in pos.mmd else "sell"
        changed = False
        remaining = max(0, filled_amount)
        for _uid, _p in list(getattr(self, "positions", {}).items()):
            if remaining <= 0:
                break
            if _p.code != code or _p.amount <= 0 or want not in _p.mmd:
                continue
            before_amount = _p.amount
            before_balance = getattr(_p, "balance", 0) or 0
            deducted = min(before_amount, remaining)
            _p.amount = max(0, before_amount - deducted)
            remaining -= deducted
            _p.balance = (
                0
                if _p.amount == 0
                else before_balance * (_p.amount / before_amount)
            )
            before_rate = getattr(_p, "now_pos_rate", 0) or 0
            _p.now_pos_rate = (
                0
                if _p.amount == 0
                else before_rate * (_p.amount / before_amount)
            )
            if _p.amount == 0:
                _p.close_msg = f"风控强平账本同步:{code}"
                _p.close_datetime = datetime.now()
                if _p.code not in self.positions_history:
                    self.positions_history[_p.code] = []
                self.positions_history[_p.code].append(copy.deepcopy(_p))
            changed = True
        if remaining > 0:
            LogUtil.warning(
                f"force_close 账本同步量超出本地匹配持仓 code={code} excess={remaining}"
            )
        if changed:
            _key = getattr(self, "_pkl_key", None)
            if _key:
                try:
                    self.save_to_pkl(_key)
                except Exception as _e:
                    LogUtil.warning(f"force_close 账本同步落盘失败 code={code}: {_e}")
        return changed

    @_ctp_code_operation_guard(False)
    def force_close(self, code: str, pos: POSITION, opt: Operation):
        """强制平仓
        使用对手价强平，提高成交概率
        """
        tick = self.ex.ticks([code])
        if code not in tick:
            return False

        # 根据持仓方向决定平仓方向和对手价
        # H2: POSITION 无 direction 字段, 方向经 mmd 子串判 ("buy" in mmd / "sell" in mmd)
        if "buy" in pos.mmd:
            direction = THOST_FTDC_D_Sell  # 平多仓=卖出
            # C18: 对手价=买一 buy1(bid1)。卖出限价单须<=对方买价才即时成交; 止损场景价
            # 逆行(下跌), 挂卖一 sell1 会排在卖方队列永不成交(强平失败裸奔)。对手价=买一。
            price = tick[code].buy1
            posi_direction = "2"  # 多仓
        else:
            direction = THOST_FTDC_D_Buy  # 平空仓=买入
            # C18: 对手价=卖一 sell1(ask1)。买入限价单须>=对方卖价才即时成交; 止损场景价
            # 逆行(上涨), 挂买一 buy1 会排在买方队列永不成交。对手价=卖一。
            price = tick[code].sell1
            posi_direction = "3"  # 空仓

        # D1-HIGH-2: SHFE/INE 风控强平按平今平昨拆腿(原单 OF_Close 对上期所当日仓被柜台拒
        # → H1 止损/超时强平对上期所失效)。其它交易所单 Close。
        exch, position, yd = self._ctp_pos_meta(code, posi_direction)
        direction_str = "sell" if direction == THOST_FTDC_D_Sell else "buy"
        total = 0
        for offset_flag, qty in _plan_close_offsets(exch, pos.amount, position, yd):
            total += self._send_close_leg(code, direction, qty, offset_flag, price)
            if self.trader_api.state.get_alive_orders(code):
                LogUtil.warning(
                    f"CTP force_close 中止后续平仓腿: 尚有未终结订单 code={code}"
                )
                break
        if total <= 0:
            # 强平失败=该砍的仓没砍掉, 高危: 告警(原实现不查成交直接返回成功, 是潜在 bug)
            LogUtil.warning(f"CTP force_close 未成交(强平失败!) code={code} 方向={direction_str}")
            utils.send_fs_msg(
                "futures_trader", "期货交易提醒",
                [f"强平未成交/被拒(持仓未平!) {code} {direction_str}"],
            )
            return False
        db.order_save(
            "futures", code, code, direction_str, price, total,
            f"强平:{opt.msg}", datetime.now(),
        )
        # 终检R13-#1: 强平成交后同步本地 self.positions 账本(清零+归档+落盘),
        # 否则残留僵尸条目永久拉黑同 code+方向重开并污染同 code 新仓判据。
        self._sync_positions_after_force_close(code, pos, total)
        utils.send_fs_msg(
            "futures_trader", "期货交易提醒",
            [f"期货强平 {code} 方向:{direction_str} 价格:{price} 数量:{total} 原因:{opt.msg}"],
        )
        return {"price": price, "amount": total}

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
        if not self._execute_position_query(req, reconcile_scope=None):
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

    def _wait_for_order_settlement(self, order_ref: str, code: str, action: str):
        """等待可结算回报；超时也撤单并返回柜台最新订单快照。"""
        if not self.trader_api.state.wait_for_order(
            order_ref, _CTP_CALLBACK_TIMEOUT
        ):
            return self._cancel_timed_out_order(order_ref, code, action)
        order = self.trader_api.state.get_order(order_ref)
        if order is None:
            return None
        return self._settle_part_traded(order, order_ref, code)

    def _settle_part_traded(self, order, order_ref, code):
        """审计 D1-HIGH-3: 部分成交(PartTradedQueueing)时剩余仍是活动 GFD 挂单, 稍后续成会致
        券商持仓 > 本地账本。立即撤剩余未成挂单，并在撤单返回后重读累计成交量；
        非部分成交(AllTraded/拒单/已撤)原样返回。
        """
        if getattr(order, "OrderStatus", None) == THOST_FTDC_OST_PartTradedQueueing:
            confirmed = self.cancel_order(order_ref)
            latest = self.trader_api.state.get_order(order_ref) or order
            result = "撤单已确认" if confirmed else "撤单未确认"
            filled = _ctp_order_filled_amount(latest)
            if not confirmed:
                self.trader_api.state.mark_order_reconciliation_required(
                    code, order_ref, filled
                )
            LogUtil.warning(
                f"CTP 部分成交 code={code} ref={order_ref}, {result}"
                f" 累计成交={filled}"
            )
            return latest
        return order

    def _cancel_timed_out_order(self, order_ref: str, code: str, action: str):
        """回报超时后撤活动单，并返回撤单/边界回报后的最新累计状态。"""
        latest = self.trader_api.state.get_order(order_ref)
        terminal_before_cancel = _ctp_order_is_terminal(latest)
        confirmed = False
        if not terminal_before_cancel:
            confirmed = self.cancel_order(order_ref)
        latest = self.trader_api.state.get_order(order_ref) or latest
        terminal_after_cancel = _ctp_order_is_terminal(latest)
        if terminal_after_cancel or confirmed:
            self.trader_api.state.discard_order_wait(order_ref)
        if terminal_after_cancel:
            result = "订单终态已确认"
        elif confirmed:
            result = "撤单已确认"
        else:
            # 未确认必须保留 pending，供 get_alive_orders 下一轮重试。
            result = "撤单未确认"
        filled = _ctp_order_filled_amount(latest)
        if not terminal_after_cancel and not confirmed:
            self.trader_api.state.mark_order_reconciliation_required(
                code, order_ref, filled
            )
        LogUtil.warning(
            f"CTP {action} 回报超时, {result} ref={order_ref} code={code} "
            f"累计成交={filled}"
        )
        return latest

    def cancel_order(self, order_ref: str):
        """撤单 (M4: 发出后短等待 OnRtnOrder 把状态刷成 Canceled 以确认撤单结果)。"""
        order = self.trader_api.state.get_order(order_ref)
        instrument_id = getattr(order, "InstrumentID", None)
        if not instrument_id:
            instrument_id = self.trader_api.state.get_order_instrument(order_ref)
        if not instrument_id:
            LogUtil.warning(
                f"CTP 无法撤单: 缺少 InstrumentID, ref={order_ref} (请求未发)"
            )
            return False
        req = CThostFtdcInputOrderActionField()
        req.InstrumentID = instrument_id
        exchange_id = getattr(order, "ExchangeID", None)
        order_sys_id = getattr(order, "OrderSysID", None)
        if exchange_id and order_sys_id:
            # 重启/重连后 FrontID+SessionID 已变化，优先使用交易所订单身份撤单。
            req.ExchangeID = exchange_id
            req.OrderSysID = order_sys_id
        else:
            req.OrderRef = order_ref
            req.FrontID = getattr(order, "FrontID", None) or self.trader_api.front_id
            req.SessionID = (
                getattr(order, "SessionID", None) or self.trader_api.session_id
            )
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
                self.trader_api.state.discard_order_wait(order_ref)
                return True
            time.sleep(0.1)
        # 审计 D1-LOW-1: 超时未确认 Canceled 返回 False(原 return True 让调用方误判已撤);
        # 升级告警让上层知晓撤单未确认(下轮 get_alive_orders 仍会再撤兜底)。
        LogUtil.warning(
            f"CTP 撤单未在超时内确认 Canceled, ref={order_ref} (请求已发, 状态未确认)"
        )
        return False

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
        """同步等待权威委托查询完成；失败/超时返回 False。"""
        state = self.trader_api.state
        state.acquire_order_query()
        request_id = state.next_request_id()
        try:
            state.begin_order_query(request_id, scope_code=code or None)
            req = CThostFtdcQryOrderField()
            req.BrokerID = self.ex.broker_id
            req.InvestorID = self.ex.user_id
            if code:
                req.InstrumentID = code
            if self.trader_api.ReqQryOrder(req, request_id) != 0:
                state.abort_order_query(request_id)
                return False
            if not state.wait_for_order_query(_CTP_CALLBACK_TIMEOUT):
                state.abort_order_query(request_id)
                return False
            return True
        finally:
            state.release_order_query()

    def query_trades(self, code=""):
        """同步等待权威成交查询完成；失败/超时返回 False。"""
        state = self.trader_api.state
        state.acquire_trade_query()
        request_id = state.next_request_id()
        try:
            state.begin_trade_query(request_id)
            req = CThostFtdcQryTradeField()
            req.BrokerID = self.ex.broker_id
            req.InvestorID = self.ex.user_id
            if code:
                req.InstrumentID = code
            if self.trader_api.ReqQryTrade(req, request_id) != 0:
                state.abort_trade_query(request_id)
                return False
            if not state.wait_for_trade_query(_CTP_CALLBACK_TIMEOUT):
                state.abort_trade_query(request_id)
                return False
            return True
        finally:
            state.release_trade_query()

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

    def _persist_ctp_state(self):
        """同步持久化订单/成交/隔离状态；无主 key 时仅刷新内存快照。"""
        self._ctp_order_ref_snapshot = self.trader_api.state.order_ref
        self._ctp_state_snapshot = self.trader_api.state.export_snapshot()
        key = getattr(self, "_pkl_key", None)
        if key is not None:
            BackTestTrader.save_to_pkl(self, key)
        return self._ctp_state_snapshot

    @staticmethod
    def _position_totals_from_broker(snapshot: Dict[str, Any]) -> Dict[tuple, float]:
        totals: Dict[tuple, float] = {}
        for position in snapshot.values():
            amount = float(getattr(position, "Position", 0) or 0)
            if amount == 0:
                continue
            direction = "buy" if getattr(position, "PosiDirection", None) == "2" else "sell"
            key = (getattr(position, "InstrumentID", ""), direction)
            totals[key] = totals.get(key, 0) + amount
        return totals

    def _position_totals_from_local(self) -> Dict[tuple, float]:
        totals: Dict[tuple, float] = {}
        for position in self.positions.values():
            amount = float(getattr(position, "amount", 0) or 0)
            if amount == 0:
                continue
            mmd = str(getattr(position, "mmd", "") or "")
            direction = "buy" if "buy" in mmd else "sell"
            key = (getattr(position, "code", ""), direction)
            totals[key] = totals.get(key, 0) + amount
        return totals

    def reconcile_recovery(self) -> bool:
        """重启/重连后的权威恢复门：订单、成交、挂单和持仓全部确认才放行。"""
        state = self.trader_api.state
        state.require_reconciliation("authoritative reconciliation in progress")

        if not self.query_orders():
            state.require_reconciliation("authoritative order query failed")
            return False

        alive = state.get_alive_orders()
        if alive:
            for order_ref, _order in alive:
                if not self.cancel_order(order_ref):
                    state.require_reconciliation(
                        f"active order cancel unconfirmed ref={order_ref}"
                    )
                    return False
            if not self.query_orders() or state.get_alive_orders():
                state.require_reconciliation("active orders remain after cancellation")
                return False

        known_trade_keys = set(state.get_trades_snapshot())
        if not self.query_trades():
            state.require_reconciliation("authoritative trade query failed")
            return False
        authoritative_trades = state.get_trades_snapshot()
        new_trade_keys = set(authoritative_trades) - known_trade_keys

        req = CThostFtdcQryInvestorPositionField()
        req.BrokerID = self.ex.broker_id
        req.InvestorID = self.ex.user_id
        if not self._execute_position_query(req, reconcile_scope=None):
            state.require_reconciliation("authoritative position query failed")
            return False

        broker = self._position_totals_from_broker(state.get_positions_snapshot())
        local = self._position_totals_from_local()
        all_keys = set(broker) | set(local)
        mismatches = {
            key: (local.get(key, 0), broker.get(key, 0))
            for key in all_keys
            if abs(local.get(key, 0) - broker.get(key, 0)) > 1e-9
        }
        if mismatches:
            state.require_reconciliation(f"position mismatch: {mismatches}")
            LogUtil.warning(f"CTP 恢复对账持仓不一致: {mismatches}")
            return False

        unresolved = {}
        quarantine = state.get_order_reconciliation_snapshot()
        for code, refs in quarantine.items():
            for order_ref, accounted in refs.items():
                matching_trades = {
                    key: trade
                    for key, trade in authoritative_trades.items()
                    if str(getattr(trade, "OrderRef", "") or "") == order_ref
                }
                trade_volume = sum(
                    float(getattr(trade, "Volume", 0) or 0)
                    for trade in matching_trades.values()
                )
                order = state.get_order(order_ref)
                order_volume = float(_ctp_order_filled_amount(order) or 0)
                authoritative_volume = max(trade_volume, order_volume)
                late_trade_ids = sorted(set(matching_trades) & new_trade_keys)
                if (
                    authoritative_volume > float(accounted) + 1e-9
                    or late_trade_ids
                    or (order is None and not matching_trades)
                ):
                    unresolved[(code, order_ref)] = {
                        "accounted": accounted,
                        "authoritative": authoritative_volume,
                        "new_trade_ids": late_trade_ids,
                    }
        if unresolved:
            state.require_reconciliation(f"unaccounted trade delta: {unresolved}")
            LogUtil.warning(f"CTP 恢复对账发现未入账成交，保持熔断: {unresolved}")
            return False

        if new_trade_keys:
            late_trades = {
                key: {
                    "order_ref": getattr(authoritative_trades[key], "OrderRef", ""),
                    "instrument": getattr(
                        authoritative_trades[key], "InstrumentID", ""
                    ),
                    "volume": getattr(authoritative_trades[key], "Volume", 0),
                }
                for key in sorted(new_trade_keys)
            }
            state.require_reconciliation(
                f"new trade ids require accounting replay: {late_trades}"
            )
            LogUtil.warning(
                f"CTP 恢复查询发现快照后新增成交，无法安全自动重放，保持熔断: {late_trades}"
            )
            return False

        for code in quarantine:
            state.acknowledge_order_reconciliation(code)
        state.complete_reconciliation()
        if getattr(self, "_pkl_key", None) is not None:
            self.save_to_pkl(self._pkl_key)
        return True

    def ensure_recovery_ready(self) -> bool:
        """生产启动门：恢复失败直接抛错，由启动脚本终止而非带病运行。"""
        if not self.reconcile_recovery():
            raise RuntimeError("CTP 恢复对账失败，交易保持熔断")
        return True

    def schedule_reconcile_recovery(self, max_attempts: int = 3) -> bool:
        """重连回调使用的单飞异步恢复；禁止在 SDK callback 线程内同步等待。"""
        lock = getattr(self, "_reconcile_schedule_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._reconcile_schedule_lock = lock
        with lock:
            current = getattr(self, "_reconcile_thread", None)
            if current is not None and current.is_alive():
                return False
            attempts = max(1, int(max_attempts))
            self._reconcile_thread = threading.Thread(
                target=self._run_reconcile_recovery_worker,
                args=(attempts,),
                name=f"CTPRecovery-{self.name}",
                daemon=True,
            )
            self._reconcile_thread.start()
            return True

    def _run_reconcile_recovery_worker(self, max_attempts: int) -> None:
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            try:
                if self.reconcile_recovery():
                    return
                last_error = "reconciliation returned False"
            except Exception as exc:
                last_error = str(exc)
                LogUtil.warning(
                    f"CTP 重连恢复异常 attempt={attempt}/{max_attempts}: {exc}"
                )
            if attempt < max_attempts:
                time.sleep(max(0, float(getattr(self, "_reconcile_retry_delay", 1.0))))
        self.trader_api.state.require_reconciliation(
            f"reconnect reconciliation exhausted after {max_attempts} attempts: {last_error}"
        )
        LogUtil.warning(
            f"CTP 重连恢复连续 {max_attempts} 次失败，交易保持熔断: {last_error}"
        )

    def save_to_pkl(self, key: str):
        """落盘前快照 order_ref 与纯字段订单/成交/隔离状态。"""
        self._ctp_order_ref_snapshot = self.trader_api.state.order_ref
        self._ctp_state_snapshot = self.trader_api.state.export_snapshot()
        return super().save_to_pkl(key)

    def load_from_pkl(self, key: str, save_infos: dict = None):
        """恢复持仓后, 把 order_ref 推到持久化值 + 安全余量, 避免与历史 ref 撞号 (H3-c)。"""
        ok = super().load_from_pkl(key, save_infos)
        snapshot = getattr(self, "_ctp_state_snapshot", None)
        if snapshot is not None:
            if not self.trader_api.state.import_snapshot(snapshot):
                self.trader_api.state.require_reconciliation(
                    "invalid persisted CTP state"
                )
        else:
            self.trader_api.state.require_reconciliation(
                "missing persisted CTP state"
            )
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
        # 审计 D1-HIGH-1: 风控读路径用 begin(全量 epoch reconciliation), 剔除券商已不返回的
        # 陈旧持仓键, 避免券商全平后残留的幽灵仓被喂给止损/超时强平。
        if not self._execute_position_query(qry_req, reconcile_scope=None):
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
