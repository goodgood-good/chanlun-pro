# P5b 二类买卖点（定律一 · 次级别一类递归）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 从 recursive_branch 多级 LevelResult，产二类买卖点 = L_k 一买之后、次级别 L_{k-1} 时间在后且不破前低的第一个一买（买卖点定律一）。

**Architecture:** 新建孤立 `bs2_branch.py`，无状态 `Bs2BranchCalculator.calculate(levels)→List[BuySellPoint]`；各级识别 qs 一类点、跨级关联二类。`BuySellPoint` 加可选 `level` 字段（P5a 不破）。不接 CL。

**Tech Stack:** Python 3、dataclass、pytest、poetry、ruff、plotly（验收）。

设计见 `docs/chanlun_core_redesign_5b_二类买卖点_design.md`。

---

## File Structure

- **Modify:** `src/chanlun/core/bs_branch.py` — `BuySellPoint` 加 `level: Optional[int]=None`（唯一上游改动）。
- **Create:** `src/chanlun/core/bs2_branch.py` — `Bs2BranchCalculator`(`_first_points`/`_find_second`/`calculate`)。
- **Test:** `tests/core/test_bs2_branch.py` — 受控多级 `LevelResult`+fake `DivergenceResult`。
- **Probe（gitignored）:** `scripts_local/probe_p5b_bs2.py` — 真实负向 + 受控演示。
- **不改:** `recursive_branch.py`/CL/旧 `bs_point_calculator.py`。

---

## Task 1: `BuySellPoint` 加可选 `level` 字段（上游改动 + P5a 回归）

**Files:**
- Modify: `src/chanlun/core/bs_branch.py`

- [ ] **Step 1: 改 `BuySellPoint`**（在 `divergence` 字段后加一行）

```python
@dataclass
class BuySellPoint:
    """一个买卖点信号。"""
    bs_type: str                              # "1buy" | "1sell" | "3buy" | "3sell" | "2buy" | "2sell"
    zs: ZS                                    # 关联中枢
    signal_seg: LINE                          # 信号段(一类=背驰离开段 c;三类=回试段;二类=次级别一买离开段)
    anchor_fx: FX                             # 出图锚点
    divergence: Optional[DivergenceResult]    # 一类/二类带背驰本体;三类 None
    level: Optional[int] = None               # P5b:二类归属级别 L_k;P5a 一三类 None
```

- [ ] **Step 2: 跑 P5a 回归验证不破**

Run: `poetry run pytest tests/core/test_bs_branch.py -q`
Expected: PASS（13 passed —— positional 5 参构造不破，level 默认 None）

- [ ] **Step 3: ruff + commit**

Run: `poetry run ruff check src/chanlun/core/bs_branch.py`
Expected: All checks passed!

```bash
git add src/chanlun/core/bs_branch.py
git commit -m "feat(core/bs_branch): BuySellPoint 加可选 level 字段(P5b 二类归属级别;P5a 默认 None 不破)"
```

---

## Task 2: `bs2_branch.py`（一类识别 + 二类关联）+ 受控测试 + 全套回归

**Files:**
- Create: `src/chanlun/core/bs2_branch.py`
- Test: `tests/core/test_bs2_branch.py`

- [ ] **Step 1: 写失败测试**（`tests/core/test_bs2_branch.py`）

