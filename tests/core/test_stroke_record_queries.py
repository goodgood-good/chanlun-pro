"""Queries over existing records preserve values without rebuilding history."""
from datetime import datetime, timedelta, timezone
import pickle
from types import SimpleNamespace

import pytest

from chanlun.core.stroke_resolver import StrokeCompletion, StrokeResolver


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fx(index, kind=None, value=None):
    kind = kind or ("ding" if index % 2 == 0 else "di")
    return SimpleNamespace(k=SimpleNamespace(index=index * 5), type=kind,
                           val=value if value is not None else 20 if kind == "ding" else 10)


def built(count, closed="all"):
    resolver = StrokeResolver(closed_through=closed)
    for index in range(count):
        resolver.advance(fx(index), lambda *_: True, START + timedelta(minutes=index))
    assert [row.start for row in resolver.completions] == sorted(row.start for row in resolver.completions)
    return resolver


@pytest.mark.parametrize("count", [100, 1000])
@pytest.mark.parametrize("change", ["equal_same", "weaker_same", "unqualified_opposite"])
def test_unchanged_path_does_not_construct_an_unused_edge_list(count, change, monkeypatch):
    resolver = built(count)
    before = (resolver.path(), resolver.qualifications, resolver.completions, resolver.continuations)
    if change == "unqualified_opposite":
        candidate = fx(count)
        valid = lambda *_: False
    else:
        tail = resolver.nodes[resolver.selected].fx
        candidate = fx(count, tail.type, tail.val + (1 if change == "weaker_same" else 0))
        valid = lambda *_: True
    def forbidden():
        raise AssertionError("unchanged paths must not rebuild their edges")
    monkeypatch.setattr(resolver, "edges", forbidden)
    resolver.advance(candidate, valid, START + timedelta(minutes=count))
    assert (resolver.path(), resolver.qualifications, resolver.completions, resolver.continuations) == before


@pytest.mark.parametrize("start,end,expected", [(0,1,True),(30,31,True),(62,63,True),(31,32,False),(30,32,False),(64,65,False)])
def test_completed_edge_lookup_matches_both_endpoints(start, end, expected):
    resolver = StrokeResolver()
    resolver.completions = tuple(StrokeCompletion(i, i+1, i+2, i+3, START) for i in range(0,64,2))
    assert resolver._has_completed_edge(start, end) is expected


@pytest.mark.parametrize("count", [16, 256, 4096])
def test_completed_edge_lookup_is_bounded_without_iterating_the_whole_prefix(count):
    class CountedTuple(tuple):
        reads = 0
        def __iter__(self):
            raise AssertionError("lookup must not scan all completed records")
        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)
    resolver = StrokeResolver()
    rows = tuple(StrokeCompletion(i, i+1, i+2, i+3, START) for i in range(count))
    resolver.completions = CountedTuple(rows)
    assert resolver._has_completed_edge(count-1, count)
    assert resolver.completions.reads <= count.bit_length() + 4


def test_queries_follow_completion_advance_rewind_and_pickle_restore():
    resolver = built(50, closed=None)
    assert not resolver._has_completed_edge(0, 1)
    resolver.confirm_through("all")
    assert resolver._has_completed_edge(0, 1)
    before = resolver.completions
    resolver.advance(fx(50), lambda *_: True, START + timedelta(minutes=50))
    added = resolver.completions[-1]
    assert added not in before and resolver._has_completed_edge(added.start, added.end)
    resolver.rewind_last()
    assert resolver.completions == before and not resolver._has_completed_edge(added.start, added.end)
    restored = pickle.loads(pickle.dumps(resolver))
    assert all(restored._has_completed_edge(row.start, row.end) for row in before)
