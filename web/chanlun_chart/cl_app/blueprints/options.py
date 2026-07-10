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


options_bp = Blueprint("options", __name__)


@options_bp.route("/get_cl_config/<market>/<code>")
@login_required
def get_cl_config(market, code: str):
    code = code.replace("__", "/")  # 数字货币特殊处理
    cl_config = query_cl_chart_config(market, code)
    cl_config["market"] = market
    cl_config["code"] = code
    return render_template("options.html", **cl_config)


def _build_cl_config(form, keys):
    """从表单构建缠论显示配置。

    用 form.get 兜底缺键(避免裸 request.form[k] 对缺键抛 werkzeug BadRequestKeyError->HTTP 400:
    前端 save_cl_config 的 jQuery $.each 校验 return false 只中断迭代不退出函数, 取消勾选全部
    中枢类型时仍会 POST 一份缺键表单)。中枢类型 zs_bi_type/zs_xd_type 全空时返回错误, 由调用方
    干净拒绝, 保持"必须选择一个"语义、不落库退化配置。返回 (cl_config | None, error_msg | None)。
    """
    cl_config = {}
    for _k in keys:
        _v = form.get(_k, "")
        if _k in ["zs_bi_type", "zs_xd_type"]:
            _v = [x for x in _v.split(",") if x != ""]
            if len(_v) == 0:
                label = "笔中枢类型" if _k == "zs_bi_type" else "线段中枢类型"
                return None, f"{label} 必须选择一个"
        elif _v == "":
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

    keys = [
        "config_use_type",
        # 个人定制配置
        "kline_qk",
        "judge_zs_qs_level",
        # K线配置
        "kline_type",
        # 分型配置
        "fx_qy",
        "fx_qj",
        "fx_bh",
        # 笔配置
        "bi_type",
        "bi_bzh",
        "bi_qj",
        "bi_fx_cgd",
        "bi_split_k_cross_nums",
        "fx_check_k_nums",
        "allow_bi_fx_strict",
        # 线段配置
        "xd_qj",
        "xd_zs_max_lines_split",
        "xd_allow_bi_pohuai",
        "xd_allow_split_no_highlow",
        "xd_allow_split_zs_kz",
        "xd_allow_split_zs_more_line",
        "xd_allow_split_zs_no_direction",
        # 中枢配置
        "zs_bi_type",
        "zs_xd_type",
        "zs_qj",
        "zs_cd",
        "zs_wzgx",
        # MACD 配置（计算力度背驰）
        "idx_macd_fast",
        "idx_macd_slow",
        "idx_macd_signal",
        # 买卖点计算
        "cl_mmd_cal_qs_1mmd",
        "cl_mmd_cal_not_qs_3mmd_1mmd",
        "cl_mmd_cal_qs_3mmd_1mmd",
        "cl_mmd_cal_qs_not_lh_2mmd",
        "cl_mmd_cal_qs_bc_2mmd",
        "cl_mmd_cal_3mmd_not_lh_bc_2mmd",
        "cl_mmd_cal_1mmd_not_lh_2mmd",
        "cl_mmd_cal_3mmd_xgxd_not_bc_2mmd",
        "cl_mmd_cal_not_in_zs_3mmd",
        "cl_mmd_cal_not_in_zs_gt_9_3mmd",
        # 画图配置
        "enable_kchart_low_to_high",
        "chart_show_fx",
        "chart_show_bi",
        "chart_show_xd",
        "chart_show_bi_zs",
        "chart_show_xd_zs",
        "chart_show_bi_mmd",
        "chart_show_xd_mmd",
        "chart_show_bi_bc",
        "chart_show_xd_bc",
    ]
    cl_config, err = _build_cl_config(request.form, keys)
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