```python
"""tests/core/test_bs2_branch.py — P5b 二类买卖点 TDD。

受控多级 LevelResult + fake DivergenceResult（_seg 范式造 leave_seg）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.bs2_branch import Bs2BranchCalculator


def _fx(kidx, val, ftype):
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(idx, _type, sv, ev) -> XD:
    if _type == "up":
        s, e = _fx(idx, sv, "di"), _fx(idx + 1, ev, "ding")
    else:
        s, e = _fx(idx, sv, "ding"), _fx(idx + 1, ev, "di")
    xd = XD(start=s, end=e, _type=_type, index=idx)
    xd.done = True
    return xd


def _zs() -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = [_seg(0, "up", 6, 9)]
    z.zd, z.zg = 6, 9
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _dv(_type, leave_seg, kind="qs", is_beichi=True, provisional=False) -> DivergenceResult:
    return DivergenceResult(is_beichi=is_beichi, kind=kind,
                            compare_seg=leave_seg, leave_seg=leave_seg, provisional=provisional)


def _lr(level, dvs) -> LevelResult:
    return LevelResult(level=level, zss=[_zs() for _ in dvs],
                       done_divergence=list(dvs), zslxs=[], upgrade_idx=[])


def test_basic_2buy():
    # L1 一买(c向下,end k=10 val=5) + L0 后续一买(end k=12 val=6,≥5,>10) → L1 二买
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 6)
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1
    assert pts[0].bs_type == "2buy" and pts[0].level == 1
    assert pts[0].anchor_fx is c_sub.end
    assert pts[0].divergence is not None


def test_2buy_breaks_prev_low_filtered():
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 3)               # 低点 3 < 5 破前低 → 跳过
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2buy_sub_before_lk_filtered():
    c_k = _seg(9, "down", 10, 5)                  # t=10
    c_sub = _seg(3, "down", 9, 6)                 # end k=4 < 10 在前 → 不算
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2buy_takes_first():
    c_k = _seg(9, "down", 10, 5)
    c_a = _seg(11, "down", 9, 6)                  # end k=12
    c_b = _seg(15, "down", 9, 7)                  # end k=16 更晚
    levels = [_lr(0, [_dv("down", c_a), _dv("down", c_b)]), _lr(1, [_dv("down", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1 and pts[0].anchor_fx is c_a.end   # 取最早


def test_2buy_same_direction_only():
    # L0 只有一卖(向上),L1 一买无同向配对 → 无二买
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "up", 6, 11)
    levels = [_lr(0, [_dv("up", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_l0_no_second():
    c = _seg(9, "down", 10, 5)
    levels = [_lr(0, [_dv("down", c)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2sell_symmetric():
    # L1 一卖(c向上,high=15) + L0 后续一卖(high=14≤15,>10) → L1 二卖
    c_k = _seg(9, "up", 5, 15)
    c_sub = _seg(11, "up", 6, 14)
    levels = [_lr(0, [_dv("up", c_sub)]), _lr(1, [_dv("up", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1 and pts[0].bs_type == "2sell" and pts[0].level == 1


def test_provisional_excluded():
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 6)
    levels = [_lr(0, [_dv("down", c_sub, provisional=True)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_empty_returns_empty():
    assert Bs2BranchCalculator().calculate([]) == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_bs2_branch.py -q`
Expected: FAIL（`ModuleNotFoundError: bs2_branch`）

- [ ] **Step 3: 写实现**（`src/chanlun/core/bs2_branch.py`）

