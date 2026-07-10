"""锁定 QMT 批量预热加速(默认关闭)的两个安全兜底行为。

两个场景都属于 ``PREWARM_QMT_BATCH`` 功能(默认 0 关闭),测试通过 monkeypatch 打开开关
并 mock 掉真实数据源/QMT 依赖,只验证决策逻辑,不触碰磁盘/exchange。

- Bug A: 交易时段自动跳过批量预下载(``prewarm_batch_download`` 会长时间持
  ``_XTDATA_NATIVE_LOCK`` 阻塞用户实时 ``klines()``),降级为逐只 download。
- Bug B: 单标的某周期首次 ``skip_download=True`` 失败后,第二次重试强制
  ``skip_download=False``,退化为真正 download 兜底(批量某 chunk 失败时不至于死在空数据)。
"""
import threading

from cl_app.blueprints import symbols


def test_batch_predownload_skipped_during_trading_hours(monkeypatch):
    """Bug A: 交易时段不触发批量预下载,直接走逐只 download 路径。"""
    mgr = symbols.PrewarmManager()

    # 打开默认关闭的批量预热开关。
    monkeypatch.setattr(symbols, "PREWARM_QMT_BATCH", 1)
    # 当前处于交易时段 → 期望跳过批量下载。
    # raising=False: 修复前 symbols 模块尚未 import market_now_trading。
    monkeypatch.setattr(symbols, "market_now_trading", lambda m: True, raising=False)

    class FakeEx:
        def __init__(self):
            self.download_calls = 0

        def prewarm_batch_download(self, *a, **k):
            self.download_calls += 1

    fake = FakeEx()
    import chanlun.exchange as chanlun_exchange

    monkeypatch.setattr(chanlun_exchange, "get_exchange", lambda m: fake)

    # 逐只计算全部 mock 掉,只记录传入的 skip_download。
    seen_skip = []

    def fake_prewarm_one(self, market, code, cl_config, cancel_event, skip_download=False):
        seen_skip.append(skip_download)
        return True

    monkeypatch.setattr(symbols.PrewarmManager, "_prewarm_one_code", fake_prewarm_one)
    monkeypatch.setattr(symbols.PrewarmManager, "_done_file_path", lambda self, m: None)
    monkeypatch.setattr(symbols.PrewarmManager, "_persist_task", lambda self, t: None)
    monkeypatch.setattr(symbols, "query_cl_chart_config", lambda m, c: {})

    task = symbols.PrewarmTask(market="a", total=1)
    mgr._run_task(task, [{"code": "000001", "name": "x"}])

    # 交易时段:批量下载一次都不该调用。
    assert fake.download_calls == 0
    # 逐只计算仍照常进行,走原有 download 路径(skip_download=False)。
    assert seen_skip == [False]


def test_prewarm_retry_forces_download_on_second_attempt(monkeypatch):
    """Bug B: 首次 skip_download=True 失败后,第二次重试强制 skip_download=False。"""
    mgr = symbols.PrewarmManager()

    # 只测单周期,保持断言干净。
    monkeypatch.setattr(symbols, "PREWARM_INTERVALS", ["1"])
    # 避免重试之间 2s 退避真的 sleep。
    monkeypatch.setattr(symbols.time, "sleep", lambda *a, **k: None)
    # 磁盘冷层 miss → 进入 compute 重试路径。
    monkeypatch.setattr(symbols, "_get_chart_cache_entry", lambda k: None)
    # 让位逻辑 no-op,避免依赖用户活跃状态。
    monkeypatch.setattr(
        symbols.PrewarmManager,
        "_yield_to_user_if_active",
        staticmethod(lambda *a, **k: None),
    )

    seen_skip = []

    def fake_compute(market, code, freq, cl_config, skip_download=False):
        seen_skip.append(skip_download)
        return False  # 两次都失败 → 观察完整重试序列

    monkeypatch.setattr(symbols, "compute_and_cache_chart_data", fake_compute)

    mgr._prewarm_one_code(
        market="a",
        code="__prewarm_bugb__",  # 唯一 code,保证 RAM 缓存 miss
        cl_config={},
        cancel_event=threading.Event(),
        skip_download=True,
    )

    # 第一次遵循外层 skip_download=True,第二次强制 False(退化为真正 download)。
    assert seen_skip == [True, False]


def test_cancelled_inflight_code_is_not_written_to_done(tmp_path, monkeypatch):
    """取消中的标的不是坏标的，resume 时必须重新处理。"""
    mgr = symbols.PrewarmManager()
    done_path = tmp_path / "a_done.txt"

    monkeypatch.setattr(symbols, "PREWARM_QMT_BATCH", 0)
    monkeypatch.setattr(symbols, "query_cl_chart_config", lambda _market, _code: {})
    monkeypatch.setattr(symbols, "_get_user_recent_codes", lambda _market: [])
    monkeypatch.setattr(
        symbols.PrewarmManager,
        "_done_file_path",
        lambda _self, _market: str(done_path),
    )
    monkeypatch.setattr(symbols.PrewarmManager, "_persist_task", lambda _self, _task: None)

    def cancel_while_running(
        _self, market, code, cl_config, cancel_event, skip_download=False
    ):
        cancel_event.set()
        return False

    monkeypatch.setattr(
        symbols.PrewarmManager,
        "_prewarm_one_code",
        cancel_while_running,
    )

    task = symbols.PrewarmTask(market="a", total=1)
    mgr._run_task(task, [{"code": "SH.600000", "name": "浦发银行"}])

    assert task.status == "cancelled"
    assert not done_path.exists() or done_path.read_text(encoding="utf-8") == ""
