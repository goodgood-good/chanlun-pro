"""B2 + B2 follow-up 单元测试: 验证 CTPState 锁/事件正确性 (不依赖 openctp_ctp).

测试场景:
1. next_order_ref(): N 线程 × M 次递增, 验证 ref 严格递增 + 无重复 + 总计 N*M
2. set_order / get_order: 并发写读, 验证无异常 + 所有写入最终可见
3. set_position + get_positions_snapshot: 在快照迭代时并发修改 positions,
   不抛 "dictionary changed size during iteration"
4. wait_for_order: 注册等待 + 另一线程 set_order 触发, wait 立即返回
5. wait_for_order timeout: 无回调时按指定超时返回 False
6. wait_for_position_query: prepare + mark_done 触发, wait 立即返回
7. next_request_id: 与 order_ref 独立递增, 各自原子
"""

import threading
import time


from chanlun.trader._ctp_state import CTPState


def test_next_order_ref_strictly_monotonic_no_duplicates():
    """8 线程 × 1000 递增 → 总数 8000, 全部唯一, 严格递增 1..8000."""
    state = CTPState()
    N_THREADS = 8
    M_PER_THREAD = 1000

    collected = []
    collected_lock = threading.Lock()

    def worker():
        local = [state.next_order_ref() for _ in range(M_PER_THREAD)]
        with collected_lock:
            collected.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(collected) == N_THREADS * M_PER_THREAD
    # 唯一性: 无重复 ref
    assert len(set(collected)) == N_THREADS * M_PER_THREAD, "duplicate order_ref"
    # 严格递增 1..N*M
    as_int_sorted = sorted(int(r) for r in collected)
    assert as_int_sorted == list(range(1, N_THREADS * M_PER_THREAD + 1)), (
        f"non-contiguous refs: first={as_int_sorted[:5]} last={as_int_sorted[-5:]}"
    )
    # state.order_ref 终值
    assert state.order_ref == N_THREADS * M_PER_THREAD


def test_set_order_get_order_concurrent_writers_readers():
    """4 写线程并发 set_order, 4 读线程并发 get_order, 最终所有写入可见."""
    state = CTPState()
    WRITERS = 4
    READERS = 4
    N_PER_WRITER = 500
    stop_readers = threading.Event()

    read_errors = []

    def writer(worker_id):
        for i in range(N_PER_WRITER):
            ref = f"w{worker_id}_{i}"
            state.set_order(ref, {"ref": ref, "wid": worker_id})

    def reader():
        try:
            while not stop_readers.is_set():
                # 随机读取一个可能存在的 ref; 不存在则返回 None, 不应抛
                _ = state.get_order("w0_0")
                _ = state.get_order("nonexistent")
        except Exception as e:
            read_errors.append(e)

    writer_threads = [threading.Thread(target=writer, args=(i,)) for i in range(WRITERS)]
    reader_threads = [threading.Thread(target=reader) for _ in range(READERS)]
    for t in reader_threads:
        t.start()
    for t in writer_threads:
        t.start()
    for t in writer_threads:
        t.join()
    stop_readers.set()
    for t in reader_threads:
        t.join()

    assert not read_errors, f"reader 抛异常: {read_errors[0]!r}"
    # 所有写入均可见
    expected = WRITERS * N_PER_WRITER
    assert len(state.orders) == expected, f"orders={len(state.orders)} != {expected}"
    # 抽查一些 ref 仍可读到
    assert state.get_order("w0_0") == {"ref": "w0_0", "wid": 0}
    assert state.get_order(f"w{WRITERS - 1}_{N_PER_WRITER - 1}") is not None


