"""Authenticated manual stock-screening page and progress endpoints."""

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..services.screening import manager
from ..services import screening_workbench as workbench

screening_bp = Blueprint("screening", __name__)


@screening_bp.get("/screening")
@screening_bp.get("/decision-support/early-screening")
@screening_bp.get("/early-screening")
@login_required
def page():
    return render_template("early_screening.html")


@screening_bp.get("/screening/tasks")
@screening_bp.get("/xuangu/task_list/a")
@login_required
def tasks():
    return redirect("/screening")


@screening_bp.get("/screening/notifications")
@login_required
def notifications():
    return jsonify(workbench.notification_history())


@screening_bp.get("/screening/workbench")
@login_required
def dashboard():
    try:
        if request.args.getlist("source") not in ([], ["latest"]):
            raise ValueError("仅支持最新选股结果，请刷新页面")
        return jsonify(workbench.dashboard())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@screening_bp.route("/screening/reviews", methods=["GET", "POST"])
@login_required
def reviews():
    owner = current_user.get_id()
    if request.method == "GET":
        return jsonify(workbench.reviews(owner, request.args.get("source", "")))
    try:
        return jsonify(workbench.save_review(owner, request.get_json(silent=True)))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@screening_bp.get("/screening/status")
@login_required
def status():
    return jsonify(manager.status())


@screening_bp.get("/screening/results")
@login_required
def results():
    return jsonify(manager.results())


@screening_bp.get("/screening/evidence")
@login_required
def evidence_page():
    # One chart component, with a frozen history source for saved evidence.
    frequency = request.args.get("frequency", "")
    interval = {"1m": "1", "5m": "5", "30m": "30"}.get(frequency)
    fields = {"source", "code", "frequency", "point"}
    if (set(request.args) - {"market"} != fields or not interval
            or any(len(request.args.getlist(k)) != 1 for k in request.args)):
        return jsonify(error="请从最新选股候选打开本次证据图"), 400
    return redirect(url_for("index_show", market=request.args.get("market", "a"), code=request.args["code"], frequency=frequency,
                            layout="single", intervals=interval, chart_sidebar="collapsed",
                            screening_source=request.args["source"], screening_point=request.args["point"]))


@screening_bp.get("/screening/evidence/data")
@login_required
def evidence_data():
    try:
        fields = ("source", "code", "frequency", "point")
        if set(request.args) - {"market"} != set(fields) or any(len(request.args.getlist(k)) != 1 for k in request.args):
            raise ValueError("请从最新选股候选打开本次证据图")
        payload = manager.evidence(*(request.args[k] for k in fields), market=request.args.get("market", "a"))
        response = jsonify(payload)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (ValueError, KeyError, OSError) as exc:
        return jsonify(error=str(exc)), 409


@screening_bp.post("/screening/start")
@login_required
def start():
    try:
        return jsonify(manager.start(request.get_json(silent=True))), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409


@screening_bp.post("/screening/cancel")
@login_required
def cancel():
    return jsonify(manager.cancel())
