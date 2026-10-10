"""Native TradingView snapshots in an isolated browser with no live requests."""
from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, unquote, urlsplit

from .notification_text import POINTS, heading, price

APP_ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)
ORIGIN = "http://notification-chart.invalid"
VIEW = "/?screening_source=notification&default_study=MACD"
PERIODS = ("1m", "5m", "30m")
# Calendar windows are consistent across regular and continuously traded markets.
VISIBLE_DAYS = {"1m": 30, "5m": 90, "30m": None}
HISTORY_RESOLVED = {'target_reached', 'source_exhausted'}
PANEL_ID = re.compile(r'(?:(?:proof|context):)?(?:1m|5m|30m)\Z|history:(?:1m|5m|30m):[a-f0-9]{64}\Z')


class PanelImageCache:
    """Reuse only exported pixels belonging to this exact frozen event."""

    def __init__(self, event, root):
        self.identity = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=True,
                                                  separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.root = Path(root) / self.identity
        self.entries = {}

    def read(self):
        output = {}
        try:
            saved = json.loads((self.root / 'panels.json').read_text(encoding='utf-8'))
            if saved.get('schema') not in ('notification-frozen-panels-v1', 'notification-frozen-panels-v2') or saved.get('event_sha256') != self.identity:
                return output
            for frequency, digest in saved.get('panels', {}).items():
                if not isinstance(frequency, str) or not PANEL_ID.fullmatch(frequency) or not isinstance(digest, str):
                    continue
                image = (self.root / (frequency.replace(':', '_') + '.png')).read_bytes()
                if image.startswith(b'\x89PNG\r\n\x1a\n') and hashlib.sha256(image).hexdigest() == digest:
                    output[frequency] = 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')
                    self.entries[frequency] = digest
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return output

    def save(self, frequency, data):
        if not isinstance(frequency, str) or not PANEL_ID.fullmatch(frequency) or not isinstance(data, str) or not data.startswith('data:image/png;base64,'):
            raise ValueError('invalid frozen panel result')
        image = base64.b64decode(data.split(',', 1)[1], validate=True)
        if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 8 * 1024 * 1024:
            raise ValueError('invalid frozen panel PNG')
        self.root.mkdir(parents=True, exist_ok=True)
        filename = frequency.replace(':', '_')
        target = self.root / (filename + '.png')
        temporary = self.root / (filename + '.' + secrets.token_hex(6) + '.tmp')
        try:
            temporary.write_bytes(image)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self.entries[frequency] = hashlib.sha256(image).hexdigest()
        record = self.root / 'panels.json'
        temporary = self.root / ('panels.' + secrets.token_hex(6) + '.tmp')
        try:
            temporary.write_text(json.dumps({'schema': 'notification-frozen-panels-v2',
                                             'event_sha256': self.identity, 'panels': self.entries}), encoding='utf-8')
            os.replace(temporary, record)
        finally:
            temporary.unlink(missing_ok=True)


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


def prepare_periods(event, runs_root, *, reused_images=None):
    """Keep the trigger's frozen proof; add independently labelled context charts."""
    from .notification_charts import load_chart_evidence
    from .notification_periods import context_evidence

    frame, snapshot, signal = load_chart_evidence(event, runs_root)
    reused_images = reused_images or {}
    primary = None if event['frequency'] in reused_images else _prepare_panel(event, frame, snapshot, signal)
    panels = []
    for frequency in PERIODS:
        if frequency in reused_images:
            panels.append({'settings': {'frequency': frequency, 'signal_panel': frequency == event['frequency']},
                           'cached_image': reused_images[frequency]})
            continue
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


