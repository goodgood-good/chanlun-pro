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
    try:
        ex = get_exchange(Market(market))
        klines = ex.klines(code, frequency)
        if klines is None or len(klines) == 0:
            return None
        lock = chart_calc_locks.get(cache_key)
        with lock:
            return prepend_klines_and_replace_cache(
                market, code, frequency, cl_config, klines, cache_key,
            )
    except Exception as e:
        LogUtil.warning(
            f"[sse_refresh] 重算失败 {market}:{code}:{frequency}: {e}"
        )
        return None
