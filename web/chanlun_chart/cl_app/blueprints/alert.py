"""
提醒（警报）相关接口蓝图。
  - `/alert_list/<market>`
  - `/alert_edit/<market>/<id>`
  - `/alert_save`
  - `/alert_del/<id>`
  - `/alert_records/<market>`
  - `/jobs` （任务列表视图）
"""

import json

from flask import Blueprint, render_template, request, current_app
from flask_login import login_required

from chanlun.market import Market
from chanlun import fun
from chanlun.persistence.db import db
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil
from chanlun.zixuan import ZiXuan


alert_bp = Blueprint("alert", __name__)


@alert_bp.route("/alert_list/<market>")
@login_required
def alert_list(market):
    _alert_tasks = current_app.extensions.get("alert_tasks")
    al = _alert_tasks.task_list(market)
    al = [
        {
            "id": _l.id,
            "market": _l.market,
            "task_name": _l.task_name,
            "zx_group": _l.zx_group,
            "interval_minutes": _l.interval_minutes,
            "frequency": _l.frequency,
            "check_bi_type": _l.check_bi_type,
            "check_bi_beichi": _l.check_bi_beichi,
            "check_bi_mmd": _l.check_bi_mmd,
            "check_xd_type": _l.check_xd_type,
            "check_xd_beichi": _l.check_xd_beichi,
            "check_xd_mmd": _l.check_xd_mmd,
            "check_idx_ma_info": _l.check_idx_ma_info,
            "check_idx_macd_info": _l.check_idx_macd_info,
            "is_send_msg": _l.is_send_msg,
            "is_run": _l.is_run,
        }
        for _l in al
    ]
    return {"code": 0, "msg": "", "count": len(al), "data": al}


@alert_bp.route("/alert_edit/<market>/<id>")
@login_required
def alert_edit(market, id):
    _alert_tasks = current_app.extensions.get("alert_tasks")
    alert_config = {
        "id": "",
        "market": market,
        "task_name": "",
        "zx_group": "我的关注",
        "interval_minutes": 5,
        "frequency": "5m",
        "check_bi_type": "up,down",
        "check_bi_beichi": "pz,qs",
        "check_bi_mmd": "",
        "check_xd_type": "up,down",
        "check_xd_beichi": "pz,qs",
        "check_xd_mmd": "",
        "check_idx_ma_info_enable": 0,
        "check_idx_ma_info_slow": 10,
        "check_idx_ma_info_fast": 5,
        "check_idx_ma_info_cross_up": 0,
        "check_idx_ma_info_cross_down": 0,
        "check_idx_macd_info_enable": 0,
        "check_idx_macd_info_cross_up": 0,
        "check_idx_macd_info_cross_down": 0,
        "is_send_msg": 1,
        "is_run": 1,
    }
    if id != "0":
        # DB 查询失败（连接断开/表不存在等）时降级为默认 alert_config，避免编辑页 500，
        # 让用户至少能看到表单；真因日志可观测。
        try:
            _alert_config = _alert_tasks.alert_get(id)
        except Exception as e:
            LogUtil.warning(f"[alert_edit] alert_get({id}) failed: {e}")
            _alert_config = None
        if _alert_config is not None:
            check_idx_ma_info = (
                json.loads(_alert_config.check_idx_ma_info)
                if _alert_config.check_idx_ma_info
                else {
                    "enable": 0,
                    "slow": 10,
                    "fast": 5,
                    "cross_up": 0,
                    "cross_down": 0,
                }
            )
            check_idx_macd_info = (
                json.loads(_alert_config.check_idx_macd_info)
                if _alert_config.check_idx_macd_info
                else {
                    "enable": 0,
                    "cross_up": 0,
                    "cross_down": 0,
                }
            )
            alert_config = {
                "id": _alert_config.id,
                "market": _alert_config.market,
                "task_name": _alert_config.task_name,
                "zx_group": _alert_config.zx_group,
                "interval_minutes": _alert_config.interval_minutes,
                "frequency": _alert_config.frequency,
                "check_bi_type": _alert_config.check_bi_type,
                "check_bi_beichi": _alert_config.check_bi_beichi,
                "check_bi_mmd": _alert_config.check_bi_mmd,
                "check_xd_type": _alert_config.check_xd_type,
                "check_xd_beichi": _alert_config.check_xd_beichi,
                "check_xd_mmd": _alert_config.check_xd_mmd,
                "check_idx_ma_info_enable": check_idx_ma_info["enable"],
                "check_idx_ma_info_slow": check_idx_ma_info["slow"],
                "check_idx_ma_info_fast": check_idx_ma_info["fast"],
                "check_idx_ma_info_cross_up": check_idx_ma_info["cross_up"],
                "check_idx_ma_info_cross_down": check_idx_ma_info["cross_down"],
                "check_idx_macd_info_enable": check_idx_macd_info["enable"],
                "check_idx_macd_info_cross_up": check_idx_macd_info["cross_up"],
                "check_idx_macd_info_cross_down": check_idx_macd_info["cross_down"],
                "is_send_msg": _alert_config.is_send_msg,
                "is_run": _alert_config.is_run,
            }

    # 获取自选组
    zx = ZiXuan(market)
    zixuan_groups = zx.zixuan_list

    # 交易所支持周期
    frequencys = get_exchange(Market(market)).support_frequencys()

    return render_template(
        "alert.html",
        zixuan_groups=zixuan_groups,
        frequencys=frequencys,
        **alert_config,
    )


