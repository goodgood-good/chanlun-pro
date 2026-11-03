from pathlib import Path
import json
import pandas as pd
import pytest

from chanlun.screening import runner, snapshot_presentation as views


@pytest.fixture(scope="module")
def source():
    return (
        pd.read_parquet(
            Path(__file__).parent / "fixtures/retired_first_SZ.000729_5m.parquet"
        )
        .iloc[-1500:]
        .copy()
    )


def test_fact_snapshot_and_later_evidence_equal_the_original_full_chart(
    source, tmp_path
):
    original = runner.snapshot_for(source, "SZ.000729", "5m")
    with views.presentation_scope():
        with views.defer_presentation(tmp_path / "cache", "fixture", enabled=True):
            compact = runner.snapshot_for(source, "SZ.000729", "5m")
        assert views.PENDING in compact and "chart" not in compact
        assert {k: v for k, v in compact.items() if k != views.PENDING} == {
            k: v for k, v in original.items() if k != "chart"
        }
        assert views.materialize(compact, source, "SZ.000729", "5m") == original
        assert json.dumps(compact, ensure_ascii=False) == json.dumps(
            original, ensure_ascii=False
        )
    assert views.PENDING not in compact


def test_replay_snapshot_omits_only_presentation_and_never_registers_a_chart(source):
    original = runner.snapshot_for(source, "SZ.000729", "5m")
    with views.facts_only():
        replay = runner.snapshot_for(source, "SZ.000729", "5m")
    assert replay == {k: v for k, v in original.items() if k != "chart"}
    assert views.PENDING not in replay


@pytest.mark.parametrize('eager', [False, True])
def test_live_and_deferred_paths_publish_identical_frozen_evidence(source, tmp_path, eager):
    original = runner.snapshot_for(source, 'SZ.000729', '5m')
    with views.presentation_scope():
        with views.defer_presentation(tmp_path/'cache', 'fixture', enabled=True, eager=eager):
            snapshot = runner.snapshot_for(source, 'SZ.000729', '5m')
        views.materialize(snapshot, source, 'SZ.000729', '5m')
    assert snapshot == original
    assert json.dumps(snapshot, ensure_ascii=False) == json.dumps(original, ensure_ascii=False)


def test_eager_live_presentation_cannot_enter_independent_replay(source, tmp_path):
    with views.defer_presentation(tmp_path/'cache', 'fixture', enabled=True, eager=True):
        with views.facts_only():
            replay = runner.snapshot_for(source, 'SZ.000729', '5m')
    assert 'chart' not in replay and views.PENDING not in replay


def test_different_structure_cannot_be_attached_to_frozen_facts(source, tmp_path):
    with views.presentation_scope():
        with views.defer_presentation(None, None, enabled=False):
            compact = runner.snapshot_for(source, "SZ.000729", "5m")
        compact["snapshot_revision"] = "not-the-same-evidence"
        with pytest.raises(ValueError, match="Structure changed"):
            views.materialize(compact, source, "SZ.000729", "5m")


@pytest.mark.parametrize("changed", [False, True])
def test_json_cache_round_trip_keeps_equivalent_view_members_but_rejects_a_changed_price(
    monkeypatch, changed
):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from chanlun.screening import runtime_cache
    from chanlun.cl_utils import tv_chart

    original = {
        "levels": [{"points": [{"anchor_tick": 100}]}],
        "center_view_selections": [{"original_core_unit_ids": ("a", "b", "c")}],
    }
    cached = json.loads(json.dumps(original))
    cached[views.PENDING] = True
    if changed:
        cached["levels"][0]["points"][0]["anchor_tick"] = 101

    @contextmanager
    def runtime(*args):
        yield SimpleNamespace(cd=object())

    monkeypatch.setattr(runtime_cache, "runtime_for_snapshot", runtime)
    monkeypatch.setattr(
        tv_chart,
        "cl_data_to_tv_chart",
        lambda *a, **k: {
            "strict_structure_mode": "replace",
            "strict_structure": original,
            "fxs": [],
            "bis": [],
            "xds": [],
        },
    )
    if changed:
        with pytest.raises(ValueError, match="Structure changed"):
            views.materialize(cached, SimpleNamespace(attrs={}), "A", "30m")
    else:
        views.materialize(cached, SimpleNamespace(attrs={}), "A", "30m")
        assert "chart" in cached and views.PENDING not in cached
