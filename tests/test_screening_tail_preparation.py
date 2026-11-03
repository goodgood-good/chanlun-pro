from datetime import datetime
import time

import pytest

from chanlun.screening import tail_preparation as prep, closing_bar, runner
from chanlun.screening.rules import CN
from tests.test_screening_closing_bar import inputs


def completed_worker(connection, batches):
    for codes, base, through, targets in batches:
        proofs = {}
        for code in codes:
            for period, context in targets[code].items():
                frame, _, _ = inputs(period)
                frame["code"] = code
                proofs.setdefault(code, {})[period] = closing_bar.prepared_content_key(
                    frame, code, period, context["cutoff"]
                )
        connection.send(
            {
                "ready": codes,
                "base": base,
                "through": through,
                "checked_at": time.time(),
                "proofs": proofs,
            }
        )
    connection.send({"done": True})
    connection.close()


def failed_worker(connection, batches):
    connection.send(
        {"failed": batches[0][0], "base": batches[0][1], "error": "source failed"}
    )
    connection.send({"done": True})
    connection.close()


def hanging_worker(connection, batches):
    connection.send({"heartbeat": True})
    time.sleep(60)


def repeated_progress_worker(connection, batches):
    for _ in range(600):
        connection.send({"progress": [0, 1, 2]})
        time.sleep(0.1)


def gated_cohort_worker(connection, batches):
    """The third cohort cannot be prepared before cohort two is consumed."""
    from pathlib import Path
    gate = next(
        context['test_gate']
        for _, _, _, targets in batches
        for periods in targets.values()
        for context in periods.values()
        if 'test_gate' in context
    )
    for index, (codes, base, through, targets) in enumerate(batches):
        if index == 4:
            deadline = time.monotonic() + 5
            while not Path(gate).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('second cohort was not released independently')
                time.sleep(.01)
        proofs = {}
        for code in codes:
            for period, context in targets[code].items():
                frame, _, _ = inputs(period)
                frame['code'] = code
                proofs.setdefault(code, {})[period] = closing_bar.prepared_content_key(
                    frame, code, period, context['cutoff']
                )
        connection.send({
            'ready': codes, 'base': base, 'through': through,
            'checked_at': time.time(), 'proofs': proofs,
        })
    connection.send({'done': True})
    connection.close()


def universe():
    cutoff = int(datetime(2026, 10, 13, 15, tzinfo=CN).timestamp())
    stocks = [
        {
            "market": "a",
            "code": "SH.688139",
            "name": "fixture",
            "_monitor_periods": ["5m"],
        },
        {
            "market": "a",
            "code": "SH.688139",
            "name": "fixture",
            "_monitor_periods": ["30m"],
        },
        {"market": "us", "code": "AAPL.US", "name": "external"},
    ]
    settings = {"frequencies": ["5m", "30m"]}
    contexts = {p: {"cutoff": cutoff} for p in settings["frequencies"]}
    return stocks, settings, contexts


def test_live_main_prepares_peer_inputs_at_main_clock_without_future_minutes(tmp_path):
    from chanlun.screening.timeframe_policy import POLICY
    stocks, settings, contexts = universe()
    stocks = stocks[:1]
    settings.update(scope='symbols',timeframe_policy=POLICY,frequencies=['5m'])
    main = contexts['5m']['cutoff']
    contexts['1m'] = {'cutoff':main+60}
    ready = []
    messages = list(prep.prepare_inputs(stocks, settings, contexts, tmp_path/'cache', tmp_path,
        worker_target=completed_worker, on_ready=lambda batch:ready.extend(batch)))
    assert messages[-1]['failed'] == 0
    proof = ready[0]['_completed_inputs']
    assert set(proof) == {'1m','5m','30m'}
    assert proof['1m']['cutoff'] == proof['5m']['cutoff'] == main
    assert proof['30m']['cutoff'] <= main


def test_dependency_native_batches_release_early_cohorts_before_whole_pool(tmp_path):
    from chanlun.screening.timeframe_policy import POLICY
    stocks, settings, contexts = universe()
    stocks = [{**stocks[0], 'code':f'SH.{600000+i}'} for i in range(18)]
    settings.update(scope='symbols',timeframe_policy=POLICY,frequencies=['5m'])
    cohorts = []
    list(prep.prepare_inputs(stocks, settings, contexts, tmp_path/'cache', tmp_path,
        worker_target=completed_worker, first_batch_size=2,
        on_ready=lambda batch:cohorts.append([dict(stock) for stock in batch])))
    assert len(cohorts[0]) == 2
    assert all(len(cohort) <= 2 for cohort in cohorts)
    assert sum(map(len,cohorts)) == 18
    assert all(set(stock['_completed_inputs']) == {'1m','5m','30m'}
               for cohort in cohorts for stock in cohort)


