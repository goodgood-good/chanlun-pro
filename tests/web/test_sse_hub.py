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


class FakeClient:
    def __init__(self, name="c"):
        self.name = name
        self.unsubbed = False

    def _unsub(self):
        self.unsubbed = True


def test_lru_force_evict_unsubs_active_clients():
    """M3: 满载且全部有活跃 client 时,强制淘汰最老循环并 _unsub 其 client(令前端重连),
    而非留半僵尸连接(循环停了但 client 卡在 _closed.wait、连心跳都收不到)。"""
    hub = SseHub(max_loops=2)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    a, b, c = FakeClient("a"), FakeClient("b"), FakeClient("c")
    hub.subscribe("k1", a, start)
    hub.subscribe("k2", b, start)
    hub.subscribe("k3", c, start)  # 满载,k1/k2 都有 client → 强制淘汰最老 k1
    assert loops["k1"].stopped is True
    assert a.unsubbed is True       # 被淘汰循环的 client 已 _unsub(干净关闭)
    assert b.unsubbed is False      # 未被淘汰的不动
    assert set(hub.active_keys()) == {"k2", "k3"}


def test_lru_prefers_empty_sub_over_active():
    """M3: 存在空闲 sub(异常残留)时优先淘汰它,不碰有活跃 client 的循环。"""
    hub = SseHub(max_loops=2)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    x, y = FakeClient("x"), FakeClient("y")
    hub.subscribe("kx", x, start)
    hub.subscribe("ky", y, start)
    hub._subs["kx"]["clients"].clear()   # 模拟 kx 变空(异常残留)
    hub.subscribe("kz", FakeClient("z"), start)  # 应优先淘汰空闲 kx,保留有 client 的 ky
    assert "kx" not in hub.active_keys()
    assert "ky" in hub.active_keys()
    assert y.unsubbed is False           # 非强制路径,ky 的 client 不被 unsub
