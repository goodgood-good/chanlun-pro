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
