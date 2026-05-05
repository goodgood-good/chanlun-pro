"""PrewarmManager 最小回归测试集 (M6)。

覆盖：
- _prioritize_hot_codes (M2 / 业务核心)
- PrewarmTask to_dict / from_dict (序列化 + 老格式向后兼容)
- _load_done_codes (M5 + UnicodeDecodeError 容错)
- cancel() 三态语义 (Q5 修复)
- start() 速率限制 (M1)

不覆盖（成本高 / 依赖外部）：
- _run_task 端到端跑预热（需 mock 大量 chanlun.exchange）
- _persist_task 多线程并发写盘竞争（已通过手动 round-trip 验证）
"""
import time

import pytest

from cl_app.blueprints.symbols import (  # noqa: E402
    _PREWARM_DONE_SUFFIX,
    PrewarmManager,
    PrewarmTask,
)


# ===========================================================================
# 纯函数测试：_prioritize_hot_codes
# ===========================================================================


class TestPrioritizeHotCodes:
    """staticmethod，不需要 manager 实例。"""

    def test_basic_promotion(self):
        pending = [{"code": c} for c in "ABCD"]
        result = PrewarmManager._prioritize_hot_codes(
            pending, hot_codes=["C", "D"], processed=set(), cursor=0
        )
        assert [x["code"] for x in result] == ["C", "D", "A", "B"]

    def test_empty_hot_returns_unchanged(self):
        pending = [{"code": "A"}, {"code": "B"}]
        result = PrewarmManager._prioritize_hot_codes(
            pending, hot_codes=[], processed=set(), cursor=0
        )
        assert [x["code"] for x in result] == ["A", "B"]

    def test_already_processed_not_promoted(self):
        # 'A' 在 hot_codes 但已 processed → 不再上提；只有 'C' 上前
        pending = [{"code": c} for c in "ABC"]
        result = PrewarmManager._prioritize_hot_codes(
            pending, hot_codes=["C", "A"], processed={"A"}, cursor=0
        )
        assert [x["code"] for x in result] == ["C", "A", "B"]

    def test_cursor_protects_submitted_head(self):
        # cursor=2 表示 A、B 已提交，仅重排尾部 [C, D]
        pending = [{"code": c} for c in "ABCD"]
        result = PrewarmManager._prioritize_hot_codes(
            pending, hot_codes=["D"], processed=set(), cursor=2
        )
        assert [x["code"] for x in result] == ["A", "B", "D", "C"]

    def test_hot_order_preserved(self):
        # hot_codes 内部顺序 ['B', 'A'] 应被尊重
        pending = [{"code": c} for c in "ABC"]
        result = PrewarmManager._prioritize_hot_codes(
            pending, hot_codes=["B", "A"], processed=set(), cursor=0
        )
        assert [x["code"] for x in result] == ["B", "A", "C"]


# ===========================================================================
# 序列化：PrewarmTask.to_dict / from_dict
# ===========================================================================


class TestPrewarmTaskRoundTrip:
    def test_full_roundtrip(self):
        t1 = PrewarmTask(market="a", total=100)
        t1.done = 50
        t1.succeeded = 45
        t1.failed = 5
        t1.current = ("CODE_X", "NAME_X")
        t1.status = "running"
        t1.error_msg = "oops"
        t1.resumed_skipped = 8
        d = t1.to_dict()
        t2 = PrewarmTask.from_dict(d)
        assert (t2.market, t2.total, t2.done) == ("a", 100, 50)
        assert (t2.succeeded, t2.failed) == (45, 5)
        assert t2.current == ("CODE_X", "NAME_X")
        assert t2.status == "running"
        assert t2.error_msg == "oops"
        assert t2.resumed_skipped == 8

    def test_missing_fields_use_defaults(self):
        # 老格式 JSON：仅含必要字段
        d = {"market": "us"}
        t = PrewarmTask.from_dict(d)
        assert t.market == "us"
        assert t.total == 0
        assert t.done == 0
        assert t.current == ("", "")
        assert t.resumed_skipped == 0
        assert t.error_msg == ""
        # from_dict 默认 status='aborted'（保留中断恢复语义）
        assert t.status == "aborted"


