# P4b 递归装配实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `recursive_branch.py`，把 L0 线段自底向上递归装配成多级层级树（L0→L1→L2…），主链走势类型递归 + 旁路升级标注。

**Architecture:** 复用 `zs_branch`(中枢+内联背驰) + `zslx_branch`(走势类型)，循环 `units→zs_branch→zslx_branch→_as_units→units` 逐级。唯一上游改动：`zs_branch` 参数化 `min_zs_lines`(L0=4/L≥1=3)。升级标注(9段/expand)独立旁路、不改走势类型(原文 line16429 解耦)。

**Tech Stack:** Python 3 / dataclasses / pytest / poetry；依赖 `zs_branch`/`zslx_branch`/`classify_rel`。不依赖 CL、不动旧 `recursive_calculator`。

设计依据：`docs/chanlun_core_redesign_4b_递归装配_design.md`。

---

## File Structure

- **Modify** `src/chanlun/core/zs_branch.py`：`MIN_LINES` 类常量 → `__init__` 的 `min_zs_lines` 参数（默认 4）。
- **Create** `src/chanlun/core/recursive_branch.py`：`RecursiveBranchCalculator`、`LevelResult`、`_as_units`、`_mark_upgrades`。
- **Create** `tests/core/test_recursive_branch.py`：自带 helper（`_seg`/`_make_zs`/`_make_zslx`/`_ld`）+ 单测。
- **本地（不入库）** `scripts_local/probe_p4b_review.py`：真实数据分层出图验收。

不改：`zslx_branch.py`、旧 `recursive_calculator.py`/`zslx_calculator.py`、`cl.py`。

---

## Task 1: zs_branch 参数化 `min_zs_lines`（默认 4 保 P1/P3）

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`（`__init__` + `calculate` 3 处）
- Test: `tests/core/test_zs_branch.py`（追加）

- [ ] **Step 1: 写失败测试**（追加到 `tests/core/test_zs_branch.py` 末尾）

```python
def test_calculator_min_zs_lines_param():
    """min_zs_lines 可配：默认 4(L0)，递归 L≥1 传 3。"""
    assert zs_branch.ZsBranchCalculator().min_zs_lines == 4
    assert zs_branch.ZsBranchCalculator(min_zs_lines=3).min_zs_lines == 3
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py::test_calculator_min_zs_lines_param -q`
Expected: FAIL —— `AttributeError: 'ZsBranchCalculator' object has no attribute 'min_zs_lines'`

- [ ] **Step 3: 实现**

在 `src/chanlun/core/zs_branch.py` 的 `ZsBranchCalculator` 类里：

(a) 删除 `MIN_LINES = 4` 类常量那一行（保留下面的 `_NO_CAP`）。

(b) `__init__` 加 `min_zs_lines` 参数（接在现有参数末尾）：

```python
    def __init__(
        self,
        ld_provider: Optional[LdProvider] = None,
        frequency: Optional[str] = None,
        wzgx: str = Config.ZS_WZGX_ZGD.value,
        min_zs_lines: int = 4,
    ):
        """``ld_provider`` 缺省时不判背驰（退化纯结构，保 P1 行为）。

        ``wzgx`` 默认 ZGD（核心区间口径）。``min_zs_lines`` 最小中枢段数：L0
        线段级=4(项目口径)，递归 L≥1 走势类型级=3(原文「3 个次级走势类型重叠」)。
        """
        self.ld_provider = ld_provider
        self.frequency = frequency
        self.wzgx = wzgx
        self.min_zs_lines = min_zs_lines
```

(c) `calculate` 内 3 处 `self.MIN_LINES` 改为 `self.min_zs_lines`：

```python
        zc = ZsCalculator(
            require_alternation=False,
            min_zs_lines=self.min_zs_lines,
            max_zs_lines=self._NO_CAP,
        )
```
以及两处 correct_entry：
```python
        done: List[ZS] = [correct_exit(correct_entry(z, self.min_zs_lines)) for z in zc.zss]
        pending: Optional[ZS] = zc.pending_zs
        if pending is not None:
            pending = correct_entry(pending, self.min_zs_lines)
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q 2>&1 | tail -4`
Expected: PASS（新测试 + P1/P3 全部测试绿——默认 4，行为不变）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): MIN_LINES 类常量参数化为 min_zs_lines(默认4,递归L≥1传3)(P4b)"
```

---

