from pathlib import Path


def test_index_exposes_csrf_protected_logout_command():
    source = (
        Path(__file__).resolve().parents[2]
        / "web"
        / "chanlun_chart"
        / "cl_app"
        / "templates"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="logout-form"' in source
    assert 'action="/logout"' in source
    assert 'name="csrf_token"' in source
    assert 'id: "logout"' in source
    assert 'document.getElementById("logout-form").submit()' in source