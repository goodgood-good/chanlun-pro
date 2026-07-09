# -*- coding: utf-8 -*-
"""C2: fetch bt_data pkl 四处非原子写(open(p,"wb")打开即截断)+ patch_* 的
pickle.load 在 try 外, 中断后缓存半毁且当日 _is_pkl_fresh 判新鲜跳过不自愈。

修复: _atomic_pickle_dump 用 tmp+os.replace 原子写; _load_pkl_or_none 容错读。
"""
import pickle

import pytest

from chanlun.recursive_bt.data import fetch


def test_atomic_dump_roundtrip(tmp_path):
    p = str(tmp_path / "SH.600000.pkl")
    obj = {"code": "SH.600000", "n_small": 123, "dates": [1, 2, 3]}
    fetch._atomic_pickle_dump(obj, p)
    with open(p, "rb") as fp:
        assert pickle.load(fp) == obj
    # 成功后不应残留 .tmp
    import os
    assert not os.path.exists(p + ".tmp")


def test_atomic_dump_preserves_original_on_write_failure(tmp_path, monkeypatch):
    """写第二版时 pickle.dump 抛异常, 原文件必须完好(证明非 open(wb) 截断语义)。"""
    p = str(tmp_path / "SZ.000001.pkl")
    v1 = {"code": "SZ.000001", "ver": 1}
    fetch._atomic_pickle_dump(v1, p)

    class _BoomPickle:
        @staticmethod
        def dump(obj, fp):
            raise IOError("disk full mid-write")

    monkeypatch.setattr(fetch, "pickle", _BoomPickle)
    with pytest.raises(IOError):
        fetch._atomic_pickle_dump({"code": "SZ.000001", "ver": 2}, p)
    # 还原真 pickle 读回: 原 v1 完好未被截断
    with open(p, "rb") as fp:
        assert pickle.load(fp) == v1


def test_load_or_none_truncated(tmp_path):
    p = str(tmp_path / "bad.pkl")
    with open(p, "wb") as fp:
        fp.write(b"\x80\x04\x95broken-not-a-pickle")
    assert fetch._load_pkl_or_none(p) is None


def test_load_or_none_missing(tmp_path):
    assert fetch._load_pkl_or_none(str(tmp_path / "nope.pkl")) is None


def test_load_or_none_valid(tmp_path):
    p = str(tmp_path / "ok.pkl")
    obj = {"a": 1}
    fetch._atomic_pickle_dump(obj, p)
    assert fetch._load_pkl_or_none(p) == obj