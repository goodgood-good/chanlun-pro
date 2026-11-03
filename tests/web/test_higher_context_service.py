from types import SimpleNamespace

from cl_app.services import higher_context


def test_empty_period_context_starts_no_worker_or_files(tmp_path):
    existing = set(tmp_path.iterdir())
    manager = SimpleNamespace(root=tmp_path)
    assert higher_context.snapshot(manager, "run", "revision") == {
        "schema":"native-period-link-v1","status": "not_started", "completed": 0, "checked": 0, "total": 0, "rows": {},
    }
    assert higher_context.status_version(manager, "run").startswith('native-period-link-v1:run:')
    assert higher_context._service(manager).executor is None
    assert set(tmp_path.iterdir()) == existing
