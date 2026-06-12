# P4c 背驰贯通（自底向上 BUILD 嵌套背驰森林）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 把 `RecursiveBranchCalculator` 多级 `LevelResult` 里各级**已固化（done）背驰段**，自底向上 BUILD 成嵌套背驰森林（`NestedDivergence`），供 P6 区间套 READ。

**Architecture:** 新建孤立 `beichi_nest.py`，无状态 `BeichiNestCalculator.calculate(levels)→森林`；嵌套口径=段(`leave_seg` c)严格时间包含+同向、只相邻级别、只 done(非 provisional)、多包含取最内层。不接 CL、不改上游（纯消费 `LevelResult`/`DivergenceResult`）。

**Tech Stack:** Python 3、dataclass、pytest、poetry、ruff、plotly（验收出图）。

设计见 `docs/chanlun_core_redesign_4c_背驰贯通_design.md`。

---

## File Structure

- **Create:** `src/chanlun/core/beichi_nest.py` — `NestedDivergence`(森林节点) + `BeichiNestCalculator`(`_span`/`_find_parent`/`calculate`)。唯一新代码文件。
- **Test:** `tests/core/test_beichi_nest.py` — 受控 `LevelResult`+fake `DivergenceResult`，沿用 `_seg` 范式造 `leave_seg` 时间区间（绕浮点敏感）。
- **Probe（验收，gitignored）:** `scripts_local/probe_p4c_nest.py` — 真实数据 → 多级递归 → 嵌套森林 → Plotly 横条嵌套图，人工审。
- **不改:** `zs_branch.py`/`recursive_branch.py`/`zslx_branch.py`/CL（P4c 纯下游消费）。

---

## Task 1: `NestedDivergence` 数据结构 + 嵌套口径 helper（`_span`/`_find_parent`）

**Files:**
- Create: `src/chanlun/core/beichi_nest.py`
- Test: `tests/core/test_beichi_nest.py`

- [ ] **Step 1: 写失败测试**（`tests/core/test_beichi_nest.py`）

```python
"""tests/core/test_beichi_nest.py — P4c 背驰贯通 TDD。

受控 fake DivergenceResult（leave_seg 用受控 K 线序号区间）+ fake LevelResult，
绕开笔划分浮点敏感。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.beichi_nest import NestedDivergence, BeichiNestCalculator


def _seg(_type: str, s_k: int, e_k: int) -> XD:
    """造 leave_seg(XD)：K 线序号区间 [s_k, e_k]，方向 _type（up/down）。"""
    def _fx(kidx, ftype):
        k = CLKline(k_index=kidx, date=None, h=0.0, l=0.0, o=0.0, c=0.0, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=0.0)
    if _type == "up":
        start, end = _fx(s_k, "di"), _fx(e_k, "ding")
    else:
        start, end = _fx(s_k, "ding"), _fx(e_k, "di")
    xd = XD(start=start, end=end, _type=_type, index=0)
    xd.done = True
    return xd


def _dv(_type, s_k, e_k, is_beichi=True, provisional=False) -> DivergenceResult:
    """造 DivergenceResult：leave_seg 时间 [s_k,e_k]、方向 _type；compare_seg 占位同段。"""
    c = _seg(_type, s_k, e_k)
    return DivergenceResult(is_beichi=is_beichi, kind="qs",
                            compare_seg=c, leave_seg=c, provisional=provisional)


def _node(level, zi, _type, s_k, e_k) -> NestedDivergence:
    return NestedDivergence(level=level, zs_index=zi, divergence=_dv(_type, s_k, e_k))


def test_span_returns_kline_index_range():
    calc = BeichiNestCalculator()
    assert calc._span(_dv("up", 3, 7)) == (3, 7)


def test_find_parent_strict_contain_same_dir():
    lo = _node(0, 0, "up", 3, 5)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is hi


def test_find_parent_opposite_dir_none():
    lo = _node(0, 0, "up", 3, 5)
    hi = _node(1, 0, "down", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is None


def test_find_parent_not_contained_none():
    # lo 右界 9 超出 hi 右界 8 → 非严格包含
    lo = _node(0, 0, "up", 3, 9)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is None


def test_find_parent_boundary_flush_ok():
    # 边界贴合（lo_e == hi_e，同一转折点）应算包含
    lo = _node(0, 0, "up", 3, 8)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is hi


def test_find_parent_innermost_wins():
    # lo[4,5] 同时落在 hi_a[1,8]、hi_b[3,6] → 取最内层 hi_b（跨度小）
    lo = _node(0, 0, "up", 4, 5)
    hi_a = _node(1, 0, "up", 1, 8)
    hi_b = _node(1, 1, "up", 3, 6)
    assert BeichiNestCalculator()._find_parent(lo, [hi_a, hi_b]) is hi_b
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_beichi_nest.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'chanlun.core.beichi_nest'`）

