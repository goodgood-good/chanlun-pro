"""Round11 B3: 决策/覆盖状态文件改原子写(tmp+os.replace), 与 ledger/dedup 一致, 防崩溃截断。"""
import json

from chanlun.recursive_bt.strategy_optimizer.decision import _atomic_write_json


def test_atomic_write_json_writes_and_cleans_tmp(tmp_path):
    p = tmp_path / "state.json"
    _atomic_write_json(p, {"a": 1, "b": "x"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "x"}
    # .tmp 应已被 os.replace 消费, 不残留
    assert not (tmp_path / "state.json.tmp").exists()


def test_atomic_write_json_overwrites_existing(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"old": true}', encoding="utf-8")
    _atomic_write_json(p, {"new": 42})
    assert json.loads(p.read_text(encoding="utf-8")) == {"new": 42}