## Task 2: recursive_branch 骨架 + LevelResult + _as_units + _mark_upgrades

**Files:**
- Create: `src/chanlun/core/recursive_branch.py`
- Create: `tests/core/test_recursive_branch.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/core/test_recursive_branch.py`）

```python
"""tests/core/test_recursive_branch.py — P4b 递归装配 TDD。

自带 helper（受控 ZS/ZSLX/线段，绕开笔划分浮点敏感）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS, ZSLX
from chanlun.core import recursive_branch


def _seg(index, _type, start_val, end_val) -> XD:
    def _fx(kidx, val, ftype):
        k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=val)
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _make_zs(core_segs, zd, zg) -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _make_zslx(zss, zslx_type, index=99) -> ZSLX:
    z = ZSLX(zslx_level=None, start=zss[0].lines[0].start, end=zss[-1].lines[-1].end,
             start_line=zss[0].lines[0], end_line=zss[-1].lines[-1],
             _type="up", index=index, done=True)
    z.zss = list(zss)
    z.zslx_type = zslx_type
    z.zs_high = max(x.gg for x in zss)
    z.zs_low = min(x.dd for x in zss)
    return z


# ---- _as_units: ZSLX index 重排 ----
def test_as_units_reindexes():
    z1 = _make_zslx([_make_zs([_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8)], 5, 8)], "盘整")
    z2 = _make_zslx([_make_zs([_seg(3, "up", 16, 19), _seg(4, "down", 19, 16), _seg(5, "up", 16, 19)], 16, 19)], "盘整")
    out = recursive_branch._as_units([z1, z2])
    assert [u.index for u in out] == [0, 1]
    assert out[0].zs_high == z1.zss[0].gg            # zs_high 不被改写(P4a 已填)


# ---- _mark_upgrades: 9段 / expand 标注 ----
def test_mark_upgrades_nine_segments():
    # 9 段中枢 → 升级标注
    nine = [_seg(i, "up" if i % 2 == 0 else "down", 5, 8) for i in range(9)]
    z = _make_zs(nine, 5, 8)
    assert recursive_branch._mark_upgrades([z]) == [0]


def test_mark_upgrades_expand_pair():
    # 两中枢本体相交(expand) → 后者标升级
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 9, 6), _seg(4, "up", 6, 9), _seg(5, "down", 9, 6)], 6, 9)  # body[6,9]∩[5,8]
    assert recursive_branch._mark_upgrades([z1, z2]) == [1]


def test_mark_upgrades_clean_trend_none():
    # 干净趋势(本体分离)→ 无升级标注
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 19, 16), _seg(4, "up", 16, 19), _seg(5, "down", 19, 16)], 16, 19)
    assert recursive_branch._mark_upgrades([z1, z2]) == []
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_recursive_branch.py::test_as_units_reindexes -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'chanlun.core.recursive_branch'`

- [ ] **Step 3: 实现**（新建 `src/chanlun/core/recursive_branch.py`，先只写数据结构 + 两个 helper）

