import json
from types import SimpleNamespace

from chanlun.screening.higher_context import SCHEMA
from cl_app.services import higher_context


def test_snapshot_requires_matching_run_revision_and_committed_lines(tmp_path):
    run_id, revision = "a" * 32, "revision-1"
    manager = SimpleNamespace(root=tmp_path)
    directory = tmp_path / run_id / "higher_context"
    directory.mkdir(parents=True)
    request = {"schema": SCHEMA, "run_id": run_id, "source_revision": revision,
               "candidates": [{"market": "a", "code": "SH.600000", "source_closed_at": 100}]}
    status = {"run_id": run_id, "source_revision": revision, "status": "running", "completed": 1, "total": 1}
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    row = {"market": "a", "code": "SH.600000", "source_closed_at": 100, "category": "up_segment"}
    other = {**row, "code": "SH.600001"}
    (directory / "rows.jsonl").write_text(json.dumps(row)+"\n"+json.dumps(other)+"\n", encoding="utf-8")
    assert higher_context.snapshot(manager, run_id, revision)["rows"] == {("a", "SH.600000", 100): row}
    assert higher_context.snapshot(manager, run_id, "revision-2")["rows"] == {}


def test_launch_only_confirmed_5m_symbols_and_only_once(tmp_path, monkeypatch):
    run_id, revision = "b" * 32, "revision-1"
    rows = [
        {"market": "a", "code": "SH.600000", "frequency": "5m", "source_closed_at": 100,
         "point": {"status": "confirmed"}},
        {"market": "a", "code": "SH.600001", "frequency": "5m", "source_closed_at": 100,
         "point": {"status": "approaching"}},
        {"market": "a", "code": "SH.600002", "frequency": "1m", "source_closed_at": 100,
         "point": {"status": "confirmed"}},
    ]
    result = {"run_id": run_id, "source_revision": revision, "status": "completed",
              "selected": rows, "observations": []}
    manager = SimpleNamespace(root=tmp_path, results=lambda: result)
    manual = {"run_id": run_id, "source_revision": revision,
              "status": "completed", "source_current": True}
    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(higher_context.subprocess, "Popen", launch)
    higher_context.ensure(manager, manual)
    higher_context.ensure(manager, manual)
    directory = tmp_path / run_id / "higher_context"
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    assert request["candidates"] == [{"market": "a", "code": "SH.600000", "source_closed_at": 100}]
    assert len(launches) == 1
