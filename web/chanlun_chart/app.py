"""
WSGI entrypoint for the TradingView web application.

Adds project `src` and web server path to `sys.path`, supports a WPF
launcher mode that wraps stdio into GBK to avoid encoding issues, and
bootstraps the Flask/Tornado server.
"""

import os
import pathlib
import sys

from chanlun.tools.log_util import LogUtil

src_path = pathlib.Path(__file__).parent.parent / ".." / "src"
sys.path.append(str(src_path))
web_server_path = pathlib.Path(__file__).parent
sys.path.append(str(web_server_path))

def _wrap_stdio_gbk() -> None:
    """
    Wrap stdin/stdout/stderr to GBK encoding for WPF launcher mode.
    Ensures print flushes and converts unicode to GBK safely.
    """

    class _Filter:
        def __init__(self, target):
            self.target = target

        def write(self, s):
            # errors="replace"：日志含非 GBK 字符（emoji 等）时不让 WPF 模式崩溃。
            self.target.buffer.write(s.encode("gbk", errors="replace"))
            self.target.flush()

        def flush(self):
            self.target.flush()

        def close(self):
            self.target.close()

    sys.stdin = _Filter(sys.stdin)
    sys.stdout = _Filter(sys.stdout)
    sys.stderr = _Filter(sys.stderr)


import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.wsgi import WSGIContainer
from chanlun import config
from cl_app import create_app
from cl_app.blueprints.tv import start_symbol_preload_thread

def _warm_chart_cache_from_disk() -> None:
    """启动期 chart_data 预热。把上次访问的 entry 从 fdb 回填 RAM。

    cache_key 由 (market, code, frequency, hash(cl_config)) 组成；这里用
    query_cl_chart_config(market, code) 获取 cl_config——与 tv.py history 入口
    的 key 构造方式完全一致，命中率最高。
    """
    from cl_app.services.last_chart_state import load_last_state
    state = load_last_state()
    if not state:
        return
    market = state["market"]
    code = state["code"]
    frequency = state["frequency"]

    from chanlun.cl_utils import query_cl_chart_config
    from cl_app.services.chart_cache import (
        _build_cache_key,
        _normalize_cache_entry,
        chart_data_cache,
    )
    from chanlun.persistence.file_db import fdb

    cl_config = query_cl_chart_config(market, code)
    if not isinstance(cl_config, dict):
        cl_config = {}
    cache_key = _build_cache_key(market, code, frequency, cl_config)
    try:
        disk_entry = fdb.get_chart_cache(cache_key)
    except Exception as e:
        LogUtil.warning(
            f"[chart_warm] 读磁盘 entry 失败 key={cache_key} err={e}"
        )
        return
    if disk_entry is None:
        LogUtil.info(
            f"[chart_warm] 磁盘冷层无 {market}:{code}:{frequency} entry，跳过"
        )
        return
    normalized = _normalize_cache_entry(disk_entry)
    if normalized is None:
        return
    chart_data_cache[cache_key] = normalized
    LogUtil.info(
        f"[chart_warm] 已预热 {market}:{code}:{frequency} 到 RAM"
    )


