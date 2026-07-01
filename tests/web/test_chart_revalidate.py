"""锁定后台重验证(方向1)的去重语义。

stale-while-revalidate 的后台刷新:同一 cache_key 在飞(inflight)时重复提交必须
被跳过(去重),否则用户快速切标的 / SSE 8s 轮询会把同一标的重算堆叠成 N 份,
反而加剧 CPU/QMT 争抢。任务结束(成功或异常)后必须把 key 清出 inflight,
否则一次失败会永久阻塞该 key 后续刷新。
"""
import threading
import time

from cl_app.services import chart_revalidate


def _wait_until(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_submit_revalidation_dedups_inflight(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake(market, code, frequency, cl_config, cache_key):
        calls.append(code)
        started.set()
        release.wait(2.0)

    monkeypatch.setattr(chart_revalidate, "_do_revalidate", fake)

    ok1 = chart_revalidate.submit_revalidation("a", "X", "5m", {}, "KEY")
    assert ok1 is True
    assert started.wait(2.0)  # 任务已在跑且阻塞

    # 同 key 在飞 → 去重跳过
    ok2 = chart_revalidate.submit_revalidation("a", "X", "5m", {}, "KEY")
    assert ok2 is False

    release.set()
    assert _wait_until(lambda: "KEY" not in chart_revalidate._inflight)
    assert calls == ["X"]  # 只真正执行了一次


def test_inflight_cleared_after_completion(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(
        chart_revalidate, "_do_revalidate", lambda *a: release.wait(2.0)
    )
    assert chart_revalidate.submit_revalidation("a", "Y", "5m", {}, "K2") is True
    release.set()
    assert _wait_until(lambda: "K2" not in chart_revalidate._inflight)

    # 清出后可再次提交
    monkeypatch.setattr(chart_revalidate, "_do_revalidate", lambda *a: None)
    assert chart_revalidate.submit_revalidation("a", "Y", "5m", {}, "K2") is True
    assert _wait_until(lambda: "K2" not in chart_revalidate._inflight)


def test_inflight_cleared_on_exception(monkeypatch):
    def boom(*a):
        raise RuntimeError("compute failed")

    monkeypatch.setattr(chart_revalidate, "_do_revalidate", boom)
    assert chart_revalidate.submit_revalidation("a", "Z", "5m", {}, "K3") is True
    # 即便计算抛异常,inflight 也必须清出,不能永久阻塞后续刷新
    assert _wait_until(lambda: "K3" not in chart_revalidate._inflight)


def test_do_revalidate_holds_chart_calc_locks(monkeypatch):
    """_do_revalidate 必须持有 chart_calc_locks(cache_key),与 tv_history/sse_refresh
    的重算路径互斥——否则后台重验证会与它们对同一 cache_key 并发全量重算,读到
    "半新半旧"的 existing_entry(2026-07 深度审查发现的锁域不一致问题)。"""
    from cl_app.services.chart_compute import chart_calc_locks

    cache_key = "a:LOCKCHECK:5m"
    entered = threading.Event()
    release = threading.Event()

    def fake_compute(market, code, frequency, cl_config):
        entered.set()
        release.wait(2.0)
        return True

    monkeypatch.setattr(
        "cl_app.services.chart_compute.compute_and_cache_chart_data", fake_compute
    )

    ok = chart_revalidate.submit_revalidation("a", "LOCKCHECK", "5m", {}, cache_key)
    assert ok is True
    assert entered.wait(2.0), "后台重验证应已开始执行"

    # 此刻 _do_revalidate 正卡在 fake_compute 内部(release 尚未 set)。
    # 若它持有 chart_calc_locks(cache_key),同一 key 的非阻塞获取必须失败。
    lock = chart_calc_locks.get(cache_key)
    got = lock.acquire(blocking=False)
    try:
        assert got is False, (
            "chart_revalidate 执行期间应持有 chart_calc_locks(cache_key),"
            "与 tv_history/sse_refresh 的重算路径互斥"
        )
    finally:
        if got:
            lock.release()
        release.set()
        _wait_until(lambda: cache_key not in chart_revalidate._inflight)
