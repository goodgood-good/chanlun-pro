# P4a 走势类型划分（基于 zs_branch 内联背驰）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `zslx_branch.py`，把 zs_branch 的 L0 已完成中枢序列(`done_zss` + `done_divergence`)用双信号状态机切成走势类型 `List[ZSLX]`。

**Architecture:** 孤立模块、不接 CL、不动旧 `zslx_calculator`。状态机蓝本=旧 `ZslxCalculator`，两处关键替换——方向信号 `is_qs`→`zs_branch.classify_rel`(本体包络)、背驰信号「自判 `beichi`」→「查 `done_divergence[i]`」(复用 P3 内联背驰)。输出复用 `ZSLX`(LINE 子类)，填 `zs_high/zs_low`(中枢包络)为 P4b 喂回备用。

**Tech Stack:** Python 3 / dataclasses / pytest / poetry；依赖 `zs_branch.classify_rel`+`DivergenceResult`、`cl_interface.{LINE,ZS,ZSLX}`。**不**依赖 `beichi_calculator`（背驰已在 `done_divergence`）。

设计依据：`docs/chanlun_core_redesign_4a_走势类型划分_design.md`。

---

## File Structure

- **Create** `src/chanlun/core/zslx_branch.py`：`ZslxBranchCalculator`（`_finalize` 静态 + `calculate` 状态机）。唯一新增生产侧文件，孤立。
- **Create** `tests/core/test_zslx_branch.py`：受控中枢序列单测（自带 `_seg`/`_make_zs`/`_dv` helper，自包含、不 import 其他 test 文件）。
- **本地（不入库）** `scripts_local/probe_p4a_review.py`：真实数据出图验收。

不改：`zs_branch.py`、`zslx_calculator.py`(旧)、`cl.py`、任何生产配置。

---

## Task 1: 模块骨架 + `_finalize`（ZSLX 产出：分类 + 边界 a/b + 包络）

**Files:**
- Create: `src/chanlun/core/zslx_branch.py`
- Create: `tests/core/test_zslx_branch.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/core/test_zslx_branch.py`，含 helper）

```python
"""tests/core/test_zslx_branch.py — P4a 走势类型划分 TDD。

自带 _seg/_make_zs/_dv helper（自包含，不依赖其它 test 文件）。受控 ZS 序列喂入，
确定性复现走势类型边界（绕开笔划分浮点敏感——输入即确定性中枢）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS, ZSLX
from chanlun.core import zslx_branch
from chanlun.core.zs_branch import DivergenceResult


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
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


def _make_zs(start_seg, core_segs, zd, zg) -> ZS:
    z = ZS(zs_type="xd", start=start_seg)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _dv(is_beichi: bool, kind: str = "qs") -> DivergenceResult:
    s = _seg(0, "up", 1, 2)
    return DivergenceResult(is_beichi=is_beichi, kind=kind, compare_seg=s, leave_seg=s, provisional=False)


# 一个本体在 [lo,hi] 的标准中枢（进入段 + 3 段核心震荡）
def _zs_at(base_idx, entry, lo, hi):
    mid = (lo + hi) / 2
    core = [_seg(base_idx + 1, "down", hi, lo), _seg(base_idx + 2, "up", lo, hi),
            _seg(base_idx + 3, "down", hi, lo)]
    return _make_zs(entry, core, lo, hi)


# ---- Task 1: _finalize ----
def test_finalize_single_zhongshu_is_consolidation():
    z = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    zslx = zslx_branch.ZslxBranchCalculator._finalize([z], 0, None, done=False)
    assert zslx.zslx_type == "盘整"
    assert zslx.zss == [z]
    assert zslx.done is False
    assert zslx.zs_high == z.gg and zslx.zs_low == z.dd      # 单中枢包络
    assert zslx.start_line is z.start                         # 进入段 a


def test_finalize_uptrend_two_zhongshu():
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    zslx = zslx_branch.ZslxBranchCalculator._finalize([z1, z2], 0, "trend_up", done=True)
    assert zslx.zslx_type == "上涨" and zslx._type == "up"
    assert zslx.zs_high == max(z1.gg, z2.gg)
    assert zslx.zs_low == min(z1.dd, z2.dd)
    assert zslx.start_line is z1.start                        # 第一中枢进入段
    assert zslx.end_line is z2.lines[-1]                      # 末中枢末段(z.end 缺→fallback)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zslx_branch.py::test_finalize_single_zhongshu_is_consolidation -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'chanlun.core.zslx_branch'`

- [ ] **Step 3: 实现**（新建 `src/chanlun/core/zslx_branch.py`，先只写类 + `_finalize`）

