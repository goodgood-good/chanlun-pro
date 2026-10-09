"""行情调用优先级标记（纯 Python，无行情 SDK 依赖，可被 web 层安全 import）。

prewarm/批量用 ``lb_low_priority()`` 在 caller 线程上标记，``ExchangeChangQiao.klines()`` 入口
读取 ``_lb_call_priority`` 并随分段任务透传，让 prewarm 走独立的低 QPS 限流桶、不与交互请求
争抢。threadlocal 不跨线程，但 klines() 主体与 caller 同线程，读到后即随 ``executor.submit``
传参，故分段在别的 worker 线程上也拿得到正确优先级。

单独成模块是为了不强制 web 层在 import 时拉起 longbridge（exchange_cq 顶层硬依赖 longbridge，
A 股-only 部署可能未装）。本模块只依赖标准库。
"""
import contextlib
import multiprocessing
import threading

_lb_call_priority = threading.local()


def current_priority() -> str:
    """返回当前线程的 longbridge 调用优先级（默认 ``"interactive"``）。"""
    return getattr(_lb_call_priority, "value", "interactive")


def quote_request_priority() -> str:
    """Shared provider priority: process workers and marked prewarm stay background."""
    if multiprocessing.current_process().name != "MainProcess" or current_priority() != "interactive":
        return "background"
    return "interactive"


@contextlib.contextmanager
def lb_low_priority():
    """在 with 块内把当前线程的 longbridge K线调用标记为 prewarm 低优先。

    长桥和使用 ``quote_request_priority`` 的行情源都遵守此标记；不访问网络。
    """
    prev = current_priority()
    _lb_call_priority.value = "prewarm"
    try:
        yield
    finally:
        _lb_call_priority.value = prev
