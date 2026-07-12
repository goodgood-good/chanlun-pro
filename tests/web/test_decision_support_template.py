from __future__ import annotations

from pathlib import Path
import re


INDEX_PATH = Path("web/chanlun_chart/cl_app/templates/index.html")
CSS_PATH = Path("web/chanlun_chart/cl_app/static/css/app.css")
REQUIRED_DS_IDS = (
    "decision_support_panel",
    "ds_track_toggle",
    "ds_candidate_list",
    "ds_plan_view",
    "ds_evidence_view",
    "ds_risk_view",
    "ds_paper_view",
    "ds_status",
)


def _index_html() -> str:
    return INDEX_PATH.read_text(encoding="utf-8")


def test_index_contains_accessible_decision_support_workbench() -> None:
    html = _index_html()

    for label in ("候选", "计划", "证据", "风控", "模拟盘"):
        assert label in html
    for element_id in REQUIRED_DS_IDS:
        assert f'id="{element_id}"' in html
    assert html.index('id="chart_menu"') < html.index('id="decision_support_panel"')
    assert re.search(r'id="ds_status"[^>]*aria-live="polite"', html)
    assert re.search(r'id="ds_track_toggle"[^>]*role="group"', html)
    assert len(re.findall(r'class="ds-track-button"[^>]*aria-pressed=', html)) == 2


def test_index_has_five_button_tabs_with_linked_panels() -> None:
    html = _index_html()

    assert len(re.findall(r'class="ds-tab-button[^\"]*"[^>]*role="tab"', html)) == 5
    for panel_id in (
        "ds_candidate_list",
        "ds_plan_view",
        "ds_evidence_view",
        "ds_risk_view",
        "ds_paper_view",
    ):
        assert f'aria-controls="{panel_id}"' in html
        assert re.search(
            rf'id="{panel_id}"[^>]*role="tabpanel"',
            html,
        )


def test_index_has_no_automatic_order_control() -> None:
    html = _index_html().lower()

    assert "自动下单" not in html
    assert "ds_place_order" not in html
    assert "place_order" not in html
    assert "broker_order" not in html


def test_decision_support_css_is_scoped_fluid_and_keyboard_accessible() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    candidate_rule = re.search(r"\.ds-candidate-button\s*\{(?P<body>[^}]*)\}", css)
    facts_rule = re.search(r"\.ds-facts\s*\{(?P<body>[^}]*)\}", css)
    fact_row_rule = re.search(r"\.ds-fact-row\s*\{(?P<body>[^}]*)\}", css)

    assert candidate_rule is not None
    assert facts_rule is not None
    assert fact_row_rule is not None
    assert re.search(r"display\s*:\s*grid\s*;", facts_rule.group("body"))
    assert re.search(r"display\s*:\s*flex\s*;", fact_row_rule.group("body"))
    assert re.search(r"\.ds-fact-row\s+dd\s*\{[^}]*margin\s*:\s*0\s*;", css)
    assert re.search(r"min-width\s*:\s*0\s*;", css)
    assert re.search(r"overflow-wrap\s*:\s*anywhere\s*;", css)
    assert re.search(r"overflow-y\s*:\s*auto\s*;", css)
    assert re.search(r"grid-template-columns\s*:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\)", css)
    assert "@container" in css
    assert ":focus-visible" in css
    assert not re.search(r"(?:^|\s)width\s*:\s*\d+px\s*;", candidate_rule.group("body"))
    assert "gradient(" not in css
    assert "letter-spacing: -" not in css
