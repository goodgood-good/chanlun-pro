"""SSE 刷新：拉最新K线+全量重算，并按指纹变化决定是否推送。

recompute 复用 prepend_klines_and_replace_cache(与 tv_history 的 cache_tail_gap
重算同口径)，在 chart_calc_locks 持锁期执行，与 tv_history 互斥保证原子性；
decide_push 用指纹比对，数据未变则不推(省流量+防前端闪烁)。
"""
from chanlun.exchange import get_exchange
from chanlun.market import Market
from chanlun.tools.log_util import LogUtil

from .sse_signature import compute_signature


def decide_push(prev_sig, chart_data):
    """返回 (是否推送, 新指纹)。指纹与上次不同(含首次 prev_sig=None)则推送。"""
    sig = compute_signature(chart_data)
    return (sig != prev_sig, sig)


def recompute_chart_data(market, code, frequency, cl_config, cache_key):
    """拉最新K线+全量重算+写回 chart_data_cache，返回完整 chart_data(失败返回 None)。"""
    # 局部 import 避免与 chart_compute / kline_recompute 形成顶层 import 链。
    from .chart_compute import chart_calc_locks
    from .kline_recompute import prepend_klines_and_replace_cache
    from .chart_cache import (
        _TRANSIENT_NEGATIVE_TTL_SECONDS,
        _is_negatively_cached,
        _klines_fetch_incomplete,
        _mark_negative_cache,
    )
    try:
        # 负缓存:退市/新上市等空数据标的 5min 内不再反复拉数据源(审查 L1)。与 tv_history
        # 共享同一负缓存,口径一致;只有 client 连着才会走到这,故不会无谓占用。
        if _is_negatively_cached(cache_key):
            return None
        ex = get_exchange(Market(market))
        klines = ex.klines(code, frequency)
        if _klines_fetch_incomplete(klines):
            # 拉取存在缺口时短暂退避并保留旧缓存，三十秒内自愈，不受真实空数据的
            # 五分钟负缓存抑制。
            _mark_negative_cache(cache_key, ttl=_TRANSIENT_NEGATIVE_TTL_SECONDS)
            return None
        if klines is None or len(klines) == 0:
            _mark_negative_cache(cache_key)
            return None
        lock = chart_calc_locks.get(cache_key)
        # 非阻塞 acquire:若用户 /tv/history 正持同一 key 锁(可能卡在慢速 ex.klines,美股 5-50s),
        # SSE 本拍直接跳过而不是干等 → 不白占 sse_pool worker、不让后台重算拖慢用户首屏/翻页;
        # 下个周期(SSE_REFRESH_MS)再来。用户始终优先(tv_history 仍阻塞式持锁)。(审查 H2)
        if not lock.acquire(blocking=False):
            return None
        try:
            return prepend_klines_and_replace_cache(
                market, code, frequency, cl_config, klines, cache_key,
            )
        finally:
            lock.release()
    except Exception as e:
        LogUtil.warning(
            f"[sse_refresh] 重算失败 {market}:{code}:{frequency}: {e}"
        )
        return None