def history_description(frequency, coverage):
    """Describe the actual obtained range without treating a budget as origin."""
    from chanlun.screening.rules import CN
    def day(value):
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, CN).strftime('%Y-%m-%d')
        return str(value or '未知')[:10]
    target = f"近 {VISIBLE_DAYS[frequency]} 天" if VISIBLE_DAYS[frequency] else '当前来源全部可用历史'
    state = coverage.get('state', 'pending')
    span = f"{day(coverage.get('available_from'))} 至 {day(coverage.get('available_to'))}"
    if state == 'target_reached':
        return f'{frequency} {target} · 已取数据 {span}'
    if state == 'source_exhausted':
        short = '；不足目标范围' if coverage.get('target_reached') is False and VISIBLE_DAYS[frequency] else ''
        return f'{frequency} {coverage.get("source_label") or "当前来源"}已取尽 · 已取数据 {span}{short}'
    text = {'pending': '历史范围正在补齐', 'unsupported': '该来源暂不支持扩展',
            'invalid': '历史校验未通过', 'source_error': '历史来源请求失败，等待重试',
            'render_error': '长期背景导出失败，等待重试'}.get(state, '历史范围待核验')
    return f'{frequency} {target} · {text}'


def _background_panel(event, frame, snapshot, coverage):
    structure = snapshot.get('analysis_scope') == 'native_centers' and snapshot.get('display_scope') == 'notification_background'
    data_only = snapshot.get('analysis_scope') == 'notification_background'
    if not structure and not data_only:
        raise ValueError('background indicators must have an independent scope')
    panel = _prepare_panel(event, frame, snapshot,
        {'anchor_at': snapshot['source_closed_at'], 'anchor_price': float(frame.close.iloc[-1])}, context=True)
    frequency = snapshot['source_frequency']
    from chanlun.screening.rules import CN
    visible_from = datetime.fromtimestamp(panel['settings']['from'], CN).strftime('%Y-%m-%d')
    visible_to = datetime.fromtimestamp(panel['settings']['cutoff'], CN).strftime('%Y-%m-%d %H:%M:%S')
    caption = (history_description(frequency, coverage)
               + f'\n画面 {visible_from} 至 {visible_to}（北京时间）'
               + '\n截至原通知行情时刻；不复制原买卖点或止损线')
    if data_only:
        caption += '\n背景结构未提供，仅显示K线与指标'
    panel['settings'].update(background_only=True, background_data_only=data_only, panel_id='history:'+frequency,
        cache_key='history:'+frequency+':'+coverage['scope'], purpose='independent_background',
        title=f'{frequency} · 长期背景（{"仅K线与指标" if data_only else "结构与指标独立计算"}） · {event.get("name") or event["code"]}',
        caption=caption,
        marker=False, label='', stop_price=None, stop_label='', reference_anchor_at=None)
    return panel


