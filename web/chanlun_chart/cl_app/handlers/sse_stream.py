"""Tornado 原生 SSE 端点 /tv/stream：服务端定时重算缠论并推给订阅的前端。

为何不用 Flask: Tornado WSGIContainer 会缓冲完整响应, 无法做 SSE 流式长连接,
故 SSE 必须是 Tornado 原生 handler, 注册在 app.py 的 tornado_app 路由表。
鉴权复用 Flask-Login(见 sse_auth)。同一 cache_key 多个 client 共享一个刷新循环。
"""
import asyncio
import json

import tornado.web
from tornado.ioloop import IOLoop, PeriodicCallback

from chanlun import config
from chanlun.cl_utils import query_cl_chart_config
from cl_app.services.sse_auth import is_request_authenticated
from cl_app.services.sse_hub import SseHub
from cl_app.services.sse_refresh import decide_push, recompute_chart_data

_hub = SseHub()


def get_hub() -> SseHub:
    return _hub


def _refresh_interval_ms(market: str) -> int:
    if market == "us":
        return int(getattr(config, "SSE_REFRESH_MS_US", 5000))
    return int(getattr(config, "SSE_REFRESH_MS", 3000))


class SseStreamHandler(tornado.web.RequestHandler):
    def initialize(self, flask_app, pool=None):
        self._flask_app = flask_app
        self._pool = pool
        self._cache_key = None
        self._closed = None

    async def get(self):
        # 局部 import 避免与 tv blueprint / chart_cache 形成顶层 import 链。
        from cl_app.blueprints.tv import _parse_tv_symbol, resolution_maps
        from cl_app.services.chart_cache import _build_cache_key

        symbol = self.get_argument("symbol", "")
        resolution = self.get_argument("resolution", "")

        if not is_request_authenticated(
            self._flask_app, self.request.headers.get("Cookie")
        ):
            self.set_status(401)
            self.finish()
            return

        market, code = _parse_tv_symbol(symbol)
        frequency = resolution_maps.get(resolution)
        if market is None or code is None or frequency is None:
            self.set_status(400)
            self.finish()
            return

        cl_config = query_cl_chart_config(market, code)
        if not isinstance(cl_config, dict):
            cl_config = {}
        self._cache_key = _build_cache_key(market, code, frequency, cl_config)

        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("X-Accel-Buffering", "no")
        self.set_header("Connection", "keep-alive")

        self._closed = asyncio.Event()
        start_loop = self._make_start_loop(market, code, frequency, cl_config)
        if not _hub.subscribe(self._cache_key, self, start_loop):
            self.set_status(503)
            self.finish()
            return

        # 不 return, 保持长连接, 直到客户端断开(on_connection_close 唤醒)。
        await self._closed.wait()

    def _make_start_loop(self, market, code, frequency, cl_config):
        """返回 start_loop(cache_key)：创建该 key 的 PeriodicCallback 刷新循环。"""
        def start_loop(cache_key):
            ctx = {"last_sig": None}
            interval = _refresh_interval_ms(market)

            async def _tick():
                if not _hub.clients_of(cache_key):
                    return
                # 阻塞的拉数据+重算丢线程池, 不阻塞 IOLoop。
                chart_data = await IOLoop.current().run_in_executor(
                    self._pool, recompute_chart_data,
                    market, code, frequency, cl_config, cache_key,
                )
                should = False
                if chart_data is not None:
                    should, sig = decide_push(ctx["last_sig"], chart_data)
                    if should:
                        ctx["last_sig"] = sig
                # 包装成与 /tv/history 一致的响应(含 s/update)，前端复用 getBars
                # 的合并逻辑(_processHistoryResponse)直接消费; json.dumps 一次, 多
                # client 共享同一份字符串。
                data_str = None
                if should:
                    data_str = json.dumps({"s": "ok", "update": True, **chart_data})
                # list() 快照: _send 失败会注销 client(改 hub), 避免迭代中修改。
                for client in list(_hub.clients_of(cache_key)):
                    await client._send(data_str)

            pc = PeriodicCallback(_tick, interval)
            pc.start()
            IOLoop.current().add_callback(_tick)  # 首次立即推一帧
            return pc

        return start_loop

    async def _send(self, data_str):
        """有 data_str(已格式化 JSON)推数据帧, 否则发心跳(保活)。写失败则注销。"""
        try:
            if data_str is not None:
                self.write("event: chanlun\ndata: " + data_str + "\n\n")
            else:
                self.write(": ping\n\n")
            await self.flush()
        except Exception:
            self._unsub()

    def on_connection_close(self):
        self._unsub()

    def _unsub(self):
        if self._cache_key is not None:
            _hub.unsubscribe(self._cache_key, self)
            self._cache_key = None
        if self._closed is not None and not self._closed.is_set():
            self._closed.set()


def build_routes(flask_app, pool=None):
    """flag 开则返回 /tv/stream 路由(供 app.py 注册), 关则返回空列表。"""
    if not getattr(config, "ENABLE_SSE_PUSH", False):
        return []
    return [
        (r"/tv/stream", SseStreamHandler, {"flask_app": flask_app, "pool": pool}),
    ]