def _parse_interval_minutes(value, default=60):
    """interval_minutes 前端 layui number 校验基于 isNaN 放行 '5.5'/'1e3'(小数/科学计数),
    原始字符串到后端裸 int() 会 ValueError→alert_save 无 try→Flask 500(前端 ajax 无 error 回调
    故静默保存失败)。稳健解析为整数分钟并 clamp 到 1-1380(与前端范围一致)。"""
    try:
        minutes = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(1, min(minutes, 1380))


def _binary_form_value(name, required=False):
    value = request.form.get(name)
    if value in {None, ""} and not required:
        return 0
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return int(value)


def _integer_form_value(name, default=0, minimum=0, maximum=10000):
    value = request.form.get(name)
    if value in {None, ""}:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is out of range")
    return parsed


def _required_form_text(name, maximum=128):
    value = str(request.form.get(name) or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} is required")
    return value


@alert_bp.route("/alert_save", methods=["POST"])
@login_required
def alert_save():
    _alert_tasks = current_app.extensions.get("alert_tasks")
    try:
        alert_id = str(request.form.get("id") or "").strip()
        if alert_id:
            if not alert_id.isdigit() or int(alert_id) < 1:
                raise ValueError("id must be a positive integer")
        market = _required_form_text("market", maximum=32)
        if market not in {item.value for item in Market}:
            raise ValueError("market is invalid")
        check_idx_ma_infos = json.dumps(
            {
                "enable": _binary_form_value("check_idx_ma_info_enable"),
                "slow": _integer_form_value("check_idx_ma_info_slow"),
                "fast": _integer_form_value("check_idx_ma_info_fast"),
                "cross_up": _binary_form_value("check_idx_ma_info_cross_up"),
                "cross_down": _binary_form_value("check_idx_ma_info_cross_down"),
            }
        )
        check_idx_macd_infos = json.dumps(
            {
                "enable": _binary_form_value("check_idx_macd_info_enable"),
                "cross_up": _binary_form_value("check_idx_macd_info_cross_up"),
                "cross_down": _binary_form_value("check_idx_macd_info_cross_down"),
            }
        )
        alert_config = {
            "id": alert_id,
            "market": market,
            "task_name": _required_form_text("task_name"),
            "interval_minutes": _parse_interval_minutes(
                request.form.get("interval_minutes")
            ),
            "zx_group": _required_form_text("zx_group"),
            "frequency": _required_form_text("frequency", maximum=32),
            "check_bi_type": request.form.get("check_bi_type", ""),
            "check_bi_beichi": request.form.get("check_bi_beichi", ""),
            "check_bi_mmd": request.form.get("check_bi_mmd", ""),
            "check_xd_type": request.form.get("check_xd_type", ""),
            "check_xd_beichi": request.form.get("check_xd_beichi", ""),
            "check_xd_mmd": request.form.get("check_xd_mmd", ""),
            "check_idx_ma_info": check_idx_ma_infos,
            "check_idx_macd_info": check_idx_macd_infos,
            "is_send_msg": _binary_form_value("is_send_msg", required=True),
            "is_run": _binary_form_value("is_run", required=True),
        }
    except ValueError as exc:
        return {
            "ok": False,
            "code": "invalid_request",
            "msg": str(exc),
        }, 400
    try:
        _alert_tasks.alert_save(alert_config)
    except RuntimeError as exc:
        if str(exc) != "scheduler is not running":
            raise
        return {
            "ok": False,
            "msg": "任务调度器未运行，请使用正式启动入口。",
        }, 503
    return {"ok": True}


@alert_bp.route("/alert_del/<id>", methods=["POST"])
@login_required
def alert_del(id):
    _alert_tasks = current_app.extensions.get("alert_tasks")
    try:
        res = _alert_tasks.alert_del(id)
    except RuntimeError as exc:
        if str(exc) != "scheduler is not running":
            raise
        return {
            "ok": False,
            "msg": "任务调度器未运行，请使用正式启动入口。",
        }, 503
    return {"ok": res}


@alert_bp.route("/alert_records/<market>")
@login_required
def alert_records(market):
    task_name = request.args.get("task_name")
    records = db.alert_record_query(market, task_name)
    rls = [
        {
            "code": _r.stock_code,
            "name": _r.stock_name,
            "frequency": _r.frequency,
            "line_type": _r.line_type,
            "msg": _r.alert_msg,
            "is_done": _r.bi_is_done,
            "is_td": _r.bi_is_td,
            "task_name": _r.task_name,
            "datetime_str": fun.datetime_to_str(_r.alert_dt),
        }
        for _r in records
    ]
    return {
        "code": 0,
        "msg": "",
        "count": len(rls),
        "data": rls,
    }


@alert_bp.route("/jobs")
@login_required
def jobs():
    scheduler = current_app.extensions.get("scheduler")
    from cl_app import _scheduler_task_snapshot

    jobs_snapshot = _scheduler_task_snapshot(scheduler)
    return render_template("jobs.html", jobs=jobs_snapshot)