def test_later_dependency_cohort_releases_before_remaining_native_work(tmp_path):
    from chanlun.screening.timeframe_policy import POLICY
    stocks, settings, contexts = universe()
    stocks = [{**stocks[0], 'code': f'SH.{600000+i}'} for i in range(10)]
    settings.update(scope='symbols', timeframe_policy=POLICY, frequencies=['5m'])
    gate = tmp_path / 'second-cohort-consumed'
    contexts['5m']['test_gate'] = str(gate)
    cohorts = []

    def accept(items):
        assert len(items) <= 2, 'a later cohort reintroduced a whole-remainder barrier'
        assert all(set(item['_completed_inputs']) == {'1m', '5m', '30m'} for item in items)
        cohorts.append(items)
        if len(cohorts) == 2:
            gate.touch()

    messages = list(prep.prepare_inputs(
        stocks, settings, contexts, tmp_path / 'cache', tmp_path,
        worker_target=gated_cohort_worker, first_batch_size=2, on_ready=accept,
    ))
    assert messages[-1]['state'] == 'completed'
    assert messages[-1]['failed'] == 0
    assert gate.exists()
    assert [item['code'] for cohort in cohorts for item in cohort] == [
        stock['code'] for stock in stocks
    ]


def test_changing_cohort_size_keeps_every_input_proof_identical(tmp_path):
    from copy import deepcopy
    from chanlun.screening.timeframe_policy import POLICY
    stocks, settings, contexts = universe()
    stocks = [{**stocks[0], 'code': f'SH.{600000+i}'} for i in range(12)]
    settings.update(scope='symbols', timeframe_policy=POLICY, frequencies=['5m'])
    results = []
    for size in (2, 100):
        items = deepcopy(stocks)
        run = tmp_path / str(size)
        run.mkdir()
        list(prep.prepare_inputs(
            items, settings, contexts, run / 'cache', run,
            worker_target=completed_worker, first_batch_size=size,
        ))
        results.append({
            stock['code']: {
                period: {key: value for key, value in proof.items() if key != 'checked_at'}
                for period, proof in stock['_completed_inputs'].items()
            }
            for stock in items
        })
    assert results[0] == results[1]


def test_native_base_is_prepared_once_for_two_requested_periods(tmp_path):
    stocks, settings, contexts = universe()
    messages = list(
        prep.prepare_inputs(
            stocks,
            settings,
            contexts,
            tmp_path / "cache",
            tmp_path,
            worker_target=completed_worker,
        )
    )
    assert messages[-1] == {
        "state": "completed",
        "completed": 1,
        "total": 1,
        "failed": 0,
        "reused": 0,
    }
    assert set(stocks[0]["_completed_inputs"]) == {"5m", "30m"}
    assert stocks[0]["_completed_inputs"] == stocks[1]["_completed_inputs"]
    assert "_completed_inputs" not in stocks[2]


def test_a_new_coordinator_reuses_durable_preparation_before_any_analysis(tmp_path):
    stocks,settings,contexts=universe()
    list(prep.prepare_inputs(stocks,settings,contexts,tmp_path/'cache',tmp_path,worker_target=completed_worker))
    fresh,_,_=universe()
    messages=list(prep.prepare_inputs(fresh,settings,contexts,tmp_path/'cache',tmp_path,worker_target=failed_worker))
    assert messages==[{'state':'completed','completed':0,'total':0,'failed':0,'reused':1}]
    assert fresh[0]['_completed_inputs']==stocks[0]['_completed_inputs']
    changed,_,_=universe()
    proof=prep._prepared_path(tmp_path/'cache','SH.688139','5m')
    proof.write_text('{corrupt',encoding='utf-8')
    messages=list(prep.prepare_inputs(changed,settings,contexts,tmp_path/'cache',tmp_path,worker_target=failed_worker))
    assert messages[-1]['total']==1 and messages[-1]['failed']==1


def test_failed_batch_never_creates_a_completion_proof(tmp_path):
    stocks, settings, contexts = universe()
    messages = list(
        prep.prepare_inputs(
            stocks,
            settings,
            contexts,
            tmp_path / "cache",
            tmp_path,
            worker_target=failed_worker,
        )
    )
    assert messages[-1]["failed"] == 1
    assert all("_completed_inputs" not in stock for stock in stocks)


