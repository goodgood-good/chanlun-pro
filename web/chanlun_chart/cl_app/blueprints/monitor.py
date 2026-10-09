"""Authenticated controls for persistent screening and signal monitoring."""

from flask import Blueprint, jsonify, render_template, request, abort, send_file
from flask_login import login_required
import hashlib
import sqlite3

from ..services.signal_monitor import service
from ..services.recent_notifications import reader_for


monitor_bp = Blueprint("monitor", __name__)


@monitor_bp.get("/public/alert-chart/<name>.png")
def notification_chart(name):
    # Image clients cannot log in; an unforgeable, expiring URL grants access
    # to exactly one generated PNG, never an evidence file or directory.
    charts = service.notifications.charts
    path = charts.store.resolve(name, request.args.get("expires"), request.args.get("signature")) if charts else None
    if path is None:
        abort(404)
    response = send_file(path, mimetype="image/png", conditional=True, max_age=300)
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@monitor_bp.get("/monitor")
@login_required
def page():
    return render_template("signal_monitor.html")


@monitor_bp.get("/monitor/status")
@login_required
def status():
    try:
        response = jsonify(service.snapshot(page_options=request.args) if request.args.get('view') == 'paged'
                           else service.snapshot())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    response.headers["Cache-Control"] = "private, no-store"
    response.set_etag(hashlib.sha256(response.get_data()).hexdigest())
    response.make_conditional(request)
    return response


@monitor_bp.get('/monitor/analytics')
@login_required
def analytics():
    response=jsonify({**service.analytics.summary(),'delivery':service.notifications.latency_summary()})
    response.headers['Cache-Control']='private, no-store'
    return response


@monitor_bp.get('/monitor/notifications')
@login_required
def recent_notifications():
    try:
        response = jsonify(reader_for(str(service.root)).list(request.args))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (OSError, sqlite3.Error):
        return jsonify(error='通知记录暂不可用，请稍后重试'), 503
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@monitor_bp.get('/monitor/notifications/<identity>')
@login_required
def notification_detail(identity):
    try:
        result = reader_for(str(service.root)).detail(identity)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (OSError, sqlite3.Error):
        return jsonify(error='通知详情暂不可用，请稍后重试'), 503
    if result is None:
        abort(404)
    response = jsonify(result)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@monitor_bp.get('/monitor/notifications/<identity>/images/<int:index>.png')
@login_required
def notification_private_image(identity, index):
    try:
        path = (reader_for(str(service.root)).image_path(identity, index, request.args.get('version'))
                if set(request.args) == {'version'} and len(request.args.getlist('version')) == 1 else None)
    except (ValueError, OSError, sqlite3.Error):
        path = None
    if path is None:
        abort(404)
    response = send_file(path, mimetype='image/png', conditional=True, max_age=300)
    response.headers['Cache-Control'] = 'private, max-age=300'
    return response


@monitor_bp.route('/monitor/holding-risk',methods=['GET','POST'])
@login_required
def holding_risk():
    try:
        value=service.holding_risks() if request.method=='GET' else service.configure_holding_risk(request.get_json(silent=True))
        response=jsonify(value);response.headers['Cache-Control']='private, no-store'
        return response
    except (ValueError,OSError) as exc:
        return jsonify(error=str(exc)),400


@monitor_bp.post("/monitor/<action>")
@login_required
def control(action):
    try:
        options = {"page_options": request.args} if request.args.get('view') == 'paged' else {}
        if action == "start":
            result = service.enable(request.get_json(silent=True), **options)
        elif action == "pause":
            result = service.pause(**options)
        elif action == "check":
            result = service.check_now(**options)
        elif action == "dingtalk":
            result = service.configure_notifications(request.get_json(silent=True), **options)
        else:
            return jsonify(error="未知监听操作"), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
