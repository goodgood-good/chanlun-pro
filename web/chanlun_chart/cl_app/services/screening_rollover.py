"""A display-only rolling list; never an input to screening or notifications."""
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import re
import threading
import zlib

SCHEMA = 'screening-workbench-display-v1'
FILENAME = 'workbench_display.json.gz'
RUN_ID = re.compile(r'[a-f0-9]{32}')


def _read(path):
    try:
        raw = path.read_bytes()
        return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
    except (OSError, ValueError, EOFError, zlib.error):
        return None


def _period(row):
    return row.get('market', 'a'), row['code'], row['frequency']


def _stamp(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _valid_snapshot(saved):
    return (isinstance(saved, dict) and saved.get('schema') == SCHEMA
        and isinstance(saved.get('run_id'), str) and RUN_ID.fullmatch(saved['run_id'])
        and isinstance(saved.get('through'), int) and isinstance(saved.get('rows'), list)
        and all(isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ('id','code','frequency'))
                and isinstance(row.get('point'), dict) for row in saved['rows']))


def _in_scope(row, settings, universe):
    if settings.get('frequencies') and row['frequency'] not in settings['frequencies']:
        return False
    if settings.get('point_types') and row['point'].get('point_type') not in settings['point_types']:
        return False
    symbol = row.get('market', 'a'), row['code']
    if universe is not None:
        return symbol in universe
    scope = settings.get('scope')
    if scope == 'codes':
        return symbol[0] == 'a' and symbol[1] in settings.get('codes', [])
    if scope == 'symbols':
        return symbol in {(r.get('market', 'a'), r['code']) for r in settings.get('symbols', [])}
    if scope == 'watchlist':
        # A group name is not proof that a previous result is still a member.
        # Wait for this run's committed universe before retaining its old rows.
        return False
    return scope != 'all_a' or symbol[0] == 'a'


