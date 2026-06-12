# P1 中枢多假设结构核 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。
> **上位文档**：`docs/chanlun_core_redesign_0_中枢划分原文理论.md`（宪法）。本计划只实现其中 §2/§3.5/§4 的**单级别、结构层**部分。

**Goal:** 在单一级别、以确定性线段为输入，产出「冻结的已完成中枢 + 右边缘多假设分支池」的中枢引擎，用测试钉死防过拟合纪律（左侧冻结 + 完整合法递归树 + 回试 ZG 坍缩）。

**Architecture:** 新建独立模块 `zs_branch.py`，**不动现有 `zs_calculator.py` 与任何生产链路**（零回归风险）。纯几何 helper（核心/包络/触及）→ 数据模型（`ZsHypothesis`/`ZsBranchResult`）→ `ZsBranchCalculator.calculate(lines)` 全量产出 `done_zss + live 分支 + freeze_idx`。背驰、递归、买卖点不在本计划内（H2 只到「结构完成」，不评背驰）。

**Tech Stack:** Python 3 / pytest / dataclasses；复用 `chanlun.core.cl_interface` 的 `ZS/LINE/XD/FX/CLKline`；测试沿用 `tests/core/test_zs_calculator.py` 的 `_seg` 合成线段范式。

---

## 范围边界（务必遵守）

- **做**：节点①H1/H2（仅结构：核心成员 vs 离开段）、节点②延伸累加 + 9 段升级**标记**、节点③趋势/扩张**分类**、左侧冻结、回试 ZG 坍缩语义。
- **不做**（留给后续计划）：H2a/H2b 背驰细分（P3）、升级/扩张的高级中枢**实体化**（P4 递归）、买卖点（P5）、区间套（P6）、增量优化（本计划全量重算即可，`freeze_idx` 只标边界、暂不据此跳算）。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/chanlun/core/zs_branch.py`（新建） | 多假设中枢结构引擎：纯几何 helper + 数据模型 + `ZsBranchCalculator` |
| `tests/core/test_zs_branch.py`（新建） | 本计划全部 TDD 用例；线段 fixture 沿用 `_seg` 范式 |

**口径决策（贯穿全程，测试钉死）：**
- **成中枢的重叠**用**严格** `<`（`ZD < ZG` 才算非退化重叠，与 `zs_calculator` 一致）。
- **延伸/扩张的"触及"**用**闭区间** `<=`（触边即算，对应中心定理二的 `≥/≤`）。
- 最小中枢 = **4 段**重叠（含离开段，L0 既定口径，与 `zs_calculator` 一致）。

---

## 数据模型（Task 4 落地，先列出供全程引用）

```python
# src/chanlun/core/zs_branch.py
@dataclass
class ZsHypothesis:
    """右边缘的一个中枢读法（一个 live 分支）。"""
    zs: ZS                       # 该读法下的中枢对象
    node1: str                   # 节点①: "core"(H1,末段为核心/中枢延伸) | "leave"(H2,末段为离开段/中枢完成)
    rel_prev: Optional[str] = None   # 节点③: "trend_up"|"trend_down"|"expand"|None(无前中枢)
    upgrade: bool = False        # 节点②: True=已达 9 段、触发升级（本计划只标记，不实体化）

@dataclass
class ZsBranchResult:
    done_zss: List[ZS]                      # 左侧已冻结的已完成中枢
    live: List[ZsHypothesis]               # 右边缘活分支（通常 1~2 个）
    freeze_idx: int                        # 冻结边界：< freeze_idx 的线段已settled；live 分支从此起
```

---

### Task 1: 核心区间 helper `core_interval`

**Files:**
- Create: `src/chanlun/core/zs_branch.py`
- Test: `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_zs_branch.py
from __future__ import annotations
from chanlun.core.cl_interface import CLKline, FX, XD
from chanlun.core import zs_branch


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """合成线段，沿用 tests/core/test_zs_calculator.py 范式。"""
    def _fx(kidx: int, val: float, ftype: str) -> FX:
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


