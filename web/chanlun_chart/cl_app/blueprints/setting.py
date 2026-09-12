"""
系统设置相关接口蓝图。

  - `/setting`
  - `/setting/save`
"""

from flask import Blueprint, render_template, request
from flask_login import login_required

from chanlun.persistence.db import db


setting_bp = Blueprint("setting", __name__)

@setting_bp.route("/setting", methods=["GET"])
@login_required
def setting():
    proxy = db.cache_get("req_proxy")
    set_config = {
        "proxy_host": proxy["host"] if proxy is not None else "",
        "proxy_port": proxy["port"] if proxy is not None else "",
    }
    return render_template("setting.html", **set_config)


@setting_bp.route("/setting/save", methods=["POST"])
@login_required
def setting_save():
    required_fields = (
        "proxy_host",
        "proxy_port",
    )
    missing_fields = [name for name in required_fields if name not in request.form]
    if missing_fields:
        return {
            "ok": False,
            "msg": "缺少表单字段",
            "fields": missing_fields,
        }, 400

    proxy_host = request.form["proxy_host"].strip()
    proxy_port = request.form["proxy_port"].strip()
    if bool(proxy_host) != bool(proxy_port):
        return {"ok": False, "msg": "代理 Host 和 Port 必须同时填写或同时留空"}, 400
    if proxy_port:
        try:
            parsed_proxy_port = int(proxy_port)
        except ValueError:
            parsed_proxy_port = 0
        if not 1 <= parsed_proxy_port <= 65535:
            return {"ok": False, "msg": "代理 Port 必须是 1 到 65535 的整数"}, 400

    proxy = {
        "host": proxy_host,
        "port": proxy_port,
    }

    db.cache_set_many({"req_proxy": proxy})

    return {"ok": True}