def main() -> None:
    """Start the Tornado HTTP server hosting the Flask app."""
    is_wpf_launcher = "wpf_launcher" in sys.argv
    if is_wpf_launcher:
        _wrap_stdio_gbk()

    try:
        app = create_app()

        # HTTP 线程池容量可配置，默认 32。
        # 多 tab + 多周期并发时 IO（QMT/CQ 拉数据）是瓶颈而非 CPU，扩大 worker 数
        # 不会显著抢占 GIL；用环境变量 CHANLUN_HTTP_WORKERS 覆盖。
        try:
            http_workers = int(os.environ.get('CHANLUN_HTTP_WORKERS', '32'))
            if http_workers < 1:
                http_workers = 32
        except (TypeError, ValueError):
            http_workers = 32
        LogUtil.info(f"HTTP 线程池容量: {http_workers}")
        # 启动期预压缩 charting_library / datafeeds 下的 .js/.css 为 .gz。
        # 第一次启动会多 5-15s（gzip level 9 单核），之后凭 mtime 跳过。
        from cl_app.services.static_precompress import precompress_static_assets
        static_root = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cl_app", "static",
        )
        precompress_static_assets(static_root)

        # Tornado Application 路由——charting_library / datafeeds 走自定义
        # CachedStaticFileHandler（immutable cache + 透明 gzip），其余 fallback Flask
        from tornado.web import Application, FallbackHandler
        from cl_app.handlers.cached_static import CachedStaticFileHandler

        wsgi_container = WSGIContainer(
            app, executor=ThreadPoolExecutor(http_workers)
        )
        # SSE 实时推送路由（flag 关时返回空）。重算用独立线程池, 与 WSGI 请求池
        # 隔离, 避免后台持续重算抢占用户 HTTP 请求的 worker。
        from cl_app.handlers.sse_stream import build_routes as sse_build_routes
        sse_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="SseRefresh")
        tornado_app = Application(
            [
                (
                    r"/static/charting_library/(.*)",
                    CachedStaticFileHandler,
                    {"path": os.path.join(static_root, "charting_library")},
                ),
                (
                    r"/static/datafeeds/(.*)",
                    CachedStaticFileHandler,
                    {"path": os.path.join(static_root, "datafeeds")},
                ),
                *sse_build_routes(app, pool=sse_pool),
                (r".*", FallbackHandler, {"fallback": wsgi_container}),
            ]
        )
        s = HTTPServer(tornado_app)
        s.bind(9900, config.WEB_HOST)

        # 先启动 symbol 预加载后台线程：daemon 线程，不阻塞主流程，
        # 让其与 HTTP 服务启动并行，争取在用户首次发起请求前完成首轮缓存填充。
        start_symbol_preload_thread()

        # chart_data 启动预热：把用户上次访问的 entry 从磁盘冷层回填 RAM，
        # 首个 first=true 请求直接 RAM 命中。失败/无历史状态均为空操作。
        try:
            _warm_chart_cache_from_disk()
        except Exception as e:
            LogUtil.warning(f"[chart_warm] 启动预热未执行: {e}")

        LogUtil.info("启动成功")
        # 启动时打印长桥月度配额状态，便于确认当月用量与数据源选择
        try:
            from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
            from chanlun import config as _cfg
            _tracker = LbQuotaTracker.instance()
            _used = _tracker.count()
            _limit = getattr(_cfg, "LB_QUOTA_MONTHLY_LIMIT", 0)
            _exhausted = _tracker.is_exhausted(_limit)
            _src = getattr(_cfg, "US_HISTORY_KLINE_SOURCE", "longbridge")
            LogUtil.info(
                f"[lb_quota] 当月已用 {_used}/{_limit if _limit > 0 else '∞'} symbol，"
                f"exhausted={_exhausted}，US_HISTORY_KLINE_SOURCE={_src}"
            )
        except Exception as _e:
            # 配额横幅打印失败不应阻断主流程
            LogUtil.warning(f"[lb_quota] 启动配额读取失败: {_e}")
        # ⚠️ 严禁改为 s.start(0) 或 s.start(N)（多进程模式）！
        # 当前架构所有缓存（tv.py 的 chart_data_cache / stock_cache / chart_calc_locks /
        # _history_req_locks，以及 file_db、QMT/CQ 的 singleton 实例字段）都是
        # **进程内内存**，多进程会让缓存命中率瞬间归零、per-key 锁失效（不同进程不共享锁）。
        # 如需扩容，请用反向代理 + 多端口部署，或先把缓存改造到 Redis。
        s.start(1)

        # 默认自动开浏览器；opt-out：环境变量 CHANLUN_NO_AUTO_OPEN=1 或命令行 nobrowser。
        url = "http://127.0.0.1:9900"
        no_auto_open = os.environ.get("CHANLUN_NO_AUTO_OPEN", "0").strip() == "1"
        nobrowser_flag = len(sys.argv) >= 2 and sys.argv[1] == "nobrowser"
        if not (no_auto_open or nobrowser_flag):
            webbrowser.open(url)
        else:
            LogUtil.info("")
            LogUtil.info(f">>> Web 已启动，请在浏览器访问：{url}")
            LogUtil.info('>>> 当前已禁用自动开浏览器（CHANLUN_NO_AUTO_OPEN=1 或 nobrowser）')
            LogUtil.info("")
        IOLoop.instance().start()

    except Exception:
        # 完整堆栈仅写入日志，控制台只提示简短信息，避免暴露内部路径与变量。
        LogUtil.exception("启动 Web 服务时发生异常")
        if is_wpf_launcher is False:
            input("启动失败，详情见日志文件，按回车键退出")


if __name__ == "__main__":
    main()