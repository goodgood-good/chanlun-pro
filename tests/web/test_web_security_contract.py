from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_tv_state_writes_are_not_csrf_exempt():
    source = (ROOT / "web/chanlun_chart/cl_app/blueprints/tv.py").read_text(
        encoding="utf-8"
    )
    assert "@csrf.exempt" not in source


def test_removed_alert_surface_is_absent():
    for relative in (
        "web/chanlun_chart/cl_app/blueprints/alert.py",
        "web/chanlun_chart/cl_app/alert_tasks.py",
        "web/chanlun_chart/cl_app/templates/alert.html",
        "web/chanlun_chart/cl_app/static/js/alert.js",
    ):
        assert not (ROOT / relative).exists()


def test_secret_form_is_not_logged_to_console():
    source = (ROOT / "web/chanlun_chart/cl_app/templates/setting.html").read_text(
        encoding="utf-8"
    )
    assert "console.log(data.field)" not in source


def test_all_inline_scripts_have_nonce_and_no_inline_event_handlers():
    templates = ROOT / "web/chanlun_chart/cl_app/templates"
    for template in templates.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        for attrs in re.findall(r"<script\b([^>]*)>", source, flags=re.IGNORECASE):
            if re.search(r"\bsrc\s*=", attrs, flags=re.IGNORECASE):
                continue
            assert re.search(r"\bnonce\s*=", attrs, flags=re.IGNORECASE), template
        assert not re.search(
            r"\son(?:click|submit|load|error|change|input|mouseover)\s*=",
            source,
            flags=re.IGNORECASE,
        ), template


def test_chart_uses_same_origin_iframe_under_nonce_csp():
    source = (ROOT / "web/chanlun_chart/cl_app/static/js/charts.js").read_text(
        encoding="utf-8"
    )
    enabled = re.search(
        r"CHART_ENABLED_FEATURES\s*=\s*Object\.freeze\(\[([^\]]+)\]\)",
        source,
    )
    assert enabled is not None
    assert '"iframe_loading_same_origin"' in enabled.group(1)
    assert "enabled_features: viewportOptions.enabledFeatures" in source
    assert 'location.assign("/?market="' in source


def test_xuangu_memos_are_rendered_as_text_and_copy_matches_atomic_publish():
    source = (ROOT / "web/chanlun_chart/cl_app/templates/xuangu_list.html").read_text(
        encoding="utf-8"
    )
    assert ".html(task_infos" not in source
    assert source.count(".text(task_infos") == 4
    assert "失败时保留原组" in source