- [ ] **Step 3: 写实现**（`src/chanlun/core/beichi_nest.py`）

```python
"""beichi_nest.py — P4c 缠论背驰贯通（自底向上 BUILD 嵌套背驰森林）。

把 recursive_branch 多级 LevelResult 里各级已固化背驰段，按「同向 + 严格时间
包含」自底向上挂成嵌套森林（宪法 §7.1 第27课嵌套性 / §7.2 贯通到顶 BUILD）。
是 P6 区间套 READ 的输入。孤立、不接 CL、不改上游。
设计见 docs/chanlun_core_redesign_4c_背驰贯通_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from chanlun.core.zs_branch import DivergenceResult


@dataclass
class NestedDivergence:
    """嵌套背驰森林的一个节点：一段已固化背驰 + 被它严格包含+同向的次级别背驰。"""
    level: int                                       # 背驰所在级别 (0=L0)
    zs_index: int                                    # 该级 done_divergence 中的索引(回溯定位)
    divergence: DivergenceResult                     # 背驰本体(P3/P4b 已算,自带 leave_seg 时间区间)
    children: List["NestedDivergence"] = field(default_factory=list)  # 被严格包含+同向的次级别背驰


class BeichiNestCalculator:
    """背驰嵌套森林计算器。无状态，每次 calculate 全量重算。"""

    @staticmethod
    def _span(dv: DivergenceResult) -> Tuple[int, int]:
        """背驰段 = 离开段 c 的 K 线序号区间 [start_k, end_k]。leave_seg 是 LINE
        (XD/ZSLX)，start/end 是 FX、FX.k 是代表 K 线、.k_index 是序号。P3 _divergence_for
        已守卫 leave_seg.start/end 非 None。"""
        c = dv.leave_seg
        return (c.start.k.k_index, c.end.k.k_index)

    def _find_parent(self, lo: "NestedDivergence",
                     hi_nodes: List["NestedDivergence"]) -> Optional["NestedDivergence"]:
        """在 hi_nodes 中找唯一严格时间包含 lo 且同向者；多个候选取最内层(跨度最小)；
        无则 None(断链)。"""
        lo_s, lo_e = self._span(lo.divergence)
        lo_dir = lo.divergence.leave_seg._type
        best, best_w = None, None
        for hi in hi_nodes:
            if hi.divergence.leave_seg._type != lo_dir:          # 同向
                continue
            hi_s, hi_e = self._span(hi.divergence)
            if hi_s <= lo_s and lo_e <= hi_e:                    # 严格时间包含(边界可贴合)
                w = hi_e - hi_s
                if best_w is None or w < best_w:                 # 取最内层
                    best, best_w = hi, w
        return best
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_beichi_nest.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: ruff + commit**

Run: `poetry run ruff check src/chanlun/core/beichi_nest.py tests/core/test_beichi_nest.py`
Expected: All checks passed!

```bash
git add src/chanlun/core/beichi_nest.py tests/core/test_beichi_nest.py
git commit -m "feat(core/beichi_nest): NestedDivergence + 嵌套口径 _span/_find_parent(严格时间包含+同向+最内层)(P4c)"
```

---

## Task 2: `calculate` BUILD 主链（自底向上挂载 + 顶层森林）+ 全套回归

**Files:**
- Modify: `src/chanlun/core/beichi_nest.py`（加 `calculate`）
- Test: `tests/core/test_beichi_nest.py`（加 calculate 用例）

- [ ] **Step 1: 写失败测试**（追加到 `tests/core/test_beichi_nest.py` 末尾）

```python
from chanlun.core.recursive_branch import LevelResult


def _lr(level, dvs) -> LevelResult:
    """造 fake LevelResult：只填 level + done_divergence(其余占位,calculate 不碰)。"""
    return LevelResult(level=level, zss=[], done_divergence=list(dvs), zslxs=[], upgrade_idx=[])


def test_calculate_empty_returns_empty():
    assert BeichiNestCalculator().calculate([]) == []


