"""Round11 N3: 传给异步落盘线程池的 save_infos 必须是深拷贝快照(与 self.positions/历史集合不
共享引用), 否则后台 pickle 迭代时主线程 execute 正改这些容器 → "dict/list changed size during
iteration" → 该次落盘 raise 被吞成 warning = 资金账本静默丢盘。"""

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION


def test_save_to_pkl_snapshots_positions_for_async_write(monkeypatch):
    tr = BackTestTrader("t", mode="trade", market="a")
    tr.positions["X:1buy"] = POSITION(code="X", mmd="1buy", amount=100)
    captured = {}
    import chanlun.trading.backtest_trader as mod

    monkeypatch.setattr(
        mod.fdb, "cache_pkl_to_file", lambda key, infos: captured.update(infos=infos)
    )
    tr.save_to_pkl("testkey")
    # 深拷贝快照: 与活引用不是同一对象
    assert captured["infos"]["positions"] is not tr.positions
    # 隔离性: 落盘后主线程再改 self.positions(模拟后台 pickle 期间的并发改)不影响已提交快照
    tr.positions["Y:1buy"] = POSITION(code="Y", mmd="1buy", amount=50)
    assert "Y:1buy" not in captured["infos"]["positions"]