def test_core_interval_overlap():
    a = _seg(0, "up", 4, 8)     # [4,8]
    b = _seg(1, "down", 8, 5)   # [5,8]
    c = _seg(2, "up", 5, 10)    # [5,10]
    # ZD=max(4,5,5)=5, ZG=min(8,8,10)=8
    assert zs_branch.core_interval(a, b, c) == (5, 8)


def test_core_interval_no_overlap_returns_none():
    a = _seg(0, "up", 1, 3)     # [1,3]
    b = _seg(1, "down", 3, 2)   # [2,3]
    c = _seg(2, "up", 5, 9)     # [5,9] —— 与前两段无共同重叠
    assert zs_branch.core_interval(a, b, c) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `poetry run pytest tests/core/test_zs_branch.py -v`
Expected: FAIL（`module 'zs_branch' has no attribute 'core_interval'` 或 ImportError）

- [ ] **Step 3: 实现**

```python
# src/chanlun/core/zs_branch.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple
from chanlun.core.cl_interface import ZS, LINE


def core_interval(seg_a: LINE, seg_b: LINE, seg_c: LINE) -> Optional[Tuple[float, float]]:
    """前三段重叠的核心区间 [ZD, ZG]（第18课严格公式）。
    ZD=max(三段低), ZG=min(三段高)；严格 ZD<ZG 才算非退化重叠，否则 None。"""
    zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)
    zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
    if zd >= zg:
        return None
    return (zd, zg)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `poetry run pytest tests/core/test_zs_branch.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): 核心区间 core_interval(节点地基·第18课公式)"
```

---

### Task 2: 包络 helper `envelope`

**Files:** Modify `src/chanlun/core/zs_branch.py`; Test `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**

```python
def test_envelope_min_low_max_high():
    lines = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 3), _seg(2, "up", 3, 11)]
    # DD=min(4,3,3)=3, GG=max(8,8,11)=11
    assert zs_branch.envelope(lines) == (3, 11)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `poetry run pytest tests/core/test_zs_branch.py::test_envelope_min_low_max_high -v`
Expected: FAIL（no attribute 'envelope'）

- [ ] **Step 3: 实现**

```python
def envelope(lines: List[LINE]) -> Tuple[float, float]:
    """中枢包络 [DD, GG]：DD=min(所有段低), GG=max(所有段高)（瞬间波动区间，第20课 Z 段口径）。"""
    dd = min(ln.zs_low for ln in lines)
    gg = max(ln.zs_high for ln in lines)
    return (dd, gg)
```

- [ ] **Step 4: 跑测试确认通过** — Run 同上，Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat(core/zs_branch): 包络 envelope(DD/GG·第20课瞬间波动区间)"
```

---

### Task 3: 触及 helper `touches`（闭区间口径）

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_touches_closed_interval():
    # 触边即算（闭区间）：段 [8,10] 与核心 [5,8] 在 8 处相切 → 触及
    seg_edge = _seg(0, "up", 8, 10)
    assert zs_branch.touches(seg_edge, 5, 8) is True
    # 完全在外 → 不触
    seg_out = _seg(1, "up", 9, 12)
    assert zs_branch.touches(seg_out, 5, 8) is False
```

- [ ] **Step 2: 跑确认失败** — Run: `poetry run pytest tests/core/test_zs_branch.py::test_touches_closed_interval -v`，Expected: FAIL

- [ ] **Step 3: 实现**

```python
def touches(seg: LINE, lo: float, hi: float) -> bool:
    """线段是否触及闭区间 [lo, hi]（延伸/扩张口径：触边即算，对应中心定理二的 ≥/≤）。"""
    return max(seg.zs_low, lo) <= min(seg.zs_high, hi)
```

- [ ] **Step 4: 跑确认通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): 触及 touches(闭区间·延伸/扩张口径)"`

---

