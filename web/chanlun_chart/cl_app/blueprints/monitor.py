"""Authenticated controls for persistent screening and signal monitoring."""

from flask import Blueprint, jsonify, render_template, request, abort, send_file, make_response
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


@monitor_bp.route('/monitor/notification-channels', methods=['GET', 'POST'])
@login_required
def notification_channels():
    try:
        settings = service.notifications.channels
        result = settings.snapshot() if request.method == 'GET' else settings.save(request.get_json(silent=True))
        if request.method == 'POST':
            service.notifications._wake.set()
        response = jsonify(channels=result)
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (OSError, TimeoutError):
        return jsonify(error='通知地址配置暂不可用，请稍后重试'), 503


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


@monitor_bp.get('/monitor/notifications/<identity>/charts/<int:index>')
@login_required
def notification_record_chart(identity, index):
    from ..services.notification_viewer import ChartUnavailable, current_chart_url
    reader = reader_for(str(service.root))
    if len(request.args.getlist('version')) > 1:
        abort(400)
    try:
        event = reader.chart_event(identity, index)
    except (ValueError, OSError, sqlite3.Error):
        event = None
    if event is None:
        abort(404)
    settings, error, status = None, None, 200
    try:
        version, payload = reader.chart_viewer.panel(event, version=request.args.get('version'))
        settings = {**payload['settings'], 'datafeed_url':f'/monitor/notifications/{identity}/charts/{index}/{version}/tv'}
    except (ChartUnavailable, OSError, ValueError):
        error = '该次通知的图表未保存、已过期或记录已更新。可返回通知记录重新打开，或单独查看当前行情。'
        status = 409
    response = make_response(render_template('notification_viewer.html', chart_settings=settings,
        chart_error=error, current_chart_url=current_chart_url(event), embedded=request.args.get('embedded') == '1',
        chart_title=f"{event.get('name') or event.get('code','')} · {event.get('frequency','')} · 通知图表"), status)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@monitor_bp.get('/monitor/notifications/<identity>/current-chart/<int:index>')
@login_required
def notification_current_chart(identity, index):
    from chanlun import config
    from ..services.notification_viewer import ChartUnavailable, current_chart_settings, event_version
    if len(request.args.getlist('version')) > 1:
        abort(400)
    try:
        detail = reader_for(str(service.root)).detail(identity)
    except (ValueError, OSError, sqlite3.Error):
        abort(404)
    if detail is None or index >= len(detail.get('signals', [])):
        abort(404)
    settings, error, status = None, None, 200
    try:
        settings = current_chart_settings(detail['signals'][index], detail)
        if request.args.get('version', event_version(settings)) != event_version(settings):
            settings, error, status = None, '该条通知已更新，请重新选择通知加载图表。', 409
    except ChartUnavailable as exc:
        error, status = str(exc), 409
    response = make_response(render_template('notification_current_chart.html', chart_settings=settings,
        chart_error=error, enable_sse=config.ENABLE_SSE_PUSH), status)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@monitor_bp.get('/monitor/notifications/<identity>/charts/<int:index>/<version>/tv/<operation>')
@login_required
def notification_record_chart_data(identity, index, version, operation):
    from ..services.notification_viewer import ChartUnavailable, ChartChanged
    if operation not in {'config', 'symbols', 'history', 'search'}:
        abort(404)
    reader = reader_for(str(service.root))
    try:
        event = reader.chart_event(identity, index)
        if event is None:
            abort(404)
        _, payload = reader.chart_viewer.panel(event, version=version)
        resolution = payload['settings']['resolution']
        if operation == 'config':
            result = {'supported_resolutions':[resolution], 'supports_search':True, 'supports_group_request':False,
                      'supports_marks':False, 'supports_timescale_marks':False, 'supports_time':False}
        elif operation == 'search':
            result = []
        elif operation == 'symbols':
            if request.args.get('symbol') != payload['symbol_info']['ticker']:
                abort(404)
            result = payload['symbol_info']
        else:
            if (request.args.get('symbol') != payload['symbol_info']['ticker']
                    or request.args.get('resolution') != resolution):
                abort(404)
            result = payload['history'] if request.args.get('firstDataRequest') == 'true' else {'s':'no_data'}
        response = jsonify(result)
    except ChartChanged as exc:
        response = jsonify(error=str(exc))
        response.status_code = 409
    except (ChartUnavailable, OSError, ValueError, sqlite3.Error):
        response = jsonify(error='通知图表证据暂不可读取，请重新打开。')
        response.status_code = 404
    response.headers['Cache-Control'] = 'private, no-store'
    if operation == 'history':
        from .tv import _gzip_tv_history_response
        response = _gzip_tv_history_response(response)
    return response


@monitor_bp.route('/monitor/holding-risk',methods=['GET','POST'])
@login_required
def holding_risk():
    try:
        value=service.holding_risks() if request.method=='GET' else service.configure_holding_risk(request.get_json(silent=True))
        response=jsonify(value)
        response.headers['Cache-Control']='private, no-store'
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
        elif action == "notification-policy":
            result = service.configure_point_notifications(request.get_json(silent=True), **options)
        else:
            return jsonify(error="未知监听操作"), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
