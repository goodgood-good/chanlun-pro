"""Tornado 原生 SSE 端点 /tv/stream：服务端定时重算缠论并推给订阅的前端。

为何不用 Flask: Tornado WSGIContainer 会缓冲完整响应, 无法做 SSE 流式长连接,
故 SSE 必须是 Tornado 原生 handler, 注册在 app.py 的 tornado_app 路由表。
鉴权复用 Flask-Login(见 sse_auth)。同一 cache_key 多个 client 共享一个刷新循环。
"""
import asyncio
import hashlib
import json
import threading

import tornado.web
from tornado.ioloop import IOLoop, PeriodicCallback
from flask_login.utils import decode_cookie
from werkzeug.http import parse_cookie

from chanlun import config
from chanlun.cl_utils import query_cl_chart_config
from chanlun.tools.log_util import LogUtil
from cl_app.services.sse_auth import is_request_authenticated
from cl_app.services.sse_hub import SseHub
from cl_app.services.sse_refresh import decide_push, recompute_chart_data

_hub = SseHub()
_SEND_TIMEOUT_SECONDS = 5.0
_RECOMPUTE_TIMEOUT_SECONDS = max(
    0.1, float(getattr(config, "SSE_RECOMPUTE_TIMEOUT_SECONDS", 20.0))
)
_RECOMPUTE_MAX_PENDING = max(
    1, int(getattr(config, "SSE_MAX_PENDING_RECOMPUTES", 8))
)
_runtime_lock = threading.Lock()
_recompute_slots = threading.BoundedSemaphore(_RECOMPUTE_MAX_PENDING)
_runtime_inflight = set()
_runtime_timed_out = set()
_runtime_closed = False


def get_hub() -> SseHub:
    return _hub


async def _run_recompute_bounded(pool, ctx, args):
    """Run one recompute with a deadline and a strict global submission cap."""
    existing = ctx.get("future")
    if existing is not None:
        if not existing.done():
            return False, None
        ctx.pop("future", None)

    with _runtime_lock:
        if _runtime_closed:
            return False, None
    if not _recompute_slots.acquire(blocking=False):
        return False, None

    try:
        future = IOLoop.current().run_in_executor(
            pool,
            recompute_chart_data,
            *args,
        )
    except Exception as exc:
        _recompute_slots.release()
        LogUtil.warning(f"[sse] recompute submit failed: {exc}")
        return False, None

    ctx["future"] = future
    with _runtime_lock:
        _runtime_inflight.add(future)

    def _finished(done_future):
        try:
            if not done_future.cancelled():
                done_future.exception()
        except (asyncio.CancelledError, Exception):
            pass
        with _runtime_lock:
            _runtime_inflight.discard(done_future)
            _runtime_timed_out.discard(done_future)
        try:
            _recompute_slots.release()
        except ValueError:
            LogUtil.warning("[sse] recompute slot released more than once")

    future.add_done_callback(_finished)
    try:
        value = await asyncio.wait_for(
            asyncio.shield(future),
            timeout=max(0.1, float(_RECOMPUTE_TIMEOUT_SECONDS)),
        )
        ctx.pop("future", None)
        return True, value
    except asyncio.TimeoutError:
        with _runtime_lock:
            if future in _runtime_inflight:
                _runtime_timed_out.add(future)
        LogUtil.warning(
            f"[sse] recompute timed out after "
            f"{max(0.1, float(_RECOMPUTE_TIMEOUT_SECONDS)):g}s"
        )
        return False, None
    except asyncio.CancelledError:
        future.cancel()
        raise
    except Exception as exc:
        ctx.pop("future", None)
        LogUtil.warning(f"[sse] recompute failed: {exc}")
        return True, None


def sse_runtime_status():
    with _runtime_lock:
        return {
            "inflight": len(_runtime_inflight),
            "timed_out": len(_runtime_timed_out),
            "closed": _runtime_closed,
        }


def start_sse_runtime():
    global _runtime_closed
    with _runtime_lock:
        # The HTTP server can accept an SSE connection just before the explicit
        # runtime bootstrap reaches this function.  In that case recomputes are
        # valid work on an already-open runtime, not evidence of a restart.
        if not _runtime_closed:
            return
        if _runtime_inflight:
            raise RuntimeError("cannot restart SSE runtime with active recomputes")
        _runtime_closed = False


