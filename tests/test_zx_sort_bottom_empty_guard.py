# -*- coding: utf-8 -*-
"""R15-C3: db.zx_stock_sort_bottom 对 MAX(position) 空结果(None)未判空, None+1 抛
TypeError → except rollback+raise → 自选股"置底"路由(blueprints/zixuan.py:197 →
ZiXuan.sort_bottom_stock:153)裸 500。空组(该 market+zx_group 无行, 或 position 全 NULL)
触发。修复=(max_position or 0) + 1。
"""
from chanlun.persistence.db import db


class _FakeQuery:
    def __init__(self, scalar_val):
        self._scalar = scalar_val
        self.updated = None

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self._scalar

    def update(self, values, **k):
        self.updated = values
        return 1


class _FakeSession:
    def __init__(self, scalar_val):
        self.q = _FakeQuery(scalar_val)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a, **k):
        return self.q

    def commit(self):
        pass

    def rollback(self):
        pass


def test_zx_stock_sort_bottom_empty_group_no_crash(monkeypatch):
    """空组 MAX=None → 修复前 None+1 TypeError; 修复后 (None or 0)+1=1。"""
    fs = _FakeSession(None)
    monkeypatch.setattr(db, "Session", lambda: fs)
    result = db.zx_stock_sort_bottom("a", "不存在的组", "SH.600000")
    assert result is True
    assert fs.q.updated == {"position": 1}  # None→0, +1


def test_zx_stock_sort_bottom_uses_max_plus_one(monkeypatch):
    """回归: 非空组 MAX=5 → position=6, 守卫不误伤。"""
    fs = _FakeSession(5)
    monkeypatch.setattr(db, "Session", lambda: fs)
    result = db.zx_stock_sort_bottom("a", "组", "SH.600000")
    assert result is True
    assert fs.q.updated == {"position": 6}