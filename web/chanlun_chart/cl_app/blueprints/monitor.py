"""Authenticated controls for persistent screening and signal monitoring."""

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from ..services.signal_monitor import service


monitor_bp = Blueprint("monitor", __name__)


@monitor_bp.get("/monitor")
@login_required
def page():
    return render_template("signal_monitor.html")


@monitor_bp.get("/monitor/status")
@login_required
def status():
    response = jsonify(service.snapshot())
    response.headers["Cache-Control"] = "private, no-store"
    return response


@monitor_bp.post("/monitor/<action>")
@login_required
def control(action):
    try:
        if action == "start":
            result = service.enable(request.get_json(silent=True))
        elif action == "pause":
            result = service.pause()
        elif action == "check":
            result = service.check_now()
        elif action == "dingtalk":
            result = service.configure_notifications(request.get_json(silent=True))
        else:
            return jsonify(error="未知监听操作"), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