# ===========================================================================
# _load_done_codes：M5 resume + 损坏文件容错
# ===========================================================================


class TestLoadDoneCodes:
    def _make_pm(self, persist_dir):
        # 跳过 __init__（避免触发 _load_persisted_tasks 扫盘）
        pm = PrewarmManager.__new__(PrewarmManager)
        pm._persist_dir = lambda: persist_dir
        return pm

    def test_no_file_returns_empty(self, tmp_path):
        pm = self._make_pm(tmp_path)
        assert pm._load_done_codes("any_market") == set()

    def test_loads_skipping_blank_lines(self, tmp_path):
        pm = self._make_pm(tmp_path)
        path = tmp_path / f"mkt{_PREWARM_DONE_SUFFIX}"
        path.write_text("A\nB\n  \n\nC\n", encoding="utf-8")
        assert pm._load_done_codes("mkt") == {"A", "B", "C"}

    def test_tolerates_corrupt_utf8(self, tmp_path):
        pm = self._make_pm(tmp_path)
        path = tmp_path / f"corrupt{_PREWARM_DONE_SUFFIX}"
        # 半截 UTF-8 多字节 + 非法字节
        path.write_bytes(b"OK_ROW\n\xff\xff invalid \xff\n")
        # 不应抛 UnicodeDecodeError；返回空集
        assert pm._load_done_codes("corrupt") == set()


# ===========================================================================
# manager fixture：per-test 隔离 + 重置模块级速率限制状态
# ===========================================================================


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """每个测试用例独立的 PrewarmManager + 隔离数据目录。"""
    import chanlun.config as _cfg

    monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)

    # 重置模块级速率限制时间戳（其他测试用例可能写过）
    import cl_app.blueprints.symbols as s

    s._prewarm_last_start_at.clear()
    return PrewarmManager()


# ===========================================================================
# cancel() 三态语义（Q5 修复）
# ===========================================================================


class TestCancelSemantics:
    def test_no_task_returns_not_found_code(self, manager):
        result = manager.cancel("never_existed")
        assert result["ok"] is False
        assert result.get("code") == "not_found"

    def test_finished_task_idempotent_ok(self, manager):
        task = PrewarmTask(market="xx", total=10)
        task.status = "finished"
        task.finished_at = time.time()
        with manager._lock:
            manager._tasks["xx"] = task
        result = manager.cancel("xx")
        assert result["ok"] is True
        assert result.get("cancelled") is False

    def test_running_task_sets_cancel_event(self, manager):
        task = PrewarmTask(market="yy", total=10)
        # status 默认 "running"
        with manager._lock:
            manager._tasks["yy"] = task
        result = manager.cancel("yy")
        assert result["ok"] is True
        assert result.get("cancelled") is True
        assert task.cancel_event.is_set()


# ===========================================================================
# 速率限制（M1）
# ===========================================================================


class TestRateLimit:
    def test_rejects_when_recently_started(self, manager, monkeypatch):
        import cl_app.blueprints.symbols as s

        monkeypatch.setattr(s, "PREWARM_RATE_LIMIT_SECONDS", 60)
        s._prewarm_last_start_at["rl_a"] = time.time()
        result = manager.start("rl_a", [{"code": "X", "name": "X"}])
        assert result["ok"] is False
        assert result.get("code") == "rate_limited"

    def test_disabled_rate_limit_allows_immediate_start(self, manager, monkeypatch):
        import cl_app.blueprints.symbols as s

        # 禁用速率限制
        monkeypatch.setattr(s, "PREWARM_RATE_LIMIT_SECONDS", 0)
        s._prewarm_last_start_at["rl_b"] = time.time()
        # mock _run_task 让 worker 线程立即返回，不跑真实预热
        monkeypatch.setattr(manager, "_run_task", lambda task, codes: None)
        result = manager.start("rl_b", [{"code": "X", "name": "X"}])
        # 关键断言：不被速率限制拦截
        assert result.get("code") != "rate_limited"

    def test_first_start_not_rate_limited(self, manager, monkeypatch):
        import cl_app.blueprints.symbols as s

        monkeypatch.setattr(s, "PREWARM_RATE_LIMIT_SECONDS", 60)
        # 没有历史时间戳 → 第一次启动应通过速率限制
        monkeypatch.setattr(manager, "_run_task", lambda task, codes: None)
        result = manager.start("first_run", [{"code": "X", "name": "X"}])
        assert result.get("code") != "rate_limited"