def test_calculate_basic_nesting():
    # L0[3,5]up 落在 L1[1,8]up → 挂为 child；顶层森林只剩 L1
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1
    assert forest[0].level == 1
    assert len(forest[0].children) == 1
    assert forest[0].children[0].level == 0


def test_calculate_opposite_dir_both_top():
    # 异向 → 不挂，两者皆顶层
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("down", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 2


def test_calculate_not_contained_both_top():
    # L0 右界超出 → 不挂
    levels = [_lr(0, [_dv("up", 3, 9)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 2


def test_calculate_dangling_low_is_root():
    # L0 背驰无 L1 父(L1 异向) → L0 自成顶层根
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("down", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    levels_in_forest = sorted(n.level for n in forest)
    assert levels_in_forest == [0, 1]


def test_calculate_provisional_excluded():
    # provisional=True 不入森林
    levels = [_lr(0, [_dv("up", 3, 5, provisional=True)])]
    assert BeichiNestCalculator().calculate(levels) == []


def test_calculate_non_beichi_excluded():
    # is_beichi=False 不入森林
    levels = [_lr(0, [_dv("up", 3, 5, is_beichi=False)])]
    assert BeichiNestCalculator().calculate(levels) == []


def test_calculate_none_divergence_skipped():
    # done_divergence 含 None(该中枢无背驰) → 跳过不报错
    levels = [_lr(0, [None, _dv("up", 3, 5)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1 and forest[0].level == 1


def test_calculate_three_level_chain():
    # L0[4,5] ⊂ L1[3,6] ⊂ L2[1,9] 同向 → 深度 3 链:L2 根→L1→L0
    levels = [
        _lr(0, [_dv("up", 4, 5)]),
        _lr(1, [_dv("up", 3, 6)]),
        _lr(2, [_dv("up", 1, 9)]),
    ]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1
    l2 = forest[0]
    assert l2.level == 2 and len(l2.children) == 1
    l1 = l2.children[0]
    assert l1.level == 1 and len(l1.children) == 1
    assert l1.children[0].level == 0
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_beichi_nest.py -q`
Expected: FAIL（`AttributeError: 'BeichiNestCalculator' object has no attribute 'calculate'`）

- [ ] **Step 3: 写实现**（在 `beichi_nest.py` 的 `BeichiNestCalculator` 中加 `calculate`，并在 import 行补 `LevelResult`）

import 行改为：
```python
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult
```

在 `_find_parent` 之后加方法：
```python
    def calculate(self, levels: List[LevelResult]) -> List[NestedDivergence]:
        """各级 done 背驰段自底向上 BUILD 成嵌套森林。返回顶层森林(所有未被更高
        级别包含的背驰节点;最高级 + 断链低级各成树根)。"""
        if not levels:
            return []

        # 1. 每级抽出「已固化 + is_beichi」的背驰 → 叶节点
        per_level: List[List[NestedDivergence]] = []
        for lr in levels:
            nodes: List[NestedDivergence] = []
            for zi, dv in enumerate(lr.done_divergence):
                if dv is not None and dv.is_beichi and not dv.provisional:   # 仅已坐实背驰
                    nodes.append(NestedDivergence(level=lr.level, zs_index=zi, divergence=dv))
            per_level.append(nodes)

        # 2. 自底向上:相邻级别 (k → k+1),把 L_k 节点挂到 严格包含+同向 的 L_{k+1} 节点
        attached: set = set()
        for k in range(len(per_level) - 1):
            for lo in per_level[k]:
                parent = self._find_parent(lo, per_level[k + 1])
                if parent is not None:
                    parent.children.append(lo)
                    attached.add(id(lo))

        # 3. 顶层森林 = 所有未被挂载的节点(最高级别 + 断链的低级别各成树根)
        return [n for nodes in per_level for n in nodes if id(n) not in attached]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_beichi_nest.py -q`
Expected: PASS（15 passed）

- [ ] **Step 5: 全套回归 + ruff**

Run: `poetry run pytest tests/core/ -q`
Expected: PASS（既有 249 + 新增 → 全绿，零回归）

Run: `poetry run ruff check src/chanlun/core/beichi_nest.py tests/core/test_beichi_nest.py`
Expected: All checks passed!

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/beichi_nest.py tests/core/test_beichi_nest.py
git commit -m "feat(core/beichi_nest): calculate 自底向上 BUILD 嵌套背驰森林(抽叶→相邻级别挂→顶层森林)(P4c)"
```

---

## Task 3: 真实数据分层出图验收

**Files:**
- Create（gitignored）: `scripts_local/probe_p4c_nest.py`
- Modify: `.gitignore`（加 `beichi_nest_review.html`）

- [ ] **Step 1: 写 probe 出图脚本**（`scripts_local/probe_p4c_nest.py`）

```python
# scripts_local/probe_p4c_nest.py — P4c 嵌套背驰森林真实数据验收(本地, gitignored)
import logging
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.recursive_branch import RecursiveBranchCalculator
from chanlun.core.beichi_nest import BeichiNestCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

levels = RecursiveBranchCalculator().calculate(bis, ld, "zs_wzgx_zgd", frequency="1m")
forest = BeichiNestCalculator().calculate(levels)

# 各级 done 背驰段总数(对照)
total = sum(1 for lr in levels for dv in lr.done_divergence
            if dv is not None and dv.is_beichi and not dv.provisional)
print(f"levels={[(lr.level, len(lr.zss)) for lr in levels]}  森林顶层={len(forest)}  背驰段总数={total}")


def _walk(node, depth=0):
    s_k, s_e = node.divergence.leave_seg.start.k.k_index, node.divergence.leave_seg.end.k.k_index
    d = node.divergence.leave_seg._type
    print("  " * depth + f"L{node.level} zs{node.zs_index} {d} [{s_k},{s_e}] children={len(node.children)}")
    for ch in node.children:
        _walk(ch, depth + 1)


for root in forest:
    _walk(root)

# 出图:各级背驰段画成按 level 分行的时间横条;嵌套父子用竖向连线
fig = go.Figure()
color = {"up": "#d62728", "down": "#2ca02c"}


def _draw(node):
    s_k = node.divergence.leave_seg.start.k.k_index
    e_k = node.divergence.leave_seg.end.k.k_index
    d = node.divergence.leave_seg._type
    y = node.level
    fig.add_trace(go.Scatter(
        x=[s_k, e_k], y=[y, y], mode="lines",
        line=dict(color=color[d], width=8), opacity=0.6,
        name=f"L{node.level} {d}", showlegend=False,
        hovertext=f"L{node.level} zs{node.zs_index} {d} [{s_k},{e_k}]"))
    for ch in node.children:
        # 父子竖向连线(中点)
        cs = ch.divergence.leave_seg.start.k.k_index
        ce = ch.divergence.leave_seg.end.k.k_index
        fig.add_trace(go.Scatter(
            x=[(s_k + e_k) / 2, (cs + ce) / 2], y=[y, ch.level],
            mode="lines", line=dict(color="#888", width=1, dash="dot"),
            showlegend=False))
        _draw(ch)


for root in forest:
    _draw(root)

fig.update_layout(title="P4c 嵌套背驰森林(y=级别,横条=背驰段,虚线=父子嵌套)",
                  yaxis=dict(title="级别 L", dtick=1), xaxis=dict(title="K线序号"),
                  height=400 + 80 * len(levels))
fig.write_html("beichi_nest_review.html")
print("written beichi_nest_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p4c_nest.py`
Expected: 打印各级背驰段数 + 森林结构树 + 生成 `beichi_nest_review.html`

- [ ] **Step 3: gitignore 审阅图**（`.gitignore` 在 `recursive_branch_review.html` 行后加一行）

```
recursive_branch_review.html
beichi_nest_review.html
```

```bash
git add .gitignore
git commit -m "chore: gitignore P4c 审阅图 beichi_nest_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `beichi_nest_review.html` 交付用户，审：
- 高级别背驰段（横条）是否**框住**其内的次级别同向背驰段（虚线连到下一行、且子段时间区间在父段内，§7.1）。
- 嵌套链是否合理（同向、逐级收缩）；断链顶层背驰是否确实"未被上级套住"。
- **不通过 → 诊断口径（同向/包含严格度），按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2 模块接口→Task1/2；§3 BUILD 算法→Task2；§4 口径(_span/_find_parent)→Task1；§5 边界(断链/最内/provisional/None)→Task2 测试；§6 测试+验收→Task1/2/3。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`NestedDivergence(level/zs_index/divergence/children)`、`_span→Tuple[int,int]`、`_find_parent(lo,hi_nodes)→Optional`、`calculate(List[LevelResult])→List[NestedDivergence]`、fake `_dv`/`_lr` 构造签名与 `DivergenceResult`/`LevelResult` 一致，全程统一。
- **执行注意**：Task1 import 暂不含 `LevelResult`（Task2 才加，避免 ruff unused）；probe 用 `PYTHONPATH=src`。
