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


def test_max_loops_rejects():
    hub = SseHub(max_loops=1)

    def start(k):
        return FakeLoop()

    assert hub.subscribe("k1", "c", start) is True
    assert hub.subscribe("k2", "c", start) is False