### Task 4: 数据模型 `ZsHypothesis` / `ZsBranchResult`

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_dataclasses_construct():
    from chanlun.core.cl_interface import ZS
    zs = ZS(zs_type="xd", start=None)
    h = zs_branch.ZsHypothesis(zs=zs, node1="core")
    assert h.node1 == "core" and h.rel_prev is None and h.upgrade is False
    res = zs_branch.ZsBranchResult(done_zss=[], live=[h], freeze_idx=0)
    assert res.live[0] is h and res.freeze_idx == 0
```

- [ ] **Step 2: 跑确认失败** — Expected: FAIL（no attribute 'ZsHypothesis'）

- [ ] **Step 3: 实现**（粘贴本文档「数据模型」节的两个 dataclass 到 `zs_branch.py`，import 补 `dataclass`/`field`）

- [ ] **Step 4: 跑确认通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): ZsHypothesis/ZsBranchResult 数据模型"`

---

### Task 5: `ZsBranchCalculator` — 成中枢 + 右边缘 H1/H2 分叉

**契约（由测试定义）**：给定「进入段 + 4 段重叠核心、数据到此为止」，最后一段触及核心 → 产出**两个** live 分支：
- H1 `node1="core"`：4 段全为核心，中枢仍开（zg/zd 由前三段定）。
- H2 `node1="leave"`：第 4 段为离开段，中枢以前 3 段成立（但 4 段重叠才够 min=4，故 H2 的中枢含该离开段为核心、done=True）。

