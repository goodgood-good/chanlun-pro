"""
选股任务相关接口蓝图。

  - `/xuangu/task_list/<market>`
  - `/xuangu/task_add`
"""

from flask import Blueprint, render_template, request, current_app
from flask_login import login_required

from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.zixuan import ZiXuan
from chanlun.xuangu.strict_xuangu import validate_frequency_sequence

from ..services.trading_screening_scope import (
    ScreeningScopeAuthorizationError,
    admit_explicit_validation_codes,
    parse_explicit_scope_limit,
)


xuangu_bp = Blueprint("xuangu", __name__)

MARKET_LABELS = {
    "a": "沪深 A 股",
    "hk": "港股",
    "futures": "国内期货",
    "ny_futures": "纽约期货",
    "fx": "外汇",
    "us": "美股",
    "currency": "数字货币（合约）",
    "currency_spot": "数字货币（现货）",
}


@xuangu_bp.route("/xuangu/task_list/<market>")
@login_required
def xuangu_task_list(market):
    # 获取自选组
    zx = ZiXuan(market)
    zixuan_groups = zx.zixuan_list

    # 交易所支持周期
    frequencys = get_exchange(Market(market)).support_frequencys()

    _xuangu_tasks = current_app.extensions.get("xuangu_tasks")
    xuangu_task_list = _xuangu_tasks.xuangu_task_config_list()

# 任务备忘录。
    task_infos = {
        _k: {
            "task_memo": _v["task_memo"],
            "frequency_memo": _v["frequency_memo"],
        }
        for _k, _v in xuangu_task_list.items()
    }

    return render_template(
        "xuangu_list.html",
        market=market,
        market_label=MARKET_LABELS.get(market, market.upper()),
        tasks=xuangu_task_list,
        task_infos=task_infos,
        zixuan_groups=zixuan_groups,
        frequencys=frequencys,
    )


@xuangu_bp.route("/xuangu/task_add", methods=["POST"])
@login_required
def xuangu_task_add():
    _xuangu_tasks = current_app.extensions.get("xuangu_tasks")
    payload = request.get_json(silent=True) if request.is_json else None

    def request_value(name: str):
        if isinstance(payload, dict) and name in payload:
            return payload.get(name)
        return request.values.get(name)

    market = request_value("market") or ""
    task_name = request_value("task_name") or ""
    frequencys = request_value("frequencys") or ""
    target_zx_group = request_value("target_zx_group") or ""
    opt_type = request_value("opt_type") or ""

    # 旧版 ``source=all`` 会在后台展开整个市场。普通 Web 入口现在只接受
    # 用户逐项提供的代码，并把默认反馈环限制在 12 只、绝对上限限制在 20 只。
    src_zx_group = str(request_value("src_zx_group") or "").strip()
    if src_zx_group.casefold() == "all":
        return {
            "ok": False,
            "code": "full_market_source_forbidden",
            "msg": "普通选股入口已禁用 source=all；请显式填写不超过 12 只代码",
        }, 400
    try:
        scope_limit = parse_explicit_scope_limit(request_value("scope_limit"))
        explicit_codes = admit_explicit_validation_codes(
            request_value("codes"),
            max_symbols=scope_limit,
        )
    except ScreeningScopeAuthorizationError as exc:
        return {
            "ok": False,
            "code": exc.reason_code,
            "msg": str(exc),
        }, 403
    except ValueError as exc:
        return {
            "ok": False,
            "code": "explicit_codes_required",
            "msg": str(exc),
        }, 400

    frequencys = [frequency.strip() for frequency in frequencys.split(",")]
    opt_type = opt_type.split(",")

    # 只接受任务执行器明确支持的方向，避免无效任务进入逐标的计算。
    if (
        not opt_type
        or len(opt_type) != len(set(opt_type))
        or any(o not in ("long", "short") for o in opt_type)
    ):
        return {
            "ok": False,
            "msg": "选股方向(opt_type)必须唯一且仅支持 long/short",
        }
    if any(not frequency for frequency in frequencys):
        return {"ok": False, "msg": "选股周期不能为空"}

    if task_name not in _xuangu_tasks.xuangu_task_config_list().keys():
        return {"ok": False, "msg": "选股任务不存在"}

    allow_freq_num = _xuangu_tasks.xuangu_task_config_list()[task_name][
        "frequency_num"
    ]
    if len(frequencys) != allow_freq_num:
        return {
            "ok": False,
            "msg": f"选股周期错误，该任务可选周期数量 : {allow_freq_num}",
        }

    try:
        frequencys = list(validate_frequency_sequence(frequencys))
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    try:
        supported_frequencys = get_exchange(Market(market)).support_frequencys()
    except Exception:
        return {"ok": False, "msg": "市场或行情源不可用，无法校验选股周期"}
    if any(frequency not in supported_frequencys for frequency in frequencys):
        return {"ok": False, "msg": "选股周期不受当前市场支持"}

    try:
        run_res = _xuangu_tasks.run_xuangu(
            market,
            task_name,
            frequencys,
            opt_type,
            list(explicit_codes),
            target_zx_group,
            scope_limit,
        )
    except RuntimeError as exc:
        if str(exc) != "scheduler is not running":
            raise
        return {
            "ok": False,
            "msg": "任务调度器未运行，请使用正式启动入口。",
        }, 503

    return {
        "ok": run_res,
        "msg": "选股任务已存在，请在当前任务中查看任务" if run_res is False else "",
    }
