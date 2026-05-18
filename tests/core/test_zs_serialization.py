"""tests/core/test_zs_serialization.py — 中枢(ZS)序列化回归测试。

历史 bug:``ZS.to_dict()`` 输出 ``'level': self.level``，是原始 ``Level`` 枚举，
导致 ``json.dumps(zs.to_dict())`` 与 ``str(zs)``（``__str__`` 内部走 json.dumps）
抛 ``TypeError: Object of type Level is not JSON serializable``。

同文件 ``ZSLX.to_dict()`` 用的是 ``self.zslx_level.value``（正确），只有 ``ZS`` 漏了。

本测试锁定:中枢对象可被 JSON 序列化、可安全 str()。
"""

from __future__ import annotations

import json


def test_zs_to_dict_is_json_serializable(cl_with_synthetic_klines):
    """中枢 to_dict() 的输出必须能被 json.dumps 序列化(level 不能是裸枚举)。"""
    cd = cl_with_synthetic_klines(500, seed=42, trend="up", multi_freq=True)
    zss = cd.get_xd_zss()
    assert zss, "合成上涨数据应至少产生 1 个线段中枢，否则本测试无意义"
    for zs in zss:
        # json.dumps 若遇到非可序列化对象会抛 TypeError —— 不抛即通过
        json.dumps(zs.to_dict(), ensure_ascii=False)


def test_zs_str_does_not_crash(cl_with_synthetic_klines):
    """str(中枢) 不得崩溃(ZS.__str__ 内部 json.dumps(to_dict()))。"""
    cd = cl_with_synthetic_klines(500, seed=42, trend="up", multi_freq=True)
    zss = cd.get_xd_zss()
    assert zss
    for zs in zss:
        s = str(zs)
        assert isinstance(s, str) and s
