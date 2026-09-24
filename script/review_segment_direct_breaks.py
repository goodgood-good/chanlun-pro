"""Separate L071 first-break questions from the existing fractal qualifiers.

Source read: D:/缠论/chanlun_lesson_corpus/L071_线段划分标准的再分辨(2007-08-16230206).md,
lines 76-124, especially 100-106; L078 lines 208-211 require the predecessor
relationship first. The deliberately conservative scan requires a reversal
past the ENTIRE prior body, a third pen beyond the reversal's endpoint, no
intermediate return through its origin, and an already confirmed predecessor.
These questions are not resolved by the same local-extremum test under review.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from script.audit_all_segment_results import write_json
from script.review_segment_causal_prefixes import read
from script.review_every_segment_deep import OUT, PREVIOUS, FRESH, hashes
from script.explain_interior_segment_candidates import unfinished_second


def questions(row, pens, previous):
    if previous is None or not previous['confirmed']:
        return []
    before = previous['saved_confirmed_at']
    start, end, up = row['start_pen'], row['end_pen'], row['direction'] == 'up'
    horizon = row['confirmation_horizon']
    body_low, body_high = pens[start]['start_tick'], pens[start]['start_tick']
    out = []
    for pivot in range(start + 1, horizon - 1, 2):
        for index in range(max(start, pivot - 2), pivot):
            body_low = min(body_low, pens[index]['start_tick'], pens[index]['end_tick'])
            body_high = max(body_high, pens[index]['start_tick'], pens[index]['end_tick'])
        if pivot < start + 3:
            continue
        a, b, c = pens[pivot:pivot + 3]
        endpoint = a['start_tick']
        if not (endpoint > row['start_tick'] if up else endpoint < row['start_tick']):
            continue
        body_break = a['end_tick'] < body_low if up else a['end_tick'] > body_high
        third_extends = c['end_tick'] < a['end_tick'] if up else c['end_tick'] > a['end_tick']
        no_origin_return = b['end_tick'] < a['start_tick'] if up else b['end_tick'] > a['start_tick']
        if not (body_break and third_extends and no_origin_return):
            continue
        dependencies = pens[start:pivot + 3]
        if not all(p['locked'] and not p.get('selection_pending') and p.get('confirmed_at') is not None for p in dependencies):
            continue
        ready = max(max(p['confirmed_at'], p.get('selected_at') or 0) for p in dependencies)
        first_ready = max(max(p['confirmed_at'], p.get('selected_at') or 0) for p in pens[start:pivot + 1])
        if before > first_ready:
            continue
        if row['confirmed'] and (pivot - 1 > end or ready > row['saved_confirmed_at']):
            continue
        if row['confirmed'] and pivot - 1 == end and ready == row['saved_confirmed_at']:
            continue
        out.append({'family': 'direct_whole_body_first_break', 'end_pen': pivot - 1, 'pivot': pivot,
                    'first_witness': pivot, 'witness': pivot + 2, 'has_gap': False,
                    'first_sequence': [], 'second_outcome': {'status': 'not_required'},
                    'dependencies_closed': True, 'dependencies_available_at': ready,
                    'first_available_at': first_ready, 'previous_confirmed_at': before,
                    'previous_number': previous['number'], 'body_low_tick': body_low,
                    'body_high_tick': body_high, 'first_start_tick': a['start_tick'],
                    'first_end_tick': a['end_tick'], 'middle_end_tick': b['end_tick'],
                    'third_end_tick': c['end_tick'], 'native_end_pen': end,
                    'native_confirmed_at': row.get('saved_confirmed_at'),
                    'candidate_time': a['start_time'], 'first_witness_after_native_endpoint': pivot > end,
                    'second_witness_after_native_endpoint': pivot + 2 > end,
                    'candidate_after_native_projection': pivot - 1 >= end,
                    'resolution': 'direct_first_break_requires_individual_review',
                    'context': [], 'source_refs': ['L071:100-106', 'L078:208-211']})
    return out


def inspect(path):
    a = read(path)
    old = PREVIOUS if a['id'].startswith('qqq_requested_') else FRESH
    geom = read(old / 'geometry' / f"{a['id']}.json.gz")
    causal = read(old / 'causal_reviews' / f"{a['id']}.json.gz")
    actual = {(p['start_time'], p['end_time']): p for p in causal['pens']}
    components = {g['component']: [{**p, 'confirmed_at': actual[p['start_time'], p['end_time']]['confirmed_at'],
                                  'selected_at': actual[p['start_time'], p['end_time']]['selected_at']}
                                 for p in g['pens']] for g in geom['components']}
    traces = {g['component']: g['decisions'] for g in geom['components']}
    prev, found = {}, []
    for row in a['rows']:
        items = questions(row, components[row['component']], prev.get(row['component']))
        for item in items:
            earlier = [e for e in traces[row['component']]
                       if e['start'] == row['start_pen'] and e['check'] <= item['pivot']
                       and (e.get('witness_index', -1) >= item['pivot'] or e['kind'] == 'PendingBoundary')]
            for event in earlier:
                ctx = {'event': event, 'requires_priority_adjudication': True}
                if (event['kind'] == '_GapConfirmationInvalidated' or
                        event['kind'] == 'PendingBoundary' and event.get('reason') == 'waiting-second-feature'):
                    ctx['second_sequence_at_third_pen'] = unfinished_second(
                        components[row['component']][:item['witness'] + 1],
                        event['candidate_index'] + 1, row['direction'] == 'up')
                item['context'].append(ctx)
            item['priority_context'] = 'earlier_hypothesis_present' if earlier else 'no_earlier_hypothesis_recorded'
            found.append({'number': row['number'], 'direction': row['direction'], 'price_quantum': row['price_quantum'],
                          'native_confirmed': row['confirmed'], 'native_start_time': row['start_time'],
                          'native_end_time': row['end_time'], 'candidate': item})
        prev[row['component']] = row
    return {'id': a['id'], 'code': a['code'], 'frequency': a['frequency'], 'market': a['market'],
            'source_revision': a['source_revision'], 'native_segments': len(a['rows']), 'questions': found}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--complete', action='store_true')
    args = parser.parse_args()
    before = hashes()
    rows = [inspect(path) for path in sorted((OUT / 'datasets').glob('*.json.gz'))]
    if args.complete:
        assert len(rows) == read(OUT / 'all_results.json')['completed']
    assert hashes() == before
    summary = {'complete': args.complete, 'windows': len(rows), 'segments': sum(r['native_segments'] for r in rows),
               'question_windows': sum(bool(r['questions']) for r in rows),
               'question_segments': len({(r['id'], q['number']) for r in rows for q in r['questions']}),
               'questions': sum(len(r['questions']) for r in rows),
               'priority_contexts': dict(Counter(q['candidate']['priority_context'] for r in rows for q in r['questions'])),
               'datasets': rows, 'audit_implementation_sha256': before,
               'scanner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(OUT / ('direct_breaks.json' if args.complete else 'direct_breaks_partial.json'), summary)
    print({k: v for k, v in summary.items() if k not in ['datasets', 'audit_implementation_sha256']}, flush=True)
    for r in rows:
        if r['questions']:
            print({k: v for k, v in r.items() if k != 'questions'} | {'numbers': [q['number'] for q in r['questions']]}, flush=True)


if __name__ == '__main__':
    main()