def prepare_history_layout(event, runs_root, *, reused_images=None, history_period=None):
    """Three period groups; preserve proof pixels and add independent history.

    At most one background period advances a bounded fetch slice per call.
    A normal first render does not start long-history paging.
    """
    from .notification_charts import load_chart_evidence
    from .notification_periods import background_evidence, context_evidence, HistoryPending, HistoryUnavailable
    primary_frame, primary_snapshot, primary_signal = load_chart_evidence(event, runs_root)
    reused = reused_images or {}
    panels, histories, history_panels = [], {}, {}
    primary = event['frequency']

    def frozen(frequency, frame=None, snapshot=None, signal=None):
        purpose = 'proof' if frequency == primary else 'context'
        key = purpose+':'+frequency
        image = reused.get(key) or reused.get(frequency)
        if image:
            return {'settings': {'frequency': frequency, 'panel_id': key, 'cache_key': key,
                     'purpose': 'frozen_signal' if purpose == 'proof' else 'frozen_context',
                     'signal_panel': purpose == 'proof'}, 'cached_image': image}
        panel = _prepare_panel(event, frame, snapshot, signal, context=purpose != 'proof')
        panel['settings'].update(panel_id=key, cache_key=key,
                                 purpose='frozen_signal' if purpose == 'proof' else 'frozen_context')
        return panel

    for frequency in PERIODS:
        frame = primary_frame if frequency == primary else None
        snapshot = primary_snapshot if frequency == primary else None
        if frequency == primary:
            panels.append(frozen(frequency, frame, snapshot, primary_signal))
        background = None
        try:
            background = background_evidence(event, runs_root, frequency, primary_frame.attrs,
                                               anchor_frame=frame, fetch=frequency == history_period)
        except HistoryPending as exc:
            histories[frequency] = dict(exc.coverage)
        except HistoryUnavailable as exc:
            histories[frequency] = dict(exc.coverage)
        except Exception as exc:
            histories[frequency] = {'state': 'invalid', 'reason': 'background_preparation_failed',
                                    'error_code': type(exc).__name__, 'target_days': VISIBLE_DAYS[frequency]}
        if background is not None:
            history_frame, history_snapshot, coverage = background
            histories[frequency] = dict(coverage)
            key = 'history:'+frequency+':'+coverage['scope']
            if key in reused:
                panel = {'settings': {'frequency': frequency, 'panel_id': 'history:'+frequency,
                         'cache_key': key, 'purpose': 'independent_background', 'signal_panel': False,
                         'background_only': True}, 'cached_image': reused[key]}
            else:
                panel = _background_panel(event, history_frame, history_snapshot, coverage)
            # A primary proof already covering the exact same target needs no
            # duplicate chart. A longer/different independent background does.
            if frequency != primary or int(history_frame.date.iloc[0].timestamp()) < int(primary_frame.date.iloc[0].timestamp()):
                panels.append(panel)
                history_panels[frequency] = 'history:'+frequency
            else:
                history_panels[frequency] = 'proof:'+frequency
            continue
        if frequency == primary:
            continue
        key = 'context:'+frequency
        if key in reused or frequency in reused:
            panels.append(frozen(frequency))
            continue
        try:
            frame, snapshot = context_evidence(event, runs_root, frequency, primary_frame.attrs)
            panels.append(frozen(frequency, frame, snapshot,
                {'anchor_at': snapshot['source_closed_at'], 'anchor_price': float(frame.close.iloc[-1])}))
            # Persist the first same-period anchor without starting another
            # network history slice or replacing an earlier saved anchor.
            try:
                background_frame, background_snapshot, coverage = background_evidence(
                    event, runs_root, frequency, primary_frame.attrs, anchor_frame=frame, fetch=False)
                histories[frequency] = dict(coverage)
                panels[-1] = _background_panel(event, background_frame, background_snapshot, coverage)
                history_panels[frequency] = 'history:'+frequency
            except (HistoryPending, HistoryUnavailable) as exc:
                # Persisting an anchor without fetching cannot turn an earlier
                # source/contract failure in this slice into normal progress.
                if histories.get(frequency, {}).get('state') not in ('source_error','invalid','unsupported'):
                    histories[frequency] = dict(exc.coverage)
        except Exception as exc:
            panels.append({'settings': {'frequency': frequency, 'panel_id': key, 'signal_panel': False},
                           'error': '该周期行情暂不可用；已保留信号周期图表', 'error_type': type(exc).__name__})
    return {'panels': panels, 'history_coverage': histories, 'history_panel_ids': history_panels}


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


