"""线程安全的 CTP order_ref / orders / positions 状态容器 (B2 抽出).

抽出原因:
1. ``order_ref`` 在 CTPTrader 主线程中递增, 但同一进程会被多个交易方法并发调用
   (例如 force_close_all 在循环中调 force_close), 原始 ``self.order_ref += 1``
   read-modify-write 不是原子操作, 高并发下可能产生重复 ref。
2. ``orders`` / ``positions`` 字典由 CTP SDK 的回调线程 (OnRtnOrder /
   OnRspQryInvestorPosition) 写入, 但 CTPTrader 主线程读取并迭代; 没有锁的话
   迭代过程可能抛 ``RuntimeError: dictionary changed size during iteration``,
   或读到部分写入的状态。
3. 把状态从 ``MyTraderCallback`` 抽出来后, 单元测试无需 openctp_ctp 依赖也能
   验证锁的正确性 (parent class CThostFtdcTraderApi 是 C++ 扩展, 难以 mock)。

调用规范:
- CTP 报单方法在递增 + 使用 ref 时, 必须用 ``next_order_ref()`` 拿到本地变量
  ``order_ref``, 后续 ``get_order(order_ref)`` 必须用同一变量, 不要再读
  ``state.order_ref`` (否则有可能读到其它线程递增后的值)。
"""

import threading
from typing import Any, Dict, Optional


class CTPState:
    """CTP 交易状态的线程安全门面。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.order_ref: int = 0
        self.orders: Dict[str, Any] = {}
        self.positions: Dict[str, Any] = {}

    def next_order_ref(self) -> str:
        """原子递增 ``order_ref`` 并返回字符串形式 (CTP 报单要求 str)."""
        with self._lock:
            self.order_ref += 1
            return str(self.order_ref)

    def get_order(self, ref: str) -> Optional[Any]:
        """线程安全读取 order; 返回 ``None`` 表示尚未收到回报。"""
        with self._lock:
            return self.orders.get(ref)

    def set_order(self, ref: str, order: Any) -> None:
        with self._lock:
            self.orders[ref] = order

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
