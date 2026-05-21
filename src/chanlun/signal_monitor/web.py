# -*- coding: utf-8 -*-
"""手动力度对比的 Flask 蓝图（中枢-free 信号监控 · Phase 2）。

  - ``/strength_compare/<market>/<code>`` —— 参考线段（按起止时间定位）与
    当前在走线段做实时力度对比，复用 ``strength_compare`` 内核。

前端在 TV 图表上点选一根参考线段后，把它的起止 K 线时间发到本接口即可。
只读操作，用 GET，避开 CSRF。蓝图对象由 web 的 ``create_app`` 注册。
"""
from flask import Blueprint, request
from flask_login import login_required

from chanlun.base import Market
from chanlun.cl_utils import query_cl_chart_config, web_batch_get_cl_datas
from chanlun.exchange import get_exchange
from chanlun.signal_monitor.strength_compare import (
    list_compare_lines,
    manual_strength_compare,
)
from chanlun.tools.log_util import LogUtil

signal_compare_bp = Blueprint("signal_compare", __name__)


def _compute_cd(market: str, code: str, frequency: str):
    """拉 K 线并算出单周期缠论数据；失败返回 (None, 错误信息)。"""
    ex = get_exchange(Market(market))
    klines = ex.klines(code, frequency)
    cl_config = query_cl_chart_config(market, code)
    cds = web_batch_get_cl_datas(market, code, {frequency: klines}, cl_config)
    if not cds:
        return None, "缠论数据计算失败"
    return cds[0], None


@signal_compare_bp.route("/strength_compare/lines/<market>/<code>")
@login_required
def strength_compare_lines(market, code):
    """列出可作参考段的笔/线段，供手动对比面板的下拉框。

    query：``frequency`` 必填；``line_kind`` 可选（xd 默认 / bi）。
    """
    frequency = request.args.get("frequency", "").strip()
    line_kind = request.args.get("line_kind", "xd").strip()
    if not frequency:
        return {"ok": False, "error": "缺少参数 frequency"}
    if line_kind not in ("xd", "bi"):
        return {"ok": False, "error": f"line_kind 非法: {line_kind}"}
    try:
        cd, err = _compute_cd(market, code, frequency)
        if cd is None:
            return {"ok": False, "error": err}
        return {"ok": True, "line_kind": line_kind, "lines": list_compare_lines(cd, line_kind)}
    except Exception as e:
        LogUtil.warning(f"[strength_compare_lines] {market} {code} {frequency} err={e}")
        return {"ok": False, "error": f"获取线段列表失败: {e}"}


@signal_compare_bp.route("/strength_compare/<market>/<code>")
@login_required
def strength_compare(market, code):
    """手动力度对比。

    query 参数：
      - ``frequency``  必填，K 线周期
      - ``ref_start``  必填，参考线段起点 K 线时间（``YYYY-MM-DD HH:MM:SS``）
      - ``ref_end``    必填，参考线段终点 K 线时间
      - ``line_kind``  可选，``xd``（线段，默认）/ ``bi``（笔）

    返回 ``manual_strength_compare`` 的结果 dict（含 ``ok`` / ``error`` / 对比明细）。
    """
    frequency = request.args.get("frequency", "").strip()
    ref_start = request.args.get("ref_start", "").strip()
    ref_end = request.args.get("ref_end", "").strip()
    line_kind = request.args.get("line_kind", "xd").strip()

    if not frequency or not ref_start or not ref_end:
        return {"ok": False, "error": "缺少参数 frequency / ref_start / ref_end"}
    if line_kind not in ("xd", "bi"):
        return {"ok": False, "error": f"line_kind 非法: {line_kind}"}

    try:
        ex = get_exchange(Market(market))
        klines = ex.klines(code, frequency)
        cl_config = query_cl_chart_config(market, code)
        cds = web_batch_get_cl_datas(market, code, {frequency: klines}, cl_config)
        if not cds:
            return {"ok": False, "error": "缠论数据计算失败"}
        result = manual_strength_compare(cds[0], ref_start, ref_end, line_kind)
        result.update({"market": market, "code": code, "frequency": frequency})
        return result
    except Exception as e:
        LogUtil.warning(f"[strength_compare] {market} {code} {frequency} err={e}")
        return {"ok": False, "error": f"对比失败: {e}"}
