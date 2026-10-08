"""Native TradingView snapshots in an isolated browser with no live requests."""
from __future__ import annotations

import base64
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal
import json
import logging
import math
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .notification_text import POINTS, heading, price

APP_ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)
ORIGIN = "http://notification-chart.invalid"
VIEW = "/?screening_source=notification&default_study=MACD"
PERIODS = ("1m", "5m", "30m")
# Calendar windows are consistent across regular and continuously traded markets.
VISIBLE_DAYS = {"1m": 30, "5m": 90, "30m": None}


def prepare(event, runs_root):
    from .notification_charts import load_chart_evidence

    return _prepare_panel(event, *load_chart_evidence(event, runs_root))


def _prepare_panel(event, frame, snapshot, signal, *, context=False):
    from .constants import frequency_maps, market_session, market_timezone, market_types
    from chanlun.screening.rules import CN

    frequency, market = snapshot["source_frequency"], event["market"]
    resolution = frequency_maps[frequency]
    times = [int(t.timestamp()) for t in frame.date]
    positions = {t: i for i, t in enumerate(times)}
    if signal["anchor_at"] not in positions:
        raise ValueError("signal anchor is absent from source candles")
    if times[-1] != snapshot["source_closed_at"] or (
        times[-1] > event["source_closed_at"] if context else times[-1] != event["source_closed_at"]
    ):
        raise ValueError("chart candles and signal have different cutoffs")
    columns = {key: frame[column].astype(float).tolist()
               for key, column in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
    if not all(math.isfinite(v) for values in columns.values() for v in values):
        raise ValueError("chart candles must be finite")
    chart = snapshot["chart"]
    for name in ("macd_dif", "macd_dea", "macd_hist", "higher_macd_dif", "higher_macd_dea", "higher_macd_hist"):
        if len(chart.get(name, [])) != len(frame) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in chart[name]
        ):
            raise ValueError("frozen MACD is incomplete")
    history = {**chart, **columns, "t": times, "s": "ok", "update": False,
               "full_snapshot": False, "bar_time_label": "end",
               "strict_structure_mode": "replace",
               "strict_structure": {k: v for k, v in snapshot.items() if k != "chart"}}
    anchor_pos = positions[signal["anchor_at"]]
    days = VISIBLE_DAYS[frequency]
    history_start = 0 if days is None else bisect_left(times, times[-1] - days * 86400)
    starts = [anchor_pos - 40, history_start]
    centers = [c for level in snapshot["levels"] for c in level.get("centers", [])]
    center = next((c for c in centers if c["center_id"] == event.get("center_id")), None)
    if center and center.get("points"):
        starts.append(positions.get(center["points"][0]["time"], anchor_pos) - 10)
    units = {u["unit_id"]: u for u in snapshot.get("screening_segments", [])}
    for identity in signal.get("compare_leg_unit_ids", []):
        if identity in units:
            starts.append(positions.get(units[identity]["start_time"], anchor_pos) - 10)
    left = max(0, min(starts))
    quantum = float(snapshot["structure_price_quantum"])
    source_decimals = min(8, max(0, int(math.ceil(-math.log10(quantum)))))
    decimals = min(source_decimals, max(2, max(-Decimal(str(round(value, source_decimals))).normalize().as_tuple().exponent
                                             for key in ("o", "h", "l", "c") for value in columns[key])))
    symbol = market + ":" + event["code"]
    consolidation = event.get("signal_type") == "consolidation_divergence"
    risk = event.get('signal_type') == 'holding_risk'
    invalidated = consolidation and event.get('stage') == 'invalidated'
    anchor_price = signal['anchor_price'] if context or risk or invalidated else event['anchor_price']
    label = ('结构止损触发' if event['point_type'] == 'risk_stop' else '反向'+POINTS[event['reverse_point_type']]) if risk else (
             ('预警失效复核' if invalidated else "盘整背驰预警" if event["stage"] == "warning" else "盘整背驰确认")
             if consolidation else POINTS[event["point_type"]])
    timezone = market_timezone.get(market, "Asia/Shanghai")
    cutoff = datetime.fromtimestamp(times[-1], CN).strftime("%Y-%m-%d %H:%M:%S")
    display_from = datetime.fromtimestamp(times[left], CN).strftime('%Y-%m-%d')
    caption = f"行情截止 {cutoff}（北京时间） · 图中时间 {timezone}"
    if frequency == '30m':
        caption += f"\n30m 全部可用历史：{display_from} 至 {cutoff[:10]}"
    else:
        caption += f"\n{frequency} 显示范围：{display_from} 至 {cutoff[:10]}"
    stop = ((event.get("exit_plan") or {}).get("stop_loss") or {})
    stop_price = stop.get("price") if stop.get("status") == "reference" else None
    if risk:
        stop_price = event.get('stop_price')
    if isinstance(stop_price, bool) or not isinstance(stop_price, (int, float)) or not math.isfinite(stop_price):
        stop_price = None
    if context:
        stop_price = None
    role = "信号周期" if not context else f"辅助周期 · 信号来自 {event['frequency']}"
    return {
        "history": history,
        "symbol_info": {"name": symbol, "ticker": symbol, "description": event.get("name") or event["code"],
                        "type": market_types.get(market, "stock"), "session": market_session.get(market, "24x7"),
                        "timezone": timezone, "exchange": market.upper(), "listed_exchange": market.upper(),
                        "minmov": 1, "pricescale": 10 ** decimals, "has_intraday": True, "has_daily": False,
                        "supported_resolutions": [resolution], "volume_precision": 0, "data_status": "endofday"},
        "settings": {"market": market, "code": event["code"], "resolution": resolution,
                     "frequency": frequency, "cutoff": times[-1], "from": times[left],
                     "title": f"{frequency} · {role} · {heading(event)}",
                     "caption": caption,
                     'invalidation_reason':event.get('invalidation_reason') if invalidated and not context else None,
                     'reference_anchor_at':signal.get('reference_anchor_at') if invalidated and not context and signal.get('reference_basis_current') and signal.get('reference_anchor_at') in positions else None,
                     'reference_anchor_price':signal.get('reference_anchor_price'),
                     "anchor_at": signal["anchor_at"], "anchor_price": anchor_price,
                     "marker": (consolidation or risk) and not context,
                     "signal_panel": not context, "up": event["point_type"].endswith(("buy", "down")),
                     "label": label + " " + price(anchor_price, quantum),
                     "stop_price": stop_price,
                     "stop_label": "结构止损参考 " + price(stop_price, quantum)},
    }


