# -*- coding: utf-8 -*-
"""生产核心 chanlun.core.CL 的结构化快照 + 规范化序列化。

gen_fixtures(生成 golden)与 test_golden_master(断言)共用,确保口径一致。
"""
import json
from typing import Any


def _round_floats(obj: Any, ndigits: int = 8) -> Any:
    """递归把所有 float 定点到 ndigits 位,压掉跨平台 1-ulp 噪声。"""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def cl_snapshot(cd) -> dict:
    """把 CL 的全结构输出序列化为可比较 dict。

    买卖点(mmds)与背驰(bcs)已内嵌在 BI/XD.to_dict() 中,无需单列。
    """
    return {
        "code": cd.code,
        "frequency": cd.frequency,
        "kline_num": len(cd.get_klines()),
        "bis": [b.to_dict() for b in cd.get_bis()],
        "xds": [x.to_dict() for x in cd.get_xds()],
        "bi_zss": [z.to_dict() for z in cd.get_bi_zss()],
        "xd_zss": [z.to_dict() for z in cd.get_xd_zss()],
    }


def canonical_json(obj: Any, ndigits: int = 8) -> str:
    """规范化:浮点定点 + 键排序 + UTF-8。消除键序/平台浮点噪声。"""
    return json.dumps(
        _round_floats(obj, ndigits), sort_keys=True, ensure_ascii=False, indent=2
    )
