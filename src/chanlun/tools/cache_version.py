# -*- coding: utf-8 -*-
"""核心计算/渲染源码内容指纹 —— 供 chart_data / cl_object 缓存 key 使用。

背景：chart_data / cl_object 缓存原只用手动 schema 版本号失效,但该版本号语义是
「输出**字段结构**变化才 bump」。当只改**计算逻辑**(线段划分算法、中枢、买卖点
判定等——值变、字段不变)时,版本号不会动 → 旧缓存不失效 → 前端看到旧代码算的陈旧结果。

本指纹对影响图表内容的源文件取内容 md5,拼进缓存 key:任一文件一改 → 指纹变 →
key 变 → 旧条目全 miss、自动重算。免再手动 bump 版本号(易忘)。进程内只算一次。
"""
import hashlib
import pathlib

_FP = None


def _fingerprint_files():
    """参与内容指纹的源文件列表:core 计算 + core/types + 图表渲染 + exchange 数据产出。

    覆盖:`chanlun/core/*.py`(笔/线段/中枢/走势类型/买卖点/递归/cl) + `core/types/*.py`
    + `cl_utils/{tv_chart,strict_chart,strict_chart_runtime,chart_config}.py`(渲染/严格运行时/配置)
    + `exchange/*.py`(含 _lookback 与各
    数据源 klines)。D3-M3:exchange 层对原始数据的解释(时区/周期合成/精度归一/去重)一改会
    改变图表 date/OHLC 值,但原指纹只含 _lookback → 这类修复不失效旧图表缓存;纳入整个 exchange
    目录后,数据产出口径一改 → 指纹变 → chart_data/cl_object 缓存自动失效。
    """
    pkg = pathlib.Path(__file__).resolve().parents[1]   # .../chanlun
    files = sorted((pkg / "core").rglob("*.py"))
    files += [
        pkg / "cl_utils" / "tv_chart.py",
        pkg / "cl_utils" / "strict_chart.py",
        pkg / "cl_utils" / "strict_chart_runtime.py",
        pkg / "cl_utils" / "chart_config.py",
    ]
    files += sorted((pkg / "exchange").glob("*.py"))
    return files


def source_fingerprint() -> str:
    """影响图表内容的源文件内容 md5 指纹(8 hex);模块级缓存,进程内只算一次。"""
    global _FP
    if _FP is not None:
        return _FP
    h = hashlib.md5()
    for f in _fingerprint_files():
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    _FP = h.hexdigest()[:8]
    return _FP
