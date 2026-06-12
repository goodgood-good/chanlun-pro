import json
from pathlib import Path

import scripts.audit_original_trading_system_matrix as matrix


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_original_trading_system_matrix_marks_remaining_system_gaps(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    reports = tmp_path / "reports"
    monkeypatch.setattr(matrix, "ROOT", root)
    monkeypatch.setattr(matrix, "REPORT_DIR", reports)

    _write(
        root / "src/chanlun/core/cl.py",
        '"1m": [("5m", "kuozhan"), ("30m", "tongjibie")]\n'
        '"5m": [("30m", "tongjibie")]\n',
    )
    _write(root / "src/chanlun/core/zs_upgrade.py", "def tongjibie_zhongshu_ex(): pass\n")
    _write(
        root / "src/chanlun/recursive_bt/engine.py",
        "def _structural_signal_fields(): pass\n"
        "def recommended_sell_ratio(policy='original_layered'): pass\n",
    )
    _write(
        root / "src/chanlun/recursive_bt/portfolio.py",
        "def _position_structural_invalidation(): pass\nsell_ratio_policy = 'original_layered'\n",
    )
    _write(reports / "tsla_tongjibie_candidate_audit.md", "# audit\n")
    _write(reports / "tsla_wffull_window_v7_registry_trade_invalidation_audit.md", "# audit\n")
    _write_json(
        reports / "chanlun_original_index.json",
        {"paragraph_count": 20728, "image_stats": {"count": 1061}},
    )
    _write_json(reports / "chanlun_original_logic_matrix.json", {"gap_count": 0})
    _write_json(
        reports / "us_core3_mtf3_20260601_0610_v8_registry_layered_summary.json",
        {
            "trade_count": 3,
            "no_future_policy": {
                "strict_no_future": True,
                "decision_time": "visible bar close",
                "execution_time": "next bar open",
                "signal_seen_registry_complete": True,
                "stale_reappearing_signal_risk": False,
            },
        },
    )
    _write_json(
        reports / "tsla_cascade_confirmation_audit.json",
        {
            "signals": "D:/x/us_tsla_mtf3_20260601_0610_v8_registry_layered_signals.csv",
            "min_level": 1,
            "events": [
                {
                    "event": {"bs_type": "3buy"},
                    "snapshots": [
                        {"label": "anchor_time", "matched_signal_present": False},
                        {"label": "before_visible", "matched_signal_present": False},
                        {"label": "visible_time", "matched_signal_present": True},
                    ],
                },
                {
                    "event": {"bs_type": "3sell"},
                    "snapshots": [
                        {"label": "anchor_time", "matched_signal_present": False},
                        {"label": "visible_time", "matched_signal_present": True},
                    ],
                },
            ],
        },
    )

    payload = matrix.build_matrix()
    by_id = {row["id"]: row for row in payload["rows"]}

    assert payload["status_counts"]["pass"] == 6
    assert payload["partial_count"] == 1
    assert payload["gap_count"] == 1
    assert by_id["SELECTION-THREE-SYSTEMS"]["status"] == "partial"
    assert by_id["POSITION-MULTI-LAYER"]["status"] == "pass"
    assert by_id["ROBUST-LOW-DD-HIGH-RETURN-EVIDENCE"]["status"] == "gap"
