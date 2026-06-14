# -*- coding: utf-8 -*-
"""生产核心 chanlun.core.CL 的结构化快照 + 规范化序列化。

gen_fixtures(生成 golden)与 test_golden_master(断言)共用,确保口径一致。

快照取**结构指纹**而非对象 .to_dict() 全量:只记 笔/段/中枢 的位置(端点
日期)、关键价位、状态与买卖点/背驰名称。刻意不含深层嵌套的原始K线——
那会让 golden 膨胀几十万行且不可 review,且把实现内部当成了行为。指纹钉的是
**可观测行为**(refactor 内部自由变动、输出不变即视为行为保持)。
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


def _iso(dt) -> Any:
    return dt.isoformat() if dt is not None else None


def _fx_point(fx) -> Any:
    """线端点(分型)的精简坐标:日期 + 价位。"""
    if fx is None:
        return None
    k = getattr(fx, "k", None)
    return {"date": _iso(getattr(k, "date", None)), "val": getattr(fx, "val", None)}


def _line_brief(line) -> dict:
    """笔/线段指纹:序号、方向、端点、高低、是否完成、买卖点、背驰。"""
    return {
        "index": line.index,
        "type": line.type,
        "start": _fx_point(line.start),
        "end": _fx_point(line.end),
        "high": line.high,
        "low": line.low,
        "done": bool(line.is_done()),
        "mmds": sorted(m.name for m in line.get_mmds()),
        "bcs": sorted(b.type for b in line.get_bcs()),
    }


def _zs_brief(zs) -> dict:
    """中枢指纹:序号、类型、级别、起止日期、四指标 zg/zd/gg/dd、段数、状态。"""
    level = getattr(zs, "level", None)
    return {
        "index": zs.index,
        "type": zs.type,
        "level": level.value if level is not None else None,
        "start": _fx_point(zs.start.start) if zs.start is not None else None,
        "end": _fx_point(zs.end.end) if zs.end is not None else None,
        "zg": zs.zg,
        "zd": zs.zd,
        "gg": zs.gg,
        "dd": zs.dd,
        "line_num": zs.line_num,
        "done": bool(zs.done),
        "real": bool(zs.real),
    }


def cl_snapshot(cd) -> dict:
    """把 CL 的全结构输出序列化为可比较的紧凑指纹。

    买卖点/背驰以名称形式内嵌在每条 笔/线段 指纹里(_line_brief)。
    """
    return {
        "code": cd.code,
        "frequency": cd.frequency,
        "kline_num": len(cd.get_klines()),
        "bis": [_line_brief(b) for b in cd.get_bis()],
        "xds": [_line_brief(x) for x in cd.get_xds()],
        "bi_zss": [_zs_brief(z) for z in cd.get_bi_zss()],
        "xd_zss": [_zs_brief(z) for z in cd.get_xd_zss()],
    }


def canonical_json(obj: Any, ndigits: int = 8) -> str:
    """规范化:浮点定点 + 键排序 + UTF-8。消除键序/平台浮点噪声。"""
    return json.dumps(
        _round_floats(obj, ndigits), sort_keys=True, ensure_ascii=False, indent=2
    )