def capture(payload, *, diagnostics=None, panel_sink=None, completion_event=None):
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
                identity = {'frequency': panel['settings']['frequency'],
                            'panel_id': panel['settings'].get('panel_id', panel['settings']['frequency']),
                            'purpose': panel['settings'].get('purpose', 'frozen_signal' if panel['settings'].get('signal_panel') else 'frozen_context')}
                if panel.get('cached_image'):
                    images.append({'data': panel['cached_image'], **identity})
                    continue
                if panel.get('error'):
                    images.append({**identity, 'error': panel['error'],
                                   'error_type': panel.get('error_type')})
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
                    if panel_sink is not None:
                        try:
                            panel_sink(panel['settings'].get('cache_key', panel['settings']['frequency']), data)
                        except OSError:
                            LOG.warning('notification panel cache unavailable frequency=%s', panel['settings']['frequency'])
                    images.append({'data': data, **identity})
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
                    images.append({**identity,
                                   'error': '该周期图表暂不可用；已保留信号周期图表',
                                   'error_type': type(exc).__name__})
                finally:
                    if page is not composer:
                        page.close()
            if composer is None:
                if not any(panel.get('cached_image') and panel['settings'].get('signal_panel', True) for panel in panels):
                    raise ValueError('no verified signal chart to export')
                # The primary pixels are already frozen. A blank canvas can
                # compose them without re-opening a TradingView chart.
                composer = context.new_page()
                composer.set_content('<!doctype html><meta charset="utf-8">')
                compose_source = (APP_ROOT / 'static/js/notification_chart.js').read_text(encoding='utf-8')
                begin = compose_source.index('window.composeNotificationCharts = async function (panels) {')
                end = compose_source.index('\n    };', begin) + len('\n    };')
                # Playwright invokes a returned function with its argument
                # (null here). Install without returning the compositor.
                composer.evaluate('() => {\n' + compose_source[begin:end] + '\n}')
            data = composer.evaluate("images => window.composeNotificationCharts(images)", images) if len(images) > 1 else images[0]['data']
            histories = deepcopy(payload.get('history_coverage'))
            if histories is not None:
                for frequency, coverage in histories.items():
                    if coverage.get('state') not in HISTORY_RESOLVED:
                        continue
                    required = payload.get('history_panel_ids', {}).get(frequency, 'history:'+frequency)
                    if not any(image['panel_id'] == required and image.get('data') for image in images):
                        coverage.update(state='render_error', reason='background_export_failed', can_resume=True)
                lines = [history_description(frequency, histories.get(frequency, {})) for frequency in PERIODS]
                data = composer.evaluate("""async ({data, lines}) => {
                    const image = new Image(); image.src = data; await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = image.width; canvas.height = image.height + 126;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#f1f5f9'; ctx.fillRect(0, 0, canvas.width, 126);
                    ctx.fillStyle = '#334155'; ctx.font = '21px sans-serif';
                    lines.forEach((line, index) => ctx.fillText(line, 28, 34 + index * 34, canvas.width - 56));
                    ctx.drawImage(image, 0, 126); return canvas.toDataURL('image/png');
                }""", {'data': data, 'lines': lines})
            if completion_event and all(image.get('data') for image in images):
                from chanlun.screening.rules import CN
                # DingTalk's chart transport sends pixels only. Label the new
                # supplement without altering any frozen individual panel.
                caption = '原行情截止 ' + datetime.fromtimestamp(completion_event['source_closed_at'], CN).strftime('%Y-%m-%d %H:%M:%S')
                if completion_event.get('recorded_at'):
                    caption += ' · 原记录 ' + datetime.fromtimestamp(completion_event['recorded_at'], CN).strftime('%Y-%m-%d %H:%M:%S')
                data = composer.evaluate("""async ({data, title, caption}) => {
                    const image = new Image(); image.src = data; await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = image.width; canvas.height = image.height + 88;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#eef2ff'; ctx.fillRect(0, 0, canvas.width, 88);
                    ctx.fillStyle = '#1e293b'; ctx.font = 'bold 26px sans-serif';
                    ctx.fillText(title, 28, 35, canvas.width - 56);
                    ctx.font = '19px sans-serif'; ctx.fillText(caption, 28, 67, canvas.width - 56);
                    ctx.drawImage(image, 0, 88);
                    return canvas.toDataURL('image/png');
                }""", {'data': data, 'title': '原通知附图补全 · ' + heading(completion_event),
                        'caption': caption + '（北京时间）'})
            prefix = "data:image/png;base64,"
            if not isinstance(data, str) or not data.startswith(prefix):
                raise ValueError("TradingView did not export a PNG")
            from .notification_charts import RenderedChart
            present = [frequency for frequency in PERIODS if any(image['frequency'] == frequency and image.get('data') for image in images)]
            primary_frequency = next((panel['settings']['frequency'] for panel in panels
                                      if panel['settings'].get('signal_panel', True)), None)
            return RenderedChart(base64.b64decode(data[len(prefix):], validate=True),
                                 periods=present,
                                 missing_periods=[frequency for frequency in PERIODS if frequency not in present],
                                 errors={image['frequency']: image.get('error_type') or image['error']
                                         for image in images if image.get('error')},
                                 primary_frequency=primary_frequency, history_coverage=histories,
                                 panels=[{k: image[k] for k in ('frequency', 'panel_id', 'purpose')} |
                                         {'rendered': bool(image.get('data'))} for image in images])
        finally:
            browser.close()
