import json
from pathlib import Path


REPORT = Path("audit/chanlun_live_integration/v31_backtest.json")
STRUCTURE = Path(
    "audit/chanlun_live_integration/v31_independent_timeframe_structure.json"
)


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v31_artifact_never_presents_cash_hold_as_full_system_return() -> None:
    report = load(REPORT)
    assert report["result_scope"] == "COMPONENT_ONLY_BLOCKED_BEFORE_ENTRY"
    assert report["full_v31_performance"]["total_return"] == "NOT_EVALUATED"
    assert report["full_v31_performance"]["maximum_drawdown"] == "NOT_EVALUATED"
    assert report["cash_only_component_observation"]["total_return"] == 0.0
    assert "not a compliant full-V3.1" in report[
        "cash_only_component_observation"
    ]["interpretation"]


def test_v31_artifact_carries_the_frozen_structure_block_and_live_disable() -> None:
    report = load(REPORT)
    structure = load(STRUCTURE)
    assert structure["decision"] == "BLOCKED_BY_FROZEN_STRUCTURE"
    assert report["first_failed_gate"]["status"] == "BLOCKED_BY_FROZEN_STRUCTURE"
    assert report["frozen_structure"]["status"] == "PASS_ZERO_CHANGE"
    assert report["live_status"] == "LIVE_DISABLED"
    assert report["trade_counts"]["orders"] == 0
    assert report["trade_counts"]["fills"] == 0


def test_v31_parameter_paths_are_distinct_in_the_final_artifact() -> None:
    report = load(REPORT)
    snapshots = report["parameter_manifest"]["snapshots"]
    assert snapshots["ETF_PROXY"]["parameter_set_id"] != snapshots[
        "INDIVIDUAL_THREE_PROGRAM"
    ]["parameter_set_id"]
    assert report["selection_path_evaluated"] == "ETF_PROXY"
