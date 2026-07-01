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
from concurrent.futures import ThreadPoolExecutor

from chanlun.tools.log_util import LogUtil

# 后台重验证线程池。worker 数克制: 重验证是 CPU 密集(缠论纯 Python, GIL 串行),
# 开太多并不能并行加速, 只会与用户请求抢 GIL; 4 个够吸收突发切标的, 又不喧宾夺主。
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ChartRevalidate")

# 在飞 cache_key 集合 + 锁(去重)。
_inflight: set = set()
_lock = threading.Lock()


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
    """把一次后台重验证排进线程池(去重)。

    Returns:
        True  — 已排队(本次成为该 cache_key 的在飞任务);
        False — 该 cache_key 已有在飞任务, 跳过(去重)/ 或线程池提交失败。
    """
    with _lock:
        if cache_key in _inflight:
            return False
        _inflight.add(cache_key)

    def _task() -> None:
        try:
            _do_revalidate(market, code, frequency, cl_config, cache_key)
        except Exception as e:
            LogUtil.warning(
                f"[chart_revalidate] 重验证失败 {market}:{code}:{frequency}: {e}"
            )
        finally:
            # 无论成功/异常都必须清出 inflight, 否则一次失败永久阻塞该 key 后续刷新。
            with _lock:
                _inflight.discard(cache_key)

    try:
        _pool.submit(_task)
        return True
    except Exception as e:
        # 线程池已关闭 / 队列异常: 回滚 inflight, 让下次能重试。
        with _lock:
            _inflight.discard(cache_key)
        LogUtil.warning(f"[chart_revalidate] 提交后台重验证失败 {cache_key}: {e}")
        return False
