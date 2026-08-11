from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = (
    ROOT / "src/chanlun/decision_support/trading_system",
    ROOT / "web/chanlun_chart/cl_app/services",
)
FORBIDDEN_IMPORTS = (
    "chanlun.recursive_bt",
    "chanlun.signal_monitor",
    "chanlun.strategy",
    "chanlun.xuangu.xuangu",
)
REMOVED_SIGNAL_PATHS = (
    "src/chanlun/monitor.py",
    "src/chanlun/signal_monitor",
    "src/chanlun/recursive_bt/monitor",
    "src/chanlun/strategy",
    "src/chanlun/xuangu/xuangu.py",
    "src/chanlun/xuangu/xuangu_by_same.py",
    "web/chanlun_chart/cl_app/alert_tasks.py",
    "web/chanlun_chart/cl_app/blueprints/alert.py",
    "web/chanlun_chart/cl_app/templates/alert.html",
    "web/chanlun_chart/cl_app/static/js/alert.js",
    "script/crontab/xuangu_by_process.py",
    "script/crontab/xuangu_by_same.py",
    "script/crontab/run_history_xuangu.py",
    "script/trader/reboot_trader_a_stock.py",
    "script/trader/reboot_trader_currency.py",
    "script/trader/reboot_trader_ctp.py",
    "script/trader/reboot_trader_futures.py",
    "script/trader/reboot_trader_hk_stock.py",
)
REMOVED_SECTOR_PIPELINE_PATHS = (
    "src/chanlun/decision_support/tdx_industry_sectors.py",
    "src/chanlun/decision_support/trading_system/backtest/data_source.py",
    "src/chanlun/decision_support/trading_system/backtest/runner.py",
    "tools/backtest_chanlun_trading_system.py",
)


def test_active_runtime_has_no_removed_package_imports() -> None:
    offenders = []
    paths = [
        *(path for root in RUNTIME_ROOTS for path in root.rglob("*.py")),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORTS:
            if forbidden in source:
                offenders.append(f"{path.relative_to(ROOT)}:{forbidden}")
    assert offenders == []

def test_removed_signal_authorities_are_physically_absent() -> None:
    remaining = []
    for relative in REMOVED_SIGNAL_PATHS:
        path = ROOT / relative
        if path.is_dir():
            if any(path.rglob("*.py")):
                remaining.append(relative)
        elif path.exists():
            remaining.append(relative)
    assert remaining == []


def test_removed_tdx_sector_pipeline_is_physically_absent() -> None:
    assert [
        relative
        for relative in REMOVED_SECTOR_PIPELINE_PATHS
        if (ROOT / relative).exists()
    ] == []
    forbidden = (
        "tdx_880_industry_index",
        "tdx_native_880_index",
        "tdx-industry-index",
        "tdx-industry:",
        "resolve_tdx_industry_index_quantum",
        "build_tdx_industry_price_basis_metadata",
    )
    offenders = []
    roots = (
        ROOT / "src/chanlun",
        ROOT / "web/chanlun_chart/cl_app",
        ROOT / "tools",
    )
    for path in (path for root in roots for path in root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")
    assert offenders == []


def test_web_application_has_no_removed_signal_calls_or_switches() -> None:
    forbidden = (
        "register_recursive_monitor_jobs",
        "register_signal_jobs",
        "_migrate_signal_document",
        "_migrate_sector_coverage_snapshot",
        ".mmd_exists(",
        ".bc_exists(",
        "collect_branch_signals(",
    )
    offenders = []
    for path in (ROOT / "web/chanlun_chart/cl_app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")
    assert offenders == []


def test_current_runtime_has_no_reduced_ranking_or_unbound_mutation_profile() -> None:
    forbidden = (
        "HISTORICAL_TRIGGER_SUMMARY",
        "LIVE_FULL_RANKING",
        "sector_ranking_source_profile",
        "_UnboundMutationScope",
        "standalone-compatible",
        "mutation_fence_bind_during_unbound_scope",
    )
    offenders = []
    roots = (
        ROOT / "src/chanlun",
        ROOT / "web/chanlun_chart/cl_app",
        ROOT / "tools",
    )
    for path in (path for root in roots for path in root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")
    assert offenders == []


def test_production_cl_calls_bind_market_explicitly() -> None:
    offenders = []
    roots = (
        ROOT / "src/chanlun",
        ROOT / "web/chanlun_chart/cl_app",
        ROOT / "tools",
    )
    for path in (path for root in roots for path in root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if name != "CL":
                continue
            market_keyword = any(
                keyword.arg == "market" for keyword in node.keywords
            )
            if len(node.args) < 5 and not market_keyword:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
