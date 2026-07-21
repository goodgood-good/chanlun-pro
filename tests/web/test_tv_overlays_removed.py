from __future__ import annotations

from pathlib import Path

from cl_app import create_app


def test_legacy_tv_overlays_route_is_removed() -> None:
    app = create_app()
    app.config.update(TESTING=True, LOGIN_DISABLED=True)

    response = app.test_client().get(
        "/tv/overlays?symbol=a:SH.600519&resolution=5"
    )

    assert response.status_code == 404


def test_legacy_overlay_serializers_are_absent_from_source() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (
        root / "web/chanlun_chart/cl_app/blueprints/tv.py"
    ).read_text(encoding="utf-8")

    assert "def tv_overlays" not in source
    assert "_serialize_overlay_zss" not in source
    assert "_serialize_overlay_zslx" not in source
