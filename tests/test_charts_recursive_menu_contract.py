from __future__ import annotations

import pathlib


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHARTS_JS = (
    _REPO_ROOT / "web" / "chanlun_chart" / "cl_app" / "static" / "js" / "charts.js"
)


def test_recursive_signal_menu_toggles_are_registered():
    src = _CHARTS_JS.read_text(encoding="utf-8")

    assert "..._mmdLevels.slice(1).map((L) => L.key)" in src
    assert "..._bcLevels.slice(1).map((L) => L.key)" in src
    assert "mmd_L' + _lv" in src
    assert "bc_L' + _lv" in src