```python
"""zslx_branch.py — P4a 走势类型划分（基于 zs_branch 内联背驰）。

把 zs_branch 的 L0 已完成中枢序列(done_zss + done_divergence)切成走势类型(ZSLX)：
双信号边界——背驰(复用 done_divergence,不重判) + 方向断裂(classify_rel 本体包络)。
孤立、不接 CL、不依赖 beichi_calculator(背驰已在 done_divergence 里)。

设计见 docs/chanlun_core_redesign_4a_走势类型划分_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, classify_rel


class ZslxBranchCalculator:
    """级别无关的走势类型划分（基于 zs_branch 中枢+内联背驰）。无状态，全量重算。"""

    @staticmethod
    def _finalize(
        zss: List[ZS], start_idx: int, cur_dir: Optional[str], done: bool
    ) -> ZSLX:
        """把一个中枢列表收尾成 ZSLX：分类、边界(含进入/离开段 a/b)、包络。"""
        if len(zss) == 1:
            zslx_type = "盘整"
            z = zss[0]
            direction = "up" if z.lines[-1].end.val >= z.lines[0].start.val else "down"
        else:
            direction = "up" if cur_dir == "trend_up" else "down"
            zslx_type = "上涨" if direction == "up" else "下跌"
        # 走势类型边界 = 第一中枢进入段 a → 末中枢离开段 b（原文 a+A+b），缺则退化用核心段
        first = zss[0].start if zss[0].start is not None else zss[0].lines[0]
        last = zss[-1].end if zss[-1].end is not None else zss[-1].lines[-1]
        zslx = ZSLX(
            zslx_level=getattr(zss[0], "level", None),
            start=first.start, end=last.end,
            start_line=first, end_line=last,
            _type=direction, index=start_idx, done=done,
        )
        zslx.zss = list(zss)
        zslx.zslx_type = zslx_type
        # 喂回 zs_branch 备用(P4b)：ZsCalculator 靠构成段 zs_high/zs_low 判重叠
        zslx.zs_high = max(zs.gg for zs in zss)
        zslx.zs_low = min(zs.dd for zs in zss)
        return zslx
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zslx_branch.py -q`
Expected: PASS（2 个 `_finalize` 测试绿）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zslx_branch.py tests/core/test_zslx_branch.py
git commit -m "feat(core/zslx_branch): ZslxBranchCalculator._finalize 产ZSLX(分类+边界a/b+包络)(P4a)"
```

---

## Task 2: `calculate` 双信号状态机（空/单/趋势/方向断裂/expand/背驰）

**Files:**
- Modify: `src/chanlun/core/zslx_branch.py`（`ZslxBranchCalculator` 加 `calculate`）
- Test: `tests/core/test_zslx_branch.py`

- [ ] **Step 1: 写失败测试**（追加到测试文件末尾）

```python
# ---- Task 2: calculate 状态机 ----
def test_calculate_empty_returns_empty():
    assert zslx_branch.ZslxBranchCalculator().calculate([], []) == []


def test_calculate_single_zhongshu_unfinished_consolidation():
    z = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z], [None])
    assert len(wts) == 1
    assert wts[0].zslx_type == "盘整" and wts[0].done is False   # 末个未完成


def test_calculate_uptrend_three_zhongshu_one_zslx():
    """3 个依次抬高的同向中枢 → 1 个上涨趋势(末个 done=False)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3], [None, None, None])
    assert len(wts) == 1
    assert wts[0].zslx_type == "上涨" and wts[0]._type == "up"
    assert wts[0].zss == [z1, z2, z3] and wts[0].done is False


def test_calculate_direction_break_splits_two_zslx():
    """上涨趋势(z1,z2) 后接下跌中枢 z3 → 方向断裂 → 切 2 个走势类型。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "down", 16, 8), 5, 8)      # 本体跌回 [5,8] → trend_down vs cur_dir trend_up
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3], [None, None, None])
    assert len(wts) == 2
    assert wts[0].zslx_type == "上涨" and wts[0].done is True and wts[0].zss == [z1, z2]
    assert wts[1].zslx_type == "盘整" and wts[1].done is False and wts[1].zss == [z3]


