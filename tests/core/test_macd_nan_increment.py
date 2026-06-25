"""§2.1 回归:MACD 增量路径 NaN 中毒 + 守卫不扰动干净数据。

背景(audit/_round7_core.md §2.1):全量路径 _full_calculation 有 .fillna(0),但增量路径
_incremental_calculation 原无 NaN 防护。一根 close=NaN 的坏 bar 经增量摄入会把 _ema_*_val
基准永久污染 → 之后每根 dif/dea/hist 全 NaN → query_macd_ld 全 NaN → 背驰恒判不出 →
1/2 类买卖点静默消失。只在常驻 CL 增量场景(live/SSE/monitor)显形。

修复:_incremental_calculation 对 close 做 NaN/Inf 兜底(顶最近有限 close,不填 0 免假跳变),
配合 kline_data_processor._convert 的 OHLC ffill 根因防护。
"""
import math

import pandas as pd

from chanlun.core.macd import MACD
from chanlun.core.types import Kline


def _k(i: int, c: float) -> Kline:
    return Kline(
        index=i,
        date=pd.Timestamp("2024-01-01 09:30:00") + pd.Timedelta(minutes=i),
        h=(c + 1.0) if math.isfinite(c) else c,
        l=(c - 1.0) if math.isfinite(c) else c,
        o=c,
        c=c,
        a=1.0,
    )


def test_macd_increment_nan_close_does_not_poison():
    """坏 bar(close=NaN)经增量摄入后,后续正常 bar 的 hist 必须仍有限(修复前会被永久毒成 NaN)。"""
    m = MACD()
    klines = [_k(i, 100.0 + i * 0.1) for i in range(40)]
    m.process_macd(klines)  # 全量首载
    assert all(math.isfinite(x) for x in m.hist), "全量路径不应有 NaN"

    # 增量摄入一根 close=NaN 的坏 bar(count 增 → 走增量路径)
    klines.append(_k(40, float("nan")))
    m.process_macd(klines)

    # 再增量摄入一根正常 bar
    klines.append(_k(41, 104.5))
    m.process_macd(klines)

    # §2.1 核心断言:末根(正常 bar)hist 必须有限——修复前会因 EMA 基准被 NaN 污染而恒为 NaN
    assert math.isfinite(m.hist[-1]), "MACD 被 NaN 坏 bar 永久中毒(§2.1 未修复)"
    assert all(math.isfinite(x) for x in m.hist), "MACD hist 含 NaN(§2.1 中毒扩散)"
    assert all(math.isfinite(x) for x in m.dif), "MACD dif 含 NaN(§2.1)"
    assert all(math.isfinite(x) for x in m.dea), "MACD dea 含 NaN(§2.1)"


def test_macd_increment_inf_close_does_not_poison():
    """Inf close 同样不应中毒(守卫用 math.isfinite,覆盖 NaN 与 Inf)。"""
    m = MACD()
    klines = [_k(i, 100.0 + i * 0.1) for i in range(40)]
    m.process_macd(klines)
    klines.append(_k(40, float("inf")))
    m.process_macd(klines)
    klines.append(_k(41, 104.5))
    m.process_macd(klines)
    assert all(math.isfinite(x) for x in m.hist), "MACD 被 Inf 坏 bar 中毒"


def test_macd_guard_noop_on_clean_data_inc_equals_full():
    """守卫对干净数据零扰动:逐根增量生长的 hist 必须与一次性全量计算逐位相等。"""
    klines = [_k(i, 100.0 + (i % 7) - 3.0) for i in range(50)]

    m_full = MACD()
    m_full.process_macd(klines)

    m_inc = MACD()
    for j in range(30, 51):  # 30→50 逐步生长,触发增量路径
        m_inc.process_macd(klines[:j])

    assert len(m_inc.hist) == len(m_full.hist) == 50
    for a, b in zip(m_inc.hist, m_full.hist):
        assert abs(a - b) < 1e-9, "守卫扰动了干净数据的增量==全量等价性"
