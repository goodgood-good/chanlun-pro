"""
图表配置项相关接口蓝图。

  - `/get_cl_config/<market>/<code>`
  - `/set_cl_config`
  - `/reset_cl_config`
  - `/export_cl_config`
"""

from flask import Blueprint, render_template, request
from flask_login import login_required

from chanlun.cl_utils import query_cl_chart_config, del_cl_chart_config, set_cl_chart_config
from chanlun.cl_utils.chart_config import CL_CHART_CONFIG_PERSIST_KEYS


options_bp = Blueprint("options", __name__)
CL_CHART_CONFIG_FORM_KEYS = CL_CHART_CONFIG_PERSIST_KEYS
_FORM_META_KEYS = frozenset({"market", "code", "is_del"})


@options_bp.route("/get_cl_config/<market>/<code>")
@login_required
def get_cl_config(market, code: str):
    code = code.replace("__", "/")  # 数字货币特殊处理
    cl_config = query_cl_chart_config(market, code)
    cl_config["market"] = market
    cl_config["code"] = code
    return render_template("options.html", **cl_config)


def _build_cl_config(form, keys):
    """从白名单表单构建标量配置，未勾选 checkbox 统一记为 ``"0"``。"""
    cl_config = {}
    for _k in keys:
        _v = form.get(_k, "")
        if _v == "":
            _v = "0"
        cl_config[_k] = _v
    return cl_config, None


@options_bp.route("/set_cl_config", methods=["POST"])
@login_required
def set_cl_config():
    market = request.form["market"]
    code = request.form["code"]
    is_del = request.form["is_del"]
    if is_del == "true":
        res = del_cl_chart_config(market, code)
        return {"ok": res}

    unsupported = sorted(
        set(request.form.keys())
        - _FORM_META_KEYS
        - set(CL_CHART_CONFIG_FORM_KEYS)
    )
    if unsupported:
        return {
            "ok": False,
            "msg": "包含不再支持的配置项: " + ",".join(unsupported),
        }, 400

    cl_config, err = _build_cl_config(
        request.form,
        CL_CHART_CONFIG_FORM_KEYS,
    )
    if err is not None:
        return {"ok": False, "msg": err}
    res = set_cl_chart_config(market, code, cl_config)
    return {"ok": res}


@options_bp.route("/reset_cl_config", methods=["POST"])
@login_required
def reset_cl_config():
    market = request.form["market"]
    res = del_cl_chart_config(market, "common")
    return {"ok": res}


@options_bp.route("/export_cl_config", methods=["GET"])
@login_required
def export_cl_config():
    market = request.args.get("market")
    code = request.args.get("code")
    if code is not None:
        code = code.replace("__", "/")
    cl_config = query_cl_chart_config(market, code)
    return cl_config