def test_calculate_expand_is_boundary():
    """单中枢后接本体相交的中枢(expand) → 断裂(非趋势延续)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 6, 7), 6, 9)         # 本体[6,9] 与 z1[5,8] 相交 → expand
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2], [None, None])
    assert len(wts) == 2                                 # expand 断裂 → 两个盘整
    assert all(w.zslx_type == "盘整" for w in wts)


def test_calculate_beichi_terminates_trend():
    """上涨趋势在 z3 离开段背驰(done_divergence[2].is_beichi) → 走势类型在 z3 终结。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    z4 = _zs_at(30, _seg(30, "up", 30, 38), 38, 41)
    dv = [None, None, _dv(True), None]                   # z3 处背驰
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4], dv)
    assert len(wts) == 2
    assert wts[0].zss == [z1, z2, z3] and wts[0].done is True   # 背驰终结
    assert wts[1].zss == [z4] and wts[1].done is False          # z4 另起
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zslx_branch.py::test_calculate_uptrend_three_zhongshu_one_zslx -q`
Expected: FAIL —— `AttributeError: 'ZslxBranchCalculator' object has no attribute 'calculate'`

- [ ] **Step 3: 实现**（在 `ZslxBranchCalculator` 内、`_finalize` 下方加 `calculate`）

```python
    def calculate(
        self,
        done_zss: List[ZS],
        done_divergence: List[Optional[DivergenceResult]],
    ) -> List[ZSLX]:
        """把已完成中枢序列切成走势类型（末个 done=False）。

        双信号边界：方向断裂(classify_rel 本体包络) + 背驰(查 done_divergence,
        不重判)。done_divergence 与 done_zss 索引对齐。
        """
        if not done_zss:
            return []
        wts: List[ZSLX] = []
        cur: Optional[List[ZS]] = [done_zss[0]]
        cur_start = 0                         # cur 第一个中枢在 done_zss 的索引
        cur_dir: Optional[str] = None         # "trend_up"|"trend_down"|None(单中枢未定)

        for i in range(1, len(done_zss)):
            zi = done_zss[i]
            if cur is None:                   # 上一走势类型被背驰终结，zi 另起
                cur, cur_start, cur_dir = [zi], i, None
                continue

            rel = classify_rel(cur[-1], zi)   # "trend_up"|"trend_down"|"expand"
            if len(cur) >= 2:
                boundary = rel != cur_dir     # 趋势：方向不一致(含 expand)即断裂
            else:
                boundary = rel == "expand"    # 单中枢：expand 断裂；trend_* 延续成趋势

            if boundary:
                wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                cur, cur_start, cur_dir = [zi], i, None
            else:
                cur.append(zi)
                if len(cur) == 2:
                    cur_dir = rel             # 趋势方向坐实(trend_up/down)

            # 背驰边界（仅非断裂时；zi 刚并入 cur，其背驰 = done_divergence[i]）
            if not boundary and cur is not None:
                dv = done_divergence[i]
                if dv is not None and dv.is_beichi:
                    wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                    cur, cur_dir = None, None

        if cur is not None:
            wts.append(self._finalize(cur, cur_start, cur_dir, done=False))
        return wts
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zslx_branch.py -q`
Expected: PASS（Task 1 的 2 个 + Task 2 的 6 个 = 8 个全绿）

> 执行注记：若某断言因 `classify_rel`(本体包络)对所造中枢的实际判定与预期不符而失败，先临时 `print([classify_rel(zss[k], zss[k+1]) for k in range(len(zss)-1)])` 看实际相邻关系，再据实微调 `_zs_at` 的 `lo/hi`（让本体包络呈现期望的 trend_up/trend_down/expand），保持测试**意图**（趋势延续/方向断裂/expand断裂/背驰终结）不变。

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zslx_branch.py tests/core/test_zslx_branch.py
git commit -m "feat(core/zslx_branch): calculate 双信号状态机(classify_rel方向断裂+done_divergence背驰)(P4a)"
```

---

## Task 3: 全套回归（不破坏现有 + lint）

**Files:** 无新增改动，纯验证。

- [ ] **Step 1: 跑 zslx_branch 全套**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zslx_branch.py -q 2>&1 | tail -3`
Expected: PASS（8 passed）

- [ ] **Step 2: 跑 core 全套（防跨模块回归）**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/ -q 2>&1 | tail -3`
Expected: PASS（旧的 229 + 新 8 = 237 passed，0 failed）

- [ ] **Step 3: lint**

Run: `cd D:/project/chanlun-pro && poetry run ruff check src/chanlun/core/zslx_branch.py`
Expected: All checks passed!（若有未用 import 清理后重跑）

- [ ] **Step 4: 提交（仅当 Step 1-3 有清理改动时）**

```bash
git add src/chanlun/core/zslx_branch.py
git commit -m "chore(core/zslx_branch): P4a lint 清理 + 全套回归绿"
```

---

## Task 4: 真实数据出图验收（人工审）

**Files:**
- Create（本地不入库）: `scripts_local/probe_p4a_review.py`
- Output: `zslx_branch_review.html`

- [ ] **Step 1: 写验收 probe 脚本**