```python
"""bs2_branch.py — P5b 缠论二类买卖点（定律一 · 次级别一类递归）。

从 recursive_branch 多级 LevelResult 产二类买卖点：L_k 一买之后、次级别 L_{k-1}
时间在后且不破前低的第一个一买 = L_k 二买（买卖点定律一,原文 3562/3598）。孤立、
不接 CL、不动旧 bs_point_calculator。设计见 docs/chanlun_core_redesign_5b_二类买卖点_design.md。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE, ZS
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.bs_branch import BuySellPoint


class Bs2BranchCalculator:
    """二类买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        """各级识别一类点,跨级关联二类:L_k 一买后、L_{k-1} 不破前低的第一个一买。"""
        first_by_level = {lr.level: self._first_points(lr) for lr in levels}
        out: List[BuySellPoint] = []
        for lr in levels:
            k = lr.level
            if k == 0:                                   # L0 无次级别 → 无二买
                continue
            sub = first_by_level.get(k - 1, [])          # 次级别 L_{k-1} 一类点
            for _zs_k, _dv_k, c_k in self._first_points(lr):
                found = self._find_second(c_k, sub)
                if found is not None:
                    zs_sub, dv_sub, c_sub = found
                    bs = "2buy" if c_k._type == "down" else "2sell"
                    out.append(BuySellPoint(bs, zs_sub, c_sub, c_sub.end, dv_sub, level=k))
        return out

    @staticmethod
    def _first_points(level: LevelResult) -> List[Tuple[ZS, DivergenceResult, LINE]]:
        """该级已固化一类点:(zs, divergence, 离开段 c)。仅趋势背驰 qs、非 provisional。"""
        out: List[Tuple[ZS, DivergenceResult, LINE]] = []
        for i, dv in enumerate(level.done_divergence):
            if dv is not None and dv.is_beichi and dv.kind == "qs" and not dv.provisional:
                out.append((level.done_zss[i], dv, dv.leave_seg))
        return out

    @staticmethod
    def _find_second(c_k: LINE,
                     sub: List[Tuple[ZS, DivergenceResult, LINE]]
                     ) -> Optional[Tuple[ZS, DivergenceResult, LINE]]:
        """L_k 一类点 c_k 之后,次级别同向、不破前低/高的第一个(时间最早)一类点。"""
        t_k = c_k.end.k.k_index
        val_k = c_k.end.val
        best: Optional[Tuple[ZS, DivergenceResult, LINE]] = None
        best_t: Optional[int] = None
        for zs_sub, dv_sub, c_sub in sub:
            if c_sub._type != c_k._type:                 # 同向
                continue
            t_sub = c_sub.end.k.k_index
            if t_sub <= t_k:                             # 必须在后
                continue
            if c_k._type == "down" and c_sub.end.val < val_k:   # 一买:破前低 → 跳过
                continue
            if c_k._type == "up" and c_sub.end.val > val_k:     # 一卖:破前高 → 跳过
                continue
            if best_t is None or t_sub < best_t:         # 取时间最早
                best, best_t = (zs_sub, dv_sub, c_sub), t_sub
        return best
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_bs2_branch.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 全套回归 + ruff**

Run: `poetry run pytest tests/core/ -q`
Expected: PASS（既有 282 + 新增 9 → 全绿，零回归）

Run: `poetry run ruff check src/chanlun/core/bs2_branch.py tests/core/test_bs2_branch.py`
Expected: All checks passed!

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/bs2_branch.py tests/core/test_bs2_branch.py
git commit -m "feat(core/bs2_branch): 二类买卖点(定律一:L_k一买后次级别L_{k-1}不破前低的首个一买)(P5b)"
```

---

## Task 3: 真实数据 + 受控演示验收

**Files:**
- Create（gitignored）: `scripts_local/probe_p5b_bs2.py`
- Modify: `.gitignore`（加 `bs2_branch_review.html`）

- [ ] **Step 1: 写 probe 脚本**（`scripts_local/probe_p5b_bs2.py`）

