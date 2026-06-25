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
    # 默认值与 config 实际值(均 8000)对齐:getattr 默认仅在 config 缺该属性时生效,
    # 原 5000/3000 是与现实不符的死值,易误导读者以为"美股5s/其他3s"(审查 L2)。
    if market == "us":
        return int(getattr(config, "SSE_REFRESH_MS_US", 8000))
    return int(getattr(config, "SSE_REFRESH_MS", 8000))


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
        # H1: 若该 key 刷新循环已存在,我是"后加入者"。循环只在指纹变化时推,数据停滞期
        # (收盘/盘整)后加入者可能一帧都收不到 → 记下,订阅成功后给自己补发一次当前快照。
        joining_existing = self._cache_key in _hub.active_keys()
        if not _hub.subscribe(self._cache_key, self, start_loop):
            self.set_status(503)
            self.finish()
            return

        # H1: 后加入既有循环者,单独补发一次当前权威数据(直接读缓存不重算,近零成本),
        # 否则要等到下次指纹变化才首次收到缠论(多窗口/多设备同标的常见)。
        if joining_existing:
            await self._send_current_snapshot()

        # 不 return, 保持长连接, 直到客户端断开(on_connection_close 唤醒)。
        await self._closed.wait()

    def _make_start_loop(self, market, code, frequency, cl_config):
        """返回 start_loop(cache_key)：创建该 key 的 PeriodicCallback 刷新循环。"""
        # 捕获共享线程池(进程级,任意 handler 取值相同)到局部变量,使下面的闭包不引用 self →
        # 触发循环创建的"首个 handler"断开后可正常 GC,不被循环长期钉住(审查 L4)。
        pool = self._pool

        def start_loop(cache_key):
            ctx = {"last_sig": None, "running": False}
            interval = _refresh_interval_ms(market)

            async def _tick():
                if not _hub.clients_of(cache_key):
                    return
                # running 门:启动"立即首帧"(add_callback)与首次周期回调是两条独立调度链,
                # 可同时进入 _tick → 同 key 双重重算 + last_sig check-then-act 竞态(审查 M1)。
                # 本拍未结束则跳过(下个周期再来);try/finally 确保异常也复位,不会永久卡住。
                if ctx.get("running"):
                    return
                ctx["running"] = True
                try:
                    # 阻塞的拉数据+重算丢线程池, 不阻塞 IOLoop。
                    chart_data = await IOLoop.current().run_in_executor(
                        pool, recompute_chart_data,
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
                        # full_snapshot: SSE 推的 chart_data 是 prepend 产出的"完整当前快照"。
                        # 前端据此整体替换形态列表(不走为部分响应设计的合并)→ 杜绝未完成笔/陈旧
                        # 形态等"只增不删"累积。K线/MACD 仍按增量合并保持视图不重置。
                        data_str = json.dumps(
                            {"s": "ok", "update": True, "full_snapshot": True, **chart_data}
                        )
                    # list() 快照: _send 失败会注销 client(改 hub), 避免迭代中修改。
                    for client in list(_hub.clients_of(cache_key)):
                        await client._send(data_str)
                finally:
                    ctx["running"] = False

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

    async def _send_current_snapshot(self):
        """给"后加入既有循环"的 client 补发一次当前缓存的权威数据(H1)。只读缓存不重算;
        缓存为空(循环刚起还没算过)则跳过——此时它自己的 /tv/history firstDataRequest 已兜底。"""
        try:
            from cl_app.services.chart_cache import _get_chart_cache_entry
            entry = _get_chart_cache_entry(self._cache_key)
            chart_data = entry.get("data") if isinstance(entry, dict) else None
            if chart_data:
                # 带 full_snapshot:与周期推送口径一致,前端整体替换形态(不走合并),
                # 杜绝后加入者首帧若落在合并分支时的陈旧形态残留(审查 M-1)。
                await self._send(
                    json.dumps({"s": "ok", "update": True, "full_snapshot": True, **chart_data})
                )
        except Exception:
            pass

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