# ===========================================================================
# D：_load_persisted_tasks 惰性加载
# ===========================================================================


class TestLazyLoad:
    def test_init_does_not_load(self, tmp_path, monkeypatch):
        """PrewarmManager() 不应在 __init__ 时同步扫盘。"""
        import json as _json

        import chanlun.config as _cfg

        monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)

        # 先在数据目录写一个 task json，看 __init__ 是否真不读它
        persist_dir = tmp_path / "prewarm_status"
        persist_dir.mkdir(parents=True, exist_ok=True)
        (persist_dir / "lazyt.json").write_text(
            _json.dumps({
                "market": "lazyt", "total": 100, "done": 50,
                "succeeded": 40, "failed": 10,
                "status": "aborted",
                "started_at": time.time(),
                "current_code": "", "current_name": "",
            }),
            encoding="utf-8",
        )

        pm = PrewarmManager()
        # __init__ 后 _loaded 应该是 False，_tasks 还没加载
        assert pm._loaded is False
        assert "lazyt" not in pm._tasks

    def test_get_status_triggers_load(self, tmp_path, monkeypatch):
        """get_status 首次调用时触发 _ensure_loaded。"""
        import json as _json

        import chanlun.config as _cfg

        monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)

        persist_dir = tmp_path / "prewarm_status"
        persist_dir.mkdir(parents=True, exist_ok=True)
        (persist_dir / "lazy_get.json").write_text(
            _json.dumps({
                "market": "lazy_get", "total": 100, "done": 80,
                "succeeded": 75, "failed": 5,
                "status": "aborted",
                "started_at": time.time(),
                "current_code": "", "current_name": "",
            }),
            encoding="utf-8",
        )

        pm = PrewarmManager()
        assert pm._loaded is False

        result = pm.get_status("lazy_get")
        # get_status 触发了 _ensure_loaded，并能读到磁盘 task
        assert pm._loaded is True
        assert result is not None
        assert result["market"] == "lazy_get"
        assert result["done"] == 80

    def test_ensure_loaded_idempotent(self, tmp_path, monkeypatch):
        """重复调用 _ensure_loaded 不重复扫盘。"""
        import chanlun.config as _cfg

        monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)

        pm = PrewarmManager()
        pm._ensure_loaded()
        assert pm._loaded is True
        # 第二次直接 fast path 返回，不会再调 _load_persisted_tasks
        # 通过 monkey-patch 验证：把 _load_persisted_tasks 替成会抛异常的函数
        called = []
        monkeypatch.setattr(pm, "_load_persisted_tasks", lambda: called.append(1))
        pm._ensure_loaded()
        assert called == [], "_ensure_loaded 第二次不应再调 _load_persisted_tasks"


# ===========================================================================
# F：_persist_task 多线程持久化竞争修复
# ===========================================================================


