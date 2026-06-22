"""SSE 推送的变化检测：把 chart_data 压成一个轻量指纹，相同则不推。

指纹基于 K 线根数+末根时间，以及各形态字段(笔/线段/中枢/分型/背驰/买卖点/
走势类型)的元素个数+末元素时间。任何会改变图面的变化都会让指纹变化；纯重复
重算(数据未变)则指纹一致，据此跳过推送，省流量并避免前端闪烁。
"""

_SHAPE_FIELDS = (
    "fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds",
    "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs", "xd_zslx",
)


def _last_time(shape):
    """取一个形态元素的末端时间(多点取末点, 单点取自身), 缺失返回空串。"""
    pts = shape.get("points") if isinstance(shape, dict) else None
    if isinstance(pts, list) and pts:
        return pts[-1].get("time", "")
    if isinstance(pts, dict):
        return pts.get("time", "")
    return ""


def compute_signature(chart_data: dict) -> str:
    if not isinstance(chart_data, dict):
        return "0"
    t = chart_data.get("t") or []
    parts = [f"t:{len(t)}:{t[-1] if t else ''}"]
    for f in _SHAPE_FIELDS:
        arr = chart_data.get(f) or []
        parts.append(f"{f}:{len(arr)}:{_last_time(arr[-1]) if arr else ''}")
    return "|".join(parts)