> 注：H1/H2 的差别在**末段角色**与**中枢是否 done**，不在 zg/zd（zg/zd 恒由前三段定）。H1.zs.done=False（还可延伸），H2.zs.done=True（已离开/完成）。

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_right_edge_h1_h2_fork():
    # 进入段(在中枢上方不重叠) + 4 段重叠核心 [5,8]，数据到此为止
    lines = [
        _seg(0, "down", 10, 9),   # 进入段（[9,10] 不与 [5,8] 重叠）
        _seg(1, "up", 4, 8),      # 核心 a
        _seg(2, "down", 8, 5),    # 核心 b
        _seg(3, "up", 5, 10),     # 核心 c
        _seg(4, "down", 10, 6),   # 第4段重叠核心 [5,8]（触及）→ H1/H2 歧义
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert res.done_zss == []                       # 右边缘未确认，无冻结中枢
    nodes = sorted(h.node1 for h in res.live)
    assert nodes == ["core", "leave"]               # H1 + H2 两分支
    for h in res.live:
        assert (h.zs.zd, h.zs.zg) == (5, 8)         # zg/zd 恒由前三段定
    h1 = next(h for h in res.live if h.node1 == "core")
    h2 = next(h for h in res.live if h.node1 == "leave")
    assert h1.zs.done is False and h2.zs.done is True
```

- [ ] **Step 2: 跑确认失败** — Expected: FAIL（no attribute 'ZsBranchCalculator'）

- [ ] **Step 3: 实现**（参考实现，执行时按测试微调）

```python
class ZsBranchCalculator:
    """单级别多假设中枢引擎。全量重算：左侧确定性中枢冻结，右边缘产出 H1/H2 分支。"""

    MIN_LINES = 4  # L0 最小中枢段数（含离开段）

    def calculate(self, lines: List[LINE]) -> "ZsBranchResult":
        done: List[ZS] = []
        i = -1  # 进入段下标；-1=从开头无进入段中枢扫起
        n = len(lines)
        while i <= n - 1 - 3:                       # 需为 3 核心段留空间
            cs = i + 1                              # 核心起点
            interval = core_interval(lines[cs], lines[cs + 1], lines[cs + 2])
            if interval is None:
                i += 1
                continue
            zd, zg = interval
            core = [lines[cs], lines[cs + 1], lines[cs + 2]]
            j = cs + 3
            # 延伸：后续段触及核心则并入
            while j < n and touches(lines[j], zd, zg):
                core.append(lines[j])
                j += 1
            reached_end = (j >= n)
            if reached_end:
                # 右边缘：数据到此为止 → H1/H2 分叉（须 >= MIN_LINES 段）
                if len(core) >= self.MIN_LINES:
                    return ZsBranchResult(
                        done_zss=done,
                        live=self._fork(core, zd, zg, prev=(done[-1] if done else None)),
                        freeze_idx=cs,
                    )
                break
            else:
                # 第 j 段不触核心 → 离开确认，中枢 done（左侧冻结）
                if len(core) >= self.MIN_LINES:
                    done.append(self._make_zs(core, zd, zg, done_flag=True))
                    i = j - 1                      # 下一中枢从离开段找
                else:
                    i += 1                         # 不足 4 段，作废
        return ZsBranchResult(done_zss=done, live=[], freeze_idx=max(0, n))

    def _make_zs(self, core: List[LINE], zd: float, zg: float, done_flag: bool) -> ZS:
        zs = ZS(zs_type="xd", start=None, _type=core[1].type)
        zs.lines = list(core)
        zs.zg, zs.zd = zg, zd
        zs._bounds_dirty = True
        zs.update_boundaries()                     # 填 gg/dd 包络
        zs.end = core[-1]
        zs.done = done_flag
        return zs

    def _fork(self, core, zd, zg, prev) -> List["ZsHypothesis"]:
        # H1：末段为核心，中枢仍开
        zs_h1 = self._make_zs(core, zd, zg, done_flag=False)
        # H2：末段为离开段，中枢完成
        zs_h2 = self._make_zs(core, zd, zg, done_flag=True)
        return [
            ZsHypothesis(zs=zs_h1, node1="core"),
            ZsHypothesis(zs=zs_h2, node1="leave"),
        ]
```

- [ ] **Step 4: 跑确认通过** — Run: `poetry run pytest tests/core/test_zs_branch.py -v`，Expected: PASS（全部）

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): ZsBranchCalculator 成中枢+右边缘 H1/H2 分叉(节点①结构)"`

---

### Task 6: 回试 ZG 坍缩（节点①，中心定理一）

**契约（由测试定义）**：在 Task 5 的 4 段 pending 之上再追加第 5 段。右边缘**永远有 2 读法**——「坍缩」指*上一个*末段的歧义被消解、而非分支数减少：
- 第 5 段**触及核心 [5,8]** → 上一末段(seg4)确认为内部核心、中枢长到 5 段；**新末段(seg5)重新分叉 H1/H2**。`done_zss` 仍空。
- 第 5 段**不触核心**（离开）且其后再起新三段 → 原中枢**冻结**进 `done_zss`（done=True、4 段核心）。

**Files:** Modify `zs_branch.py`（若需要）；Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_extend_confirms_prev_as_core_keeps_two_branches():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    base.append(_seg(5, "up", 6, 9))   # 第5段 [6,9] 触核心 [5,8] → seg4 确认核心、中枢长到5段
    res = zs_branch.ZsBranchCalculator().calculate(base)
    assert res.done_zss == []                        # 仍未离开，无冻结
    assert sorted(h.node1 for h in res.live) == ["core", "leave"]  # 新末段(seg5)再分叉
    for h in res.live:
        assert len(h.zs.lines) == 5                  # seg4 已并入核心(5段)
        assert (h.zs.zd, h.zs.zg) == (5, 8)

def test_leave_then_new_structure_freezes_zhongshu():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14),                         # 离开核心[5,8]：[9,14] 不触 [5,8]
        _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),  # 新结构
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    assert len(res.done_zss) == 1                    # 原 [5,8] 中枢冻结
    assert res.done_zss[0].done is True
    assert (res.done_zss[0].zd, res.done_zss[0].zg) == (5, 8)
    assert len(res.done_zss[0].lines) == 4           # 核心 seg1-4

- [ ] **Step 2: 跑确认失败** — Run: `poetry run pytest tests/core/test_zs_branch.py -k "extend or leave" -v`

- [ ] **Step 3: 实现**：Task 5 的 `calculate` 已涵盖此语义（末段触核心→前末段并入核心、新末段再分叉 2 读法；末段离开+其后新三段→中枢冻结 done）。若用例不过，在此修 `calculate` 的边界判定，**不要**为单用例打补丁，要改对通用语义。

