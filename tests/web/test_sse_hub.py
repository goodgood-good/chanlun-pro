"""Task3: SseHub 订阅注册表生命周期。"""
from cl_app.services.sse_hub import SseHub


class FakeLoop:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_first_sub_starts_loop():
    hub = SseHub()
    started = []

    def start(k):
        loop = FakeLoop()
        started.append(loop)
        return loop

    assert hub.subscribe("k", "c1", start) is True
    assert len(started) == 1


def test_second_sub_no_new_loop():
    hub = SseHub()
    started = []

    def start(k):
        loop = FakeLoop()
        started.append(loop)
        return loop

    hub.subscribe("k", "c1", start)
    hub.subscribe("k", "c2", start)
    assert len(started) == 1
    assert hub.clients_of("k") == {"c1", "c2"}


def test_last_unsub_stops_loop():
    hub = SseHub()
    loops = []

    def start(k):
        loop = FakeLoop()
        loops.append(loop)
        return loop

    hub.subscribe("k", "c1", start)
    hub.subscribe("k", "c2", start)
    hub.unsubscribe("k", "c1")
    assert loops[0].stopped is False
    hub.unsubscribe("k", "c2")
    assert loops[0].stopped is True
    assert "k" not in hub.active_keys()


def test_max_loops_lru_evicts():
    """达上限时 LRU 淘汰最老循环而非拒绝 503: k2 订阅成功, k1 被淘汰并停止。"""
    hub = SseHub(max_loops=1)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    assert hub.subscribe("k1", "c", start) is True
    assert hub.subscribe("k2", "c", start) is True  # 不再 False; LRU 淘汰 k1
    assert loops["k1"].stopped is True
    assert "k1" not in hub.active_keys()
    assert "k2" in hub.active_keys()


def test_active_sub_refreshes_lru():
    """活跃订阅刷新 LRU 位置, 不被优先淘汰。"""
    hub = SseHub(max_loops=2)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    hub.subscribe("k1", "c", start)
    hub.subscribe("k2", "c", start)
    hub.subscribe("k1", "c2", start)  # k1 再活跃 → 移到 LRU 末尾
    hub.subscribe("k3", "c", start)   # 达上限 → 淘汰最老 k2(非 k1)
    assert loops["k2"].stopped is True
    assert loops["k1"].stopped is False
    assert set(hub.active_keys()) == {"k1", "k3"}