def prepare_periods(event, runs_root):
    """Keep the trigger's frozen proof; add independently labelled context charts."""
    from .notification_charts import load_chart_evidence
    from .notification_periods import context_evidence

    frame, snapshot, signal = load_chart_evidence(event, runs_root)
    primary = _prepare_panel(event, frame, snapshot, signal)
    panels = []
    for frequency in PERIODS:
        if frequency == event['frequency']:
            panels.append(primary)
            continue
        try:
            context_frame, context_snapshot = context_evidence(event, runs_root, frequency, frame.attrs)
            panel = _prepare_panel(event, context_frame, context_snapshot,
                                   {'anchor_at': context_snapshot['source_closed_at'],
                                    'anchor_price': float(context_frame.close.iloc[-1])}, context=True)
            panels.append(panel)
        except Exception as exc:
            # One unavailable context period must not suppress verified signal
            # evidence. Never relabel another period or synthesize missing bars.
            LOG.warning('notification context unavailable market=%s code=%s frequency=%s error=%s',
                        event['market'], event['code'], frequency, type(exc).__name__)
            panels.append({'settings': {'frequency': frequency, 'signal_panel': False},
                           'error': '该周期行情暂不可用；已保留信号周期图表',
                           'error_type': type(exc).__name__})
    return {'panels': panels}


