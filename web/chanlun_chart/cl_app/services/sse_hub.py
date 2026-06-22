"""SSE 订阅注册表：管理 cache_key→clients，以及每个 key 的刷新循环启停。

单进程 Tornado IOLoop 内调用，无需锁(若未来跨线程访问，调用方用
IOLoop.add_callback 投递回 IOLoop 线程)。循环句柄由调用方注入(start_loop_fn
返回一个带 .stop() 的对象), 本模块不依赖 Tornado, 便于纯单测。
"""
from chanlun.tools.log_util import LogUtil


class SseHub:
    def __init__(self, max_loops: int = 100):
        self.max_loops = max_loops
        # cache_key -> {"clients": set, "loop": handle}
        self._subs: dict = {}

    def subscribe(self, cache_key, client, start_loop_fn) -> bool:
        """订阅。首个订阅者触发 start_loop_fn 启动循环；达上限则拒绝并返回 False。"""
        sub = self._subs.get(cache_key)
        if sub is None:
            if len(self._subs) >= self.max_loops:
                LogUtil.warning(
                    f"[sse_hub] 拒绝订阅 {cache_key}: 达并发循环上限 {self.max_loops}"
                )
                return False
            loop = start_loop_fn(cache_key)
            self._subs[cache_key] = {"clients": {client}, "loop": loop}
            return True
        sub["clients"].add(client)
        return True

    def unsubscribe(self, cache_key, client) -> None:
        """退订。最后一个订阅者离开时停止并删除该 key 的循环。"""
        sub = self._subs.get(cache_key)
        if not sub:
            return
        sub["clients"].discard(client)
        if not sub["clients"]:
            loop = sub.get("loop")
            if loop is not None:
                try:
                    loop.stop()
                except Exception as e:
                    LogUtil.warning(f"[sse_hub] 停止循环 {cache_key} 失败: {e}")
            del self._subs[cache_key]

    def clients_of(self, cache_key) -> set:
        sub = self._subs.get(cache_key)
        return set(sub["clients"]) if sub else set()

    def active_keys(self) -> list:
        return list(self._subs.keys())
