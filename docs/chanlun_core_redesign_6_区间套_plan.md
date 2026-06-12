# P6 区间套（自顶向下 READ · 标可操作性）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 消费 P4c 嵌套背驰森林，自顶向下 DFS 给每个节点标区间套属性（depth/is_innermost/is_nested/operable），完成宪法 §7 BUILD+READ 闭环。

**Architecture:** 新建孤立 `interval_nest.py`，无状态 `IntervalNestCalculator.calculate(forest)→List[NestRead]`；DFS 顶层根 depth=1、往内 +1；operable=最内层&被套住。不接 CL、不改上游。

**Tech Stack:** Python 3、dataclass、pytest、poetry、ruff、plotly（验收）。

设计见 `docs/chanlun_core_redesign_6_区间套_design.md`。

---

## File Structure

- **Create:** `src/chanlun/core/interval_nest.py` — `NestRead` + `IntervalNestCalculator.calculate`(DFS)。
- **Test:** `tests/core/test_interval_nest.py` — 受控 `NestedDivergence` 森林（divergence 占位 None，只测 children 结构）。
- **Probe（gitignored）:** `scripts_local/probe_p6_nest.py` — 真实数据负向 + 受控 3 级嵌套演示标 operable 出图。
- **不改:** `beichi_nest.py`/CL/旧 `recursive_calculator`。

---

## Task 1: `NestRead` + `IntervalNestCalculator.calculate`（DFS 标属性）+ 全套回归

**Files:**
- Create: `src/chanlun/core/interval_nest.py`
- Test: `tests/core/test_interval_nest.py`

- [ ] **Step 1: 写失败测试**（`tests/core/test_interval_nest.py`）

```python
"""tests/core/test_interval_nest.py — P6 区间套 TDD。

受控 NestedDivergence 森林（divergence 占位 None：P6 calculate 只读 children 结构、
不读 divergence 内容）。
"""
from __future__ import annotations

from chanlun.core.beichi_nest import NestedDivergence
from chanlun.core.interval_nest import NestRead, IntervalNestCalculator


def _node(level, children=None) -> NestedDivergence:
    return NestedDivergence(level=level, zs_index=0, divergence=None,
                            children=children if children is not None else [])


def _by_node(reads):
    return {id(r.node): r for r in reads}


def test_calculate_empty_returns_empty():
    assert IntervalNestCalculator().calculate([]) == []


def test_three_level_chain():
    # L2 → L1 → L0：L0 最内层+被套=可操作；L1/L2 非最内层=不可操作
    l0 = _node(0)
    l1 = _node(1, [l0])
    l2 = _node(2, [l1])
    reads = IntervalNestCalculator().calculate([l2])
    assert len(reads) == 3
    by = _by_node(reads)
    assert (by[id(l0)].depth, by[id(l0)].is_innermost, by[id(l0)].is_nested, by[id(l0)].operable) == (3, True, True, True)
    assert (by[id(l1)].depth, by[id(l1)].is_innermost, by[id(l1)].is_nested, by[id(l1)].operable) == (2, False, True, False)
    assert (by[id(l2)].depth, by[id(l2)].is_innermost, by[id(l2)].is_nested, by[id(l2)].operable) == (1, False, False, False)


def test_isolated_single_node_not_operable():
    # 孤立单节点:最内层但没被套 → 不可操作(§7.1 孤立背驰无意义)
    n = _node(0)
    reads = IntervalNestCalculator().calculate([n])
    assert len(reads) == 1
    r = reads[0]
    assert (r.depth, r.is_innermost, r.is_nested, r.operable) == (1, True, False, False)


def test_same_parent_multiple_children():
    # L1 含 2 个 L0 叶 → 两 L0 都可操作;L1 非最内层
    a, b = _node(0), _node(0)
    l1 = _node(1, [a, b])
    reads = IntervalNestCalculator().calculate([l1])
    by = _by_node(reads)
    assert by[id(a)].operable and by[id(b)].operable
    assert by[id(a)].depth == 2 and by[id(b)].depth == 2
    assert not by[id(l1)].is_innermost and by[id(l1)].depth == 1 and not by[id(l1)].operable


def test_multiple_trees_independent():
    # 一棵 3 级链 + 一个孤立根 → 标注互不干扰
    l0 = _node(0)
    l1 = _node(1, [l0])
    l2 = _node(2, [l1])
    iso = _node(0)
    reads = IntervalNestCalculator().calculate([l2, iso])
    assert len(reads) == 4
    by = _by_node(reads)
    assert by[id(l0)].operable                 # 链最内层可操作
    assert not by[id(iso)].operable            # 孤立不可操作
    assert by[id(iso)].depth == 1 and by[id(iso)].is_innermost
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_interval_nest.py -q`
Expected: FAIL（`ModuleNotFoundError: interval_nest`）

