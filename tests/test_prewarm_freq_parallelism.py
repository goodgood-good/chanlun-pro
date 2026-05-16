"""H3 回归:批量预热单标的内的"周期并行度"必须按市场区分。

native 数据源(a 股 xtquant、tdx 期货)的客户端线程不安全,单标的内的
多周期计算必须串行(并行度 1);HTTP 数据源(长桥 / futu 等)可并行。

老实现 ``_prewarm_one_code`` 对所有市场统一用 ``PREWARM_FREQ_PARALLELISM``
(默认 2),会让 a 股批量预热并发 2 个 xtquant ``ex.klines`` 调用 ——
与 tv.py / symbols.py 多处注释明确声明的"native 必须串行"相冲突。

本测试锁定"按市场解析周期并行度"的行为。
"""

from __future__ import annotations

from cl_app.blueprints.symbols import (
    PREWARM_FREQ_PARALLELISM,
    _resolve_freq_parallelism,
)


def test_native_markets_force_serial():
    """native 市场单标的内周期并行度必须为 1。

    a→qmt(xtquant)、futures→tdx_futures、ny_futures→tdx_ny_futures、
    fx→tdx_fx —— 均为 native 客户端,线程不安全,周期间必须串行。
    """
    for market in ("a", "futures", "ny_futures", "fx"):
        assert _resolve_freq_parallelism(market) == 1, (
            f"native 市场 {market!r} 不应并行计算周期(xtquant/tdx 线程不安全)"
        )


def test_http_markets_use_configured_parallelism():
    """HTTP 数据源市场(hk/us 长桥、currency* binance)沿用 PREWARM_FREQ_PARALLELISM。"""
    for market in ("us", "hk", "currency", "currency_spot"):
        assert _resolve_freq_parallelism(market) == PREWARM_FREQ_PARALLELISM, (
            f"HTTP 市场 {market!r} 应使用配置的并行度 {PREWARM_FREQ_PARALLELISM}"
        )
