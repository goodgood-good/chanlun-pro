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


def test_max_loops_rejects_new_key_without_evicting_active_stream():
    """满载时拒绝新 key，已连接用户的流不能被驱逐。"""
    hub = SseHub(max_loops=1)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    assert hub.subscribe("k1", "c", start) is True
    assert hub.subscribe("k2", "c2", start) is False
    assert loops["k1"].stopped is False
    assert "k1" in hub.active_keys()
    assert "k2" not in hub.active_keys()


def test_existing_key_can_add_client_without_consuming_loop_slot():
    hub = SseHub(max_loops=2)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    hub.subscribe("k1", "c", start)
    hub.subscribe("k2", "c", start)
    assert hub.subscribe("k1", "c2", start) is True
    assert hub.subscribe("k3", "c3", start) is False
    assert loops["k2"].stopped is False
    assert loops["k1"].stopped is False
    assert set(hub.active_keys()) == {"k1", "k2"}


class FakeClient:
    def __init__(self, name="c"):
        self.name = name
        self.unsubbed = False

    def _unsub(self):
        self.unsubbed = True


def test_full_hub_never_unsubscribes_active_clients():
    hub = SseHub(max_loops=2)
    loops = {}

    def start(k):
        loop = FakeLoop()
        loops[k] = loop
        return loop

    a, b, c = FakeClient("a"), FakeClient("b"), FakeClient("c")
    hub.subscribe("k1", a, start)
    hub.subscribe("k2", b, start)
    assert hub.subscribe("k3", c, start) is False
    assert loops["k1"].stopped is False
    assert a.unsubbed is False
    assert b.unsubbed is False
    assert set(hub.active_keys()) == {"k1", "k2"}


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


def test_connection_limit_applies_per_client_identity():
    hub = SseHub(max_loops=10, max_connections_per_client=2)

    def start(_key):
        return FakeLoop()

    assert hub.subscribe("k1", FakeClient("a"), start, client_id="session-a") is True
    assert hub.subscribe("k2", FakeClient("b"), start, client_id="session-a") is True
    assert hub.subscribe("k3", FakeClient("c"), start, client_id="session-a") is False
    assert hub.subscribe("k3", FakeClient("d"), start, client_id="session-b") is True


def test_connection_limit_applies_per_key():
    hub = SseHub(max_clients_per_key=2)

    def start(_key):
        return FakeLoop()

    assert hub.subscribe("k", FakeClient("a"), start, client_id="a") is True
    assert hub.subscribe("k", FakeClient("b"), start, client_id="b") is True
    assert hub.subscribe("k", FakeClient("c"), start, client_id="c") is False