- [ ] **Step 3: 写实现**（`src/chanlun/core/interval_nest.py`）

```python
"""interval_nest.py — P6 缠论区间套（自顶向下 READ · 标可操作性）。

消费 beichi_nest 的嵌套背驰森林，自顶向下 DFS 给每个节点标区间套属性
（depth/is_innermost/is_nested/operable）。可操作 ⟺ 嵌套链最内层 + 被逐级套住
（宪法 §7.1 第27课 / §7.2 双向闭环 READ / §7.3 强度标注）。深度门限的可操作性
策略交策略层。孤立、不接 CL、不改上游、不动旧 recursive_calculator。
设计见 docs/chanlun_core_redesign_6_区间套_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from chanlun.core.beichi_nest import NestedDivergence


@dataclass
class NestRead:
    """一个森林节点(=某级一类点/背驰段)的区间套 READ 标注。"""
    node: NestedDivergence     # 森林节点(其 .divergence 可回溯对应买卖点)
    depth: int                 # 嵌套链层级(顶层根=1,每下一层 +1)
    is_innermost: bool         # 无 children = 最内层(最低级别背驰)
    is_nested: bool            # depth>1 = 被更高级别套住(有祖先)
    operable: bool             # is_innermost & is_nested = 结构可操作信号


class IntervalNestCalculator:
    """区间套计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, forest: List[NestedDivergence]) -> List[NestRead]:
        """森林每个节点标区间套属性。顶层根 depth=1,逐级向内 +1。"""
        out: List[NestRead] = []

        def _dfs(node: NestedDivergence, depth: int) -> None:
            innermost = not node.children                   # 无子 = 最内层
            nested = depth > 1                              # 有祖先 = 被套住
            out.append(NestRead(
                node=node, depth=depth, is_innermost=innermost,
                is_nested=nested, operable=innermost and nested,
            ))
            for ch in node.children:
                _dfs(ch, depth + 1)

        for root in forest:
            _dfs(root, 1)
        return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_interval_nest.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 全套回归 + ruff**

Run: `poetry run pytest tests/core/ -q`
Expected: PASS（既有 276 + 新增 5 → 全绿，零回归）

Run: `poetry run ruff check src/chanlun/core/interval_nest.py tests/core/test_interval_nest.py`
Expected: All checks passed!

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/interval_nest.py tests/core/test_interval_nest.py
git commit -m "feat(core/interval_nest): NestRead + IntervalNestCalculator(DFS 标嵌套深度/最内层/可操作)(P6)"
```

---

## Task 2: 真实数据 + 受控演示验收

**Files:**
- Create（gitignored）: `scripts_local/probe_p6_nest.py`
- Modify: `.gitignore`（加 `interval_nest_review.html`）

- [ ] **Step 1: 写 probe 脚本**（`scripts_local/probe_p6_nest.py`）

