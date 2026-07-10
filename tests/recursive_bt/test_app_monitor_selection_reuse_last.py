# -*- coding: utf-8 -*-
"""R12-#3: _a_selection_candidates() 把 selector 内部异常与"今日确实零候选"合并成同一个 [],
经 current_universe()→_sync_states() 在盘中每 tick(默认5分钟)把非持仓选股池全量剔除,丢失其
增量缠论状态(重新入选要 warmup 重建,期间信号被吃掉),比 live_monitor 版本(异常前置 return、
按日节流)明显更脆弱。

修复=异常分支复用上一轮 self.last_selection_candidates(而非清空 []),使瞬时 IO/解析抖动不再
把选股池整体剔除;真正成功返回的 [] 仍允许清空。本测钉死:factory 抛异常时返回上一轮候选而非 []。
"""
from types import SimpleNamespace

from chanlun.recursive_bt.monitor.app_monitor import DynamicRecursiveMonitor


def _fake_self(prior):
    log = SimpleNamespace(warning=lambda *a, **k: None)
    config = SimpleNamespace(
        include_a_selection_pool=True,
        bt_data="x",
        a_selection_fund_data="x",
        a_selection_scan_limit=100,
        a_selection_max_codes=50,
        a_selection_lookback_bars=48,
        a_selection_buy_classes=(3, 2, 1),
        a_selection_require_three_systems=True,
        a_selection_fundamental_roe_ann_min=8.0,
    )

    def _factory(cfg):
        raise RuntimeError("selector boom (瞬时 IO 抖动)")

    return SimpleNamespace(
        market="a",
        config=config,
        log=log,
        _a_selector_factory=_factory,
        last_selection_candidates=prior,
    )


def test_selector_exception_reuses_last_candidates():
    """factory 抛异常 → 复用上一轮候选池而非清空为 []。"""
    prior = ["CAND_A", "CAND_B"]
    fs = _fake_self(list(prior))
    got = DynamicRecursiveMonitor._a_selection_candidates(fs)
    assert got == prior, f"异常时应复用上一轮候选而非清空, got={got}"


def test_selector_exception_no_prior_returns_empty():
    """首轮无 prior(空)时异常仍返回 [](回归保护)。"""
    fs = _fake_self([])
    got = DynamicRecursiveMonitor._a_selection_candidates(fs)
    assert got == []