def test_queue_backpressure_does_not_expire_the_provider_watchdog(tmp_path, monkeypatch):
    stocks, settings, contexts = universe()
    stocks = [stocks[0], {**stocks[0], 'code': 'SH.688140'}]
    settings = {**settings, 'frequencies': ['5m']}
    elapsed = [0.]
    monotonic = time.monotonic
    monkeypatch.setattr(prep.time, 'monotonic', lambda: monotonic() + elapsed[0])
    released = []
    def on_ready(items):
        released.extend(item['code'] for item in items)
        elapsed[0] += 61  # Time waiting for calculation queue capacity, without a real sleep.
    messages = list(prep.prepare_inputs(stocks, settings, contexts, tmp_path / 'cache', tmp_path,
        worker_target=completed_worker, first_batch_size=1, timeout=60, on_ready=on_ready))
    assert messages[-1]['state'] == 'completed'
    assert messages[-1]['completed'] == 2
    assert released == [item['code'] for item in stocks]
    assert all(stock.get('_completed_inputs', {}).get('5m') for stock in stocks)


def test_silent_preparation_times_out_and_leaves_per_symbol_fallback(tmp_path):
    stocks, settings, contexts = universe()
    started = time.monotonic()
    messages = list(
        prep.prepare_inputs(
            stocks,
            settings,
            contexts,
            tmp_path / "cache",
            tmp_path,
            worker_target=hanging_worker,
            timeout=2,
        )
    )
    assert time.monotonic() - started < 8
    assert messages[-1]["state"] == "fallback"
    assert all("_completed_inputs" not in stock for stock in stocks)


def test_repeated_unchanged_progress_is_not_an_infinite_download_lease(tmp_path):
    stocks, settings, contexts = universe()
    started = time.monotonic()
    messages = list(
        prep.prepare_inputs(
            stocks,
            settings,
            contexts,
            tmp_path / "cache",
            tmp_path,
            worker_target=repeated_progress_worker,
            timeout=2,
        )
    )
    assert time.monotonic() - started < 8
    assert messages[-1]["state"] == "fallback"


def test_cancel_stops_preparation_without_creating_scope_proofs(tmp_path):
    stocks, settings, contexts = universe()
    stream = prep.prepare_inputs(
        stocks,
        settings,
        contexts,
        tmp_path / "cache",
        tmp_path,
        worker_target=hanging_worker,
    )
    next(stream)
    (tmp_path / "cancel").touch()
    with pytest.raises(runner.ScreeningCancelled):
        list(stream)


def test_successful_preparation_still_binds_the_actual_read_input(tmp_path):
    stocks, settings, contexts = universe()
    list(
        prep.prepare_inputs(
            stocks,
            settings,
            contexts,
            tmp_path / "cache",
            tmp_path,
            worker_target=completed_worker,
        )
    )
    frame, _, context = inputs("5m")
    context["_screening_tail_prepared"] = stocks[0]["_completed_inputs"]["5m"]

    class NoDownload:
        def klines(self, *args, **kwargs):
            pytest.fail(
                "already acknowledged native preparation must not download again"
            )

    @closing_bar.completed_input_scope
    def check(*, cache_root):
        return closing_bar.verify_completed_input(
            NoDownload(),
            frame,
            "SH.688139",
            "5m",
            context,
            start="20260814",
            end="2026-10-13 15:00:00",
        )

    result, downloaded = check(cache_root=tmp_path / "cache")
    assert not downloaded and result.equals(frame)
    assert not closing_bar.preparation_needed(
        tmp_path / "cache", "SH.688139", "5m", context["cutoff"]
    )


def test_completed_windows_use_synchronous_ranges_not_a_shared_callback_session(monkeypatch):
    from chanlun.exchange import exchange_qmt as qmt
    from contextlib import nullcontext
    from types import SimpleNamespace

    exchange = qmt.ExchangeQMT.__new__(qmt.ExchangeQMT)
    monkeypatch.setattr(
        qmt, "_xtdata_download_interprocess_lock", lambda: nullcontext()
    )
    calls = []

    def native(code, period, start, end):
        calls.append((code, period, start, end))

    monkeypatch.setattr(qmt.xtdata, 'get_client', lambda: SimpleNamespace(supply_history_data=native))
    monkeypatch.setattr(qmt.xtdata, 'download_history_data2', lambda *_a, **_k: pytest.fail('callback download is not a completion proof'))
    assert (
        exchange.download_completed_window(
            ["SH.688139"], "5m", "20261013000000", "20261013150000"
        )
        is True
    )
    assert calls == [('688139.SH','5m','20261013000000','20261013150001')]
    def failed(*_args):
        raise RuntimeError('native range unavailable')
    monkeypatch.setattr(qmt.xtdata, 'get_client', lambda: SimpleNamespace(supply_history_data=failed))
    with pytest.raises(RuntimeError, match="unavailable"):
        exchange.download_completed_window(
            ["SH.688139"], "5m", "20261013000000", "20261013150000"
        )
