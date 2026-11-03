"""Idle, revocable preparation of source receipts; never a calculation queue."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import time

CONTROL_FILE = 'source_prefetch_control.json'
STATUS_FILE = 'source_prefetch_status.json'
SCHEMA = 'monitor-source-prefetch-v1'
HINT_SCHEMA = 'monitor-source-prefetch-hint-v2'
ADMISSION_SCHEMA = 'monitor-normal-live-admission-v1'
COHORT_SIZE = 4
MAX_SECONDS = 8


def control_value(state, version, generation):
    seed = state.get('seed') or {}
    scope = {name: state.get(name) for name in ('holding_symbols', 'monitor_frequencies', 'quote_only_symbols')}
    scope.update(seed=seed.get('run_id'), codes=seed.get('codes'), settings=seed.get('settings'),
                 suspended=sorted((state.get('suspended_periods') or {}).keys()))
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {'schema': SCHEMA, 'enabled': state.get('enabled') is True,
            'generation': generation, 'version': version, 'seed': seed.get('run_id'), 'scope': digest}


def publish_control(root, value, previous):
    if value != previous:
        from .runner import write_json
        write_json(root / CONTROL_FILE, value, durable=True)
    return value


def make_hint(launch, state, control, select, *, batch_limit=None):
    """Predict only sources from the already built plan, without admitting work."""
    try:
        return _make_hint(launch, state, control, select, batch_limit)
    except (KeyError, TypeError, ValueError):
        return None  # Optional source hints never widen normal dispatch failures.


def _make_hint(launch, state, control, select, batch_limit):
    from .timeframe_policy import POLICY
    if (launch.get('mode') != 'live' or not callable(batch_limit)
            or not control.get('enabled') or not control.get('generation')
            or not launch.get('fair_plan') or launch['settings'].get('timeframe_policy') != POLICY):
        return None
    plan = deepcopy(launch['fair_plan'])
    current = {f"{s['market']}:{s['code']}" for s in launch['settings']['symbols']}
    # This is the same actual admission calculation used by _fair_batch,
    # after memory trimming. It is a scheduling intent, not a size heuristic.
    limit = batch_limit(plan, launch['settings']['workers'])
    if type(limit) is not int or not 1 <= limit <= 64 or len(current) != limit:
        return None
    priority = set(plan.get('urgent', ())) | set(state.get('holding_symbols', ()))
    due = launch['options'].get('due_periods', {})
    extra = launch['options'].get('monitor_periods', {})
    final_symbols = [{**symbol, '_monitor_periods': due.get(f"{symbol['market']}:{symbol['code']}",
        sorted(set(launch['settings']['frequencies']) | set(extra.get(f"{symbol['market']}:{symbol['code']}", ())))) }
        for symbol in launch['settings']['symbols']]
    ordinary = sorted(f"a:{symbol['code']}" for symbol in final_symbols
        if symbol.get('market') == 'a' and f"a:{symbol['code']}" not in priority
        and f"a:{symbol['code']}" in plan['recent'] and set(symbol['_monitor_periods']) == {'5m'})
    if not ordinary:
        return None  # A holding/notification-only batch is never an idle prefetch owner.
    admission = {'schema': ADMISSION_SCHEMA, 'kind': 'normal_live', 'limit': limit,
                 'count': len(current), 'ordinary_5m_keys': ordinary,
                 'priority_keys': sorted(current & priority),
                 'members_sha256': members_digest({**launch['settings'], 'symbols': final_symbols})}
    excluded = current | priority
    excluded |= set((state.get('suspended_periods') or {}).keys())
    allowed = {f"{s['market']}:{s['code']}" for s in plan['symbols']
               if s.get('market') == 'a' and f"a:{s['code']}" not in excluded
               and f"a:{s['code']}" in plan['recent'] and '5m' in plan['periods'].get(f"a:{s['code']}", ())}
    if not allowed:
        return None
    plan['symbols'] = [s for s in plan['symbols'] if f"{s['market']}:{s['code']}" in allowed]
    plan['periods'] = {key: ['5m'] for key in allowed}
    plan['cursor'] = launch['fair_cursor']
    plan['state']['monitor_critical_cursor'] = launch['critical_cursor']
    tasks, _, _ = select(plan, launch['settings']['workers'])
    tasks = [task for task in tasks if task['frequency'] == '5m'][:64]
    cutoffs = {plan['targets'].get(f"{task['key']}:5m") for task in tasks}
    if not tasks or len(cutoffs) != 1:
        return None
    cutoff = cutoffs.pop()
    if type(cutoff) is not int or not cutoff <= plan['now'] < cutoff + 300:
        return None
    hint = {'schema': HINT_SCHEMA, 'admission': admission, 'control': deepcopy(control), 'cutoff': cutoff, 'expires_at': cutoff + 300,
            'settings': {name: launch['settings'][name] for name in (
                'recent_sessions', 'max_anchor_sessions', 'timeframe_policy')},
            'symbols': [{name: task['symbol'][name] for name in ('market', 'code', 'name') if name in task['symbol']}
                        for task in tasks]}
    return validate_hint(hint)


def validate_hint(hint):
    from .timeframe_policy import POLICY
    if not isinstance(hint, dict) or set(hint) != {'schema', 'admission', 'control', 'cutoff', 'expires_at', 'settings', 'symbols'}:
        raise ValueError('Invalid source prefetch hint')
    control, settings, symbols = hint['control'], hint['settings'], hint['symbols']
    if (hint['schema'] != HINT_SCHEMA or type(hint['cutoff']) is not int or hint['cutoff'] <= 0
            or type(hint['expires_at']) is not int or hint['expires_at'] != hint['cutoff'] + 300
            or not isinstance(control, dict)
            or set(control) != {'schema', 'enabled', 'generation', 'version', 'seed', 'scope'}
            or control['schema'] != SCHEMA or control['enabled'] is not True
            or not isinstance(control['generation'], str) or not control['generation']
            or type(control['version']) is not int or control['version'] < 0
            or not isinstance(control['seed'], str) or not control['seed']
            or not isinstance(control['scope'], str) or not re.fullmatch(r'[a-f0-9]{64}', control['scope'])
            or not isinstance(settings, dict) or set(settings) != {'recent_sessions', 'max_anchor_sessions', 'timeframe_policy'}
            or settings['timeframe_policy'] != POLICY
            or type(settings['recent_sessions']) is not int or not 1 <= settings['recent_sessions'] <= 20
            or type(settings['max_anchor_sessions']) is not int or not 1 <= settings['max_anchor_sessions'] <= 60
            or not isinstance(symbols, list) or not 1 <= len(symbols) <= 64):
        raise ValueError('Invalid source prefetch scope')
    for symbol in symbols:
        if (not isinstance(symbol, dict) or set(symbol) - {'market', 'code', 'name'}
                or symbol.get('market') != 'a' or not isinstance(symbol.get('code'), str)
                or not re.fullmatch(r'(SH|SZ|BJ)\.\d{6}', symbol['code'])
                or not isinstance(symbol.get('name', ''), str) or len(symbol.get('name', '')) > 200):
            raise ValueError('Invalid source prefetch symbol')
    if len({s['code'] for s in symbols}) != len(symbols):
        raise ValueError('Duplicate source prefetch symbol')
    validate_admission(hint['admission'])
    return deepcopy(hint)


def members_digest(settings):
    """Bind all admitted market/code/period tuples, including non-A members."""
    members, keys = [], set()
    symbols = settings.get('symbols')
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 64:
        raise ValueError('Invalid admitted member list')
    for symbol in symbols:
        if not isinstance(symbol, dict):
            raise ValueError('Invalid admitted member')
        market, code = symbol.get('market'), symbol.get('code')
        periods = symbol.get('_monitor_periods', settings.get('frequencies'))
        if (not isinstance(market, str) or not 1 <= len(market) <= 32
                or not isinstance(code, str) or not 1 <= len(code) <= 64
                or not isinstance(periods, list) or not periods
                or any(not isinstance(period, str) or not period for period in periods)
                or (market, code) in keys):
            raise ValueError('Invalid admitted market/code/period tuple')
        keys.add((market, code))
        members.append({'market': market, 'code': code, 'periods': sorted(set(periods))})
    # Preserve queue member order; only period ordering is semantically normalized.
    return hashlib.sha256(json.dumps(members, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def validate_admission(value):
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'kind', 'limit', 'count', 'ordinary_5m_keys', 'priority_keys', 'members_sha256'}
            or value['schema'] != ADMISSION_SCHEMA or value['kind'] != 'normal_live'
            or type(value['limit']) is not int or not 1 <= value['limit'] <= 64
            or type(value['count']) is not int or value['count'] != value['limit']
            or not isinstance(value['members_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['members_sha256'])):
        raise ValueError('Invalid normal live admission')
    ordinary, priority = value['ordinary_5m_keys'], value['priority_keys']
    if (not isinstance(ordinary, list) or not 1 <= len(ordinary) <= value['count']
            or any(not isinstance(key, str) or not re.fullmatch(r'a:(SH|SZ|BJ)\.\d{6}', key) for key in ordinary)
            or len(set(ordinary)) != len(ordinary)
            or not isinstance(priority, list) or len(priority) > value['count']
            or any(not isinstance(key, str) or not 1 <= len(key) <= 100 or ':' not in key for key in priority)
            or len(set(priority)) != len(priority) or set(ordinary) & set(priority)):
        raise ValueError('Invalid normal live admission members')


def admission_matches_request(hint, request):
    """Bind the private scheduling intent to the final, period-trimmed request."""
    from .timeframe_policy import POLICY
    admission = hint['admission']
    settings = request.get('settings') or {}
    symbols = settings.get('symbols', [])
    if (settings.get('scope') != 'symbols' or settings.get('timeframe_policy') != POLICY
            or not isinstance(symbols, list) or len(symbols) != admission['count']):
        return False
    try:
        if members_digest(settings) != admission['members_sha256']:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    indexed = {f"{symbol.get('market')}:{symbol.get('code')}": symbol for symbol in symbols}
    keys = set(indexed)
    held = set((request.get('holding_snapshot') or {}).get('keys', []))
    if (len(keys) != len(symbols) or not set(admission['priority_keys']) <= keys
            or not set(admission['ordinary_5m_keys']) <= keys
            or set(admission['ordinary_5m_keys']) & held):
        return False
    return all(set(indexed[key].get('_monitor_periods', settings.get('frequencies', []))) == {'5m'}
               for key in admission['ordinary_5m_keys'])


def run_idle_prefetch(root, directory, request, pool, revision, generation, *,
                      revision_reader, now=time.time, monotonic=time.monotonic,
                      prepare=None, context_reader=None, identity_reader=None):
    """Use the owned idle preparation child; a formal run always revokes this work."""
    from .runner import ScreeningCancelled, _a_calendar_context, write_json
    from .rules import CN
    from .tail_preparation import prepare_inputs, saved_preparation
    from .worker_host import read_optional, STOP_FILE
    from .worker_lifetime import process_identity
    if 'source_prefetch' not in request:
        return
    prepare = prepare or prepare_inputs
    context_reader = context_reader or _a_calendar_context
    identity_reader = identity_reader or process_identity
    metrics = {'schema': SCHEMA, 'owner_run': directory.name, 'cutoff': None, 'candidate_count': 0,
               'prepared_symbols': 0, 'receipt_count': 0, 'reused_groups_on_entry': 0,
               'started_at': now(), 'stop_reason': 'invalid_hint', 'worker_released': True}
    began = monotonic()
    worker = getattr(pool, 'preparation_worker', None)
    try:
        hint = validate_hint(request['source_prefetch'])
        state = read_optional(directory / 'status.json') or {}
        if (not isinstance(state, dict) or pool.busy or pool.closed or worker is None or worker.closed or worker.identity is not None
                or worker.process is None or not worker.process.is_alive()
                or state.get('status') != 'completed'
                or type(state.get('total')) is not int or type(state.get('completed')) is not int
                or state['total'] != hint['admission']['count'] or state['completed'] != state['total']
                or not admission_matches_request(hint, request)
                or hint['control']['generation'] != generation
                or request.get('worker_runtime', {}).get('source_revision') != revision):
            metrics['stop_reason'] = 'owner_not_idle_completed_batch'
            return
        metrics.update(cutoff=hint['cutoff'], candidate_count=len(hint['symbols']))

        def stop_reason():
            try:
                if (root / STOP_FILE).exists():
                    return 'stop_requested'
                if read_optional(root / 'latest.json') != {'run_id': directory.name}:
                    return 'new_run'
                if read_optional(root / CONTROL_FILE) != hint['control']:
                    return 'control_changed'
                if revision_reader() != revision:
                    return 'source_changed'
                if not hint['cutoff'] <= now() < hint['expires_at']:
                    return 'cutoff_expired'
                if monotonic() - began >= MAX_SECONDS:
                    return 'idle_budget'
            except (OSError, ValueError, TypeError, KeyError):
                return 'control_unavailable'
            return None

        reason = stop_reason()
        if reason:
            metrics['stop_reason'] = reason
            return
        settings = {**hint['settings'], 'scope': 'symbols', 'frequencies': ['5m']}
        context = context_reader(datetime.fromtimestamp(hint['cutoff'], CN), '5m',
                                 settings['recent_sessions'], settings['max_anchor_sessions'])
        if not isinstance(context, dict) or context.get('calendar_error') or context.get('cutoff') != hint['cutoff']:
            metrics['stop_reason'] = 'unknown_context'
            return
        peer = context_reader(datetime.fromtimestamp(hint['cutoff'], CN), '30m',
                              settings['recent_sessions'], settings['max_anchor_sessions'])
        if not isinstance(peer, dict) or peer.get('calendar_error') or type(peer.get('cutoff')) is not int or peer['cutoff'] > hint['cutoff']:
            metrics['stop_reason'] = 'unknown_context'
            return
        expected = {'5m': hint['cutoff'], '1m': hint['cutoff'], '30m': peer['cutoff']}
        def cancelled():
            reason = stop_reason()
            if reason:
                metrics['stop_reason'] = reason
            return bool(reason)

        receipts = set()
        for offset in range(0, len(hint['symbols']), COHORT_SIZE):
            if cancelled():
                break
            stocks = [{**s, '_monitor_periods': ['5m']} for s in hint['symbols'][offset:offset + COHORT_SIZE]]
            original_process = worker.process
            original_pid = original_process.pid
            original_starts = worker.started_processes
            owner = identity_reader(original_pid)
            if owner.get('alive') is not True or not owner.get('birth'):
                metrics['stop_reason'] = 'worker_identity_unavailable'
                break
            stream = prepare(stocks, settings, {'5m': deepcopy(context)}, root / 'analysis_cache', directory,
                             worker=worker, first_batch_size=COHORT_SIZE, stop_requested=cancelled)
            initial = True
            try:
                for progress in stream:
                    if initial:
                        metrics['reused_groups_on_entry'] += progress.get('reused', 0)
                        initial = False
            except ScreeningCancelled:
                if metrics['stop_reason'] == 'invalid_hint':
                    metrics['stop_reason'] = 'preparation_cancelled'
                break
            finally:
                cleanup_error = None
                try:
                    stream.close()
                except BaseException as exc:
                    cleanup_error = exc
                # Process.close() makes its pid property unreadable. Keep the
                # original numeric PID/birth and verify even when close failed.
                try:
                    current = identity_reader(original_pid)
                    retired = (current.get('alive') is False or bool(current.get('birth') and current['birth'] != owner['birth']))
                    released = (worker.process is None and retired or worker.process is not None
                                and worker.process.pid == original_pid and worker.identity is None)
                    released = released and worker.started_processes == original_starts
                except (OSError, ValueError, TypeError, KeyError):
                    released = False
                metrics['worker_released'] = released
                if not released or cleanup_error is not None:
                    raise RuntimeError('Source prefetch worker did not release its request') from cleanup_error
                for stock in stocks:
                    proofs = {p: saved_preparation(root / 'analysis_cache', stock['code'], p, cutoff)
                              for p, cutoff in expected.items()}
                    receipts.update((stock['code'], p, proof['cutoff']) for p, proof in proofs.items() if proof)
                    if all(proofs.values()):
                        metrics['prepared_symbols'] += 1
                metrics['receipt_count'] = len(receipts)
        else:
            metrics['stop_reason'] = 'completed'
    except (OSError, ValueError, TypeError, KeyError):
        metrics['stop_reason'] = 'prefetch_unavailable'
    finally:
        metrics.update(finished_at=now(), elapsed_seconds=round(monotonic() - began, 3))
        try:
            write_json(root / STATUS_FILE, metrics)
        except OSError:
            pass  # Diagnostic output cannot become a delivery/dispatch dependency.
