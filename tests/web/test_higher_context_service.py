from types import SimpleNamespace

from cl_app.services import higher_context


def test_retired_recursive_background_pass_starts_no_worker_or_files(tmp_path):
    manager = SimpleNamespace(root=tmp_path)
    assert higher_context.ensure(manager, {"status": "completed"}) is None
    assert higher_context.snapshot(manager, "run", "revision") == {
        "status": "disabled", "completed": 0, "total": 0, "rows": {},
    }
    assert higher_context.status_version(manager, "run") == "disabled"
    assert not (tmp_path / "run" / "higher_context").exists()