class RollingDisplay:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.snapshot = None
        self.loaded = False
        self.baseline_checked = None
        self.pending_baseline = None
        self.dirty = False

    def waiting_for_baseline(self, source):
        with self.lock:
            return self.baseline_checked != source or self.pending_baseline is not None

    def _bootstrap(self, result, project):
        """Read saved, previously verified output as labelled history only."""
        candidates = []
        for directory in self.root.iterdir() if self.root.exists() else ():
            if not RUN_ID.fullmatch(directory.name) or not directory.is_dir() or directory.name == result.get('run_id'):
                continue
            state = _read(directory / 'status.json')
            if not isinstance(state, dict):
                continue
            if state.get('status') not in {'completed','cancelled','interrupted','failed'} or state.get('run_id') != directory.name:
                continue
            started = state.get('started_at', 0)
            if result.get('started_at') and started >= result['started_at']:
                continue
            candidates.append((started, directory))
        self.pending_baseline = None
        for _, directory in sorted(candidates, key=lambda item: item[0], reverse=True):
            display = _read(directory/FILENAME)
            state = _read(directory/'status.json') or {}
            if (_valid_snapshot(display) and display['run_id'] == directory.name
                    and 0 < display['through'] <= state.get('completed', 0)):
                return {**display, 'started_at': state.get('started_at', 0)}
            path = directory / 'verified_results.json.gz'
            stamp = _stamp(path)
            saved = _read(path)
            if not isinstance(saved, dict):
                if self.pending_baseline is None:
                    self.pending_baseline = path, stamp
                continue
            payload = saved.get('payload', {})
            if (saved.get('schema') != 'screening-verified-results-v1' or not isinstance(payload, dict)
                    or payload.get('run_id') != directory.name):
                if self.pending_baseline is None:
                    self.pending_baseline = path, stamp
                continue
            try:
                rows = project(payload, directory.name)
            except (KeyError, TypeError, ValueError):
                if self.pending_baseline is None:
                    self.pending_baseline = path, stamp
                continue
            return {'run_id': directory.name, 'rows': rows, 'through': payload.get('results_through', 0),
                    'started_at': (_read(directory / 'status.json') or {}).get('started_at', 0)}
        return None

    def merge(self, result, rows, *, project, live_filter):
        source = result.get('run_id', 'idle')
        if not RUN_ID.fullmatch(source):
            return rows, 0
        with self.lock:
            if not self.loaded:
                saved = _read(self.root / FILENAME)
                if _valid_snapshot(saved):
                    self.snapshot = saved
                self.loaded = True
            through = result.get('results_through', result.get('completed', 0)) or 0
            stale_version=(result.get('source_current') is False
                           and (result.get('status')!='completed'
                                or bool(result.get('source_revision') or result.get('frozen_native_review'))))
            finished = result.get('status') == 'completed' and not result.get('results_loading') and not stale_version
            failed = {tuple(p) for p in result.get('_workbench_failed', [])}
            saved = self.snapshot or {}
            caught_up = saved.get('run_id') != source or through >= saved.get('through', 0)
            ready_later = self.pending_baseline and _stamp(self.pending_baseline[0]) != self.pending_baseline[1]
            if (not finished or failed) and caught_up and (self.baseline_checked != source or ready_later):
                self.baseline_checked = source
                baseline = self._bootstrap(result, project)
                prior_source = saved.get('baseline_run_id') if saved.get('run_id') == source else saved.get('run_id')
                saved_state = _read(self.root / (prior_source or '') / 'status.json') or {}
                saved_started = (saved.get('baseline_started_at', 0) if saved.get('run_id') == source
                                 else saved.get('started_at', 0)) or saved_state.get('started_at', 0)
                if baseline and (self.snapshot is None or baseline['started_at'] > saved_started):
                    self.snapshot = baseline
            saved = self.snapshot or {}
            restoring = (result.get('results_loading') and (not result.get('results_available') or through < saved.get('through', 0))
                         or stale_version)
            prior = saved.get('rows', []) if saved.get('run_id') != source or restoring else [
                row for row in saved.get('rows', []) if row.get('refresh_state') or _period(row) in failed]
            universe = _read(self.root / source / 'universe.json')
            universe = {(r.get('market', 'a'), r['code']) for r in universe} if isinstance(universe, list) else None
            replaced = {tuple(p) for p in result.get('_workbench_verified', [])} | {_period(row) for row in rows}
            if stale_version:
                # Historical review may recover only formal structures. It is
                # not a new-version negative proof for the old previews, even
                # when a recovered formal point shares their symbol/period.
                replaced=set()
                rows=[{**row,'refresh_state':'limited','result_source':row.get('result_source') or source,
                       'detail_loaded':True,'evidence_chart_available':False} for row in rows]
            current_ids={row['id'] for row in rows}
            processed = {tuple(p) for p in result.get('_workbench_processed', [])}
            processed_periods = {tuple(p) for p in result.get('_workbench_processed_periods', [])}
            def attempted(row):
                return (_period(row) in processed_periods if result.get('progress_unit') == 'symbol_period'
                        else _period(row)[:2] in processed)
            groups = defaultdict(list)
            if not finished or failed:
                for row in prior:
                    if finished and _period(row) not in failed:
                        continue
                    if row['id'] in current_ids or _period(row) in replaced or not _in_scope(row, result.get('settings', {}), universe):
                        continue
                    old_source = row.get('result_source', saved.get('run_id'))
                    groups[old_source].append({**row, 'result_source': old_source,
                        'refresh_state': 'limited' if attempted(row) or _period(row) in failed else 'pending',
                        'detail_loaded': True, 'evidence_chart_available': False})
            previous = []
            for old_source, old_rows in groups.items():
                previous.extend(live_filter(old_rows, old_source, result.get('current_source_revision'))[0])
            merged = [*rows, *previous]
            merged.sort(key=lambda row: (bool(row.get('refresh_state')),
                -(row.get('selection_available_at') or row['point'].get('available_at') or 0), row['code'], row['frequency']))
            snapshot = {'schema': SCHEMA, 'run_id': source, 'through': through, 'rows': merged,
                        'baseline_run_id': saved.get('baseline_run_id') if saved.get('run_id') == source else saved.get('run_id'),
                        'baseline_started_at': saved.get('baseline_started_at', 0) if saved.get('run_id') == source else saved.get('started_at', 0),
                        'started_at': result.get('started_at', saved.get('started_at', 0) if saved.get('run_id') == source else 0)}
            # Slow requests may finish after a newer batch/prefix was published.
            latest = _read(self.root / 'latest.json') or {}
            if (latest.get('run_id') == source and not (saved.get('run_id') == source and saved.get('through', 0) > through)
                    and (snapshot != self.snapshot or self.dirty)):
                self.snapshot = snapshot
                self.dirty = True
                path = self.root / FILENAME
                temporary = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
                try:
                    packed = gzip.compress(json.dumps(snapshot, ensure_ascii=False).encode('utf-8'), compresslevel=1)
                    # Keep the verified display with its run as well as the
                    # latest pointer, including interrupted partial batches.
                    run_folder = self.root/source
                    if run_folder.is_dir() and run_folder.resolve().parent == self.root.resolve() and not run_folder.is_symlink():
                        run_file = run_folder/FILENAME
                        run_tmp = run_file.with_name(temporary.name)
                        try:
                            run_tmp.write_bytes(packed)
                            os.replace(run_tmp, run_file)
                        except OSError:
                            pass  # The global display cache can still succeed.
                        finally:
                            try: run_tmp.unlink(missing_ok=True)
                            except OSError: pass
                    temporary.write_bytes(packed)
                    os.replace(temporary, path)
                    self.dirty = False
                except OSError:
                    pass  # A display cache failure cannot discard current results.
                finally:
                    try: temporary.unlink(missing_ok=True)
                    except OSError: pass
            return deepcopy(merged), len(previous)


@lru_cache(maxsize=8)
def display_for(root):
    return RollingDisplay(root)
