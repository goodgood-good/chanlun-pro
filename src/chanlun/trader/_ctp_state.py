"""线程安全的 CTP 交易状态容器 (B2 + B2 follow-up).

聚合 order_ref / orders / positions / events / request_id 的并发安全访问.

抽出原因:
1. ``order_ref`` 在多个交易方法中并发递增, 原始 ``self.order_ref += 1`` 不原子,
   高并发下可能产生重复 ref → 订单乱序 / 错误平仓
2. ``orders`` / ``positions`` 字典由 CTP SDK 的回调线程 (OnRtnOrder /
   OnRspQryInvestorPosition) 写入, 但主线程读取 + 迭代; 无锁时迭代可能抛
   "dictionary changed size during iteration"
3. CTP 报单后等待回报: 原 ``time.sleep(1)`` 硬编码轮询导致:
   - 回报快 (<<1s): 浪费时间
   - 回报慢 (>1s): 漏掉回报视为失败
   改用 ``threading.Event`` 由回调主动唤醒, 真正回报到达就立即返回
4. nRequestID (查询请求 ID) 不应与 order_ref (报单引用) 共用, 否则查询次数会
   错误地推进 order_ref → 实际下单时产生跳号
5. 状态从 ``MyTraderCallback`` 抽出后, 单元测试无需 openctp_ctp 依赖也能验证
   锁正确性 (parent class ``CThostFtdcTraderApi`` 是 C++ 扩展, 难以 mock)
"""

import threading
from types import SimpleNamespace
from typing import Any, Dict, Optional


# CTP OrderStatus 协议固定值。等待方只能在订单已可结算时被唤醒：
# - 0 AllTraded / 2 PartTradedNotQueueing / 4 NoTradeNotQueueing / 5 Canceled
#   都不会再留在撮合队列；
# - 1 PartTradedQueueing 已有真实成交，调用方需立即撤余单并按已成量记账。
# 3 NoTradeQueueing 仍是零成交活动 GFD 单，不能当作失败终态提前返回。
_ORDER_TERMINAL_STATUSES = frozenset({"0", "2", "4", "5"})
_ORDER_WAIT_READY_STATUSES = _ORDER_TERMINAL_STATUSES | {"1"}

_ORDER_SNAPSHOT_FIELDS = (
    "OrderRef",
    "InstrumentID",
    "OrderStatus",
    "VolumeTraded",
    "VolumeTotal",
    "VolumeTotalOriginal",
    "StatusMsg",
    "ExchangeID",
    "OrderSysID",
    "FrontID",
    "SessionID",
    "Direction",
    "CombOffsetFlag",
    "LimitPrice",
    "InsertDate",
    "InsertTime",
    "TradingDay",
)
_TRADE_SNAPSHOT_FIELDS = (
    "TradeID",
    "ExchangeID",
    "OrderSysID",
    "OrderRef",
    "InstrumentID",
    "Direction",
    "OffsetFlag",
    "Price",
    "Volume",
    "TradeDate",
    "TradeTime",
    "TradingDay",
    "SequenceNo",
)


def _snapshot_fields(value: Any, fields: tuple[str, ...]) -> Dict[str, Any]:
    """把不可可靠 pickle 的 CTP C++ 字段对象转换为纯 Python 标量字典。"""
    return {
        field: getattr(value, field)
        for field in fields
        if hasattr(value, field)
    }


def _order_is_ready_for_settlement(order: Any) -> bool:
    return (
        order is not None
        and getattr(order, "OrderStatus", None) in _ORDER_WAIT_READY_STATUSES
    )


def _order_is_terminal(order: Any) -> bool:
    return (
        order is not None
        and getattr(order, "OrderStatus", None) in _ORDER_TERMINAL_STATUSES
    )


def _order_progress_score(order: Any) -> tuple[int, float]:
    """比较同一 ref 的回报进度；终态优先，其次累计成交量。"""
    try:
        volume = float(getattr(order, "VolumeTraded", 0) or 0)
    except (TypeError, ValueError):
        volume = 0
    return (1 if _order_is_terminal(order) else 0, volume)