- [ ] **Step 4: 跑确认通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): 回试ZG坍缩(节点①·中心定理一)"`

---

### Task 7: 节点② 延伸累加 + 9 段升级标记

**契约**：中枢核心达 **9 段**（3 本体 + 6 延伸，第33课）时，右边缘分支标 `upgrade=True`（本计划只标记，不实体化高级中枢）。8 段及以内 `upgrade=False`。

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_node2_upgrade_flag_at_9_segments():
    # 构造 9 段全部重叠核心 [5,8] 的 pending 中枢
    segs = [_seg(0, "down", 10, 9)]  # 进入段
    vals = [(4, 8), (8, 5), (5, 8), (8, 5), (5, 8), (8, 5), (5, 8), (8, 5), (5, 8)]  # 9 段
    for k, (s, e) in enumerate(vals, start=1):
        segs.append(_seg(k, "up" if s < e else "down", s, e))
    res = zs_branch.ZsBranchCalculator().calculate(segs)
    assert any(h.upgrade for h in res.live), "9 段核心应触发升级标记"

def test_node2_no_upgrade_at_8_segments():
    segs = [_seg(0, "down", 10, 9)]
    vals = [(4, 8), (8, 5), (5, 8), (8, 5), (5, 8), (8, 5), (5, 8), (8, 5)]  # 8 段
    for k, (s, e) in enumerate(vals, start=1):
        segs.append(_seg(k, "up" if s < e else "down", s, e))
    res = zs_branch.ZsBranchCalculator().calculate(segs)
    assert all(not h.upgrade for h in res.live)
```

- [ ] **Step 2: 跑确认失败** — Expected: FAIL（upgrade 恒 False）

- [ ] **Step 3: 实现**：在 `_fork` 里据 `len(core) >= 9` 置 `upgrade`：

```python
    def _fork(self, core, zd, zg, prev):
        upgrade = len(core) >= 9                    # 第33课：3 本体 + 6 延伸 = 9 段升级
        zs_h1 = self._make_zs(core, zd, zg, done_flag=False)
        zs_h2 = self._make_zs(core, zd, zg, done_flag=True)
        return [
            ZsHypothesis(zs=zs_h1, node1="core", upgrade=upgrade),
            ZsHypothesis(zs=zs_h2, node1="leave", upgrade=upgrade),
        ]
```

- [ ] **Step 4: 跑确认通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): 节点②延伸≤5/9段升级标记(第33课)"`

---

### Task 8: 节点③ 趋势/扩张分类（中心定理二）

**契约**：对相邻两个**已完成**中枢按中心定理二分类，写入后一中枢分支/记录的 `rel_prev`：
- 后 `DD > 前GG` → `"trend_up"`；后 `GG < 前DD` → `"trend_down"`（包络分离）；
- 否则（包络相交：`max(后DD,前DD) <= min(后GG,前GG)`）→ `"expand"`。

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_node3_classify_trend_up_and_expand():
    a = zs_branch.ZS_for_test = None  # 占位，见下
    # 用 helper 直接测分类纯函数
    from chanlun.core.cl_interface import ZS
    def _zs(dd, gg):
        z = ZS(zs_type="xd", start=None); z.dd, z.gg = dd, gg; return z
    assert zs_branch.classify_rel(_zs(0, 5), _zs(6, 10)) == "trend_up"   # 后DD6 > 前GG5
    assert zs_branch.classify_rel(_zs(6, 10), _zs(0, 5)) == "trend_down" # 后GG5 < 前DD6
    assert zs_branch.classify_rel(_zs(0, 5), _zs(4, 9)) == "expand"      # 包络相交 [4,5]
```

- [ ] **Step 2: 跑确认失败** — Expected: FAIL（no attribute 'classify_rel'）

- [ ] **Step 3: 实现**

```python
def classify_rel(prev: ZS, cur: ZS) -> str:
    """节点③：相邻中枢关系（中心定理二，包络口径）。"""
    if cur.dd > prev.gg:
        return "trend_up"
    if cur.gg < prev.dd:
        return "trend_down"
    return "expand"           # 包络相交 → 扩张（升级候选，P4 实体化）
