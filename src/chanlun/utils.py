"""
行情 SDK 输出过滤与代理配置读取（DB 缓存优先）。
"""

import sys
import threading
from typing import Dict

from chanlun import config
from chanlun.persistence.db import db


class _StdoutNoiseFilter:
    """进程级 stdout 包装，按行丢弃第三方库(如 pytdx)漏删的纯数字调试 print。

    pytdx 的 get_instrument_quote_list 解析期货报价时每条 print(pos)(游标位置)。
    多线程下无法用 redirect_stdout 可靠抑制(全局 sys.stdout 会被各线程互相覆盖、
    甚至泄漏成 StringIO)，故在进程级包一层，按完整行判断：整行仅含数字的丢弃，
    其余原样输出。用线程局部缓冲拼行，避免多线程 write 交错串行。项目日志走
    logging(每行含时间/级别等文本)，不会被误吞。
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def write(self, s):
        buf = getattr(self._local, "buf", "") + s
        out = []
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.strip().isdigit():
                continue  # 丢弃纯数字行(pytdx 漏删的 print(pos))
            out.append(line)
            out.append("\n")
        self._local.buf = buf
        if out:
            self._real.write("".join(out))
        return len(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        # isatty / fileno / encoding 等属性透传给真实 stdout。
        return getattr(self._real, name)


def install_stdout_noise_filter() -> None:
    """进程级安装一次 stdout 噪音过滤(幂等)，在 app 启动早期调用。"""
    if not isinstance(sys.stdout, _StdoutNoiseFilter):
        sys.stdout = _StdoutNoiseFilter(sys.stdout)


def config_get_proxy() -> Dict[str, str]:
    """
    Get HTTP proxy configuration.

    Priority: DB cache key `req_proxy` overrides defaults if present.

    Returns a dict with `host` and `port`.
    """
    db_proxy = db.cache_get("req_proxy")
    if db_proxy is not None and db_proxy.get("host") and db_proxy.get("port"):
        return db_proxy
    return {"host": config.PROXY_HOST, "port": config.PROXY_PORT}