def shutdown_sse_runtime():
    """Stop loops, reject submissions, and cancel queued recomputes best-effort."""
    global _runtime_closed
    with _runtime_lock:
        _runtime_closed = True
        futures = list(_runtime_inflight)
    for future in futures:
        future.cancel()
    subscriptions = list(getattr(_hub, "_subs", {}).values())
    for sub in subscriptions:
        loop = sub.get("loop")
        if loop is not None:
            try:
                loop.stop()
            except Exception:
                pass
        for client in list(sub.get("clients", ())):
            closed = getattr(client, "_closed", None)
            if closed is not None and not closed.is_set():
                closed.set()
    getattr(_hub, "_subs", {}).clear()
    return not any(not future.done() for future in futures)


def _reset_sse_runtime_for_tests(max_pending=8):
    global _runtime_closed, _recompute_slots, _RECOMPUTE_MAX_PENDING
    with _runtime_lock:
        if _runtime_inflight:
            raise RuntimeError("cannot reset SSE runtime with active recomputes")
        _runtime_timed_out.clear()
        _RECOMPUTE_MAX_PENDING = max(1, int(max_pending))
        _recompute_slots = threading.BoundedSemaphore(_RECOMPUTE_MAX_PENDING)
        _runtime_closed = False

def _refresh_interval_ms(market: str) -> int:
    # 默认值与 config 实际值(均 8000)对齐:getattr 默认仅在 config 缺该属性时生效,
    # 原 5000/3000 是与现实不符的死值,易误导读者以为"美股5s/其他3s"(审查 L2)。
    if market == "us":
        return int(getattr(config, "SSE_REFRESH_MS_US", 8000))
    return int(getattr(config, "SSE_REFRESH_MS", 8000))


def _client_identity(flask_app, cookie_header, remote_ip) -> str:
    cookies = parse_cookie(cookie_header or "")
    session_name = flask_app.config.get("SESSION_COOKIE_NAME", "session") or "session"
    session_value = cookies.get(session_name)
    serializer = flask_app.session_interface.get_signing_serializer(flask_app)
    if session_value and serializer is not None:
        try:
            session_data = serializer.loads(
                session_value,
                max_age=flask_app.permanent_session_lifetime.total_seconds(),
            )
        except Exception:
            session_data = None
        if isinstance(session_data, dict) and session_data.get("_user_id"):
            source = (
                f"session:{session_data['_user_id']}:"
                f"{session_data.get('_id', '')}"
            )
            return hashlib.sha256(source.encode("utf-8")).hexdigest()

    remember_name = (
        flask_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
        or "remember_token"
    )
    remember_value = cookies.get(remember_name)
    try:
        remember_user = (
            decode_cookie(remember_value, key=flask_app.secret_key)
            if remember_value
            else None
        )
    except Exception:
        remember_user = None
    if remember_user:
        source = f"remember:{remember_user}:{remote_ip or 'unknown'}"
    else:
        source = f"ip:{remote_ip or 'unknown'}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


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

        cookie_header = self.request.headers.get("Cookie")
        if not is_request_authenticated(self._flask_app, cookie_header):
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
        client_id = _client_identity(
            self._flask_app, cookie_header, self.request.remote_ip
        )
        if not _hub.subscribe(
            self._cache_key, self, start_loop, client_id=client_id
        ):
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
                    _completed, chart_data = await _run_recompute_bounded(
                        pool,
                        ctx,
                        (market, code, frequency, cl_config, cache_key),
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
                    clients = list(_hub.clients_of(cache_key))
                    if clients:
                        await asyncio.gather(
                            *(client._send(data_str) for client in clients)
                        )
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
            await asyncio.wait_for(self.flush(), timeout=_SEND_TIMEOUT_SECONDS)
        except Exception:
            self._unsub()

    async def _send_current_snapshot(self):
        """给"后加入既有循环"的 client 补发一次当前缓存的权威数据(H1)。只读缓存不重算;
        缓存为空(循环刚起还没算过)则跳过——此时它自己的 /tv/history firstDataRequest 已兜底。"""
        try:
            # HIGH-2: IOLoop 线程只读 RAM, 绝不同步 pickle-load 磁盘(会把整个 Tornado IOLoop /
            # 所有 SSE 客户端卡在磁盘 IO 上)。RAM miss 则跳过补发, 由该 client 自己的
            # firstDataRequest 兜底(注释已述"缓存为空则跳过")。
            from cl_app.services.chart_cache import _get_chart_cache_entry_ram_only
            entry = _get_chart_cache_entry_ram_only(self._cache_key)
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