```python
# scripts_local/probe_p5b_bs2.py — P5b 二类买卖点真实数据+受控演示验收(本地, gitignored)
import logging
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld, CLKline, FX, XD, ZS
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.recursive_branch import RecursiveBranchCalculator, LevelResult
from chanlun.core.bs2_branch import Bs2BranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

# 真实数据(负向:L1 无 done 背驰 → 无 L1 二买)
levels = RecursiveBranchCalculator().calculate(bis, ld, "zs_wzgx_zgd", frequency="1m")
pts = Bs2BranchCalculator().calculate(levels)
print("=== 真实数据 ===")
for lr in levels:
    firsts = [(i, dv.leave_seg._type) for i, dv in enumerate(lr.done_divergence)
              if dv and dv.is_beichi and dv.kind == "qs" and not dv.provisional]
    print(f"  L{lr.level}: 中枢={len(lr.done_zss)} 一类点={firsts}")
print(f"  二类点={len(pts)}")
for p in pts:
    print(f"    {p.bs_type} level={p.level} @k{p.anchor_fx.k.k_index} val={p.anchor_fx.val:.3f}")


# 受控演示:造 2 级一类点 → L1 二买
def _fx(kidx, val, ftype):
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(idx, _type, sv, ev):
    if _type == "up":
        s, e = _fx(idx, sv, "di"), _fx(idx + 1, ev, "ding")
    else:
        s, e = _fx(idx, sv, "ding"), _fx(idx + 1, ev, "di")
    xd = XD(start=s, end=e, _type=_type, index=idx); xd.done = True
    return xd


def _zs():
    z = ZS(zs_type="xd", start=None); z.lines = [_seg(0, "up", 6, 9)]
    z.zd, z.zg = 6, 9; z._bounds_dirty = True; z.update_boundaries(); return z


def _dv(_type, c):
    return DivergenceResult(is_beichi=True, kind="qs", compare_seg=c, leave_seg=c, provisional=False)


def _lr(level, cs):
    dvs = [_dv(c._type, c) for c in cs]
    return LevelResult(level=level, zss=[_zs() for _ in cs], done_divergence=dvs, zslxs=[], upgrade_idx=[])


# L1 一买(k10,val5) + L0 后续一买(k20,val6 不破前低) → L1 二买
c_l1 = _seg(9, "down", 10, 5)
c_l0a = _seg(2, "down", 9, 7)       # k=3 在 L1 一买前 → 不算
c_l0b = _seg(19, "down", 9, 6)      # k=20 在后、不破前低 → L1 二买
demo_levels = [_lr(0, [c_l0a, c_l0b]), _lr(1, [c_l1])]
demo_pts = Bs2BranchCalculator().calculate(demo_levels)
print("=== 受控演示 ===")
print(f"  L1 一买@k{c_l1.end.k.k_index}(val={c_l1.end.val}) | L0 一买@k3(早,排除)+k20(不破前低)")
for p in demo_pts:
    print(f"  二类点 {p.bs_type} level={p.level} @k{p.anchor_fx.k.k_index} val={p.anchor_fx.val} (=L0 一买,归属 L1)")

# 出图:受控演示 时间轴 各级一类点 + 二买★
fig = go.Figure()
for lr, cs in [(demo_levels[1], [c_l1]), (demo_levels[0], [c_l0a, c_l0b])]:
    for c in cs:
        col = "green" if c._type == "down" else "red"
        fig.add_trace(go.Scatter(x=[c.end.k.k_index], y=[lr.level], mode="markers+text",
                                 marker=dict(symbol="circle", color=col, size=12),
                                 text=["一买" if c._type == "down" else "一卖"], textposition="top center",
                                 showlegend=False))
for p in demo_pts:
    fig.add_trace(go.Scatter(x=[p.anchor_fx.k.k_index], y=[p.level], mode="markers+text",
                             marker=dict(symbol="star", color="gold", size=20, line=dict(width=1, color="black")),
                             text=[f"L{p.level} {p.bs_type}"], textposition="bottom center", showlegend=False))
fig.update_layout(title="P5b 二类买卖点(受控演示:★=L_k二买=L_{k-1}一买;绿=一买/红=一卖)",
                  yaxis=dict(title="级别 L", dtick=1), xaxis=dict(title="时间(K线序号)"), height=420)
fig.write_html("bs2_branch_review.html")
print("written bs2_branch_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p5b_bs2.py`
Expected: 真实数据 L1 无一类点 → 二类点=0（负向）；受控演示 L1 二买 @k20（=L0 一买）；生成 `bs2_branch_review.html`。

- [ ] **Step 3: gitignore 审阅图**（`.gitignore` 在 `interval_nest_review.html` 行后加）

```
interval_nest_review.html
bs2_branch_review.html
```

```bash
git add .gitignore
git commit -m "chore: gitignore P5b 审阅图 bs2_branch_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `bs2_branch_review.html` 交付用户，审：
- 受控演示：★（L1 二买）是否落在「L1 一买之后、L0 第一个不破前低的一买」处（k20，而非 k3 早的那个）。
- 真实数据负向：L1 无 done 背驰（无 L1 一买）→ 无 L1 二买，符合「无次级别配对 → 无二买」。
- **不通过 → 诊断口径（时间在后/不破前低/同向/取第一个），按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2 模块接口(BuySellPoint level + Bs2)→Task1/2；§3 一类识别→Task2；§4 二类关联→Task2；§5 口径→Task2 测试；§6 测试+验收→Task1/2/3。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`BuySellPoint(...,level=)`、`Bs2BranchCalculator.calculate(levels)→List[BuySellPoint]`、`_first_points→List[Tuple[ZS,DivergenceResult,LINE]]`、`_find_second(c_k,sub)→Optional[Tuple]`、fake `_dv`/`_lr` 与上游构造一致。
- **执行注意**：Task1 先加 level 字段保 P5a 回归；Task2 一类识别排除 provisional；probe 用 `PYTHONPATH=src`。
