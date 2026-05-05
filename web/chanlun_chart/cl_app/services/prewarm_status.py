"""批量预热活动注册表（service 层）。

设计动机（L1 重构后从 blueprints/tv.py 抽出）：
用户 tv_history 走的旧版 ``prewarm_common_intervals`` 与 symbols.py 的全市场预热
会重复争用同一份 chart_calc_locks 与上游 HTTP 配额。批量预热已经覆盖该 market 时，
旧版 prewarm 直接退出即可，避免双倍开销。

API：
- ``mark_batch_prewarm_active(market, active)`` — symbols.py 在批量预热开始/结束调用
- ``is_batch_prewarm_active(market)`` — tv.py 内部老 prewarm 路径检查时调用
"""
import threading

_batch_prewarm_active_markets: set = set()
_batch_prewarm_lock = threading.Lock()


def mark_batch_prewarm_active(market: str, active: bool) -> None:
    """供 symbols.py 在批量预热任务开始/结束时调用。"""
    with _batch_prewarm_lock:
        if active:
            _batch_prewarm_active_markets.add(market)
        else:
            _batch_prewarm_active_markets.discard(market)


def is_batch_prewarm_active(market: str) -> bool:
    with _batch_prewarm_lock:
        return market in _batch_prewarm_active_markets