```python
# scripts_local/probe_p4a_review.py —— P4a 真实数据出图验收（本地临时，不入库）
import logging
import pandas as pd
import plotly.graph_objects as go

logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator
from chanlun.core.zslx_branch import ZslxBranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)
res = ZsBranchCalculator(ld_provider=ld, frequency="1m").calculate(bis)
zslxs = ZslxBranchCalculator().calculate(res.done_zss, res.done_divergence)

ks = cd.get_klines()
fig = go.Figure(go.Candlestick(
    x=[k.date for k in ks], open=[k.o for k in ks], high=[k.h for k in ks],
    low=[k.l for k in ks], close=[k.c for k in ks], name="K", opacity=0.3))
# 连续笔线
bx = [bis[0].start.k.date] + [b.end.k.date for b in bis]
by = [bis[0].start.val] + [b.end.val for b in bis]
fig.add_trace(go.Scatter(x=bx, y=by, mode="lines", line=dict(color="#333", width=1), name="笔"))

COLOR = {"上涨": "rgba(220,40,40,0.10)", "下跌": "rgba(40,140,40,0.10)", "盘整": "rgba(120,120,120,0.10)"}
for w in zslxs:
    x0 = (w.start_line.start.k.date)
    x1 = (w.end_line.end.k.date)
    lo = min(zs.dd for zs in w.zss); hi = max(zs.gg for zs in w.zss)
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=lo, y1=hi,
                  fillcolor=COLOR.get(w.zslx_type, "rgba(0,0,0,0.05)"),
                  line=dict(width=1.2, color="gray"), layer="below")
    fig.add_annotation(x=x1, y=hi, text=f"{w.zslx_type}{'·done' if w.done else '·live'}",
                       showarrow=False, font=dict(size=11), bgcolor="rgba(255,255,255,0.6)")

print(f"走势类型数={len(zslxs)}:",
      [(w.zslx_type, w.done, len(w.zss)) for w in zslxs])
fig.update_layout(title="P4a 走势类型划分验收 (a_SH_513100_1m·笔级)｜色块=走势类型(红涨/绿跌/灰盘整)",
                  xaxis_rangeslider_visible=False, height=820)
fig.write_html("zslx_branch_review.html")
print("written zslx_branch_review.html")
```

- [ ] **Step 2: 运行 probe**

Run: `cd D:/project/chanlun-pro && PYTHONPATH=src poetry run python scripts_local/probe_p4a_review.py`
Expected: 打印 `走势类型数=N: [(类型,done,中枢数)...]`、`written zslx_branch_review.html`，无异常。

- [ ] **Step 3: 加 .gitignore + 人工审**

把 `zslx_branch_review.html` 加进 `.gitignore`（紧接已有 `zs_branch_review.html` 行后加一行 `zslx_branch_review.html`），然后交付用户审：走势类型边界是否落在背驰/方向断裂处、红涨/绿跌/灰盘整分段是否合理。

- [ ] **Step 4: 据审图决定**

- 图 OK → P4a 完成，更新 memory，转 P4b。
- 图有问题 → 回到对应 Task 修（多半是 `classify_rel` 边界口径或 `_finalize` 边界提取），补测试再验。

> probe 脚本与 html 是本地件，不 `git add`（`scripts_local/` 已 gitignore）。

---

## Self-Review（计划对照 spec）

**1. Spec 覆盖：**
- §2 模块/接口（孤立、依赖 classify_rel+DivergenceResult、不依赖 beichi）→ Task 1 import ✓
- §3 双信号状态机（classify_rel 方向断裂 + done_divergence 背驰）→ Task 2 `calculate` ✓
- §3 已知缺口（boundary 另起当轮不查背驰）→ 实现照旧 zslx 结构，缺口保留 ✓
- §4 _finalize（盘整/趋势分类、边界 a/b 含进入/离开段、zs_high/zs_low 包络）→ Task 1 ✓
- §5 口径决策（classify_rel/done_divergence/expand 边界/kind 标注/live 留后）→ Task 1/2 ✓
- §6 测试 + 出图 → Task 1/2/3/4 ✓
- §0 不含（live 多假设/P4b/P4c/不接 CL/不动旧 zslx）→ 不在任何 Task（正确排除）✓

**2. Placeholder 扫描：** 无 TBD/TODO；每个 code step 给完整代码；Task 2 的「执行注记」是真实调试指引（主体代码完整）。

**3. 类型/签名一致：** `_finalize(zss, start_idx, cur_dir, done)` Task 1 定义、Task 2 `calculate` 调用一致；`calculate(done_zss, done_divergence)` 签名 spec/Task 一致；`classify_rel`/`DivergenceResult`/`ZSLX` 签名与源文件实际一致（`ZSLX(zslx_level,start,end,start_line,end_line,_type,index,done)`、`DivergenceResult(is_beichi,kind,compare_seg,leave_seg,provisional)`）。

无 gap，无需补 Task。
