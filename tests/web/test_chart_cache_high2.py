"""HIGH-2: chart 缓存磁盘 pickle-load 不得在全局 cache_lock 内同步执行。

否则一次 RAM-miss 读盘(数百KB~MB pickle)会把 cache_lock 持有到 IO 结束 → 串行化所有
tv_history/写/SSE; 且 IOLoop 线程(_send_current_snapshot)走磁盘分支会卡住整个 Tornado
IOLoop(所有 SSE 客户端)。

本测试钉死两条不变量:
  1. _get_chart_cache_entry 的磁盘读发生在 cache_lock **之外**(另一线程读盘期间可拿锁);
  2. _get_chart_cache_entry_ram_only 只读 RAM、绝不触磁盘(供 IOLoop 路径用)。
"""
import pathlib
import sys
import threading

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

from cl_app.services import chart_cache  # noqa: E402


def test_disk_read_happens_outside_cache_lock(monkeypatch):
    """RAM miss 时, 磁盘读期间 cache_lock 可被其他线程获取(=读盘没持全局锁)。"""
    key = "test:HIGH2:1m"
    chart_cache.chart_data_cache.pop(key, None)  # 确保 RAM miss
    lock_acquirable = []

    def _mock_disk(k):
        # 在"磁盘读"内, 另一线程尝试非阻塞获取 cache_lock。拿到 = 读盘没持锁。
        result = []

        def _try():
            got = chart_cache.cache_lock.acquire(blocking=False)
            result.append(got)
            if got:
                chart_cache.cache_lock.release()

        th = threading.Thread(target=_try)
        th.start()
        th.join()
        lock_acquirable.append(result[0])
        return {"t": [1, 2], "o": [1.0, 1.0], "h": [1.0, 1.0], "l": [1.0, 1.0], "c": [1.0, 1.0], "v": [1, 1]}

    monkeypatch.setattr(chart_cache.fdb, "get_chart_cache", _mock_disk)
    try:
        entry = chart_cache._get_chart_cache_entry(key)
        assert entry is not None  # 磁盘命中后正常返回
        assert lock_acquirable == [True], (
            "磁盘读期间 cache_lock 应可被其他线程获取(=读盘没持全局锁)"
        )
    finally:
        chart_cache.chart_data_cache.pop(key, None)


def test_ram_only_never_touches_disk(monkeypatch):
    """ram-only 读: RAM miss 直接返回 None, 绝不调 fdb.get_chart_cache(IOLoop 安全)。"""
    key = "test:RAMONLY:1m"
    chart_cache.chart_data_cache.pop(key, None)
    called = []
    monkeypatch.setattr(chart_cache.fdb, "get_chart_cache", lambda k: called.append(k) or {"x": 1})

    r = chart_cache._get_chart_cache_entry_ram_only(key)
    assert r is None, "RAM miss 应返回 None"
    assert called == [], "ram-only 不应触磁盘"


def test_ram_only_returns_ram_hit():
    """ram-only 读: RAM 命中返回规范化 entry。"""
    key = "test:RAMHIT:1m"
    data = {"t": [1], "o": [1.0], "h": [1.0], "l": [1.0], "c": [1.0], "v": [1]}
    entry = chart_cache._build_chart_cache_entry(data, is_full_snapshot=True)
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[key] = entry
    try:
        r = chart_cache._get_chart_cache_entry_ram_only(key)
        assert r is not None and r.get("data") == data
    finally:
        chart_cache.chart_data_cache.pop(key, None)
