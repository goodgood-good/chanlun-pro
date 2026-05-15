"""B2 单元测试: 验证 CTPState 在多线程下的锁正确性 (不依赖 openctp_ctp).

测试 3 个并发场景:
1. next_order_ref(): N 线程 × M 次递增, 验证 ref 严格递增 + 无重复 + 总计 N*M
2. set_order / get_order: 并发写读, 验证无异常 + 所有写入最终可见
3. set_position + get_positions_snapshot: 在快照迭代时并发修改 positions,
   不抛 "dictionary changed size during iteration"
"""

import threading

import pytest

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
    assert state.get_order(f"w0_0") == {"ref": "w0_0", "wid": 0}
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
                state.set_position(f"dyn_{i}", {"i": i})
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
