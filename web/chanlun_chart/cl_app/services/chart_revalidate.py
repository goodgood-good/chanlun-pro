"""后台重验证(stale-while-revalidate 的 revalidate 半边, 方向1)。

tv_history 命中"过期全量快照"时, 立即把旧快照返回前端(秒显), 同时调
``submit_revalidation`` 把"重新拉 K 线 + 全量重算 + 写回缓存"丢到后台线程池,
脱离用户请求关键路径。刷新写回缓存(并重盖 validated_at)后, 新数据经现有
SSE 推送 / TV polling 自然到达前端, 用户感知 = 秒开 + ≤数秒自愈。

去重: 同一 cache_key 在飞(inflight)时重复提交直接跳过——用户快速切标的、
或 SSE 8s 轮询会对同一标的反复触发, 不去重会把同一重算堆成 N 份, 反而加剧
CPU/QMT 争抢(正是要解决的问题)。

复用 ``compute_and_cache_chart_data``(预热同款全量计算路径), 与用户实际打开
图表的计算/缓存口径完全一致, 不会"少算" higher_macd 等字段。
"""
import threading
import time

from chanlun.tools.log_util import LogUtil

# A blocking compute cannot be safely killed in CPython. Run each attempt in a
# daemon thread, cap the number of underlying attempts, and quarantine timed-out
# keys until their original call really returns.
_MAX_ACTIVE_REVALIDATIONS = 4
_REVALIDATION_TIMEOUT_SECONDS = 30.0
_inflight: set = set()
_active_attempts = {}
_timed_out: set = set()
_lock = threading.Lock()
_closed = False

def _do_revalidate(
    market: str, code: str, frequency: str, cl_config: dict, cache_key: str
) -> bool:
    """实际重验证: 拉最新 K 线 + 全量重算 + 写回缓存。

    单独成函数(而非内联)便于单测 monkeypatch, 不在测试里真连数据源/跑缠论。
    懒 import 避免与 chart_compute 形成顶层 import 链。

    2026-07 修复(锁域不一致): 持 chart_calc_locks(cache_key)(非阻塞获取, 与
    sse_refresh.py 同款), 与 tv_history 的同步 MISS 重算/sse_refresh 的周期 tick
    互斥。此前不持锁会导致后台重验证与用户请求对同一 cache_key 并发全量重算——
    自己算好准备写回时, existing_entry 可能已被别处更新过, 读到"半新半旧"的
    时间线交叉状态。锁忙(用户请求正持锁)时直接跳过本次重验证, 不排队等待;
    用户优先, 下次 serve_stale 触发时再试。
    """
    from .chart_compute import chart_calc_locks, compute_and_cache_chart_data

    lock = chart_calc_locks.get(cache_key)
    if not lock.acquire(blocking=False):
        return False
    try:
        return compute_and_cache_chart_data(market, code, frequency, cl_config)
    finally:
        lock.release()


def submit_revalidation(
    market: str, code: str, frequency: str, cl_config: dict, cache_key: str
) -> bool:
    """Start one bounded background attempt for a cache key."""
    with _lock:
        if _closed or cache_key in _active_attempts:
            return False
        if len(_active_attempts) >= max(1, int(_MAX_ACTIVE_REVALIDATIONS)):
            LogUtil.warning("[chart_revalidate] active attempt limit reached")
            return False

        done = threading.Event()
        attempt = {"done": done, "thread": None}
        _active_attempts[cache_key] = attempt
        _inflight.add(cache_key)

    def _task() -> None:
        try:
            _do_revalidate(market, code, frequency, cl_config, cache_key)
        except Exception as e:
            LogUtil.warning(
                f"[chart_revalidate] 重验证失败 {market}:{code}:{frequency}: {e}"
            )
        finally:
            done.set()
            with _lock:
                if _active_attempts.get(cache_key) is attempt:
                    _active_attempts.pop(cache_key, None)
                _inflight.discard(cache_key)
                _timed_out.discard(cache_key)

    def _watchdog() -> None:
        timeout = max(0.0, float(_REVALIDATION_TIMEOUT_SECONDS))
        if done.wait(timeout):
            return
        with _lock:
            if _active_attempts.get(cache_key) is not attempt:
                return
            _inflight.discard(cache_key)
            _timed_out.add(cache_key)
        LogUtil.warning(
            f"[chart_revalidate] attempt timed out "
            f"{market}:{code}:{frequency} after {timeout:g}s"
        )

    thread = threading.Thread(
        target=_task,
        daemon=True,
        name=f"ChartRevalidate-{cache_key[:32]}",
    )
    attempt["thread"] = thread
    try:
        thread.start()
        threading.Thread(
            target=_watchdog,
            daemon=True,
            name=f"ChartRevalidateWatchdog-{cache_key[:32]}",
        ).start()
        return True
    except Exception as e:
        with _lock:
            if _active_attempts.get(cache_key) is attempt:
                _active_attempts.pop(cache_key, None)
            _inflight.discard(cache_key)
            _timed_out.discard(cache_key)
        LogUtil.warning(f"[chart_revalidate] 提交后台重验证失败 {cache_key}: {e}")
        return False


def revalidation_status():
    with _lock:
        return {
            "active": len(_active_attempts),
            "inflight": len(_inflight),
            "timed_out": len(_timed_out),
            "closed": _closed,
        }


def start_revalidation_runtime():
    global _closed
    with _lock:
        if _active_attempts:
            raise RuntimeError("cannot restart revalidation with active attempts")
        _inflight.clear()
        _timed_out.clear()
        _closed = False


def shutdown_revalidation(wait=False, timeout=1.0):
    """Reject new attempts and optionally join active daemon attempts briefly."""
    global _closed
    with _lock:
        _closed = True
        threads = [
            attempt["thread"]
            for attempt in _active_attempts.values()
            if attempt.get("thread") is not None
        ]
    if wait:
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
    return not any(thread.is_alive() for thread in threads)