def test_positions_snapshot_isolation_under_concurrent_writes():
    """迭代 get_positions_snapshot 时另一线程并发 set_position,
    迭代不应抛 "dictionary changed size during iteration"."""
    state = CTPState()
    # 预填一些初始 positions
    for i in range(50):
        state.set_position(f"pre_{i}", {"i": i})

    stop = threading.Event()
    writer_errors = []
    iterator_errors = []

    def writer():
        try:
            i = 100
            while not stop.is_set():
                # i % 500 限制 key 空间：持续并发写但 positions 不无界增长，
                # 避免 CI runner 慢时 iterator 迟迟不结束导致 OOM
                state.set_position(f"dyn_{i % 500}", {"i": i})
                i += 1
        except Exception as e:
            writer_errors.append(e)

    def iterator():
        try:
            for _ in range(100):
                snap = state.get_positions_snapshot()
                # 迭代快照: 即使外部并发修改 positions 也不影响 snap
                for k, v in snap.items():
                    assert "i" in v
        except Exception as e:
            iterator_errors.append(e)

    w = threading.Thread(target=writer)
    it = threading.Thread(target=iterator)
    w.start()
    it.start()
    it.join()
    stop.set()
    w.join()

    assert not writer_errors, f"writer 异常: {writer_errors[0]!r}"
    assert not iterator_errors, f"iterator 异常: {iterator_errors[0]!r}"


def test_wait_for_order_woken_by_set_order():
    """注册等待 → 另一线程 set_order → wait_for_order 立即返回 True."""
    state = CTPState()
    ref = state.next_order_ref()
    state.register_order_wait(ref)

    def callback_thread():
        time.sleep(0.05)  # 模拟 CTP 回调延迟
        state.set_order(ref, {"ref": ref, "status": "filled"})

    t0 = time.perf_counter()
    threading.Thread(target=callback_thread).start()
    woken = state.wait_for_order(ref, timeout=2.0)
    elapsed = time.perf_counter() - t0

    assert woken is True
    # 应远小于 2s timeout, 验证是 Event 触发而非超时
    assert elapsed < 0.5, f"wait_for_order 耗时 {elapsed:.3f}s 异常 (应 <0.5s)"
    order = state.get_order(ref)
    assert order is not None and order["status"] == "filled"


def test_wait_for_order_returns_false_on_timeout():
    """无 set_order 触发时, wait_for_order 按指定超时返回 False."""
    state = CTPState()
    ref = state.next_order_ref()
    state.register_order_wait(ref)

    t0 = time.perf_counter()
    woken = state.wait_for_order(ref, timeout=0.15)
    elapsed = time.perf_counter() - t0

    assert woken is False
    # 应接近 0.15s, 不应远超
    assert 0.10 <= elapsed < 0.5, f"timeout 行为异常: 实际 {elapsed:.3f}s"


def test_wait_for_position_query_woken_by_mark_done():
    """prepare → 另一线程 mark_position_query_done → wait 立即返回 True."""
    state = CTPState()
    state.prepare_position_query()

    def callback_thread():
        time.sleep(0.05)
        state.set_position("rb2401_2", {"InstrumentID": "rb2401"})
        state.mark_position_query_done()

    t0 = time.perf_counter()
    threading.Thread(target=callback_thread).start()
    woken = state.wait_for_position_query(timeout=2.0)
    elapsed = time.perf_counter() - t0

    assert woken is True
    assert elapsed < 0.5
    assert state.get_position_count() == 1


def test_next_request_id_independent_from_order_ref():
    """next_request_id 与 next_order_ref 互不影响, 各自原子递增."""
    state = CTPState()

    # 交错调用
    ref1 = state.next_order_ref()
    rid1 = state.next_request_id()
    ref2 = state.next_order_ref()
    rid2 = state.next_request_id()
    rid3 = state.next_request_id()

    assert ref1 == "1" and ref2 == "2"
    assert rid1 == 1 and rid2 == 2 and rid3 == 3
    # 内部计数器
    assert state.order_ref == 2
    assert state._request_id == 3


def test_next_request_id_concurrent_strictly_monotonic():
    """4 线程 × 500 次 next_request_id → 严格递增 1..2000, 无重复."""
    state = CTPState()
    collected = []
    lock = threading.Lock()

    def worker():
        local = [state.next_request_id() for _ in range(500)]
        with lock:
            collected.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(collected) == 2000
    assert sorted(collected) == list(range(1, 2001))
    assert state._request_id == 2000
