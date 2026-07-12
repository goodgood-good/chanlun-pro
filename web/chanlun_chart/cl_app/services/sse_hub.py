"""SSE 订阅注册表：管理 cache_key→clients，以及每个 key 的刷新循环启停。

单进程 Tornado IOLoop 内调用，无需锁(若未来跨线程访问，调用方用
IOLoop.add_callback 投递回 IOLoop 线程)。循环句柄由调用方注入(start_loop_fn
返回一个带 .stop() 的对象), 本模块不依赖 Tornado, 便于纯单测。
"""
from collections import OrderedDict

from chanlun.tools.log_util import LogUtil


class SseHub:
    def __init__(
        self,
        max_loops: int = 100,
        max_clients_per_key: int = 16,
        max_connections_per_client: int = 8,
    ):
        self.max_loops = max(1, int(max_loops))
        self.max_clients_per_key = max(1, int(max_clients_per_key))
        self.max_connections_per_client = max(1, int(max_connections_per_client))
        # cache_key -> {"clients": set, "loop": handle}; OrderedDict 维护 LRU 顺序
        self._subs: "OrderedDict" = OrderedDict()

    def subscribe(self, cache_key, client, start_loop_fn, client_id=None) -> bool:
        """订阅。首个订阅者触发 start_loop_fn 启动循环。

        满载时只清理没有 client 的异常残留；若所有循环都活跃则拒绝新 key，绝不
        驱逐在线用户。另按 key 和客户端身份限制连接数，避免单个会话耗尽资源。
        """
        identity = client_id if client_id is not None else id(client)
        identity_count = sum(
            1
            for existing in self._subs.values()
            for value in existing.get("client_ids", {}).values()
            if value == identity
        )
        if identity_count >= self.max_connections_per_client:
            LogUtil.warning("[sse_hub] client connection limit reached")
            return False

        sub = self._subs.get(cache_key)
        if sub is None:
            while len(self._subs) >= self.max_loops:
                # 仅清理异常路径遗留的空循环；活跃循环不可淘汰。
                evict_key = next(
                    (k for k, s in self._subs.items() if not s.get("clients")), None
                )
                if evict_key is None:
                    LogUtil.warning(
                        f"[sse_hub] active loop limit reached ({self.max_loops})"
                    )
                    return False
                old_key = evict_key
                old_sub = self._subs.pop(old_key)
                old_loop = old_sub.get("loop")
                if old_loop is not None:
                    try:
                        old_loop.stop()
                    except Exception as e:
                        LogUtil.warning(f"[sse_hub] stop stale loop {old_key} failed: {e}")
                LogUtil.warning(f"[sse_hub] removed stale empty loop {old_key}")
            loop = start_loop_fn(cache_key)
            self._subs[cache_key] = {
                "clients": {client},
                "client_ids": {client: identity},
                "loop": loop,
            }
            return True
        if client in sub["clients"]:
            return True
        if len(sub["clients"]) >= self.max_clients_per_key:
            LogUtil.warning("[sse_hub] per-key client limit reached")
            return False
        sub["clients"].add(client)
        sub.setdefault("client_ids", {})[client] = identity
        self._subs.move_to_end(cache_key)  # 活跃 → 刷新 LRU 位置
        return True

    def unsubscribe(self, cache_key, client) -> None:
        """退订。最后一个订阅者离开时停止并删除该 key 的循环。"""
        sub = self._subs.get(cache_key)
        if not sub:
            return
        sub["clients"].discard(client)
        sub.setdefault("client_ids", {}).pop(client, None)
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
