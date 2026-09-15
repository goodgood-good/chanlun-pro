"""Audit v10 stroke selection using source-condition counterexamples.

This records current production behavior without changing the engine. Synthetic
prices are not quoted market prices. Locally eligible alternatives are evidence
for reviewing the selection rule, not a proof that every alternative is the
original author's final whole-chart partition.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision
from chanlun.core.stroke_sequence import SourceStrokeSelection, SourceStrokeSequence
from chanlun.core.types import Kline


EQUAL_PREFIX = [
    (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (10, 6),
    (11, 7), (12, 8), (14, 10), (12, 8), (11, 7), (10, 6),
    (10.5, 6.5), (11, 7), (11.5, 7.5), (12, 8), (11.5, 7.5),
]

ORIGIN_WAIT = [
    (29, 25), (30, 26), (28, 22), (23, 18), (19, 11), (12, 8),
    (18, 10), (20, 14), (21, 18), (22, 20), (21, 9), (10, 6),
    (32, 8), (34, 30), (33, 12), (14, 10), (18, 12), (22, 16),
    (24, 20), (26, 22), (24, 20), (22, 18), (18, 14), (16, 12),
    (18, 14), (20, 16), (22, 18), (24, 20), (22, 18), (20, 16),
    (18, 14), (17, 13), (19, 15),
]

INITIAL_INCLUSION = [
    (110, 100), (109, 101), (109.5, 100.5), (109, 100), (108, 99),
    (107, 98), (106, 97), (105, 96), (106, 97),
]

COMPLETION_RETRACTION = [
    (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (12, 8),
    (14, 10), (16, 12), (18, 14), (22, 18), (20, 9), (10, 6),
    (22, 8), (24, 20), (22, 14), (14, 10), (10, 6), (8, 4), (10, 6),
]


def reflected(prices, mirror):
    return [(300 - low, 300 - high) for high, low in prices] if mirror else list(prices)


def independent_definitions(prices):
    """L062 three-K/distance plus L066 author-reply range condition.

    These checks deliberately do not call production stroke_rules functions.
    The non-inclusion fixtures supply only price/type/index to the selector.
    """
    points = []
    for center in range(1, len(prices) - 1):
        left, middle, right = prices[center - 1:center + 2]
        kind = None
        if middle[0] > max(left[0], right[0]) and middle[1] > max(left[1], right[1]):
            kind = "ding"
        elif middle[0] < min(left[0], right[0]) and middle[1] < min(left[1], right[1]):
            kind = "di"
        if kind:
            points.append(SimpleNamespace(type=kind, val=middle[0 if kind == "ding" else 1],
                                          k=SimpleNamespace(index=center)))

    def legal(first, second):
        if first.type == second.type or second.k.index - first.k.index < 4:
            return False
        top, bottom = (first, second) if first.type == "ding" else (second, first)
        span = prices[first.k.index:second.k.index + 1]
        return top.val == max(p[0] for p in span) and bottom.val == min(p[1] for p in span)

    return points, legal


def input_properties(prices):
    return dict(
        gaps=[i for i, (a, b) in enumerate(zip(prices, prices[1:]), 1)
              if a[0] < b[1] or b[0] < a[1]],
        inclusions=[i for i, (a, b) in enumerate(zip(prices, prices[1:]), 1)
                    if (a[0] >= b[0] and a[1] <= b[1]) or (b[0] >= a[0] and b[1] <= a[1])],
    )


def raw_bars(prices):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Kline(i, start + timedelta(minutes=i), high, low,
                  (high + low) / 2, (high + low) / 2, 1) for i, (high, low) in enumerate(prices)]


def snapshot(merged, calc):
    return dict(
        merged_prices=[(k.h, k.l) for k in merged.cl_klines],
        fractals=[dict(center=f.k.index, raw_center=f.k.k_index, kind=f.type, value=f.val) for f in calc.fxs],
        strokes=[dict(start=b.start.k.index, end=b.end.k.index,
                      raw_start=b.start.k.k_index, raw_end=b.end.k.k_index,
                      start_price=b.start.val, end_price=b.end.val, is_done=b.is_done(),
                      locked_at=b.locked_at, selected_at=b.selected_at,
                      completion_status=b.completion_status, completion_is_final=b.completion_is_final)
                 for b in calc.bis],
        pending_continuation=calc.continuation_blocked_at,
        unresolved_endpoint_choices=[asdict(d) for d in calc.unresolved_endpoint_choices],
        initial_context_unresolved=merged.initial_context_unresolved,
        initial_excluded_raw_count=merged.initial_excluded_raw_count,
        range_audit=calc.audit_endpoint_ranges(), adjacency_audit=calc.audit_endpoint_adjacency(),
    )


def production(prices):
    merged, calc = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(raw_bars(prices))
    calc.calculate(merged.cl_klines)
    return snapshot(merged, calc)


def streaming(prices, expected):
    """Replay the same immutable bars incrementally; record actual revisions."""
    raw = raw_bars(prices)
    merged, calc = CL_Kline_Process(), BiCalculator()
    previous, events = None, []
    for end in range(1, len(raw) + 1):
        merged.process_cl_klines(raw[:end])
        calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                       validated_incremental_prefix=True)
        state = snapshot(merged, calc)
        observed = (state['strokes'], state['pending_continuation'])
        if observed != previous:
            events.append(dict(raw_bars=end, strokes=state['strokes'],
                               pending_continuation=state['pending_continuation']))
        previous = observed
    return dict(prefixes=len(raw), changes=events, final_matches_batch=state == expected)


def selection_trace(prices):
    points, legal = independent_definitions(prices)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    events = []
    for point in points:
        previous = selection.path()
        relation = sequence.append(point, legal)
        decision = selection.append(relation, sequence)
        events.append(dict(
            center=point.k.index, kind=point.type, value=point.val,
            eligible=[points[i].k.index for i in relation.eligible],
            irreducible=[points[i].k.index for i in relation.adjacent],
            previous=[points[i].k.index for i in previous],
            selected=[points[i].k.index for i in selection.path()],
            candidate_parent=None if selection.nodes[-1].parent < 0 else points[selection.nodes[-1].parent].k.index,
            candidate_origin=points[selection.nodes[-1].origin].k.index,
            action=decision.action, pending=selection.pending_continuation,
            omitted=[dict(candidate=points[o.candidate].k.index, discarded=points[o.discarded].k.index,
                          support=[points[o.stroke_start].k.index, points[o.stroke_end].k.index])
                     for o in decision.omitted],
            live_starts=[dict(center=points[i].k.index, origin=points[selection.nodes[i].origin].k.index)
                         for i in sorted(sequence.live_starts())],
        ))
    return events


def path_conditions(prices, centers):
    points, legal = independent_definitions(prices)
    by_center = {p.k.index: p for p in points}
    edges = []
    for start, end in zip(centers, centers[1:]):
        # An independent exhaustive longest-path check on the fixed interval.
        # This tests complete subdivisions, not the production omission mask.
        counts = {start: 0}
        for p in points:
            if not start < p.k.index <= end:
                continue
            previous = [count + 1 for center, count in counts.items()
                        if legal(by_center[center], p)]
            if previous:
                counts[p.k.index] = max(previous)
        edges.append(dict(start=start, end=end, locally_legal=legal(by_center[start], by_center[end]),
                          longest_qualified_subdivision=counts.get(end, 0)))
    return edges


def long_origin_wait(legs=64):
    prices = list(ORIGIN_WAIT[:-1])
    for leg in range(legs):
        start = prices[-1][0]
        target = 23 if leg % 2 == 0 else 18
        prices.extend((start + (target - start) * step / 4,
                       start + (target - start) * step / 4 - 4) for step in range(1, 5))
    high, low = prices[-1]
    prices.append((high + 1, low + 1))
    return prices


def file_record(path):
    path = Path(path)
    return dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def audit(source_root):
    equal = []
    for endpoint_high in (13, 14, 16):
        values = EQUAL_PREFIX + [(11, 7), (endpoint_high - 1, endpoint_high - 5),
                                 (endpoint_high, endpoint_high - 4), (endpoint_high - 2, endpoint_high - 6)]
        for mirror in (False, True):
            prices = reflected(values, mirror)
            equal.append(dict(mirror=mirror,endpoint_high=endpoint_high,prices=prices,
                              properties=input_properties(prices),trace=selection_trace(prices),
                              current=production(prices),earlier_equal_alternative=path_conditions(prices,[1,5,19])))
    waits = []
    for extended in (False, True):
        for mirror in (False, True):
            prices = reflected(long_origin_wait() if extended else ORIGIN_WAIT, mirror)
            waits.append(dict(extended=extended,mirror=mirror,prices=prices,properties=input_properties(prices),
                              trace=selection_trace(prices),current=production(prices),
                              later_continuous_chain=path_conditions(prices,list(range(15,len(prices)-1,4))),
                              later_component_with_left_shoulder=production(prices[14:])))
    initialization = []
    for context in (False, True):
        for mirror in (False, True):
            prices = reflected(([(108,98)] if context else [])+INITIAL_INCLUSION, mirror)
            initialization.append(dict(context=context,mirror=mirror,prices=prices,current=production(prices)))
    completion = []
    for mirror in (False, True):
        prices = reflected(COMPLETION_RETRACTION,mirror)
        completion.append(dict(mirror=mirror,prices=prices,properties=input_properties(prices),
                               before=production(prices[:11]),after=production(prices)))
    for case in equal + waits + initialization:
        case['streaming'] = streaming(case['prices'], case['current'])
    for case in completion:
        case['streaming'] = streaming(case['prices'], case['after'])
    sources=[]
    for prefix,kind,lines in [('L062_','body',[(37,118)]),('L065_','body_and_separately_identified_annotations',[(31,100),(166,214)]),
                              ('L066_','author_reply',[(256,280)]),('L069_','body',[(28,49),(151,223)]),
                              ('L077_','body_and_separately_identified_annotations',[(118,148),(169,295)])]:
        path=next((source_root/'chanlun_lesson_corpus').glob(prefix+'*.md'))
        text=path.read_text(encoding='utf-8').splitlines()
        sources.append(dict(**file_record(path),kind=kind,line_spans=lines,
                            excerpts=[dict(start=start,end=end,text='\n'.join(line for line in text[start-1:end]
                                         if line and not line.startswith('<!--'))) for start,end in lines]))
    code=[file_record(ROOT/'src'/'chanlun'/'core'/name) for name in
          ('stroke_sequence.py','stroke_resolver.py','stroke_rules.py','bi_calculator.py','cl_kline_process.py','types/line.py')]
    return dict(profile=STRICT_STROKE_MODE,profile_revision=strict_base_config_revision(),
                notes=['Read-only production audit; only this report is written.',
                       'Synthetic examples; source inferences are separate from literal quotations.',
                       'Locally qualified later chains do not by themselves prove their global attachment to an unresolved prefix.'],
                sources=sources,code=code,equal_reactivation=equal,origin_wait=waits,
                initialization=initialization,completion_retraction=completion)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=Path('D:/缠论'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.source_root)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2)+'\n',encoding='utf-8')
    for case in result['equal_reactivation']:
        print('equal',case['endpoint_high'],case['mirror'],
              [(b['start'],b['end']) for b in case['current']['strokes']],case['earlier_equal_alternative'])
    for case in result['origin_wait']:
        print('origin',len(case['prices']),case['mirror'],'current',len(case['current']['strokes']),
              'later component',len(case['later_component_with_left_shoulder']['strokes']))
    for case in result['initialization']:
        print('initialization',case['context'],case['mirror'],len(case['current']['strokes']))
    for case in result['completion_retraction']:
        print('completion',case['mirror'],'before',[(b['start'],b['end'],b['is_done']) for b in case['before']['strokes']],
              'after',[(b['start'],b['end'],b['is_done']) for b in case['after']['strokes']])