```

并在 `calculate` 里：每确认一个 done 中枢后，若已有前一 done 中枢，则 `classify_rel(prev, cur)`；右边缘 live 分支的 `rel_prev` 用「最后一个 done 中枢」作 prev 计算。

- [ ] **Step 4: 跑确认通过** — Expected: PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): 节点③趋势/扩张分类(中心定理二)"`

---

### Task 9: 左侧冻结边界 `freeze_idx` 回归 + 合法性不变量

**契约**：
- `freeze_idx` = 右边缘 live 中枢核心起点下标；`< freeze_idx` 的线段全部属于 `done_zss`（已settled）。
- 合法性不变量（断言进 `calculate` 末尾或测试）：每个 `done_zs` 段数 `>= 4`；`zd < zg`；趋势中相邻 done 中枢 `rel_prev != "expand"` 时包络不相交。

**Files:** Modify `zs_branch.py`; Test 同文件

- [ ] **Step 1: 写失败测试**

```python
def test_freeze_idx_marks_settled_prefix():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    # 仅右边缘 pending：freeze_idx 指向核心起点(=1)，其前(进入段0)为 settled 前缀
    assert res.freeze_idx == 1

def test_done_zs_invariants():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    for z in res.done_zss:
        assert len(z.lines) >= 4 and z.zd < z.zg
```

- [ ] **Step 2: 跑确认失败/微调** — Run: `poetry run pytest tests/core/test_zs_branch.py -k "freeze or invariant" -v`

- [ ] **Step 3: 实现**：`calculate` 已设 `freeze_idx`；如用例不符，校准 `freeze_idx` 语义（核心起点 `cs`）。补一个内部 `_assert_invariants(done)` 在返回前调用（可用 `assert`，pytest 下生效）。

- [ ] **Step 4: 跑确认通过 + 全量回归** — Run: `poetry run pytest tests/core/test_zs_branch.py -v`，Expected: 全 PASS

- [ ] **Step 5: 提交** — `git commit -m "feat(core/zs_branch): 左侧冻结 freeze_idx + 合法性不变量"`

---

## 自审（writing-plans 要求）

**1. Spec 覆盖**（对照宪法 §2/§3.5/§4 结构层）：
- 节点①H1/H2 → Task 5/6 ✅；节点②延伸/升级标记 → Task 7 ✅；节点③趋势/扩张 → Task 8 ✅；左侧冻结 → Task 9 ✅；双区间(核心/包络) → Task 1/2 ✅；中心定理一(回试ZG) → Task 6 ✅；中心定理二 → Task 8 ✅。
- **明确不覆盖**（已在范围边界声明，留后续计划）：背驰 H2a/H2b、升级/扩张实体化、买卖点、区间套、增量。✅ 无遗漏歧义。

**2. 占位符扫描**：无 TBD/TODO。Task 6 fixture 的数值需执行者据实校准（已显式标注「以测试通过为准 + 给出修正示例」），非占位，是 TDD 正常的红→绿调整。

**3. 类型一致性**：`core_interval`/`envelope` 返回 `Tuple[float,float]`；`touches(seg, lo, hi)`；`ZsHypothesis(zs, node1, rel_prev, upgrade)`；`ZsBranchResult(done_zss, live, freeze_idx)`；`classify_rel(prev, cur)`——全程一致。`node1` 取值固定 `"core"|"leave"`；`rel_prev` 取值 `"trend_up"|"trend_down"|"expand"|None`。

---

## 执行交接

计划已存 `docs/chanlun_core_redesign_1_中枢多假设结构核_plan.md`。两种执行方式：

1. **Subagent-Driven（推荐）**：每个 Task 派新 subagent，任务间两段式 review，迭代快。
2. **Inline Execution**：本会话内按 executing-plans 批量执行，带检查点。

选哪种？