def route_request(route, payload):
    """Fulfill every request locally; never touch accounts, providers or saved layouts."""
    request = route.request
    url = urlsplit(request.url)
    path = unquote(url.path)
    if url.scheme + "://" + url.netloc != ORIGIN or request.method != "GET":
        route.abort()
        return
    if path == "/":
        route.fulfill(path=str(APP_ROOT / "templates/notification_chart.html"), content_type="text/html; charset=utf-8")
    elif path.startswith("/static/"):
        root = (APP_ROOT / "static").resolve()
        target = (APP_ROOT / path.lstrip("/")).resolve()
        if root not in target.parents or not target.is_file():
            route.abort()
            return
        mime = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}.get(target.suffix)
        route.fulfill(path=str(target), content_type=mime or mimetypes.guess_type(str(target))[0] or "application/octet-stream")
    else:
        query = parse_qs(url.query)
        resolution = payload["settings"]["resolution"]
        if path == "/tv/config":
            result = {"supported_resolutions": [resolution], "supports_search": True, "supports_group_request": False,
                      "supports_marks": False, "supports_timescale_marks": False, "supports_time": False}
        elif path == "/tv/symbols":
            result = payload["symbol_info"]
        elif path == "/tv/history":
            if query.get("symbol") != [payload["symbol_info"]["ticker"]] or query.get("resolution") != [resolution]:
                route.abort()
                return
            result = payload["history"] if query.get("firstDataRequest") == ["true"] else {"s": "no_data"}
        elif path == "/tv/1.1/drawings":
            result = {"status": "ok", "data": {"complete": True, "contexts": []} if "manifest" in query
                      else {"schema": "chanlun-user-drawings", "sources": {}, "groups": {}}}
        elif path in ("/tv/1.1/charts", "/tv/1.1/study_templates"):
            result = {"status": "ok", "data": []}
        elif path == "/tv/search":
            result = []
        else:
            route.abort()
            return
        route.fulfill(body=json.dumps(result, ensure_ascii=False, allow_nan=False), content_type="application/json")


def capture(payload, *, diagnostics=None):
    from playwright.sync_api import sync_playwright

    def bind_routes(panel):
        return lambda route: route_request(route, panel)

    # One sequential worker owns each short-lived browser. A failed export closes
    # its context/process in finally and cannot contaminate the next symbol.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=20000)
        try:
            # This bundled TV snapshot implementation misaligns axes at a
            # fractional devicePixelRatio; native export is verified at 1:1.
            context = browser.new_context(viewport={"width": 1440, "height": 960}, device_scale_factor=1,
                                          locale="zh-CN", timezone_id="Asia/Shanghai", service_workers="block")
            panels = payload.get('panels', [payload])
            images = []
            composer = None
            for panel in panels:
                if panel.get('error'):
                    images.append({'frequency': panel['settings']['frequency'], 'error': panel['error']})
                    continue
                page = context.new_page()
                try:
                    page.route("**/*", bind_routes(panel))
                    page.add_init_script("window.__notificationChart=" + json.dumps(panel["settings"], ensure_ascii=False) + ";")
                    page.goto(ORIGIN + VIEW, wait_until="load", timeout=20000)
                    page.wait_for_function("window.__notificationReady === true || !!window.__notificationError", timeout=30000)
                    error = page.evaluate("window.__notificationError || null")
                    if error:
                        raise ValueError("TradingView export: " + error)
                    # Each panel uses TradingView's own export. Compose those
                    # native canvases into one attachment and one send job.
                    data = page.evaluate("() => Promise.race([window.exportNotificationChart(), new Promise((_, reject) => setTimeout(() => reject(new Error('snapshot timeout')), 15000))])")
                    if not isinstance(data, str) or not data.startswith('data:image/png;base64,'):
                        raise ValueError('TradingView did not export a PNG')
                    if diagnostics is not None:
                        diagnostics.append(page.evaluate('window.__notificationViewport'))
                    images.append({'data': data, 'frequency': panel['settings']['frequency']})
                    if panel['settings'].get('signal_panel', True):
                        composer = page
                except Exception as exc:
                    if panel['settings'].get('signal_panel', True):
                        raise
                    LOG.warning('notification chart export unavailable market=%s code=%s frequency=%s error=%s',
                                panel['settings'].get('market'), panel['settings'].get('code'),
                                panel['settings']['frequency'], type(exc).__name__)
                    if diagnostics is not None:
                        diagnostics.append({'frequency': panel['settings']['frequency'], 'status': 'failed',
                                            'error_type': type(exc).__name__})
                    images.append({'frequency': panel['settings']['frequency'],
                                   'error': '该周期图表暂不可用；已保留信号周期图表'})
                finally:
                    if page is not composer:
                        page.close()
            if composer is None:
                raise ValueError('no verified signal chart to export')
            data = composer.evaluate("images => window.composeNotificationCharts(images)", images) if len(images) > 1 else images[0]['data']
            prefix = "data:image/png;base64,"
            if not isinstance(data, str) or not data.startswith(prefix):
                raise ValueError("TradingView did not export a PNG")
            return base64.b64decode(data[len(prefix):], validate=True)
        finally:
            browser.close()
