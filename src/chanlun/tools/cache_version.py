# -*- coding: utf-8 -*-
"""核心计算/渲染源码内容指纹 —— 供 chart_data / cl_object 缓存 key 使用。

背景：chart_data / cl_object 缓存原只用手动 schema 版本号失效,但该版本号语义是
「输出**字段结构**变化才 bump」。当只改**计算逻辑**(线段划分算法、中枢、买卖点
判定等——值变、字段不变)时,版本号不会动 → 旧缓存不失效 → 前端看到旧代码算的陈旧结果。

本指纹对影响图表内容的核心源文件取内容 md5,拼进缓存 key:任一文件一改 → 指纹变 →
key 变 → 旧条目全 miss、自动重算。免再手动 bump 版本号(易忘)。进程内只算一次。
"""
import hashlib
import pathlib

_FP = None


def source_fingerprint() -> str:
    """影响图表内容的核心源文件(core 计算 + types + 图表渲染)的 md5 内容指纹(8 hex)。

    覆盖:`chanlun/core/*.py`(笔/线段/中枢/走势类型/买卖点/递归/cl) + `core/types/*.py`
    (XD/BI/LINE 等) + `cl_utils/tv_chart.py`(渲染) + `cl_utils/chart_config.py`(配置)。
    模块级缓存——源码在进程运行期不变,只算一次。"""
    global _FP
    if _FP is not None:
        return _FP
    pkg = pathlib.Path(__file__).resolve().parents[1]   # .../chanlun
    files = sorted((pkg / "core").glob("*.py"))
    files += sorted((pkg / "core" / "types").glob("*.py"))
    files += [pkg / "cl_utils" / "tv_chart.py", pkg / "cl_utils" / "chart_config.py"]
    # _lookback.py 纳入指纹: lookback 天数(如 1m 60→20)改动 → 指纹变 → cache_key 变 →
    # 旧缓存自动失效(历史教训 v25: lookback 不进 key, 改了天数仍命中陈旧根数据)。
    files += [pkg / "exchange" / "_lookback.py"]
    h = hashlib.md5()
    for f in files:
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    _FP = h.hexdigest()[:8]
    return _FP
