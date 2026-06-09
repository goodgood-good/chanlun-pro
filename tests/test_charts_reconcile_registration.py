"""tests/test_charts_reconcile_registration.py — charts.js reconcile 容器注册一致性。

回归 bug(2026-06-09):commit 4c8785f 新增了
``reconcile('recursive_mmds')`` / ``reconcile('recursive_mmd_labels')`` /
``reconcile('recursive_bcs')`` 三个调用(画 5m/30m 级别买卖点/背驰),
却忘了把这三个 type 注册进 ``CHART_CONFIG.CHART_TYPES``。

后果:``getOrInitSymbolData`` 只为 CHART_TYPES 里的 type 建空数组
(``obj_charts[symbolKey][type] = []``),未注册的 type 在 ``reconcile`` 里
``const container = this.obj_charts[symbolKey][type]`` 取到 ``undefined``,
紧接着 ``container.length`` 抛 TypeError → 整批 5m/30m 买卖点/背驰**永不渲染**
(而 recursive_zss 中枢已注册 → 中枢框能显示,买卖点/背驰不显示,症状吻合)。

本测试是纯静态解析(无 playwright 依赖):扫描 charts.js,断言每个
``this.reconcile('X', ...)`` 的目标 X 都在 CHART_TYPES 注册表里。
覆盖整个 bug 类,未来再漏注册会立即红。
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHARTS_JS = (
    _REPO_ROOT / "web" / "chanlun_chart" / "cl_app" / "static" / "js" / "charts.js"
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def _strip_line_comments(text: str) -> str:
    """去掉 // 行注释(避免把注释里的引号串当成数组项)。不处理块注释,本块无。"""
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())


def _extract_chart_types(src: str) -> set[str]:
    # 先去 // 注释再框数组:否则注释里若含 ] (如 recursive_levels[].mmds)会让
    # 非贪婪的 [...] 匹配提前截断,漏掉真正数组项。
    src = _strip_line_comments(src)
    m = re.search(r"CHART_TYPES:\s*\[(.*?)\]", src, re.DOTALL)
    assert m, "charts.js 里找不到 CHART_TYPES: [...] 定义"
    return set(re.findall(rf"['\"]({_IDENT})['\"]", m.group(1)))


def _extract_reconcile_targets(src: str) -> set[str]:
    # this.reconcile('type', ...) / .reconcile("type", ...)
    return set(re.findall(rf"\.reconcile\(\s*['\"]({_IDENT})['\"]", src))


def test_every_reconcile_target_is_registered_in_chart_types():
    src = _CHARTS_JS.read_text(encoding="utf-8")
    chart_types = _extract_chart_types(src)
    targets = _extract_reconcile_targets(src)

    assert targets, "没解析到任何 reconcile 目标,正则或文件异常"
    missing = sorted(targets - chart_types)
    assert not missing, (
        "以下 reconcile 容器未注册进 CHART_CONFIG.CHART_TYPES,"
        "会导致 obj_charts[symbolKey][type]=undefined → container.length 抛错 → 永不渲染: "
        f"{missing}。请把它们加进 charts.js 的 CHART_TYPES 数组。"
    )


def test_recursive_signal_containers_present():
    """显式钉死这次 bug 的三个容器,文档化用户「看不到 5m/30m 买卖点/背驰」的修复。"""
    chart_types = _extract_chart_types(_CHARTS_JS.read_text(encoding="utf-8"))
    for need in ("recursive_mmds", "recursive_mmd_labels", "recursive_bcs"):
        assert need in chart_types, f"{need} 必须注册进 CHART_TYPES(5m/30m 买卖点/背驰渲染依赖它)"
