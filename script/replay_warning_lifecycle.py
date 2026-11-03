"""Offline closed-bar replay; never reads quotes, monitor state, or sends alerts.

Inputs are copied, content-addressed notification parquet/snapshot evidence and
events.json. Each decision rebuilds from a prefix ending at that decision's bar.
Counts describe structural warning lifecycles, not actual provider deliveries.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chanlun.screening.runner import snapshot_for, source_revision
from chanlun.core.strict_structure.warning_lifecycle import bind_warning, assess_warning

CN = timezone(timedelta(hours=8))
CASES = [
    ('QQQ.US', '1m', '2026-10-15 23:00', '2026-10-15 23:18'),
    ('SZ.301004', '1m', '2026-10-14 10:30', '2026-10-14 10:45'),
    ('SZ.301004', '1m', '2026-10-15 14:40', '2026-10-15 14:50'),
    ('SZ.301004', '5m', '2026-10-14 13:00', '2026-10-14 14:00'),
    ('SH.600449', '1m', '2026-10-14 13:00', '2026-10-14 15:00'),
]


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=CN).timestamp())


def clock(value):
    return datetime.fromtimestamp(value, CN).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidates(snapshot, include_consolidation):
    found = {}
    for level in snapshot['levels']:
        for point in level['points']:
            if point.get('status') == 'approaching':
                key = 'point:' + point['preview_key']
                found[key] = {'signal_type':'native_point_preview', 'point_type':point['point_type'],
                    'point_id':point['point_id'], 'anchor_tick':point['anchor_tick']}
    if include_consolidation:
        # Prefer entry/departure over another comparison in the same group.
        # These are structural lifecycle counts, not monitor delivery counts.
        choices = {}
        for alert in snapshot.get('native_consolidation_alerts', []):
            if alert['status'] != 'warning':
                continue
            group = alert['center_id'], alert['direction']
            rank = alert['comparison_form'] == 'entry_departure', alert['hypothesis_id']
            if group not in choices or rank > choices[group][0]:
                choices[group] = rank, alert
        for _, alert in choices.values():
            found['consolidation:' + alert['hypothesis_id']] = {
                'signal_type':'consolidation_divergence', 'point_type':'consolidation_' + alert['direction'],
                'hypothesis_id':alert['hypothesis_id'], 'anchor_tick':alert['anchor_tick']}
    return found


def is_confirmed(snapshot, key):
    if key.startswith('point:'):
        return any(p.get('preview_key') == key[6:] and p['status'] == 'confirmed'
                   for level in snapshot['levels'] for p in level['points'])
    return any(a.get('hypothesis_id') == key[14:] and a['status'] == 'confirmed'
               for a in snapshot.get('native_consolidation_alerts', []))


def replay(inputs, events, case):
    code, frequency, begin, end = case
    start, stop = stamp(begin), stamp(end)
    sources = []
    for event in events:
        m = event.get('evidence') or {}
        parquet = inputs / (str(m.get('parquet_sha256')) + '.parquet')
        packed = inputs / (str(m.get('snapshot_sha256')) + '.json.gz')
        if (event['code'] == code and event['frequency'] == frequency
                and event.get('source_closed_at', 0) >= stop and parquet.exists() and packed.exists()):
            sources.append((event['source_closed_at'], event, parquet, packed))
    if not sources:
        return {'code':code, 'frequency':frequency, 'begin':begin, 'end':end, 'error':'No complete frozen input through case end'}
    for _, event, parquet, packed in sorted(sources, key=lambda r:r[0]):
        frame = pd.read_parquet(parquet)
        if int(frame.date.iloc[0].timestamp()) <= start and int(frame.date.iloc[-1].timestamp()) >= stop:
            break
    else:
        return {'code':code, 'frequency':frequency, 'begin':begin, 'end':end, 'error':'Frozen history does not cover the whole case'}
    assert digest(parquet) == event['evidence']['parquet_sha256']
    assert digest(packed) == event['evidence']['snapshot_sha256']
    original = json.loads(gzip.decompress(packed.read_bytes()))
    frame = pd.read_parquet(parquet)
    frame.attrs.update(structure_price_quantum=original['structure_price_quantum'],
                       price_basis_revision=original['price_basis_revision'], screening_market=event.get('market','a'))
    baseline, maintained, counts_old, counts_new = {}, {}, Counter(), Counter()
    timeline, decisions = [], []
    timestamps = [int(t.timestamp()) for t in frame.date]
    begin_clock = time.monotonic()
    for index, cutoff in enumerate(timestamps):
        if not start <= cutoff <= stop:
            continue
        prefix = frame.iloc[:index + 1].copy()
        assert max(int(t.timestamp()) for t in prefix.date) == cutoff
        snapshot = snapshot_for(prefix, code, frequency, include_consolidation=code == 'SZ.301004')
        assert snapshot['source_closed_at'] == cutoff
        raw = candidates(snapshot, code == 'SZ.301004')
        step = {'at':clock(cutoff), 'baseline':[], 'revised':[]}
        for key in set(baseline) - set(raw):
            state = 'confirmed' if is_confirmed(snapshot, key) else 'invalidated'
            counts_old[state] += 1
            step['baseline'].append({'key':key, 'state':state})
        for key in set(raw) - set(baseline):
            counts_old['warning'] += 1
            step['baseline'].append({'key':key, 'state':'warning'})
        baseline = raw
        ended = set()
        for key, live in list(maintained.items()):
            outcome = assess_warning(live['hypothesis'], snapshot, prefix)
            state = outcome['state']
            decisions.append({'at':cutoff, 'key':key, 'state':state, 'reason':outcome['reason_code']})
            if state in {'confirmed', 'invalidated'}:
                counts_new[state] += 1
                step['revised'].append({'key':key, **outcome})
                maintained.pop(key)
                ended.add(key)
            elif state == 'pending':
                counts_new['pending_checks'] += 1
                step['revised'].append({'key':key, **outcome})
            else:
                if key not in raw:
                    counts_new['retained_without_new_trigger_checks'] += 1
                if outcome.get('anchor_tick') != live['anchor_tick']:
                    counts_new['candidate_updates'] += 1
                    step['revised'].append({'key':key, **outcome})
                    live['anchor_tick'] = outcome['anchor_tick']
        for key, candidate in raw.items():
            if key in maintained or key in ended:
                continue
            try:
                hypothesis = bind_warning(snapshot, candidate)
            except (ValueError, StopIteration, KeyError) as exc:
                counts_new['admission_proof_rejected'] += 1
                step['revised'].append({'key':key, 'state':'rejected', 'error':str(exc)})
                continue
            maintained[key] = {'hypothesis':hypothesis, 'anchor_tick':candidate['anchor_tick']}
            counts_new['warning'] += 1
            step['revised'].append({'key':key, 'state':'warning', 'point_type':candidate['point_type']})
        timeline.append(step)
    return {'code':code, 'frequency':frequency, 'begin':begin, 'end':end, 'bars':len(timeline),
        'input_rows':len(frame), 'input_cutoff':clock(original['source_closed_at']),
        'parquet_sha256':digest(parquet), 'snapshot_sha256':digest(packed),
        'baseline':dict(counts_old), 'revised':dict(counts_new), 'remaining_candidates':len(maintained),
        'elapsed_seconds':round(time.monotonic() - begin_clock, 3), 'timeline':timeline, 'decisions':decisions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    events = json.loads((args.inputs / 'events.json').read_text(encoding='utf-8'))
    if (args.inputs / 'sources.json').exists():
        events += json.loads((args.inputs / 'sources.json').read_text(encoding='utf-8'))
    report = {'schema':'warning-lifecycle-replay-v1', 'timezone':'Asia/Shanghai',
        'runtime_revision':source_revision(),
        'scope':'Structural warning lifecycle; excludes delivery channels and screening admission preferences.',
        'causality':'Full rebuild from each closed-bar prefix; no future candles used in a decision.',
        'limits':[
            'Uses the saved price/history revision, not a reconstruction of every historical provider revision.',
            'Initial candidates at the beginning of a case are counted; counts are not a population failure rate.',
            'Uses the existing native-segment/center and raw MACD HTF policy, not a complete recursive Chan theory engine.',
            'Second-point continuation covers the existing confirmed-parent/nonbreaking-return branch. L053 also describes other alternatives.',
        ],
        'local_author_sources':[
            {'file':'D:/缠论/chanlun_lesson_corpus/L061_区间套定位标准图解(分析示范六)(2007-06-21.md',
             'lines':[97,121], 'use':'Comparison ownership, observation until refutation; excludes annotation at line 109.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L024_MACD对背弛的辅助判断(2007-01-18150243).md',
             'lines':[[226,238],[310,319],[334,379]], 'use':'Area comparison and provisional estimation; 2C<A admission is an engineering mapping.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L025_吻,MACD、背弛、中枢(2007-01-23151313).md',
             'lines':[1096,1102], 'use':'Author reply: shorter histogram alone is not divergence.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L021_缠中说禅买卖点分析的完备性(2007-01-09.md',
             'lines':[247,271], 'use':'First-point and subsequent return relationship.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L053_三类买卖点的再分辨.md',
             'lines':[235,256], 'use':'Broader second-point alternatives; existing branch must not be presented as exhaustive.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L020_缠中说禅走势中枢级别扩张及第三类买卖点.md',
             'lines':[427,454], 'use':'Third-point first return and core boundary.'},
            {'file':'D:/缠论/chanlun_lesson_corpus/L054_一个具体走势的分析(2007-05-24013731).md',
             'lines':[181,196], 'use':'Equality/contact in the second decomposition is oscillation; unfinished contact is observation, not confirmation.'},
        ],
        'baseline':'Existing conditional candidate generator with disappearance treated as invalidation.',
        'revised':'Same admission proofs; original hypothesis assessed independently of its first trigger.', 'cases':[]}
    for case in CASES:
        result = replay(args.inputs, events, case)
        report['cases'].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in {'timeline','decisions'}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
