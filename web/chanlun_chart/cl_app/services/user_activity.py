"""用户活跃度跟踪（service 层）。

L1 重构：从 blueprints/tv.py 抽出，让 symbols.py 不再越界 import tv 模块的下划线符号。

设计动机：
批量预热和 tv_history 共享 native 行情接口（xtquant/通达信/长桥），单连接是串行的，
并发会互相阻塞。让位的目的是让用户切标的时能立刻拿到数据，而不是排队等批量预热完。

状态：
- ``_last_user_request_ts``: 最近一次 tv_history 入口的时间戳（秒）。
  预热每个标的/每个周期前会读这个值，若距离现在 < N 秒则主动 sleep 让出。
- ``_user_recent_codes``: market -> OrderedDict[code, ts]，按市场分桶 LRU。
  预热每轮循环开始时会把这些标的提到队首，让用户实际关注的标的优先预热完。

API：
- ``_mark_user_request(market, code)`` — tv_history 入口处调用（写端）
- ``_get_last_user_request_time()`` — symbols.py 读
- ``_get_user_recent_codes(market)`` — symbols.py 读
"""
import threading
import time
from collections import OrderedDict
from typing import Dict, List

_last_user_request_ts: float = 0.0
_user_activity_lock = threading.Lock()
_user_recent_codes: Dict[str, "OrderedDict"] = {}
_USER_RECENT_TRACK_SECONDS = 600  # 10 分钟内看过的算"用户关注"
_USER_RECENT_MAX_PER_MARKET = 64  # 每个市场最多保留多少个 hot code


def _mark_user_request(market: str = None, code: str = None) -> None:
    """tv_history 入口处调用：标记用户活跃度。

    线程安全；O(1)/O(N=64) 操作，不影响 tv_history 性能。
    """
    global _last_user_request_ts
    now = time.time()
    with _user_activity_lock:
        _last_user_request_ts = now
        if market and code:
            bucket = _user_recent_codes.setdefault(market, OrderedDict())
            # 已存在则移到末尾（LRU），不存在则插入
            if code in bucket:
                bucket.move_to_end(code)
            else:
                bucket[code] = now
                # 容量上限保护
                while len(bucket) > _USER_RECENT_MAX_PER_MARKET:
                    bucket.popitem(last=False)
            # 顺便清理过期项（懒清理，避免长时间不切换市场时残留）
            cutoff = now - _USER_RECENT_TRACK_SECONDS
            stale = [c for c, ts in bucket.items() if ts < cutoff]
            for c in stale:
                bucket.pop(c, None)


def _get_last_user_request_time() -> float:
    """供 symbols.py 调用：返回最近一次 tv_history 时间戳。"""
    with _user_activity_lock:
        return _last_user_request_ts


def _get_user_recent_codes(market: str) -> List[str]:
    """供 symbols.py 调用：返回某市场最近活跃过的 code 列表（最近的在最前）。"""
    with _user_activity_lock:
        bucket = _user_recent_codes.get(market)
        if not bucket:
            return []
        now = time.time()
        cutoff = now - _USER_RECENT_TRACK_SECONDS
        # 按最近优先（OrderedDict 末尾是最新），过滤过期
        return [c for c, ts in reversed(list(bucket.items())) if ts >= cutoff]
