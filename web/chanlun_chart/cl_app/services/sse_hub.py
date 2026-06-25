"""SSE 订阅注册表：管理 cache_key→clients，以及每个 key 的刷新循环启停。

单进程 Tornado IOLoop 内调用，无需锁(若未来跨线程访问，调用方用
IOLoop.add_callback 投递回 IOLoop 线程)。循环句柄由调用方注入(start_loop_fn
返回一个带 .stop() 的对象), 本模块不依赖 Tornado, 便于纯单测。
"""
from collections import OrderedDict

from chanlun.tools.log_util import LogUtil


class SseHub:
    def __init__(self, max_loops: int = 100):
        self.max_loops = max_loops
        # cache_key -> {"clients": set, "loop": handle}; OrderedDict 维护 LRU 顺序
        self._subs: "OrderedDict" = OrderedDict()

    def subscribe(self, cache_key, client, start_loop_fn) -> bool:
        """订阅。首个订阅者触发 start_loop_fn 启动循环。

        达上限时不再拒绝(原返回 False → 前端 503 → 该标的永久拿不到实时)，而是 LRU
        淘汰最久未活跃的循环、释放槽位再订阅。多标的频繁切换/异常断连残留的旧循环
        不会把新标的挡在 503 外(切标的卡死期曾因频繁 reload 残留累积到上限触发 503)。
        """
        sub = self._subs.get(cache_key)
        if sub is None:
            while len(self._subs) >= self.max_loops:
                # 优先淘汰"无活跃 client"的循环(异常路径残留);避免砍掉仍有在线用户的循环 →
                # 否则该用户 SSE 静默失效(连心跳都停)+半僵尸长连接卡在 _closed.wait()(审查 M3)。
                evict_key = next(
                    (k for k, s in self._subs.items() if not s.get("clients")), None
                )
                if evict_key is not None:
                    old_key = evict_key
                    old_sub = self._subs.pop(old_key)
                    forced = False
                else:
                    # 全部都有活跃 client = 真过载:淘汰最旧的,但把它的 client 也干净关闭
                    # (_unsub → set _closed → 前端 onerror 重连/退回轮询),不留半僵尸连接。
                    old_key, old_sub = self._subs.popitem(last=False)
                    forced = True
                old_loop = old_sub.get("loop")
                if old_loop is not None:
                    try:
                        old_loop.stop()
                    except Exception as e:
                        LogUtil.warning(f"[sse_hub] LRU 淘汰停止循环 {old_key} 失败: {e}")
                if forced:
                    for c in list(old_sub.get("clients") or ()):
                        try:
                            c._unsub()
                        except Exception:
                            pass
                LogUtil.warning(
                    f"[sse_hub] 达上限 {self.max_loops}，LRU 淘汰循环 {old_key}"
                    + ("(强制:已关闭其在线client令其重连)" if forced else "(空闲)")
                )
            loop = start_loop_fn(cache_key)
            self._subs[cache_key] = {"clients": {client}, "loop": loop}
            return True
        sub["clients"].add(client)
        self._subs.move_to_end(cache_key)  # 活跃 → 刷新 LRU 位置
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
