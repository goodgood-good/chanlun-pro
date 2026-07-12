# -*- coding: utf-8 -*-
"""R12-#1: file_db_mixins/generic_pkl.py cache_pkl_from_file 是四个 mixin 里唯一对 pickle.load
零异常防护/零驱逐/零 TTL 的读取器,腐坏文件永久阻断实盘 reboot_trader 启动。

下游 backtest_trader.load_from_pkl(全部实盘 Trader 基类)裸调 fdb.cache_pkl_from_file,异常原样
上抛;5 个 reboot_trader_*.py 在 while True 交易循环前裸调 load_from_pkl,坏 pkl 一次 UnpicklingError
即整脚本在开跑前退出,重启复崩(比 R11 cl_object_cache 更差:连 TTL 都没有)。这正是仓库已认定该修
的形状(tests/recursive_bt/test_pkl_load_corrupt_guard 已把 engine/market_runtime/portfolio 三处
裸 pickle.load 改成'腐坏返回 None'),唯独 generic_pkl 被漏。修复=try/except 腐坏返回 None(与自身
'不存在返回 None'契约一致),坏文件保留由下次 cache_pkl_to_file 原子写覆盖自愈。
"""
import pickle

import pytest

from chanlun.file_db_mixins.generic_pkl import _GenericPklCacheMixin


class _H(_GenericPklCacheMixin):
    def __init__(self, root):
        self.cache_pkl_path = root


def test_corrupt_pkl_returns_none(tmp_path):
    """腐坏/截断 pkl → 返回 None(按 miss 处理)而非 UnpicklingError 冒泡。"""
    (tmp_path / "bad.pkl").write_bytes(b"\x80\x04broken-truncated")
    assert _H(tmp_path).cache_pkl_from_file("bad.pkl") is None


def test_corrupt_critical_pkl_raises_in_strict_mode(tmp_path):
    (tmp_path / "bad.pkl").write_bytes(b"\x80\x04broken-truncated")
    with pytest.raises(Exception):
        _H(tmp_path).cache_pkl_from_file("bad.pkl", strict=True)


def test_missing_pkl_returns_none(tmp_path):
    """不存在返回 None(既有契约回归)。"""
    assert _H(tmp_path).cache_pkl_from_file("nope.pkl") is None


def test_valid_pkl_roundtrip(tmp_path):
    """正常 pkl 正确读回(回归保护)。"""
    obj = {"positions": [1, 2, 3], "cash": 100.0}
    with open(tmp_path / "good.pkl", "wb") as f:
        pickle.dump(obj, f)
    assert _H(tmp_path).cache_pkl_from_file("good.pkl") == obj
