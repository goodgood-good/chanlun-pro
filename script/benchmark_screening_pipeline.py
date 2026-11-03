"""Compare local-only screening throughput with frozen, copied cache inputs.

Writes exclusively beneath --output. Does not start a screening manager or
send notifications; downloads and suspension-feed requests are prohibited.
Use the same --source-run and --codes for both source trees. The frozen
calculation revision is intentional for equivalence testing of infrastructure
changes; this is not a production cache migration.
"""
import argparse
from datetime import datetime
import gzip
import json
from pathlib import Path
from contextlib import nullcontext
import shutil
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-tree', type=Path, required=True)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codes', nargs='+', default=['SH.600519', 'SH.600036', 'SZ.000001', 'SZ.300750'])
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Choose a new output directory for each benchmark')
    if output.is_relative_to(args.source_run.parent.resolve()):
        raise ValueError('Benchmark output must be outside production screening storage')
    output.mkdir(parents=True)
    sys.path.insert(0, str(args.source_tree.resolve()/'src'))
    from chanlun.screening import runner
    try:
        from chanlun.screening.closing_bar import input_verification
    except ImportError:
        input_verification = lambda _enabled: nullcontext()
    from chanlun.exchange.exchange_qmt import xtdata
    import requests

    def forbidden(*_args, **_kwargs):
        raise RuntimeError('Benchmark prohibits downloads and external HTTP requests')
    for name in ('download_history_data','download_history_data2','supply_history_data'):
        if hasattr(xtdata,name):
            setattr(xtdata,name,forbidden)
    runner.suspension_evidence = forbidden
    requests.Session.request = forbidden
    status = json.loads((args.source_run/'status.json').read_text(encoding='utf-8'))
    universe = json.loads((args.source_run/'universe.json').read_text(encoding='utf-8'))
    stocks = [stock for stock in universe if stock['code'] in args.codes and stock.get('market', 'a') == 'a']
    if {stock['code'] for stock in stocks} != set(args.codes):
        raise ValueError('Every benchmark symbol must exist in the frozen A-share universe')
    settings = status['settings']
    observed = datetime.fromisoformat(status['observed_at'])
    contexts = {period: runner._a_calendar_context(observed, period, settings['recent_sessions'], settings['max_anchor_sessions'])
                for period in settings['frequencies']}
    source_cache = args.source_run.parent/'analysis_cache'
    cache = output/'cache'
    cache.mkdir()
    for stock in stocks:
        for period in settings['frequencies']:
            source = source_cache/f"{stock['code']}_{period}.json.gz"
            payload = json.loads(json.loads(gzip.decompress(source.read_bytes()))['payload'])
            if payload.get('calculation_revision') != status['source_revision']:
                raise ValueError('Source calculation changed; choose a matching completed run')
            shutil.copy2(source, cache/source.name)
            original = source_cache/payload['source_input']['file']
            target = cache/payload['source_input']['file']
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(original, target)
    passes = []
    for index in range(3):
        rows = []
        started = time.perf_counter()
        for stock in stocks:
            for period in settings['frequencies']:
                with input_verification(False):
                    result = runner.scan_symbol(stock, {**settings, 'frequencies': [period]}, contexts,
                        output/f'pass-{index}', cache_root=cache, revision=status['source_revision'])
                rows.extend(result['rows'])
        elapsed = time.perf_counter()-started
        (output/f'rows-{index}.json').write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        passes.append({'seconds': round(elapsed, 4), 'periods': len(rows),
                       'errors': sum(bool(row.get('data_errors')) for row in rows),
                       'structures_reused': sum(bool(row.get('calculation_reused')) for row in rows),
                       'selections_reused': sum(bool(row.get('selection_reused')) for row in rows),
                       'timings_ms': {key: round(sum(row.get('timings_ms', {}).get(key, 0) for row in rows), 2)
                                      for key in ('fetch', 'structure', 'validation', 'persistence', 'total')}})
    report = {'source_tree': str(args.source_tree.resolve()), 'source_run': str(args.source_run.resolve()),
              'runtime_revision': runner.source_revision(), 'frozen_calculation_revision': status['source_revision'],
              'codes': args.codes, 'passes': passes, 'repeat_median_seconds': statistics.median(p['seconds'] for p in passes[1:])}
    (output/'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
