"""R8-C2: compute_and_cache_chart_data(prewarm 全量计算)现取 per-key chart_calc_locks 非阻塞,
消除预热的 cl_data_to_tv_chart 读共享 CL 与用户 path-2 process_klines 改写的并发撕裂几何。
用户/他方正持同 key 锁时预热让位跳过(返回 True 不进 compute body); RLock 可重入使 _do_revalidate
等已持锁调用方嵌套即成功(不误跳过)。monkeypatch _impl 探测是否进入 body, 避免真实 exchange 依赖。"""

import threading

from cl_app.services import chart_compute


def _patch_impl(monkeypatch, sink):
    monkeypatch.setattr(
        chart_compute,
        "_compute_and_cache_chart_data_impl",
        lambda *a, **k: sink.append(1) or True,
    )


def test_prewarm_yields_when_calc_lock_held_by_other_thread(monkeypatch):
    key = chart_compute._build_cache_key("a", "CTEST", "1m", {})
    lock = chart_compute.chart_calc_locks.get(key)
    called = []
    _patch_impl(monkeypatch, called)
    acquired, release = threading.Event(), threading.Event()

    def _holder():
        lock.acquire()
        acquired.set()
        release.wait(3)
        lock.release()

    t = threading.Thread(target=_holder)
    t.start()
    try:
        assert acquired.wait(3)
        result = chart_compute.compute_and_cache_chart_data("a", "CTEST", "1m", {})
        assert result is True       # 让位返回 True
        assert called == []         # 未进 compute body(被 skip)
    finally:
        release.set()
        t.join(3)


def test_prewarm_computes_when_lock_free(monkeypatch):
    called = []
    _patch_impl(monkeypatch, called)
    result = chart_compute.compute_and_cache_chart_data("a", "CTEST2", "1m", {})
    assert result is True
    assert called == [1]            # 无竞争 → 进 compute body


def test_reentrant_when_caller_holds_lock(monkeypatch):
    # _do_revalidate 模式: 同线程已持锁再调 → RLock 可重入 → 仍进 body(不误跳过)
    key = chart_compute._build_cache_key("a", "CTEST3", "1m", {})
    lock = chart_compute.chart_calc_locks.get(key)
    called = []
    _patch_impl(monkeypatch, called)
    with lock:
        result = chart_compute.compute_and_cache_chart_data("a", "CTEST3", "1m", {})
    assert result is True
    assert called == [1]
