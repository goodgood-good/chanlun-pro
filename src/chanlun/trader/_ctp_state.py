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
from typing import Any, Dict, Optional


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
        # 持仓查询回报 (instrumentID_posiDirection → CThostFtdcInvestorPositionField)
        self.positions: Dict[str, Any] = {}
        # 每个 order_ref 的等待 Event, 回调到达时唤醒并移除
        self._order_events: Dict[str, threading.Event] = {}
        # 持仓查询完成 Event (OnRspQryInvestorPosition 的 bIsLast 触发)
        self._position_query_event = threading.Event()

    # ---------- order_ref ----------
    def next_order_ref(self) -> str:
        """原子递增 ``order_ref`` 并返回字符串形式 (CTP 报单要求 str)."""
        with self._lock:
            self.order_ref += 1
            return str(self.order_ref)

    # ---------- request_id (查询/撤单) ----------
    def next_request_id(self) -> int:
        """独立于 order_ref 的请求 ID 计数器, 用于 nRequestID."""
        with self._lock:
            self._request_id += 1
            return self._request_id

    # ---------- orders ----------
    def get_order(self, ref: str) -> Optional[Any]:
        """线程安全读取 order; 返回 ``None`` 表示尚未收到回报。"""
        with self._lock:
            return self.orders.get(ref)

    def set_order(self, ref: str, order: Any) -> None:
        """写入 order 并唤醒等待该 ref 的线程 (若有)."""
        with self._lock:
            self.orders[ref] = order
            evt = self._order_events.pop(ref, None)
        if evt is not None:
            evt.set()

    def register_order_wait(self, ref: str) -> threading.Event:
        """在调 ReqOrderInsert 之前注册 ref 的等待 Event.

        注意调用顺序:
            ref = state.next_order_ref()
            state.register_order_wait(ref)       # 先注册
            api.ReqOrderInsert(req)              # 再发单
            state.wait_for_order(ref, timeout)   # 然后等待回报
        若顺序颠倒, 回报可能先到达并丢失唤醒事件。
        """
        with self._lock:
            evt = threading.Event()
            self._order_events[ref] = evt
            return evt

    def wait_for_order(self, ref: str, timeout: float) -> bool:
        """等待 order 回报, 返回 True 表示已到达 / False 表示超时。

        若 ``set_order`` 已在 ``register_order_wait`` 之前发生 (理论上不应,
        见 register 文档), Event 不存在且 order 已在 dict 中, 直接返回 True.
        """
        with self._lock:
            evt = self._order_events.get(ref)
            already_set = (evt is None) and (ref in self.orders)
        if already_set:
            return True
        if evt is None:
            return False
        return evt.wait(timeout)

    # ---------- positions ----------
    def get_position_count(self) -> int:
        with self._lock:
            return len(self.positions)

    def get_positions_snapshot(self) -> Dict[str, Any]:
        """返回 positions 浅拷贝, 调用方迭代时不受 callback 写入影响。"""
        with self._lock:
            return dict(self.positions)

    def set_position(self, key: str, position: Any) -> None:
        with self._lock:
            self.positions[key] = position

    def prepare_position_query(self) -> threading.Event:
        """在调 ReqQryInvestorPosition 之前清空完成 Event, 返回 Event 供后续 wait。

        注意: 不会清空 ``positions`` dict (历史累积语义保留, 由调用方按需 clear)。
        """
        self._position_query_event.clear()
        return self._position_query_event

    def mark_position_query_done(self) -> None:
        """OnRspQryInvestorPosition 的 ``bIsLast=True`` 时调用, 唤醒等待线程."""
        self._position_query_event.set()

    def wait_for_position_query(self, timeout: float) -> bool:
        """等待持仓查询完成回报, 返回 True 表示完成 / False 表示超时."""
        return self._position_query_event.wait(timeout)
