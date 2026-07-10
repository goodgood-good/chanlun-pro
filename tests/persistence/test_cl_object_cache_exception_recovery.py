# -*- coding: utf-8 -*-
"""R11-#3: get_web_cl_data 复用旧 cd 的 process_klines 抛异常时返回空白 CL 且不驱逐坏 pkl,
同一异常无限复现直到 15 天 TTL(与自身'下次自然重算'注释矛盾)。

修复: except 分支 unlink 坏 pkl + 用全新 CL 全量重算再返回/落盘,让'下次自然重算'名副其实、
当次也返回真实缠论而非静默 0 信号。本测试构造 process_klines 恒抛的 stale pkl,断言:
(1) get_web_cl_data 返回全量重算后的非空 CL(而非 0 根空白);
(2) 磁盘坏 pkl 被驱逐并替换为好对象;
(3) 第二次调用命中好 pkl 正常返回(无限复现已断)。
"""
import datetime
import pickle
from pathlib import Path

import pandas as pd

from chanlun.core import cl
from chanlun.file_db_mixins.cl_object_cache import _CLObjectCacheMixin

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}


class _StaleCD:
    """模拟旧提交 schema 漂移 cd: get_src_klines 空(跳过 4 重校验), process_klines 恒抛。"""

    def get_src_klines(self):
        return []

    def process_klines(self, klines):
        raise RuntimeError("stale schema drift: missing _rbc")


class _Harness(_CLObjectCacheMixin):
    def __init__(self, root):
        self.cl_data_path = root

    def _config_md5(self, cl_config):
        return "testkey"

    def _atomic_write_pickle(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fp:
            pickle.dump(obj, fp)

    def _try_run_cleanup(self, *a, **k):
        pass


def _mk_klines(n=60):
    rows = []
    base = datetime.datetime(2020, 1, 1)
    price = 100.0
    for i in range(n):
        rows.append({
            "date": base + datetime.timedelta(days=i),
            "open": price, "high": price + 1.0, "low": price - 1.0,
            "close": price + 0.5, "volume": 1000.0,
        })
        price += 0.3
    return pd.DataFrame(rows)


def _pkl_path(root):
    return root / "a" / "a_SH_600519_d_testkey.pkl"


def test_process_klines_exception_evicts_bad_pkl_and_recomputes(tmp_path):
    root = tmp_path
    pp = _pkl_path(root)
    pp.parent.mkdir(parents=True, exist_ok=True)
    with open(pp, "wb") as fp:
        pickle.dump(_StaleCD(), fp)

    df = _mk_klines(60)
    fresh = cl.CL("SH.600519", "d", dict(_CFG))
    fresh.process_klines(df)
    expected_n = len(fresh.get_src_klines())
    assert expected_n > 0

    H = _Harness(root)
    result = H.get_web_cl_data("a", "SH.600519", "d", dict(_CFG), df)

    assert len(result.get_src_klines()) == expected_n, "抛异常后应全量重算返回非空 CL"

    assert pp.exists()
    with open(pp, "rb") as fp:
        reloaded = pickle.load(fp)
    assert not isinstance(reloaded, _StaleCD), "坏 pkl 未被驱逐/替换"
    assert len(reloaded.get_src_klines()) == expected_n

    result2 = H.get_web_cl_data("a", "SH.600519", "d", dict(_CFG), df)
    assert len(result2.get_src_klines()) == expected_n