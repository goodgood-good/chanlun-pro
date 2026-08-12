"""TradingView Web 应用的单进程启动入口。

入口会把项目 ``src`` 与 Web 目录加入 ``sys.path``，兼容 WPF 启动器所需的
GBK 标准流，并在同一进程中启动 Flask/Tornado 服务。
"""

import asyncio
import json
import os
import pathlib
import signal
import sys
import threading
import time


src_path = (pathlib.Path(__file__).parent.parent / ".." / "src").resolve()
web_server_path = pathlib.Path(__file__).parent.resolve()
for bootstrap_path in (web_server_path, src_path):
    value = str(bootstrap_path)
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)

from chanlun.tools.log_util import LogUtil


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
from tornado import httputil
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.web import RequestHandler
from tornado.wsgi import WSGIContainer
from chanlun.tools.daemon_executor import DaemonExecutor
from chanlun.security import (
    get_login_password,
    is_https_enabled,
    get_web_host,
    validate_web_security_config,
)
from cl_app import create_app


class NativeHealthHandler(RequestHandler):
    """绕过业务 WSGI 线程池提供本机健康检查。"""

    def initialize(
        self,
        flask_app,
        readiness_runner=None,
        readiness_timeout_seconds=3.0,
    ):
        self._flask_app = flask_app
        self._readiness_runner = readiness_runner
        self._readiness_timeout_seconds = max(
            0.1,
            min(float(readiness_timeout_seconds), 30.0),
        )

    def _finish_json(self, payload, status_code):
        self.set_status(status_code)
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Cache-Control", "no-store")
        self.set_header("X-Content-Type-Options", "nosniff")
        if status_code == 503:
            self.set_header("Retry-After", "1")
        self.finish(json.dumps(payload, ensure_ascii=False))

    def _unavailable_payload(self, market, reason):
        # 存活身份本身是常数时间快照。即使深度就绪检查仍在后台运行，也能返回
        # 当前版本与进程号，供运维区分“进程已死”和“依赖暂时繁忙”。
        identity, _status = self._flask_app.extensions["health_snapshot"](
            "healthz", market, None
        )
        return {
            "status": "not_ready",
            "revision": identity.get("revision"),
            "pid": os.getpid(),
            "market": market,
            "components": {},
            "reasons": [reason],
        }

    async def get(self):
        kind = self.request.path.strip("/")
        market = self.get_query_argument("market", "a")
        forward_session = self.get_query_argument("forward_session", None)
        snapshot = self._flask_app.extensions["health_snapshot"]
        if kind != "readyz" or self._readiness_runner is None:
            payload, status_code = snapshot(kind, market, forward_session)
            self._finish_json(payload, status_code)
            return

        future = self._readiness_runner.submit(market, forward_session)
        if future is None:
            self._finish_json(
                self._unavailable_payload(market, "health_snapshot_busy"),
                503,
            )
            return
        try:
            wrapped = asyncio.wrap_future(future)
            payload, status_code = await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=self._readiness_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._finish_json(
                self._unavailable_payload(market, "health_snapshot_timeout"),
                503,
            )
            return
        except Exception:
            LogUtil.exception("深度就绪检查执行失败")
            self._finish_json(
                self._unavailable_payload(market, "health_snapshot_failed"),
                503,
            )
            return
        self._finish_json(payload, status_code)


class NativeReadinessRunner:
    """在独立执行器中单飞运行深度就绪检查。"""

    def __init__(self, flask_app, executor):
        self._snapshot = flask_app.extensions["health_snapshot"]
        self._executor = executor
        self._lock = threading.Lock()
        self._in_flight = None

    def submit(self, market, forward_session):
        """提交一次检查；已有检查未完成时快速报告繁忙。"""

        with self._lock:
            if self._in_flight is not None and not self._in_flight.done():
                return None
            self._in_flight = self._executor.submit(
                self._snapshot,
                "readyz",
                market,
                forward_session,
            )
            return self._in_flight


