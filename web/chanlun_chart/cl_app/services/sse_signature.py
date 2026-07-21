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
    # 末根 K 线 OHLC 纳入指纹:使"末根价格在不改变形态计数时变动"(盘中绝大多数 tick)也被检测到,
    # 与 prepend 的"末根 OHLC 全等才跳过"判据对齐,消除"算了不推"导致的盘中实时停滞(审查 M2)。
    # 末根一动即推,但被 SSE_REFRESH_MS(8s)周期天然限流,不会每 tick 刷屏。
    # R5-#3: volume 'v' 一并纳入——一字涨停/跌停时 OHLC 恒定但成交量逐 tick 累积(prepend
    # web-B1 只更 _data['v'][-1]), 不入指纹则该量柱更新恒被 dedup 吞→SSE 客户端量柱冻结。
    for _k in ("o", "h", "l", "c", "v"):
        _a = chart_data.get(_k) or []
        parts.append(f"{_k}:{_a[-1] if _a else ''}")
    for f in _SHAPE_FIELDS:
        arr = chart_data.get(f) or []
        parts.append(f"{f}:{len(arr)}:{_last_time(arr[-1]) if arr else ''}")
    strict_mode = chart_data.get("strict_structure_mode")
    parts.append(f"strict_mode:{strict_mode or ''}")
    if strict_mode == "replace":
        strict = chart_data.get("strict_structure") or {}
        parts.extend(
            (
                f"strict_structure:{strict.get('structure_revision', '')}",
                f"strict_snapshot:{strict.get('snapshot_revision', '')}",
                f"strict_render:{strict.get('render_revision', '')}",
            )
        )
    elif strict_mode == "unavailable":
        error = chart_data.get("strict_structure_error") or {}
        parts.append(f"strict_error:{error.get('code', '')}")
    return "|".join(parts)