class CTPState:
    """CTP 交易状态的线程安全门面 (lock + event)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 报单引用计数器 (CTP OrderRef, 必须严格递增 + 唯一)
        self.order_ref: int = 0
        # 查询请求 ID 计数器 (CTP nRequestID, 与 order_ref 解耦)
        self._request_id: int = 0
        # 报单回报 (ref → CThostFtdcOrderField)
        self.orders: Dict[str, Any] = {}
        # 成交回报 (稳定成交键 → CThostFtdcTradeField)
        self.trades: Dict[str, Any] = {}
        # 持仓查询回报 (instrumentID_posiDirection → CThostFtdcInvestorPositionField)
        self.positions: Dict[str, Any] = {}
        # 每个 order_ref 的等待 Event, 回调到达时唤醒并移除
        self._order_events: Dict[str, threading.Event] = {}
        # 报单时保存的合约元数据。首个 OnRtnOrder 缺失时，撤单仍须能按
        # FrontID + SessionID + OrderRef + InstrumentID 构造请求。
        self._order_instruments: Dict[str, str] = {}
        # 只把 ReqOrderInsert 已返回成功、但尚无首报的 ref 暴露为 pending alive；
        # 避免注册与真正发单之间的并发窗口把“尚未发出”误当成活动单。
        self._submitted_order_refs: set[str] = set()
        # 已知/可能有成交但尚未完成权威对账的订单隔离表：code → {ref: 已入账累计量}。
        # 终态回报不会自动清除，只有显式对账/人工 ack 才能解除，避免 terminal 从
        # alive 消失后遗忘 post-return 成交增量。
        self._order_reconciliation_required: Dict[str, Dict[str, float]] = {}
        # 同一合约的完整交易操作非阻塞串行化。RLock 允许 close_buy →
        # _send_close_leg 同线程嵌套；其它线程直接失败，绝不排队复用旧持仓快照。
        self._order_operation_locks: Dict[str, Any] = {}
        # 持仓查询完成 Event (OnRspQryInvestorPosition 的 bIsLast 触发)
        self._position_query_event = threading.Event()
        # CTP 只有一个 position query Event/epoch；查询窗口必须全局串行，不能让
        # A 的 bIsLast 唤醒 B。RLock 便于同线程未来组合查询。
        self._position_query_lock = threading.RLock()
        self._position_query_request_id: Optional[int] = None
        self._position_query_success: Optional[bool] = None
        # epoch reconciliation (审计 D1-HIGH-1 幽灵仓): begin_position_query 开启时记本次查询
        # 返回的键, mark_position_query_done 据此剔除范围内未返回的陈旧键。None=未开启(open 单 code 路径)
        self._position_query_seen: Optional[set] = None
        self._position_query_scope: Optional[str] = None
        self._order_query_event = threading.Event()
        self._order_query_lock = threading.RLock()
        self._order_query_request_id: Optional[int] = None
        self._order_query_success: Optional[bool] = None
        self._order_query_seen: Optional[set[str]] = None
        self._order_query_baseline: Optional[set[str]] = None
        self._order_query_live_updates: Optional[set[str]] = None
        self._order_query_scope: Optional[str] = None
        self._trade_query_event = threading.Event()
        self._trade_query_lock = threading.RLock()
        self._trade_query_request_id: Optional[int] = None
        self._trade_query_success: Optional[bool] = None
        # 重启/断线后必须完成订单、成交、持仓权威对账才可重新交易。
        self._reconciliation_required = False
        self._reconciliation_reason = ""

    # ---------- order_ref ----------
    def next_order_ref(self) -> str:
        """原子递增 ``order_ref`` 并返回字符串形式 (CTP 报单要求 str)."""
        with self._lock:
            self.order_ref += 1
            return str(self.order_ref)

    def restore_order_ref(self, value: int) -> None:
        """进程重启后恢复 order_ref 到持久化值 (H3), 避免从 0 重来撞历史 ref。

        仅在 value 大于当前 order_ref 时推进, 保证单调不回退。
        """
        with self._lock:
            if value > self.order_ref:
                self.order_ref = value

    # ---------- request_id (查询/撤单) ----------
    def next_request_id(self) -> int:
        """独立于 order_ref 的请求 ID 计数器, 用于 nRequestID."""
        with self._lock:
            self._request_id += 1
            return self._request_id

    # ---------- per-code operation guard ----------
    def acquire_order_operation(self, code: str) -> bool:
        """非阻塞取得同 code 操作锁；False 表示已有线程正在交易该合约。"""
        with self._lock:
            operation_lock = self._order_operation_locks.get(code)
            if operation_lock is None:
                operation_lock = threading.RLock()
                self._order_operation_locks[code] = operation_lock
        return operation_lock.acquire(blocking=False)

    def release_order_operation(self, code: str) -> None:
        """释放由 acquire_order_operation 取得的同 code 操作锁。"""
        with self._lock:
            operation_lock = self._order_operation_locks.get(code)
        if operation_lock is not None:
            operation_lock.release()

    # ---------- global position-query guard ----------
    def acquire_position_query(self) -> None:
        """取得全局持仓查询锁，覆盖 prepare/begin → Req → bIsLast wait。"""
        self._position_query_lock.acquire()

    def release_position_query(self) -> None:
        self._position_query_lock.release()

    def acquire_order_query(self) -> None:
        self._order_query_lock.acquire()

    def release_order_query(self) -> None:
        self._order_query_lock.release()

    def acquire_trade_query(self) -> None:
        self._trade_query_lock.acquire()

    def release_trade_query(self) -> None:
        self._trade_query_lock.release()

    # ---------- orders ----------
    def get_order(self, ref: str) -> Optional[Any]:
        """线程安全读取 order; 返回 ``None`` 表示尚未收到回报。"""
        with self._lock:
            return self.orders.get(ref)

    def set_order(self, ref: str, order: Any) -> None:
        """写入 order；仅可结算回报才唤醒等待该 ref 的线程。"""
        with self._lock:
            self.orders[ref] = order
            if self._order_query_live_updates is not None:
                self._order_query_live_updates.add(str(ref))
            instrument_id = getattr(order, "InstrumentID", None)
            if instrument_id or _order_is_terminal(order):
                # 有 InstrumentID 时由 order 自身接管撤单元数据；终态即使字段缺失也
                # 已证明不再活动，必须无条件释放 pending，避免误报 alive/无界残留。
                self._order_instruments.pop(ref, None)
                self._submitted_order_refs.discard(ref)
            evt = None
            if _order_is_ready_for_settlement(order):
                evt = self._order_events.pop(ref, None)
        if evt is not None:
            evt.set()

    def register_order_wait(
        self, ref: str, instrument_id: Optional[str] = None
    ) -> threading.Event:
        """在调 ReqOrderInsert 之前注册 ref 的等待 Event.

        注意调用顺序:
            ref = state.next_order_ref()
            state.register_order_wait(ref, code) # 先注册并保存撤单所需合约
            api.ReqOrderInsert(req)              # 再发单
            state.wait_for_order(ref, timeout)   # 然后等待回报

        即使回报抢先到达，也会根据当前状态预置 Event；活动排队首报不会被
        误当作完成。instrument_id 用于回报完全缺失时的超时撤单兜底。
        """
        with self._lock:
            evt = threading.Event()
            if instrument_id:
                self._order_instruments[ref] = instrument_id
            if _order_is_ready_for_settlement(self.orders.get(ref)):
                evt.set()
            else:
                self._order_events[ref] = evt
            return evt

    def get_order_instrument(self, ref: str) -> Optional[str]:
        """返回报单时保存的合约；供尚无 OnRtnOrder 时构造撤单请求。"""
        with self._lock:
            return self._order_instruments.get(ref)

    def mark_order_submitted(self, ref: str) -> None:
        """ReqOrderInsert 返回成功后，将仍无首报的 ref 标记为可重试撤单。"""
        with self._lock:
            # 回调可能抢在 ReqOrderInsert 返回前到达并释放 metadata；此时 orders
            # 已接管状态，不能重新制造一个伪 pending。
            if ref in self._order_instruments:
                self._submitted_order_refs.add(ref)

    def discard_order_wait(self, ref: str) -> None:
        """释放不再可能收到有效首报的等待对象与预注册撤单元数据。"""
        with self._lock:
            self._order_events.pop(ref, None)
            self._order_instruments.pop(ref, None)
            self._submitted_order_refs.discard(ref)

    def mark_order_reconciliation_required(
        self, code: str, ref: str, accounted_volume: float = 0
    ) -> None:
        """隔离存在未知成交增量的 code，直至显式权威对账确认。"""
        with self._lock:
            refs = self._order_reconciliation_required.setdefault(code, {})
            refs[ref] = max(refs.get(ref, 0), accounted_volume or 0)

    def get_order_reconciliation_required(self, code: str) -> Dict[str, float]:
        with self._lock:
            return dict(self._order_reconciliation_required.get(code, {}))

    def acknowledge_order_reconciliation(
        self, code: str, ref: Optional[str] = None
    ) -> None:
        """仅供完成权威对账/人工确认后显式解除隔离。"""
        with self._lock:
            if ref is None:
                self._order_reconciliation_required.pop(code, None)
                return
            refs = self._order_reconciliation_required.get(code)
            if refs is None:
                return
            refs.pop(ref, None)
            if not refs:
                self._order_reconciliation_required.pop(code, None)

    def get_order_reconciliation_snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return {
                code: dict(refs)
                for code, refs in self._order_reconciliation_required.items()
            }

    # ---------- trades / durable recovery snapshot ----------
    @staticmethod
    def _trade_key(trade: Any) -> str:
        exchange_id = str(getattr(trade, "ExchangeID", "") or "")
        trade_id = str(getattr(trade, "TradeID", "") or "")
        if trade_id:
            return f"{exchange_id}:{trade_id}"
        fallback = (
            getattr(trade, "OrderRef", ""),
            getattr(trade, "InstrumentID", ""),
            getattr(trade, "TradeDate", ""),
            getattr(trade, "TradeTime", ""),
            getattr(trade, "Price", ""),
            getattr(trade, "Volume", ""),
        )
        return "fallback:" + ":".join(str(value) for value in fallback)

    def set_trade(self, trade: Any) -> bool:
        """幂等登记成交；True 表示首次见到，False 表示重复回报。"""
        key = self._trade_key(trade)
        with self._lock:
            if key in self.trades:
                return False
            self.trades[key] = trade
            return True

    def get_trades_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.trades)

    # ---------- authoritative order/trade queries ----------
    def begin_order_query(
        self, request_id: int, scope_code: Optional[str] = None
    ) -> threading.Event:
        with self._lock:
            self._order_query_request_id = request_id
            self._order_query_success = None
            self._order_query_seen = set()
            self._order_query_scope = scope_code
            self._order_query_baseline = {
                str(ref)
                for ref, order in self.orders.items()
                if not _order_is_terminal(order)
                and (
                    scope_code is None
                    or getattr(order, "InstrumentID", None) == scope_code
                )
            }
            self._order_query_baseline.update(
                str(ref)
                for ref, instrument in self._order_instruments.items()
                if ref in self._submitted_order_refs
                and (scope_code is None or instrument == scope_code)
            )
            self._order_query_live_updates = set()
            self._order_query_event.clear()
        return self._order_query_event

    def set_queried_order(self, ref: str, order: Any, request_id: int) -> bool:
        with self._lock:
            if request_id != self._order_query_request_id:
                return False
            if self._order_query_seen is not None:
                self._order_query_seen.add(str(ref))
            self.orders[str(ref)] = order
            return True

    def mark_order_query_done(self, request_id: int) -> bool:
        with self._lock:
            if request_id != self._order_query_request_id:
                return False
            seen = self._order_query_seen or set()
            baseline = self._order_query_baseline or set()
            live_updates = self._order_query_live_updates or set()
            # 权威查询未返回的旧活动单/无首报 pending 已不在柜台，不能继续当 alive；
            # 历史终态保留供审计。
            for ref in baseline - seen - live_updates:
                order = self.orders.get(ref)
                if order is not None and not _order_is_terminal(order):
                    self.orders.pop(ref, None)
                if ref in self._submitted_order_refs:
                    self._submitted_order_refs.discard(ref)
                    self._order_instruments.pop(ref, None)
            self._order_query_success = True
            self._order_query_seen = None
            self._order_query_baseline = None
            self._order_query_live_updates = None
            self._order_query_scope = None
            self._order_query_request_id = None
            self._order_query_event.set()
            return True

    def mark_order_query_failed(self, request_id: int) -> bool:
        with self._lock:
            if request_id != self._order_query_request_id:
                return False
            self._order_query_success = False
            self._order_query_seen = None
            self._order_query_baseline = None
            self._order_query_live_updates = None
            self._order_query_scope = None
            self._order_query_request_id = None
            self._order_query_event.set()
            return True

    def abort_order_query(self, request_id: int) -> None:
        with self._lock:
            if request_id == self._order_query_request_id:
                self._order_query_request_id = None
                self._order_query_success = None
                self._order_query_seen = None
                self._order_query_baseline = None
                self._order_query_live_updates = None
                self._order_query_scope = None
                self._order_query_event.clear()

    def wait_for_order_query(self, timeout: float) -> bool:
        if not self._order_query_event.wait(timeout):
            return False
        with self._lock:
            return self._order_query_success is True

    def begin_trade_query(self, request_id: int) -> threading.Event:
        with self._lock:
            self._trade_query_request_id = request_id
            self._trade_query_success = None
            self._trade_query_event.clear()
        return self._trade_query_event

    def set_queried_trade(self, trade: Any, request_id: int) -> bool:
        with self._lock:
            if request_id != self._trade_query_request_id:
                return False
            key = self._trade_key(trade)
            if key in self.trades:
                return False
            self.trades[key] = trade
            return True

    def mark_trade_query_done(self, request_id: int) -> bool:
        with self._lock:
            if request_id != self._trade_query_request_id:
                return False
            self._trade_query_success = True
            self._trade_query_request_id = None
            self._trade_query_event.set()
            return True

    def mark_trade_query_failed(self, request_id: int) -> bool:
        with self._lock:
            if request_id != self._trade_query_request_id:
                return False
            self._trade_query_success = False
            self._trade_query_request_id = None
            self._trade_query_event.set()
            return True

    def abort_trade_query(self, request_id: int) -> None:
        with self._lock:
            if request_id == self._trade_query_request_id:
                self._trade_query_request_id = None
                self._trade_query_success = None
                self._trade_query_event.clear()

    def wait_for_trade_query(self, timeout: float) -> bool:
        if not self._trade_query_event.wait(timeout):
            return False
        with self._lock:
            return self._trade_query_success is True

    def export_snapshot(self) -> Dict[str, Any]:
        """导出不含锁/Event/SDK C++ 对象的可持久化状态。"""
        with self._lock:
            return {
                "version": 1,
                "order_ref": self.order_ref,
                "orders": {
                    str(ref): _snapshot_fields(order, _ORDER_SNAPSHOT_FIELDS)
                    for ref, order in self.orders.items()
                },
                "trades": {
                    key: _snapshot_fields(trade, _TRADE_SNAPSHOT_FIELDS)
                    for key, trade in self.trades.items()
                },
                "order_instruments": dict(self._order_instruments),
                "submitted_order_refs": sorted(self._submitted_order_refs),
                "order_reconciliation_required": {
                    code: dict(refs)
                    for code, refs in self._order_reconciliation_required.items()
                },
                "reconciliation_required": self._reconciliation_required,
                "reconciliation_reason": self._reconciliation_reason,
            }

    def import_snapshot(
        self, snapshot: Any, *, require_reconciliation: bool = True
    ) -> bool:
        """恢复纯字段快照；运行时锁/Event 永远重新创建，不从磁盘反序列化。"""
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            return False
        orders = snapshot.get("orders") or {}
        trades = snapshot.get("trades") or {}
        if not isinstance(orders, dict) or not isinstance(trades, dict):
            return False
        with self._lock:
            try:
                saved_ref = int(snapshot.get("order_ref", 0) or 0)
            except (TypeError, ValueError):
                return False
            self.order_ref = max(self.order_ref, saved_ref)
            persisted_orders = {
                str(ref): SimpleNamespace(**dict(fields))
                for ref, fields in orders.items()
                if isinstance(fields, dict)
            }
            for ref, persisted_order in persisted_orders.items():
                current_order = self.orders.get(ref)
                if current_order is None or _order_progress_score(
                    persisted_order
                ) > _order_progress_score(current_order):
                    self.orders[ref] = persisted_order
            persisted_trades = {
                str(key): SimpleNamespace(**dict(fields))
                for key, fields in trades.items()
                if isinstance(fields, dict)
            }
            for key, persisted_trade in persisted_trades.items():
                self.trades.setdefault(key, persisted_trade)
            persisted_instruments = {
                str(ref): str(code)
                for ref, code in (snapshot.get("order_instruments") or {}).items()
            }
            for ref, code in persisted_instruments.items():
                self._order_instruments.setdefault(ref, code)
            persisted_submitted = {
                str(ref) for ref in (snapshot.get("submitted_order_refs") or [])
            }
            self._submitted_order_refs.update(persisted_submitted)
            persisted_quarantine = {
                str(code): {
                    str(ref): float(volume or 0) for ref, volume in refs.items()
                }
                for code, refs in (
                    snapshot.get("order_reconciliation_required") or {}
                ).items()
                if isinstance(refs, dict)
            }
            for code, refs in persisted_quarantine.items():
                current_refs = self._order_reconciliation_required.setdefault(code, {})
                for ref, volume in refs.items():
                    current_refs[ref] = max(current_refs.get(ref, 0), volume)
            # 已收到具体订单回报的 ref 不应因旧快照重新变成“无首报 pending”。
            for ref, order in self.orders.items():
                if getattr(order, "InstrumentID", None) or _order_is_terminal(order):
                    self._order_instruments.pop(ref, None)
                    self._submitted_order_refs.discard(ref)
            saved_required = bool(snapshot.get("reconciliation_required", False))
            self._reconciliation_required = require_reconciliation or saved_required
            self._reconciliation_reason = (
                "startup snapshot requires authoritative reconciliation"
                if require_reconciliation
                else str(snapshot.get("reconciliation_reason", "") or "")
            )
        return True

    # ---------- global recovery barrier ----------
    def require_reconciliation(self, reason: str) -> None:
        with self._lock:
            self._reconciliation_required = True
            self._reconciliation_reason = str(reason or "reconciliation required")

    def complete_reconciliation(self) -> None:
        with self._lock:
            self._reconciliation_required = False
            self._reconciliation_reason = ""

    def is_reconciliation_ready(self) -> bool:
        with self._lock:
            return not self._reconciliation_required

    def get_reconciliation_reason(self) -> str:
        with self._lock:
            return self._reconciliation_reason

    def wait_for_order(self, ref: str, timeout: float) -> bool:
        """等待 order 回报, 返回 True 表示已到达 / False 表示超时。

        Event 不存在时，仅当前回报已可结算才返回 True；NoTradeQueueing 等
        活动状态即使已写入 orders 也绝不能提前完成。
        """
        with self._lock:
            evt = self._order_events.get(ref)
            ready = _order_is_ready_for_settlement(self.orders.get(ref))
        if evt is None:
            return ready
        return evt.wait(timeout)

    # ---------- positions ----------
    def get_position_count(self) -> int:
        # 审计 D1-HIGH-1: 只计 Position!=0 的真实持仓。原 len(self.positions) 把已平仓的
        # 陈旧 0 仓键 / 历史交易过的合约键也计入 → max_pos 门控随交易过的合约数单调膨胀 →
        # 进程跑一段后 get_position_count>=max_pos 恒成立 → 永久拒绝一切新开仓(策略静默瘫痪)。
        with self._lock:
            return sum(
                1 for p in self.positions.values() if getattr(p, "Position", 0) != 0
            )

    def get_positions_snapshot(self) -> Dict[str, Any]:
        """返回 positions 浅拷贝, 调用方迭代时不受 callback 写入影响。"""
        with self._lock:
            return dict(self.positions)

    def set_position(
        self, key: str, position: Any, request_id: Optional[int] = None
    ) -> bool:
        with self._lock:
            if (
                request_id is not None
                and request_id != self._position_query_request_id
            ):
                return False
            self.positions[key] = position
            if self._position_query_seen is not None:
                self._position_query_seen.add(key)  # 审计 D1-HIGH-1: 记本次查询见到的键
            return True

    # ---------- alive orders (M4 挂单台账) ----------
    def get_alive_orders(self, code: str = None) -> list:
        """返回未终结的挂单 (NoTradeQueueing/PartTradedQueueing) (M4)。

        延迟 import openctp 常量, 使本模块在未装 openctp_ctp 时仍可 import (供单测)。
        返回 [(ref, order), ...]；首报缺失的已提交订单以 (ref, None) 表示；
        code 非空时仅返回该合约。
        """
        from openctp_ctp.thosttraderapi import (
            THOST_FTDC_OST_NoTradeQueueing,
            THOST_FTDC_OST_PartTradedQueueing,
        )

        alive = []
        seen_refs = set()
        with self._lock:
            for ref, o in self.orders.items():
                status = getattr(o, "OrderStatus", None)
                if status in (
                    THOST_FTDC_OST_NoTradeQueueing,
                    THOST_FTDC_OST_PartTradedQueueing,
                ):
                    if code is None or getattr(o, "InstrumentID", None) == code:
                        alive.append((ref, o))
                        seen_refs.add(ref)
            # ReqOrderInsert 已接受但首报仍缺失时 orders 没有条目；预注册合约就是
            # 唯一撤单线索，必须继续暴露给下一轮重试，不能静默失管。
            for ref, instrument_id in self._order_instruments.items():
                if ref not in self._submitted_order_refs:
                    continue
                if ref in seen_refs:
                    continue
                if code is None or instrument_id == code:
                    alive.append((ref, None))
        return alive

    def prepare_position_query(
        self, request_id: Optional[int] = None
    ) -> threading.Event:
        """在调 ReqQryInvestorPosition 之前清空完成 Event, 返回 Event 供后续 wait。

        注意: 不开启 epoch reconciliation(不剔除陈旧键), 供 open 单 code 等不需对账的查询用。
        需"权威对账"(全量风控读)请改用 begin_position_query。
        """
        with self._lock:
            # 普通查询也必须清掉上次失败遗留的 reconciliation epoch。
            self._position_query_seen = None
            self._position_query_scope = None
            self._position_query_request_id = request_id
            self._position_query_success = None
            self._position_query_event.clear()
        return self._position_query_event

    def begin_position_query(
        self,
        scope_code: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> threading.Event:
        """开始一次"权威"持仓查询并开启 epoch reconciliation(审计 D1-HIGH-1 幽灵仓)。

        记录本次查询券商返回的键; mark_position_query_done(bIsLast) 时剔除范围内未被返回的
        陈旧持仓键——券商全平后不再返回该合约行, 否则陈旧 Position!=0 键残留会被 get_positions
        当幽灵仓喂给止损/超时强平。scope_code=None 表示全量查询(剔除所有未见键); 否则仅剔除
        该 code(``{code}_*`` 键)的未见键。
        """
        with self._lock:
            self._position_query_seen = set()
            self._position_query_scope = scope_code
            self._position_query_request_id = request_id
            self._position_query_success = None
            self._position_query_event.clear()
        return self._position_query_event

    def mark_position_query_done(self, request_id: Optional[int] = None) -> bool:
        """OnRspQryInvestorPosition 的 ``bIsLast=True`` 时调用, 唤醒等待线程。

        若本次查询经 begin_position_query 开启了 epoch reconciliation(seen 非 None), 则剔除
        查询范围内未被券商返回的陈旧持仓键(幽灵仓修复, 审计 D1-HIGH-1)。
        """
        with self._lock:
            if (
                request_id is not None
                and request_id != self._position_query_request_id
            ):
                return False
            seen = self._position_query_seen
            if seen is not None:
                scope = self._position_query_scope
                prefix = f"{scope}_" if scope is not None else None
                for k in list(self.positions.keys()):
                    if k in seen:
                        continue
                    if prefix is None or k.startswith(prefix):
                        del self.positions[k]
                self._position_query_seen = None
                self._position_query_scope = None
            self._position_query_success = True
            self._position_query_request_id = None
        self._position_query_event.set()
        return True

    def mark_position_query_failed(self, request_id: int) -> bool:
        """错误响应完成查询但不执行 epoch prune；wait 方收到明确 False。"""
        with self._lock:
            if self._position_query_request_id != request_id:
                return False
            self._position_query_seen = None
            self._position_query_scope = None
            self._position_query_success = False
            self._position_query_request_id = None
        self._position_query_event.set()
        return True

    def abort_position_query(self, request_id: int) -> None:
        """Req 失败/等待超时后废弃 epoch；该 request 的迟到回报随后会被忽略。"""
        with self._lock:
            if self._position_query_request_id != request_id:
                return
            self._position_query_seen = None
            self._position_query_scope = None
            self._position_query_success = None
            self._position_query_request_id = None
            self._position_query_event.clear()

    def wait_for_position_query(self, timeout: float) -> bool:
        """等待持仓查询完成回报, 返回 True 表示完成 / False 表示超时."""
        if not self._position_query_event.wait(timeout):
            return False
        with self._lock:
            return self._position_query_success is not False