class BoundedWSGIContainer(WSGIContainer):
    """限制并发 WSGI 请求，过载时直接拒绝而不是无限排队。"""

    def __init__(self, wsgi_application, executor, max_requests):
        super().__init__(wsgi_application, executor=executor)
        self._request_slots = threading.BoundedSemaphore(max(1, int(max_requests)))
        self._active_condition = threading.Condition()
        self._active_requests = 0

    def __call__(self, request):
        if not self._request_slots.acquire(blocking=False):
            self._write_overloaded(request)
            return
        with self._active_condition:
            self._active_requests += 1
        IOLoop.current().spawn_callback(self._handle_bounded_request, request)

    async def _handle_bounded_request(self, request):
        try:
            await self.handle_request(request)
        finally:
            with self._active_condition:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._active_condition.notify_all()
            self._request_slots.release()

    def wait_for_idle(self, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._active_condition:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(remaining)
            return True

    def _write_overloaded(self, request):
        body = json.dumps(
            {
                "ok": False,
                "code": "server_busy",
                "errmsg": "Server is busy. Retry shortly.",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = httputil.HTTPHeaders(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
                "Retry-After": "1",
                "X-Content-Type-Options": "nosniff",
            }
        )
        connection = request.connection
        if connection is None:
            return
        start_line = httputil.ResponseStartLine("HTTP/1.1", 503, "Service Unavailable")
        connection.write_headers(start_line, headers, chunk=body)
        connection.finish()
        self._log(503, request)


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
        LogUtil.warning(f"[chart_warm] 读磁盘 entry 失败 key={cache_key} err={e}")
        return
    if disk_entry is None:
        LogUtil.info(f"[chart_warm] 磁盘冷层无 {market}:{code}:{frequency} entry，跳过")
        return
    normalized = _normalize_cache_entry(disk_entry)
    if normalized is None:
        return
    chart_data_cache[cache_key] = normalized
    LogUtil.info(f"[chart_warm] 已预热 {market}:{code}:{frequency} 到 RAM")


def _get_web_port() -> int:
    raw_port = os.environ.get("CHANLUN_WEB_PORT", "9900").strip()
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "CHANLUN_WEB_PORT must be an integer between 1 and 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError("CHANLUN_WEB_PORT must be an integer between 1 and 65535")
    return port


def _get_readiness_timeout_seconds() -> float:
    raw_timeout = os.environ.get("CHANLUN_READINESS_TIMEOUT_SECONDS", "3").strip()
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "CHANLUN_READINESS_TIMEOUT_SECONDS must be between 0.1 and 30"
        ) from exc
    if not 0.1 <= timeout <= 30:
        raise ValueError("CHANLUN_READINESS_TIMEOUT_SECONDS must be between 0.1 and 30")
    return timeout


def _start_runtime_with_retry(
    app,
    cancel_event,
    initial_delay=1.0,
    max_delay=30.0,
):
    """Retry transient runtime bootstrap failures while health endpoints stay live."""
    delay = max(0.01, float(initial_delay))
    maximum = max(delay, float(max_delay))
    while not cancel_event.is_set():
        try:
            app.extensions["start_runtime_services"](enable_scheduler=True)
        except Exception as exc:
            app.config["RUNTIME_BOOTSTRAP_ERROR"] = str(exc)[:200]
            LogUtil.exception("应用后台组件启动失败，稍后重试")
            if cancel_event.wait(delay):
                return False
            delay = min(maximum, delay * 2)
            continue
        app.config.pop("RUNTIME_BOOTSTRAP_ERROR", None)
        return True
    return False


def main() -> int:
    """启动承载 Flask 应用的 Tornado HTTP 服务。"""
    is_wpf_launcher = "wpf_launcher" in sys.argv
    if is_wpf_launcher:
        _wrap_stdio_gbk()

    # 安装 stdout 噪音过滤：吞掉 pytdx 等第三方库漏删的纯数字调试 print，避免刷屏。
    from chanlun.utils import install_stdout_noise_filter

    install_stdout_noise_filter()

    app = None
    server = None
    wsgi_container = None
    http_executor = None
    health_executor = None
    sse_pool = None
    runtime_executor = None
    runtime_cancel_event = threading.Event()
    io_loop = None
    previous_signal_handlers = {}
    try:
        if "CHANLUN_WEB_HOST" not in os.environ and not is_https_enabled():
            os.environ["CHANLUN_WEB_HOST"] = "127.0.0.1"
        web_host = get_web_host()
        web_port = _get_web_port()
        validate_web_security_config(web_host, get_login_password())
        app = create_app(start_scheduler=False)

        # HTTP 线程池容量可配置，默认 32。
        # 多 tab + 多周期并发时 IO（QMT/CQ 拉数据）是瓶颈而非 CPU，扩大 worker 数
        # 不会显著抢占 GIL；用环境变量 CHANLUN_HTTP_WORKERS 覆盖。
        try:
            http_workers = int(os.environ.get("CHANLUN_HTTP_WORKERS", "32"))
            if http_workers < 1:
                http_workers = 32
        except (TypeError, ValueError):
            http_workers = 32
        http_workers = min(http_workers, 64)
        try:
            http_queue = int(os.environ.get("CHANLUN_HTTP_QUEUE", "128"))
            if http_queue < 0:
                http_queue = 128
        except (TypeError, ValueError):
            http_queue = 128
        http_queue = min(http_queue, 1024)
        max_http_requests = http_workers + http_queue
        LogUtil.info(f"HTTP 线程池容量: {http_workers}，等待队列上限: {http_queue}")
        # 监听建立后再异步预压缩 charting_library / datafeeds 下的资源。
        # CachedStaticFileHandler 在生成期间会安全回退到 identity 表示。
        from cl_app.services.static_precompress import precompress_static_assets

        static_root = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cl_app",
            "static",
        )

        # Tornado Application 路由——charting_library / datafeeds 走自定义
        # CachedStaticFileHandler（immutable cache + 透明 gzip），其余 fallback Flask
        from tornado.web import Application, FallbackHandler
        from cl_app.handlers.cached_static import CachedStaticFileHandler

        http_executor = DaemonExecutor(
            http_workers,
            thread_name_prefix="WsgiRequest",
            max_pending=max_http_requests,
        )
        wsgi_container = BoundedWSGIContainer(
            app, executor=http_executor, max_requests=max_http_requests
        )
        health_executor = DaemonExecutor(
            max_workers=1,
            thread_name_prefix="ReadinessProbe",
            max_pending=1,
        )
        readiness_runner = NativeReadinessRunner(app, health_executor)
        readiness_timeout_seconds = _get_readiness_timeout_seconds()
        # SSE 实时推送路由（flag 关时返回空）。重算用独立线程池, 与 WSGI 请求池
        # 隔离, 避免后台持续重算抢占用户 HTTP 请求的 worker。
        from cl_app.handlers.sse_stream import build_routes as sse_build_routes

        sse_pool = DaemonExecutor(
            max_workers=8,
            thread_name_prefix="SseRefresh",
            max_pending=16,
        )
        tornado_app = Application(
            [
                (
                    r"/(?:livez|healthz|readyz)",
                    NativeHealthHandler,
                    {
                        "flask_app": app,
                        "readiness_runner": readiness_runner,
                        "readiness_timeout_seconds": readiness_timeout_seconds,
                    },
                ),
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
        server = HTTPServer(tornado_app, xheaders=is_https_enabled())
        server.bind(web_port, web_host)
        server.start(1)
        io_loop = IOLoop.current()
        app.config["SCHEDULER_ENABLED"] = True
        runtime_executor = DaemonExecutor(
            max_workers=2,
            thread_name_prefix="RuntimeBootstrap",
            max_pending=2,
        )

        def _bootstrap_runtime():
            if runtime_cancel_event.is_set():
                return
            if not _start_runtime_with_retry(app, runtime_cancel_event):
                return
            if runtime_cancel_event.is_set():
                return
            try:
                try:
                    _warm_chart_cache_from_disk()
                except Exception as exc:
                    LogUtil.warning(f"[chart_warm] 启动预热未执行: {exc}")
                LogUtil.info("应用后台组件启动成功")
                try:
                    from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
                    from chanlun import config as _cfg

                    tracker = LbQuotaTracker.instance()
                    used = tracker.count()
                    limit = getattr(_cfg, "LB_QUOTA_MONTHLY_LIMIT", 0)
                    exhausted = tracker.is_exhausted(limit)
                    source = getattr(_cfg, "US_HISTORY_KLINE_SOURCE", "longbridge")
                    LogUtil.info(
                        f"[lb_quota] 当月已用 {used}/{limit if limit > 0 else '∞'} symbol，"
                        f"exhausted={exhausted}，US_HISTORY_KLINE_SOURCE={source}"
                    )
                except Exception as exc:
                    LogUtil.warning(f"[lb_quota] 启动配额读取失败: {exc}")
            except Exception:
                LogUtil.exception("应用后台初始化附加步骤失败")

        def _schedule_runtime_bootstrap():
            if not runtime_cancel_event.is_set():
                runtime_executor.submit(_bootstrap_runtime)
                runtime_executor.submit(lambda: precompress_static_assets(static_root))

        # ⚠️ 严禁改为 s.start(0) 或 s.start(N)（多进程模式）！
        # 当前架构所有缓存（tv.py 的 chart_data_cache / stock_cache / chart_calc_locks /
        # _history_req_locks，以及 file_db、QMT/CQ 的 singleton 实例字段）都是
        # **进程内内存**，多进程会让缓存命中率瞬间归零、per-key 锁失效（不同进程不共享锁）。
        # 如需扩容，请用反向代理 + 多端口部署，或先把缓存改造到 Redis。
        io_loop.add_callback(_schedule_runtime_bootstrap)
        # 默认自动开浏览器；opt-out：环境变量 CHANLUN_NO_AUTO_OPEN=1 或命令行 nobrowser。
        url = f"http://127.0.0.1:{web_port}"
        no_auto_open = os.environ.get("CHANLUN_NO_AUTO_OPEN", "0").strip() == "1"
        nobrowser_flag = len(sys.argv) >= 2 and sys.argv[1] == "nobrowser"
        if not (no_auto_open or nobrowser_flag):

            def _open_browser():
                threading.Thread(
                    target=webbrowser.open,
                    args=(url,),
                    daemon=True,
                    name="BrowserLauncher",
                ).start()

            io_loop.add_callback(_open_browser)
        else:
            LogUtil.info("")
            LogUtil.info(f">>> Web 已启动，请在浏览器访问：{url}")
            LogUtil.info(
                ">>> 当前已禁用自动开浏览器（CHANLUN_NO_AUTO_OPEN=1 或 nobrowser）"
            )
            LogUtil.info("")

        def _request_stop(_signum=None, _frame=None):
            io_loop.add_callback(io_loop.stop)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _request_stop)

        io_loop.start()
        return 0

    except Exception:
        # 完整堆栈仅写入日志，控制台只提示简短信息，避免暴露内部路径与变量。
        LogUtil.exception("启动 Web 服务时发生异常")
        return 1
    finally:
        runtime_cancel_event.set()
        for signum, previous in previous_signal_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass
        if server is not None:
            try:
                server.stop()
            except Exception:
                LogUtil.exception("停止 HTTP 服务失败")
        if wsgi_container is not None and hasattr(wsgi_container, "wait_for_idle"):
            try:
                drain_seconds = float(os.environ.get("CHANLUN_HTTP_DRAIN_SECONDS", "2"))
            except (TypeError, ValueError):
                drain_seconds = 2.0
            drained = wsgi_container.wait_for_idle(max(0.0, min(drain_seconds, 30.0)))
            if not drained:
                LogUtil.warning("HTTP 请求未在关闭窗口内排空，将停止等待")
        if http_executor is not None:
            http_executor.shutdown(wait=False, cancel_futures=True)
            http_executor = None
        if app is not None:
            try:
                app.extensions["shutdown_runtime_services"]()
            except Exception:
                LogUtil.exception("停止应用后台服务失败")
        for executor in (health_executor, runtime_executor, sse_pool):
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    raise SystemExit(main())