```python
# scripts_local/probe_p6_nest.py — P6 区间套真实数据+受控演示验收(本地, gitignored)
import logging
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld, CLKline, FX, XD
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.recursive_branch import RecursiveBranchCalculator, LevelResult
from chanlun.core.beichi_nest import BeichiNestCalculator
from chanlun.core.interval_nest import IntervalNestCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

# 真实数据(负向:森林孤立 → 全 operable=False)
levels = RecursiveBranchCalculator().calculate(bis, ld, "zs_wzgx_zgd", frequency="1m")
forest = BeichiNestCalculator().calculate(levels)
reads = IntervalNestCalculator().calculate(forest)
print("=== 真实数据 ===")
print(f"森林顶层={len(forest)} 节点={len(reads)} 可操作={sum(r.operable for r in reads)}")
for r in reads:
    print(f"  L{r.node.level} depth={r.depth} innermost={r.is_innermost} nested={r.is_nested} operable={r.operable}")


# 受控演示(3 级嵌套 → 展示可操作判定)
def _demo_dv(_type, s_k, e_k):
    def _fx(kidx, ftype):
        k = CLKline(k_index=kidx, date=None, h=0.0, l=0.0, o=0.0, c=0.0, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=0.0)
    if _type == "up":
        st, en = _fx(s_k, "di"), _fx(e_k, "ding")
    else:
        st, en = _fx(s_k, "ding"), _fx(e_k, "di")
    xd = XD(start=st, end=en, _type=_type, index=0); xd.done = True
    return DivergenceResult(is_beichi=True, kind="qs", compare_seg=xd, leave_seg=xd, provisional=False)


def _demo_lr(level, dvs):
    return LevelResult(level=level, zss=[], done_divergence=list(dvs), zslxs=[], upgrade_idx=[])


demo_levels = [
    _demo_lr(0, [_demo_dv("up", 10, 20), _demo_dv("up", 30, 40), _demo_dv("down", 60, 70)]),
    _demo_lr(1, [_demo_dv("up", 8, 45), _demo_dv("down", 55, 75)]),
    _demo_lr(2, [_demo_dv("up", 5, 50)]),
]
demo_forest = BeichiNestCalculator().calculate(demo_levels)
demo_reads = IntervalNestCalculator().calculate(demo_forest)
print("=== 受控演示 ===")
print(f"森林顶层={len(demo_forest)} 节点={len(demo_reads)} 可操作={sum(r.operable for r in demo_reads)}")
for r in demo_reads:
    sg = r.node.divergence.leave_seg
    print(f"  L{r.node.level} {sg._type}[{sg.start.k.k_index},{sg.end.k.k_index}] "
          f"depth={r.depth} innermost={r.is_innermost} operable={r.operable}")

# 出图:受控演示嵌套森林 + operable ★ 高亮
fig = go.Figure()
color = {"up": "#d62728", "down": "#2ca02c"}
read_by = {id(r.node): r for r in demo_reads}


def _draw(node):
    sg = node.divergence.leave_seg
    s_k, e_k = sg.start.k.k_index, sg.end.k.k_index
    y = node.level
    r = read_by[id(node)]
    fig.add_trace(go.Scatter(x=[s_k, e_k], y=[y, y], mode="lines",
                             line=dict(color=color[sg._type], width=10), opacity=0.55, showlegend=False,
                             hovertext=f"L{node.level} {sg._type} depth={r.depth} operable={r.operable}"))
    if r.operable:
        fig.add_trace(go.Scatter(x=[(s_k + e_k) / 2], y=[y], mode="markers",
                                 marker=dict(symbol="star", color="gold", size=18,
                                             line=dict(width=1, color="black")),
                                 showlegend=False, hovertext=f"可操作 depth={r.depth}"))
    for ch in node.children:
        cs, ce = ch.divergence.leave_seg.start.k.k_index, ch.divergence.leave_seg.end.k.k_index
        fig.add_trace(go.Scatter(x=[(s_k + e_k) / 2, (cs + ce) / 2], y=[y, ch.level],
                                 mode="lines", line=dict(color="#888", width=1.5, dash="dot"), showlegend=False))
        _draw(ch)


for root in demo_forest:
    _draw(root)
fig.update_layout(title="P6 区间套(受控演示:★=可操作=嵌套链最内层+被套住;红up/绿down背驰)",
                  yaxis=dict(title="级别 L", dtick=1), xaxis=dict(title="K线序号(受控)"), height=520)
fig.write_html("interval_nest_review.html")
print("written interval_nest_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p6_nest.py`
Expected: 真实数据全 `operable=False`（森林孤立的负向验证）；受控演示 3 级链最内层 L0 `operable=True`、L1/L2 False；生成 `interval_nest_review.html`。

- [ ] **Step 3: gitignore 审阅图**（`.gitignore` 在 `bs_branch_review.html` 行后加）

```
bs_branch_review.html
interval_nest_review.html
```

```bash
git add .gitignore
git commit -m "chore: gitignore P6 审阅图 interval_nest_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `interval_nest_review.html` 交付用户，审：
- 受控演示：★（金星）是否只落在**嵌套链最内层**节点（L0 叶、且 depth>1）；高级别节点（L1/L2）和孤立 down 链最内层（L0 depth=2 被套→也该可操作）是否标对。
- 真实数据负向：森林孤立 → 全 operable=False，符合「无嵌套链 → 无可操作信号」。
- **不通过 → 诊断口径（depth 计算/operable 判据），按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2 模块接口→Task1；§3 DFS 算法→Task1；§4 口径(depth/innermost/nested/operable)→Task1 测试；§5 测试+验收→Task1/2。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`NestRead(node/depth/is_innermost/is_nested/operable)`、`calculate(forest)→List[NestRead]`、`_node` helper 与 `NestedDivergence` 构造一致（divergence=None 占位，P6 不读其内容）。
- **执行注意**：测试 `NestedDivergence.divergence=None` 占位合法（P6 calculate 只读 children）；probe 用 `PYTHONPATH=src`。
