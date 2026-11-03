"""Authenticated, read-only chart views of saved notification evidence."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode

from .dingtalk_notifications import signal_key
from .notification_evidence import NotificationEvidence


class ChartUnavailable(ValueError):
    pass


class ChartChanged(ChartUnavailable):
    pass


def current_chart_url(event):
    from chanlun.screening.markets import validate_symbol
    market, code = event.get('market', 'a'), event.get('code')
    if not isinstance(code, str) or not validate_symbol(market, code):
        return None
    return '/?' + urlencode({'market':market, 'code':code, 'frequency':event.get('frequency') or '5m',
                            'layout':'single', 'workspace_layout':'1'})


def current_chart_settings(signal, notification):
    """Live candles with annotations from this receipt, independent of archives."""
    from .constants import frequency_maps
    from .monitor_view import LABELS
    if current_chart_url(signal) is None:
        raise ChartUnavailable('该条记录未保留可定位的标的。')
    frequency = signal.get('frequency') or '5m'
    if frequency not in frequency_maps:
        raise ChartUnavailable('该条记录的图表周期不可用。')

    def number(value):
        return value if type(value) in (int, float) and math.isfinite(value) else None

    def timestamp(value):
        value = number(value)
        return int(value) if value is not None and value > 0 else None

    sent_at = timestamp(notification.get('sent_at')) if notification.get('status') == 'sent' else None
    notice_at = sent_at or timestamp(notification.get('created_at')) or timestamp(signal.get('recorded_at'))
    return {'market':signal.get('market') or 'a', 'code':signal['code'],
            'name':signal.get('name') or signal['code'], 'frequency':frequency,
            'resolution':str(frequency_maps[frequency]), 'notification_at':notice_at,
            'notification_label':'通知发出' if sent_at else '信号记录',
            'anchor_at':timestamp(signal.get('anchor_at')), 'anchor_price':number(signal.get('anchor_price')),
            'source_closed_at':timestamp(signal.get('source_closed_at')), 'point_type':signal.get('point_type'),
            'point_label':LABELS.get(signal.get('point_type'), '信号'),
            'stage_label':{'approaching':'预警未确认', 'warning':'预警', 'confirmed':'已确认',
                           'invalidated':'已失效', 'left':'已退出', 'triggered':'已触发'}.get(signal.get('stage'), '')}


def _archive_identity(event):
    return hashlib.sha256(signal_key(event).encode()).hexdigest()


def event_version(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class NotificationViewer:
    def __init__(self, root, *, now=None):
        self.root = Path(root)
        self.now = now or time.time
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def _live_paths(self, event):
        from chanlun.screening.evidence import evidence_paths
        manifest = event.get('evidence')
        if not isinstance(manifest, dict) or not all(isinstance(manifest.get(key), str)
                and re.fullmatch(r'[a-f0-9]{64}', manifest[key]) for key in ('parquet_sha256', 'snapshot_sha256')):
            raise ValueError('missing evidence manifest')
        run = event.get('evidence_run_id')
        if not isinstance(run, str) or not re.fullmatch(r'[a-f0-9]{32}', run):
            raise ValueError('missing evidence run')
        directory = self.root / 'runs' / run
        if directory.resolve().parent != (self.root / 'runs').resolve() or directory.is_symlink():
            raise ValueError('invalid evidence run')
        return evidence_paths(directory, event['code'], event['frequency'], event.get('market','a'),
                              manifest.get('namespace'))

    def available(self, event):
        if not isinstance(event, dict):
            return False
        try:
            if all(p.is_file() for p in self._live_paths(event)):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            path = self.root / 'notification_evidence' / 'events' / (_archive_identity(event) + '.json')
            if path.is_symlink():
                return False
            saved = json.loads(path.read_text(encoding='utf-8'))
            return saved.get('expires_at', 0) > self.now() and self._same_event(event, saved.get('event', {}))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def _same_event(event, saved):
        # A matching symbol or lifecycle alone is not an original chart. Its
        # saved point, prices and cutoff must also identify the same occurrence.
        return all(event.get(k) == saved.get(k) for k in ('id', 'stage', 'point_type', 'code', 'market',
            'frequency', 'recorded_at', 'source_closed_at', 'anchor_at', 'anchor_price', 'lifecycle_cycle',
            'point_id', 'hypothesis_id', 'signal_type'))

    def _archived_panel(self, event):
        from .tradingview_snapshot import prepare
        archive = NotificationEvidence(self.root / 'notification_evidence', now=self.now)
        saved = archive.read(_archive_identity(event))
        if not self._same_event(event, saved['event']):
            raise ValueError('archive belongs to another notification occurrence')
        with archive.materialized_runs(event) as (temporary,saved):
            return prepare(saved['event'], temporary,
                           **({'allow_summary_reference':True} if saved.get('partial_sources') else {})), saved['expires_at']

    def panel(self, event, *, version=None):
        actual = event_version(event)
        if version is not None and (not re.fullmatch(r'[a-f0-9]{64}', version) or version != actual):
            raise ChartChanged('通知记录已更新，请重新打开图表。')
        with self._lock:
            cached = self._cache.get(actual)
            if cached and cached[0] > self.now():
                self._cache.move_to_end(actual)
                return actual, cached[1]
        from .tradingview_snapshot import prepare
        try:
            payload, expires = self._archived_panel(event)
        except (OSError, ValueError, KeyError, TypeError):
            try:
                self._live_paths(event)  # Validate the root before loading.
                payload, expires = prepare(event, self.root / 'runs'), self.now() + 30
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                raise ChartUnavailable('该次通知的图表证据未保存、已过期或校验未通过；可查看已保存附图或打开当前行情。') from exc
        payload = deepcopy(payload)
        times = payload['history']['t']
        anchor = payload['settings']['anchor_at']
        index = times.index(anchor)
        # Start with readable context around the signal; all frozen candles
        # remain available for zooming/panning and the "all history" action.
        payload['settings'].update(interactive=True, history_from=times[0],
            **{'from':times[min(max(0, index - 80), max(0, len(times) - 180))]})
        with self._lock:
            self._cache[actual] = min(expires, self.now() + 30), payload
            self._cache.move_to_end(actual)
            while len(self._cache) > 6:
                self._cache.popitem(last=False)
        return actual, payload
