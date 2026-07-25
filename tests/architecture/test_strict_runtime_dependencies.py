from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = (
    ROOT / "src/chanlun/decision_support",
    ROOT / "web/chanlun_chart/cl_app/services",
)
FORBIDDEN_IMPORTS = (
    "chanlun.recursive_bt",
    "chanlun.signal_monitor",
    "chanlun.xuangu",
)


def test_active_runtime_has_no_legacy_package_imports() -> None:
    offenders = []
    for root in RUNTIME_ROOTS:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_IMPORTS:
                if forbidden in source:
                    offenders.append(f"{path.relative_to(ROOT)}:{forbidden}")
    assert offenders == []
