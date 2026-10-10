"""Additive, audited screening handoff; the completed pool remains authoritative."""
from concurrent.futures import ThreadPoolExecutor
import threading


class CommittedReader:
    """One bounded proof read in flight; polling never waits for file/replay I/O."""
    def __init__(self, wake):
        self._wake = wake
        self._lock = threading.Lock()
        self._executor = None
        self._future = None
        self._key = None
        self._closed = False

    def read(self, key, callback):
        with self._lock:
            if self._closed:
                return None
            if self._future is not None:
                if not self._future.done():
                    return None
                # The final key field is the observed committed count. A
                # growing run may still consume an older, nonempty prefix;
                # only its run/revision/cursor/control generation must match.
                if self._key is not None and self._key[:-1] == key[:-1]:
                    try:
                        return self._future.result()
                    finally:
                        # Empty reads and exceptions are retryable even when
                        # the durable cursor has not moved yet.
                        self._key = None
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='monitor-screening-proof')
            self._key = key
            self._future = self._executor.submit(callback)
            self._future.add_done_callback(lambda _: self._wake())
            return None

    def invalidate(self):
        with self._lock:
            self._key = None

    def close(self):
        with self._lock:
            self._closed = True
            self._key = None
            if self._future is not None:
                self._future.cancel()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None

    def resume(self):
        with self._lock:
            self._closed = False


def audited_members(result):
    """A committed symbol alone is insufficient: require its frozen point proof."""
    processed = {(s.get('market', 'a'), s['code']) for s in result.get('processed_symbols', [])}
    failed = {(e.get('market', 'a'), e['code'], e.get('frequency')) for e in result.get('errors', [])}
    verified = {(p.get('market', 'a'), p['code'], p.get('frequency'))
                for p in result.get('verified_periods', [])
                if type(p.get('source_closed_at')) is int}
    members, periods = {}, {}
    for row in result.get('selected', []):
        identity = (row.get('market', 'a'), row['code'])
        period = (*identity, row.get('frequency'))
        point = row.get('point') or {}
        if (identity not in processed or period not in verified
                or period in failed or (*identity, None) in failed
                or row.get('evidence_verified') is not True or not row.get('evidence')
                or row.get('reasons') != [] or not row.get('audit')
                or any(value is not True for value in row['audit'].values())
                or point.get('status') != 'confirmed'):
            continue
        members[':'.join(identity)] = {'market': identity[0], 'code': identity[1],
                                      'name': row.get('name', identity[1])}
        periods.setdefault(':'.join(identity), set()).add(period[2])
    return members, {key: sorted(values) for key, values in periods.items()}
