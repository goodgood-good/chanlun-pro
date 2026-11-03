"""Bounded native tail refresh before analysis; no long-history downloads."""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import multiprocessing
import os
import hashlib
import json
from itertools import zip_longest
from pathlib import Path
import time

from .closing_bar import SCHEMA, _contract, preparation_needed, prepared_content_key
from .rules import CN, validate_frame


def _prepared_path(cache_root, code, frequency):
    from .markets import storage_symbol
    return Path(cache_root)/'prepared_inputs'/f'{storage_symbol(code)}_{frequency}.json'


def saved_preparation(cache_root, code, frequency, cutoff):
    try:
        wrapper=json.loads(_prepared_path(cache_root,code,frequency).read_text(encoding='utf-8'))
        raw=wrapper['payload'].encode('utf-8')
        proof=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()==wrapper['sha256'] and proof['schema']==SCHEMA
                and proof['contract']==_contract() and proof['code']==code and proof['frequency']==frequency
                and proof['cutoff']==cutoff and cutoff<=proof['checked_at']<=time.time()
                and isinstance(proof['input_key'],str) and len(proof['input_key'])==64):
            return proof
    except (OSError,ValueError,TypeError,KeyError):
        pass
    return None


def save_preparation(cache_root, proof):
    from .runner import write_json
    raw=json.dumps(proof,sort_keys=True,separators=(',',':'))
    write_json(_prepared_path(cache_root,proof['code'],proof['frequency']),
               {'payload':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest()},durable=True)


def _prepare_worker(connection, batches):
    from .runner import _exchange

    exchange = _exchange()
    try:
        for batch_index, batch in enumerate(batches):
            codes, period, cutoff, targets = batch
            closed = datetime.fromtimestamp(cutoff, CN)

            def heartbeat(done, total, *, batch_id=batch_index):
                connection.send({"progress": [batch_id, done, total]})

            try:
                receipts=getattr(connection,'receipts',None)
                day=closed.date().isoformat()
                needed=[code for code in codes if receipts is None or receipts.get((code,period,day),0)<cutoff]
                if needed:
                    ready = exchange.download_completed_window(
                        needed,period,closed.strftime("%Y%m%d000000"),
                        closed.strftime("%Y%m%d%H%M%S"),progress=heartbeat)
                    if ready is not True:
                        raise ValueError("unacknowledged native window")
                    if receipts is not None:
                        for code in needed:receipts[code,period,day]=cutoff
                proofs = {}
                for offset, code in enumerate(codes):
                    for frequency, context in targets[code].items():
                        try:
                            observed = datetime.fromtimestamp(context["cutoff"], CN)
                            tail = exchange.klines(
                                code,
                                frequency,
                                start_date=observed.strftime("%Y%m%d"),
                                end_date=observed.strftime("%Y-%m-%d %H:%M:%S"),
                                args={
                                    "exact_end": True,
                                    "skip_download": True,
                                    "local_only": True,
                                },
                            )
                            if (
                                tail is not None
                                and not tail.empty
                                and set(tail.code) == {code}
                                and not [
                                    e
                                    for e in validate_frame(tail, context)
                                    if e != "INSUFFICIENT_HISTORY"
                                ]
                            ):
                                proofs.setdefault(code, {})[frequency] = (
                                    prepared_content_key(
                                        tail, code, frequency, context["cutoff"]
                                    )
                                )
                        except Exception:
                            pass
                    if receipts is not None and set(proofs.get(code,{}))!=set(targets[code]):
                        receipts.pop((code,period,day),None)
                    heartbeat(len(codes) + offset + 1, 2 * len(codes))
                connection.send(
                    {
                        "ready": list(proofs),
                        "failed": [code for code in codes if code not in proofs],
                        "base": period,
                        "through": cutoff,
                        "checked_at": time.time(),
                        "proofs": proofs,
                    }
                )
            except Exception as exc:
                connection.send(
                    {"failed": codes, "base": period, "error": type(exc).__name__}
                )
        connection.send({"done": True})
    finally:
        connection.close()


