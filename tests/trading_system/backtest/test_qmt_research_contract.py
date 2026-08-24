from tools import qmt_research_contract


def test_symbol_fact_algorithm_boundary_is_strict_dependency_subset() -> None:
    full = dict(qmt_research_contract.algorithm_hashes())
    fact = dict(qmt_research_contract.fact_algorithm_hashes())

    assert set(fact) < set(full)
    assert (
        "src/chanlun/decision_support/trading_system/backtest/fixed_year.py" in fact
    )
    assert "src/chanlun/core/strict_structure/recursive_engine.py" in fact
    assert "tools/backtest_qmt_fixed_year.py#symbol_fact_worker" in fact
    assert "tools/backtest_qmt_fixed_year.py" not in fact
    assert "tools/backtest_qmt_fixed_year.py" in full

    assert "src/chanlun/decision_support/trading_system/live_human_review.py" not in fact
    assert "src/chanlun/decision_support/trading_system/backtest/report.py" not in fact
    assert "tools/finalize_qmt_pit_fixed_year.py" not in fact