class TestPersistRace:
    def test_persist_serialized_under_concurrency(self, tmp_path, monkeypatch):
        """多线程并发 _persist_task 时，磁盘最终内容应是某次 to_dict() 的快照
        （不会出现"半截内容"或字段错乱），且 done 字段不会比内存已落定的某个
        snapshot 更小。"""
        import json as _json
        import threading as _t

        import chanlun.config as _cfg

        monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)

        pm = PrewarmManager()
        pm._ensure_loaded()  # 确保 persist_dir 已建

        task = PrewarmTask(market="prc_test", total=1000)
        # 我们在后续线程里递增 task.done 并触发 persist；用一个 barrier 让线程同时跑
        N = 30
        barrier = _t.Barrier(N)
        errors = []

        def worker(target_done: int):
            try:
                barrier.wait(timeout=5)
                task.done = target_done  # 模拟 worker 完成 1 个 code
                pm._persist_task(task)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [_t.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"persist 线程异常: {errors}"

        # 读磁盘：内容必须是合法 JSON（没有半截写入），且 done 在 [0, N-1]
        path = tmp_path / "prewarm_status" / "prc_test.json"
        assert path.is_file()
        data = _json.loads(path.read_text(encoding="utf-8"))
        assert data["market"] == "prc_test"
        # F 修复关键：done 字段不应错乱（必须落在 0..N-1 之间），即"最后写入者赢"
        assert 0 <= data["done"] <= N - 1, f"done 字段超出预期: {data['done']}"


# ===========================================================================
# L3：互斥按数据源 group 细化
# ===========================================================================


class TestSourceGroupMutex:
    def test_market_group_uses_config_exchange(self, monkeypatch):
        import chanlun.config as cfg

        monkeypatch.setattr(cfg, "EXCHANGE_M_TEST", "shared_bus", raising=False)
        assert PrewarmManager._market_group("m_test") == "shared_bus"

    def test_market_group_falls_back_to_market_name(self, monkeypatch):
        # 故意删一个不存在的属性确保 fallback 走 market 名
        assert PrewarmManager._market_group("never_configured_xyz") == "never_configured_xyz"

    def test_same_group_markets_are_mutually_exclusive(self, manager, monkeypatch):
        """同一 group（如 us 和 hk 都是 cq 长桥）不能并发预热。"""
        import chanlun.config as cfg
        import cl_app.blueprints.symbols as s

        monkeypatch.setattr(cfg, "EXCHANGE_GA", "shared_bus", raising=False)
        monkeypatch.setattr(cfg, "EXCHANGE_GB", "shared_bus", raising=False)
        # 关掉 rate limit + mock _run_task 让 worker 立即结束
        monkeypatch.setattr(s, "PREWARM_RATE_LIMIT_SECONDS", 0)
        monkeypatch.setattr(manager, "_run_task", lambda task, codes: None)

        r1 = manager.start("ga", [{"code": "X", "name": "X"}])
        assert r1["ok"] is True

        r2 = manager.start("gb", [{"code": "Y", "name": "Y"}])
        # 同 group 'shared_bus' → 互斥
        assert r2["ok"] is False
        assert "shared_bus" in r2["msg"] or "已在预热" in r2["msg"]

    def test_different_groups_can_run_concurrently(self, manager, monkeypatch):
        """不同 group 的 market（不同 exchange）可以并发预热（L3 核心收益）。"""
        import chanlun.config as cfg
        import cl_app.blueprints.symbols as s

        monkeypatch.setattr(cfg, "EXCHANGE_GA", "bus_alpha", raising=False)
        monkeypatch.setattr(cfg, "EXCHANGE_GB", "bus_beta", raising=False)
        monkeypatch.setattr(s, "PREWARM_RATE_LIMIT_SECONDS", 0)
        monkeypatch.setattr(manager, "_run_task", lambda task, codes: None)

        r1 = manager.start("ga", [{"code": "X", "name": "X"}])
        assert r1["ok"] is True

        r2 = manager.start("gb", [{"code": "Y", "name": "Y"}])
        # 不同 group → 不互斥，应能成功启动
        assert r2["ok"] is True, f"L3 应允许跨 group 并发，但被拒绝: {r2.get('msg')}"