```python
"""recursive_branch.py — P4b 缠论递归装配（走势类型递归主链 + 独立升级标注）。

把 L0 线段自底向上递归装配成多级层级树：units→zs_branch(中枢+内联背驰)→
zslx_branch(走势类型)→_as_units→units 逐级。升级标注(9段/扩展)是独立旁路、
不改走势类型边界(原文 line16429：中枢扩展⊥走势转折)。

孤立、不接 CL、不动旧 recursive_calculator。
设计见 docs/chanlun_core_redesign_4b_递归装配_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.cl_interface import LINE, ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, ZsBranchCalculator, classify_rel
from chanlun.core.zslx_branch import ZslxBranchCalculator

_MAX_LEVELS = 50    # 护栏；走势单元逐级收缩，正常远不及


@dataclass
class LevelResult:
    """单个递归级别的产出。"""
    level: int                                          # 0 = L0
    zss: List[ZS]                                       # 本级已完成中枢
    done_divergence: List[Optional[DivergenceResult]]   # 与 zss 索引对齐(本级内联背驰)
    zslxs: List[ZSLX]                                   # 本级走势类型
    upgrade_idx: List[int] = field(default_factory=list)  # 升级标注:9段/扩展候选的中枢索引(P5 用)


def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """ZSLX 喂回 zs_branch 当输入段：只重排 index 为连续 0,1,2…(ZsCalculator 靠
    index 定位)。zs_high/zs_low 已由 zslx_branch._finalize 填(中枢包络)，不动。"""
    for i, zslx in enumerate(zslxs):
        zslx.index = i
    return zslxs


def _mark_upgrades(done_zss: List[ZS]) -> List[int]:
    """本级中枢中「9 段升级 / 中枢扩展候选」的索引（line16429 解耦：仅标注、不改
    走势类型；实体化与 2/3 类买点留 P5）。"""
    out: List[int] = []
    for i, z in enumerate(done_zss):
        if len(z.lines) >= 9:                                       # 9 段升级(第33课)
            out.append(i)
        elif i > 0 and classify_rel(done_zss[i - 1], z) == "expand":  # 中枢扩展(中心定理二本体相交)
            out.append(i)
    return out
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_recursive_branch.py -q`
Expected: PASS（_as_units + 3 个 _mark_upgrades 测试全绿）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/recursive_branch.py tests/core/test_recursive_branch.py
git commit -m "feat(core/recursive_branch): LevelResult + _as_units(index重排) + _mark_upgrades(9段/expand标注)(P4b)"
```

---

## Task 3: `calculate` 递归循环（一级 + 两级）

**Files:**
- Modify: `src/chanlun/core/recursive_branch.py`（加 `RecursiveBranchCalculator`）
- Test: `tests/core/test_recursive_branch.py`

- [ ] **Step 1: 写失败测试**（追加到测试文件末尾）

```python
from chanlun.core.cl_interface import Config


def _ld_none(s, e):
    """fake ld_provider：返回零力度，背驰不触发(本测试靠方向反转/结构切走势类型)。"""
    return {"hist": {"up_sum": 0.0, "down_sum": 0.0}, "dif": {"max": 0.0, "min": 0.0}}


def test_calculate_empty_returns_empty():
    assert recursive_branch.RecursiveBranchCalculator().calculate(
        [], _ld_none, Config.ZS_WZGX_ZGD.value) == []


def test_calculate_level0_produces_zhongshu():
    """一段线段序列至少产出 L0 级(中枢 + 走势类型)。"""
    # 进入段 + 4 段重叠核心[5,8] + 离开
    lines = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    res = recursive_branch.RecursiveBranchCalculator().calculate(
        lines, _ld_none, Config.ZS_WZGX_ZGD.value)
    assert len(res) >= 1
    assert res[0].level == 0
    assert len(res[0].zss) >= 1                       # L0 至少 1 个中枢
    assert len(res[0].zslxs) >= 1                     # L0 至少 1 个走势类型