def prepare_inputs(
    stocks,
    settings,
    contexts,
    cache_root,
    run_dir,
    *,
    timeout=60,
    worker_target=_prepare_worker,
    on_ready=None,
    first_batch_size=100,
    stop_requested=None,
    worker=None,
):
    """Prepare only missing receipts; each worker rechecks actual local data.

    A crashed/failed preparation never counts as verified. Those symbols use
    the existing bounded per-symbol native refresh instead.
    The retained keyword ``first_batch_size`` now bounds every preparation
    cohort, so later work can refill calculation slots as soon as it is ready.
    """
    from .runner import _stop_worker, ScreeningCancelled
    if type(first_batch_size) is not int or not 1 <= first_batch_size <= 100:
        raise ValueError('preparation cohort must contain 1–100 symbols')
    def check_cancelled():
        if (stop_requested is not None and stop_requested()) or Path(run_dir, 'cancel').exists():
            raise ScreeningCancelled()

    requested = defaultdict(dict)
    target_contexts = defaultdict(dict)
    for stock in stocks:
        check_cancelled()
        if stock.get("market", "a") != "a" or stock.get('_screening_name_missing'):
            continue
        for period in stock.get("_monitor_periods", settings["frequencies"]):
            cutoff = contexts.get(period, {}).get("cutoff")
            if period in {"1m", "5m", "30m"} and type(cutoff) is int:
                requested[stock["code"]][period] = cutoff
                target_contexts[stock['code']][period] = contexts[period]
    # A live 5m job also consumes frozen 1m/30m inputs. Preparing only its main
    # period forced every calculation child to refresh peers serially. Peer
    # clocks belong to the main candle, never to a newer wall-clock minute.
    dependencies = False
    from .timeframe_policy import POLICY
    if settings.get('scope') == 'symbols' and settings.get('timeframe_policy') == POLICY:
        from .runner import _a_calendar_context
        # Every symbol sharing the same main candle has the same peer clocks.
        # Reuse only within this preparation call; each target owns its copy.
        calendar_contexts = {}
        for stock in stocks:
            if stock.get('market', 'a') != 'a' or stock.get('_monitor_periods', settings['frequencies']) != ['5m']:
                continue
            main = requested.get(stock['code'], {}).get('5m')
            if type(main) is not int:
                continue
            for period in ('1m', '30m'):
                if period in requested[stock['code']]:
                    continue
                recent_sessions = settings.get('recent_sessions', 5)
                max_anchor_sessions = settings.get('max_anchor_sessions', 20)
                context_key = (main, period, recent_sessions, max_anchor_sessions)
                if context_key not in calendar_contexts:
                    calendar_contexts[context_key] = _a_calendar_context(
                        datetime.fromtimestamp(main, CN), period,
                        recent_sessions, max_anchor_sessions)
                context = deepcopy(calendar_contexts[context_key])
                requested[stock['code']][period] = context['cutoff']
                target_contexts[stock['code']][period] = context
                dependencies = True
    acknowledged = {}
    grouped = defaultdict(list)
    for code, periods in requested.items():
        check_cancelled()
        for period, cutoff in periods.items():
            proof=saved_preparation(cache_root,code,period,cutoff)
            if proof:
                acknowledged.setdefault(code,{})[period]=proof
        needed = {
            period: cutoff
            for period, cutoff in periods.items()
            if period not in acknowledged.get(code,{}) and preparation_needed(cache_root, code, period, cutoff)
        }
        for base in ("1m", "5m"):
            cutoffs = [
                cutoff
                for period, cutoff in needed.items()
                if ("1m" if period == "1m" else "5m") == base
            ]
            if cutoffs:
                grouped[base, max(cutoffs)].append(code)
    def chunks(codes):
        # Keep every cohort small enough to refill the calculation workers.
        # A small first cohort followed by 100-symbol cohorts still leaves
        # idle slots waiting for both native bases of the entire next cohort.
        for offset in range(0, len(codes), first_batch_size):
            yield codes[offset:offset + first_batch_size]

    grouped_batches = [[
        (
            chunk,
            base,
            cutoff,
            {
                code: {
                    period: target_contexts[code][period]
                    for period in requested[code]
                    if ("1m" if period == "1m" else "5m") == base
                }
                for code in chunk
            },
        )
        for chunk in chunks(codes) if chunk
    ] for (base, cutoff), codes in sorted(grouped.items())]
    batches = ([batch for cohort in zip_longest(*grouped_batches) for batch in cohort if batch is not None]
               if dependencies else [batch for group in grouped_batches for batch in group])
    total = sum(len(batch[0]) for batch in batches)
    required_groups={(code,'1m' if period=='1m' else '5m') for code,periods in requested.items() for period in periods}
    pending_groups = {(code, base) for codes,base,_,_ in batches for code in codes}
    unreleased = list(stocks)

    def release_ready():
        nonlocal unreleased
        remaining, ready = [], []
        for stock in unreleased:
            groups = {(stock['code'], '1m' if period == '1m' else '5m')
                      for period in requested.get(stock['code'], {})
                      if stock.get('market', 'a') == 'a'}
            if groups & pending_groups:
                remaining.append(stock)
                continue
            if stock.get('market', 'a') == 'a' and stock['code'] in acknowledged:
                stock['_completed_inputs'] = dict(acknowledged[stock['code']])
            ready.append(stock)
        unreleased = remaining
        if ready and on_ready is not None:
            on_ready(ready)
            return True
        return False

    for stock in stocks:
        if stock.get('market','a')=='a' and stock['code'] in acknowledged:
            stock['_completed_inputs']=dict(acknowledged[stock['code']])
    progress = {
        "state": "completed" if not batches else "running",
        "completed": 0,
        "total": total,
        "failed": 0,
        "reused": len(required_groups)-total,
    }
    yield dict(progress)
    release_ready()
    if not batches:
        return
    ctx = multiprocessing.get_context("spawn")
    if worker is None:
        parent, child = ctx.Pipe(duplex=False)
        process = ctx.Process(target=worker_target, args=(child, batches))
    else:
        parent=worker.start(batches);process=worker.process;child=None
    job = None
    slot = {"process": process, "connection": parent, "stock": "tail-preparation"}
    try:
        if worker is None:
            process.start()
            child.close()
        else:
            progress['worker_pid']=process.pid
        if worker is None and os.name == "nt":
            from .worker_lifetime import WindowsWorkerJob

            job = WindowsWorkerJob()
            job.assign(process)
        last_progress = time.monotonic()
        progress_position = (-1, -1)
        finished = False
        while not finished:
            check_cancelled()
            if time.monotonic() - last_progress >= timeout:
                progress["state"] = "fallback"
                break
            if parent.poll(0.25):
                try:
                    item = parent.recv()
                except EOFError:
                    progress["state"] = "fallback"
                    break
                if item.get("progress"):
                    index, done, total = item["progress"]
                    if (
                        type(index) is int
                        and type(done) is int
                        and (index, done) > progress_position
                    ):
                        progress_position = (index, done)
                        last_progress = time.monotonic()
                    continue
                last_progress = time.monotonic()
                if item.get("done"):
                    finished = True
                    progress["state"] = "completed"
                for code in item.get("ready", []):
                    for period, cutoff in requested.get(code, {}).items():
                        base = "1m" if period == "1m" else "5m"
                        if base != item["base"] or cutoff > item["through"]:
                            continue
                        if (
                            datetime.fromtimestamp(cutoff, CN).date()
                            != datetime.fromtimestamp(item["through"], CN).date()
                        ):
                            continue
                        key = item.get("proofs", {}).get(code, {}).get(period)
                        if not isinstance(key, str):
                            continue
                        acknowledged.setdefault(code, {})[period] = {
                            "schema": SCHEMA,
                            "contract": _contract(),
                            "code": code,
                            "frequency": period,
                            "cutoff": cutoff,
                            "checked_at": item["checked_at"],
                            "input_key": key,
                        }
                        # Commit each acknowledged batch before announcing
                        # progress. A later coordinator exit must not redownload
                        # already verified source windows. Consumers still hash
                        # their actual candles before accepting this receipt.
                        try:
                            save_preparation(cache_root,acknowledged[code][period])
                        except OSError:
                            pass  # In-memory proof remains usable for this run.
                progress["completed"] += len(item.get("ready", [])) + len(
                    item.get("failed", [])
                )
                progress["failed"] += len(item.get("failed", []))
                for code in (*item.get('ready', []), *item.get('failed', [])):
                    pending_groups.discard((code, item.get('base')))
                yield dict(progress)
                if release_ready():
                    # A full calculation queue deliberately blocks this reader.
                    # That backpressure is not a stalled provider request.
                    last_progress = time.monotonic()
            elif not process.is_alive():
                progress["state"] = "fallback"
                break
            else:
                yield dict(progress)
        for stock in stocks:
            if stock.get("market", "a") == "a" and stock["code"] in acknowledged:
                stock["_completed_inputs"] = acknowledged[stock["code"]]
        # Incomplete preparation uses the existing per-symbol verification.
        # No unacknowledged data is promoted to verified by this fallback.
        pending_groups.clear()
        release_ready()
        yield dict(progress)
    finally:
        if worker is not None:
            worker.finish(successful=bool(locals().get('finished')))
        else:
            if job is not None:
                job.close()
            if process.pid is not None:
                _stop_worker(slot)
            else:
                parent.close()
                child.close()