def test_calculate_two_levels():
    """≥3 个方向交替、区间重叠的 L0 走势类型 → 喂回产出 L1 级。

    构造三段「上涨-下跌-上涨」L0 走势类型，区间彼此重叠(本体相交)，喂回 zs_branch
    (min_zs_lines=3) 应聚成 1 个 L1 中枢 → 出现 level==1 的 LevelResult。
    """
    lines = (
        # 上涨走势类型1：核心[5,8] + 离开冲到 20
        [_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
         _seg(4, "up", 5, 20)]
        # 下跌走势类型2：核心[16,19](本体与1、3重叠) + 离开
        + [_seg(5, "down", 20, 16), _seg(6, "up", 16, 19), _seg(7, "down", 19, 16), _seg(8, "up", 16, 19),
           _seg(9, "down", 16, 4)]
        # 上涨走势类型3：核心[6,9] + 离开
        + [_seg(10, "up", 4, 9), _seg(11, "down", 9, 6), _seg(12, "up", 6, 9), _seg(13, "down", 9, 6),
           _seg(14, "up", 6, 22)]
        # 下跌走势类型4：让走势类型3 done(方向反转封口)
        + [_seg(15, "down", 22, 18), _seg(16, "up", 18, 21), _seg(17, "down", 21, 18), _seg(18, "up", 18, 21)]
    )
    res = recursive_branch.RecursiveBranchCalculator().calculate(
        lines, _ld_none, Config.ZS_WZGX_ZGD.value)
    levels = [r.level for r in res]
    assert 0 in levels and 1 in levels               # 至少两级
    l1 = next(r for r in res if r.level == 1)
    assert len(l1.zss) >= 1                           # L1 至少 1 个中枢(由 L0 走势类型构成)
    assert all(isinstance(seg, ZSLX) for seg in l1.zss[0].lines)  # L1 中枢的构成段是 L0 走势类型
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_recursive_branch.py::test_calculate_level0_produces_zhongshu -q`
Expected: FAIL —— `AttributeError: module 'chanlun.core.recursive_branch' has no attribute 'RecursiveBranchCalculator'`

- [ ] **Step 3: 实现**（在 `recursive_branch.py` 末尾加 `RecursiveBranchCalculator`）

```python
class RecursiveBranchCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def calculate(
        self,
        xds: List[LINE],
        ld_provider: LdProvider,
        wzgx_config: str,
        frequency: Optional[str] = None,
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。

        每级：zs_branch(中枢+内联背驰) → zslx_branch(走势类型) → _as_units → 下一级。
        L0 构成段=线段(min_zs_lines=4)，L≥1 构成段=走势类型(=3,原文)。
        终止：扫不出中枢 / 走势类型 <3 / 触 _MAX_LEVELS。
        """
        if not xds:
            return []
        results: List[LevelResult] = []
        units: List[LINE] = list(xds)
        level = 0
        while level < _MAX_LEVELS:
            min_lines = 4 if level == 0 else 3
            res = ZsBranchCalculator(
                ld_provider=ld_provider, frequency=frequency,
                wzgx=wzgx_config, min_zs_lines=min_lines,
            ).calculate(units)
            if not res.done_zss:
                break
            zslxs = ZslxBranchCalculator().calculate(res.done_zss, res.done_divergence)
            results.append(LevelResult(
                level=level, zss=res.done_zss, done_divergence=res.done_divergence,
                zslxs=zslxs, upgrade_idx=_mark_upgrades(res.done_zss),
            ))
            if len(zslxs) < 3:
                break
            units = _as_units(zslxs)
            level += 1
        return results
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_recursive_branch.py -q`
Expected: PASS（_as_units + _mark_upgrades + empty + level0 + two_levels 全绿）

> 执行注记：`test_calculate_two_levels` 的线段结构是基于对 zs_branch/zslx_branch 行为的分析预期。若它因引擎实际产出(中枢数/走势类型数/重叠关系)与预期不符而失败，先临时 `print([(r.level, len(r.zss), len(r.zslxs)) for r in res])` 及 L0 走势类型的 `[(w.zslx_type, w.zs_low, w.zs_high) for w in res[0].zslxs]` 看实际，再据实微调线段的 lo/hi(让 L0 切出 ≥3 个方向交替、区间重叠的走势类型)，保持测试意图(出现 level==1 且 L1 中枢构成段是 ZSLX)不变。若反复调不出两级，报告 BLOCKED 贴实际 print——可能需把两级验证移到 Task 5 真实数据 probe，单测降级为「一级 + ZSLX 直接喂 zs_branch(min_zs_lines=3) 产中枢」。

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/recursive_branch.py tests/core/test_recursive_branch.py
git commit -m "feat(core/recursive_branch): RecursiveBranchCalculator 主链递归循环(逐级 zs_branch→zslx_branch)(P4b)"
```

---

## Task 4: 全套回归

**Files:** 无新增改动，纯验证。

- [ ] **Step 1: recursive_branch 全套**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_recursive_branch.py -q 2>&1 | tail -3`
Expected: PASS（约 8 个测试）

- [ ] **Step 2: core 全套（防跨模块回归，尤其 zs_branch 参数化）**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/ -q 2>&1 | tail -3`
Expected: PASS（旧 238 + zs_branch 参数化测试 + recursive_branch ≈ 247，0 failed）

- [ ] **Step 3: lint**

Run: `cd D:/project/chanlun-pro && poetry run ruff check src/chanlun/core/recursive_branch.py src/chanlun/core/zs_branch.py`
Expected: All checks passed!

- [ ] **Step 4: 提交（仅当 Step 1-3 有清理改动时）**

```bash
git add src/chanlun/core/recursive_branch.py
git commit -m "chore(core/recursive_branch): P4b lint 清理 + 全套回归绿"
```

---

## Task 5: 真实数据分层出图验收（人工审）

**Files:**
- Create（本地不入库）: `scripts_local/probe_p4b_review.py`
- Output: `recursive_branch_review.html`

- [ ] **Step 1: 写验收 probe 脚本**

```python
# scripts_local/probe_p4b_review.py —— P4b 真实数据分层出图验收（本地临时，不入库）
import logging
import pandas as pd
import plotly.graph_objects as go

logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.recursive_branch import RecursiveBranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)
levels = RecursiveBranchCalculator().calculate(bis, ld, "zs_wzgx_zgd", "1m")

ks = cd.get_klines()
fig = go.Figure(go.Candlestick(
    x=[k.date for k in ks], open=[k.o for k in ks], high=[k.h for k in ks],
    low=[k.l for k in ks], close=[k.c for k in ks], name="K", opacity=0.25))

LCOLOR = ["rgba(80,80,80,0.9)", "rgba(220,40,40,0.9)", "rgba(40,120,220,0.9)", "rgba(40,160,40,0.9)"]
for lr in levels:
    col = LCOLOR[lr.level % len(LCOLOR)]
    for i, z in enumerate(lr.zss):
        x0 = z.lines[0].start.k.date
        x1 = (z.end or z.lines[-1]).end.k.date
        up = " ★" if i in lr.upgrade_idx else ""
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=z.zd, y1=z.zg,
                      line=dict(width=1.5, color=col), fillcolor="rgba(0,0,0,0)")
        fig.add_annotation(x=x1, y=z.zg, text=f"L{lr.level}{up}", showarrow=False,
                           font=dict(size=10, color=col), bgcolor="rgba(255,255,255,0.5)")

print("层级:", [(lr.level, f"中枢{len(lr.zss)}", f"走势类型{len(lr.zslxs)}",
                f"升级{len(lr.upgrade_idx)}") for lr in levels])
fig.update_layout(title="P4b 递归层级验收 (a_SH_513100_1m·笔级)｜框=各级中枢(L0灰/L1红/L2蓝),★=升级候选",
                  xaxis_rangeslider_visible=False, height=840)
fig.write_html("recursive_branch_review.html")
print("written recursive_branch_review.html")
```

- [ ] **Step 2: 运行 probe**

Run: `cd D:/project/chanlun-pro && PYTHONPATH=src poetry run python scripts_local/probe_p4b_review.py`
Expected: 打印 `层级: [(0,'中枢N','走势类型M','升级K'),(1,...)...]`、`written recursive_branch_review.html`，无异常。

- [ ] **Step 3: 加 .gitignore + 人工审**

把 `recursive_branch_review.html` 加进 `.gitignore`（紧接已有 `zslx_branch_review.html` 行后），交付用户审：L1 中枢是否确由 L0 走势类型重叠构成、各级层叠是否合理、★升级候选是否落在中枢扩展/9段处。

- [ ] **Step 4: 据审图决定**

- 图 OK → P4b 完成，更新 memory，转 P4c/P5。
- 图有问题 → 回到对应 Task 修（多半是递归终止/_as_units 口径或两级结构）。

> probe 脚本与 html 是本地件，不 `git add`（`scripts_local/` 已 gitignore）。

---

## Self-Review（计划对照 spec）

**1. Spec 覆盖：**
- §2 模块/接口（LevelResult, RecursiveBranchCalculator）→ Task 2/3 ✓
- §3 主链递归循环（units→zs_branch→zslx_branch→_as_units）→ Task 3 ✓
- §4 zs_branch 参数化 min_zs_lines → Task 1 ✓
- §5 背驰力度同一 ld_provider → Task 3 calculate 透传 ✓
- §6 升级标注 _mark_upgrades（9段/expand）→ Task 2 ✓
- §7 _as_units（index 重排）→ Task 2 ✓
- §8 测试 + 真实数据出图 → Task 2/3/4/5 ✓
- §0 不含（中枢扩展精确实体化/2-3类买点→P5、多周期MACD、P4c/P6、live多假设）→ 不在任何 Task（正确排除）✓

**2. Placeholder 扫描：** 无 TBD/TODO；每个 code step 给完整代码；Task 3 的「执行注记」是真实调试指引 + 明确的降级方案（非 placeholder）。

**3. 类型/签名一致：** `LevelResult(level,zss,done_divergence,zslxs,upgrade_idx)` Task 2 定义、Task 3 构造一致；`_as_units(zslxs)`/`_mark_upgrades(done_zss)` Task 2 定义、Task 3 调用一致；`ZsBranchCalculator(...,min_zs_lines=)` Task 1 定义、Task 3 调用一致；`ZslxBranchCalculator().calculate(done_zss, done_divergence)` 与 P4a 实际接口一致。

无 gap，无需补 Task。
