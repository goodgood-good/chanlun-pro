# P5c 扩展买点（= 多级三类 / 扩张三买）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 把 P5a 三类（单级 L0）扩展到多级——各级 LevelResult 中枢上出三买/三卖（扩张三买=L1+ 中枢三类），复用 P5a `_third_class`。

**Architecture:** `recursive_branch.LevelResult` 加 `units` 字段（各级输入段，回试段定位）；新建 `bs3_branch.py` 逐级构造 `ZsBranchResult`+用 `lr.units` 复用 P5a `_third_class`、标 `level`。不接 CL。

**Tech Stack:** Python 3、dataclass、pytest、poetry、ruff、plotly（验收）。

设计见 `docs/chanlun_core_redesign_5c_扩展买点_design.md`。

---

## File Structure

- **Modify:** `src/chanlun/core/recursive_branch.py` — `LevelResult` 加 `units` + `calculate` 两处 append 填 `units=list(units)`。
- **Create:** `src/chanlun/core/bs3_branch.py` — `Bs3BranchCalculator.calculate(levels)`。
- **Test:** `tests/core/test_bs3_branch.py` — 受控多级 `LevelResult`（带 units）。
- **Probe（gitignored）:** `scripts_local/probe_p5c_bs3.py` — 真实 L0 三类=P5a 一致性 + 受控 L1 演示。
- **不改:** `bs_branch.py`/`zs_branch.py`/CL。

---

## Task 1: `recursive_branch.LevelResult` 加 `units` + 填充 + 回归

**Files:**
- Modify: `src/chanlun/core/recursive_branch.py`

- [ ] **Step 1: `LevelResult` 加 units 字段**（在 `upgrade_idx` 后）

```python
@dataclass
class LevelResult:
    """单个递归级别的产出。"""
    level: int
    zss: List[ZS]
    done_divergence: List[Optional[DivergenceResult]]
    zslxs: List[ZSLX]
    upgrade_idx: List[int] = field(default_factory=list)
    units: List[LINE] = field(default_factory=list)   # P5c:该级输入段序列(回试段定位)
```

- [ ] **Step 2: `calculate` 两处 append 填 units**

pending 分支（`if not res.done_zss:` 内）：
```python
            results.append(LevelResult(
                level=level, zss=[h.zs for h in pend],
                done_divergence=[h.divergence for h in pend],
                zslxs=[], upgrade_idx=_mark_upgrades([h.zs for h in pend]),
                units=list(units),
            ))
```

done 分支：
```python
        results.append(LevelResult(
            level=level, zss=res.done_zss, done_divergence=res.done_divergence,
            zslxs=zslxs, upgrade_idx=_mark_upgrades(res.done_zss), units=list(units),
        ))
```

- [ ] **Step 3: 跑 recursive 回归 + 验证 units 填充**

Run: `poetry run pytest tests/core/test_recursive_branch.py -q`
Expected: PASS（全绿 —— units 默认 []，positional 构造不破）

临时验证 units 非空（可在 REPL 或加打印，确认后删）：
```bash
PYTHONPATH=src poetry run python -c "
import pandas as pd, logging; logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.recursive_branch import RecursiveBranchCalculator
df=pd.read_parquet('tests/fixtures/klines/a_SH_513100_1m.parquet')
cd=CL('SH.513100','1m',{'idx_macd_fast':12,'idx_macd_slow':26,'idx_macd_signal':9}); cd.process_klines(df)
bis=cd.get_bis(); ld=lambda s,e: query_macd_ld(cd,s,e)
levels=RecursiveBranchCalculator().calculate(bis,ld,'zs_wzgx_zgd',frequency='1m')
for lr in levels:
    print(f'L{lr.level} units={len(lr.units)} zss={len(lr.zss)}')
    for z in lr.zss:
        if z.end is not None:
            print(f'  z.end in units: {any(u is z.end for u in lr.units)}')
"
```
Expected: 各级 units 非空；done 中枢 `z.end in units: True`（对象身份成立）。

- [ ] **Step 4: 全套回归 + ruff + commit**

Run: `poetry run pytest tests/core/ -q`（零回归）+ `poetry run ruff check src/chanlun/core/recursive_branch.py`

```bash
git add src/chanlun/core/recursive_branch.py
git commit -m "feat(core/recursive_branch): LevelResult 加 units 字段(P5c 各级回试段定位;默认[]不破下游)"
```

---

## Task 2: `bs3_branch.py`（多级三类，复用 P5a `_third_class`）+ 测试 + 回归

**Files:**
- Create: `src/chanlun/core/bs3_branch.py`
- Test: `tests/core/test_bs3_branch.py`

- [ ] **Step 1: 写失败测试**（`tests/core/test_bs3_branch.py`）

```python
"""tests/core/test_bs3_branch.py — P5c 多级三类买卖点 TDD。

受控多级 LevelResult（带 units）+ 中枢 z.end 离开段（_seg/_make_zs 范式）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.bs3_branch import Bs3BranchCalculator


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
    xd.zs_high, xd.zs_low = max(sv, ev), min(sv, ev)
    return xd


def _make_zs(core, zd, zg, end=None) -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = list(core)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    if end is not None:
        z.end = end
    return z


def _lr(level, zss, divs, units) -> LevelResult:
    return LevelResult(level=level, zss=list(zss), done_divergence=list(divs),
                       zslxs=[], upgrade_idx=[], units=list(units))


def _core():
    return [_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)]


def test_l0_third_buy():
    leave = _seg(3, "up", 8, 14)                  # 向上离开(冲出 ZG=9)
    retest = _seg(4, "down", 14, 10)              # 回试低点 10 ≥ ZG=9 不破
    z = _make_zs(_core(), 6, 9, end=leave)
    units = _core() + [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(0, [z], [None], units)])
    assert len(pts) == 1
    assert pts[0].bs_type == "3buy" and pts[0].level == 0
    assert pts[0].anchor_fx is retest.end


def test_l1_third_buy_is_expand():
    # 扩张三买 = L1 中枢三类(level==1)
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 10)
    z = _make_zs(_core(), 6, 9, end=leave)
    units = _core() + [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(1, [z], [None], units)])
    assert len(pts) == 1 and pts[0].bs_type == "3buy" and pts[0].level == 1


def test_pending_no_end_no_third():
    z = _make_zs(_core(), 6, 9)                   # 无 end(pending)
    assert Bs3BranchCalculator().calculate([_lr(1, [z], [None], [])]) == []


def test_l0_third_sell():
    leave = _seg(3, "down", 6, 2)
    retest = _seg(4, "up", 2, 5)                  # 高点 5 ≤ ZD=6 不破
    z = _make_zs(_core(), 6, 9, end=leave)
    units = [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(0, [z], [None], units)])
    assert len(pts) == 1 and pts[0].bs_type == "3sell" and pts[0].level == 0


def test_retest_breaks_zg_none():
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 7)               # 低点 7 < ZG=9 破 → 不产
    z = _make_zs(_core(), 6, 9, end=leave)
    assert Bs3BranchCalculator().calculate([_lr(0, [z], [None], [leave, retest])]) == []


def test_multi_level_each_third():
    # L0 + L1 各出三类 → level 集合 {0,1}
    leave0 = _seg(3, "up", 8, 14); retest0 = _seg(4, "down", 14, 10)
    z0 = _make_zs(_core(), 6, 9, end=leave0)
    leave1 = _seg(13, "up", 8, 14); retest1 = _seg(14, "down", 14, 10)
    z1 = _make_zs(_core(), 6, 9, end=leave1)
    levels = [_lr(0, [z0], [None], _core() + [leave0, retest0]),
              _lr(1, [z1], [None], _core() + [leave1, retest1])]
    pts = Bs3BranchCalculator().calculate(levels)
    assert {p.level for p in pts} == {0, 1}


def test_empty_returns_empty():
    assert Bs3BranchCalculator().calculate([]) == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_bs3_branch.py -q`
Expected: FAIL（`ModuleNotFoundError: bs3_branch`）

- [ ] **Step 3: 写实现**（`src/chanlun/core/bs3_branch.py`）

```python
"""bs3_branch.py — P5c 缠论扩展买点（= 多级三类 / 扩张三买）。

把 P5a 三类(单级 L0)扩展到多级:对各级 LevelResult 中枢出三买/三卖,复用 P5a
BsBranchCalculator._third_class,标 level。扩张三买=L1+ 中枢三类(原文 10646)。
扩展实体化=recursive L1 中枢(probe 验证 [max(DD),min(GG)]==zd/zg),无需新口径。
孤立、不接 CL、不动旧 bs_point_calculator。
设计见 docs/chanlun_core_redesign_5c_扩展买点_design.md。
"""
from __future__ import annotations

from typing import List

from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import ZsBranchResult
from chanlun.core.bs_branch import BuySellPoint, BsBranchCalculator


class Bs3BranchCalculator:
    """多级三类买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        """各级中枢出三类(扩张三买=L1+中枢三类)。复用 P5a _third_class,标 level。"""
        out: List[BuySellPoint] = []
        base = BsBranchCalculator()
        for lr in levels:
            zr = ZsBranchResult(
                done_zss=lr.zss, live=[], freeze_idx=0,
                done_divergence=lr.done_divergence,
            )
            for p in base._third_class(zr, lr.units):   # 复用 P5a 三类逻辑
                p.level = lr.level                       # 标归属级别
                out.append(p)
        return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_bs3_branch.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 全套回归 + ruff**

Run: `poetry run pytest tests/core/ -q`（既有 + 新增全绿，零回归）
Run: `poetry run ruff check src/chanlun/core/bs3_branch.py tests/core/test_bs3_branch.py`

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/bs3_branch.py tests/core/test_bs3_branch.py
git commit -m "feat(core/bs3_branch): 多级三类买卖点(扩张三买=L1+中枢三类,复用 P5a _third_class 标 level)(P5c)"
```

---

## Task 3: 真实数据验收（L0 三类=P5a 一致性 + 受控演示）

**Files:**
- Create（gitignored）: `scripts_local/probe_p5c_bs3.py`
- Modify: `.gitignore`（加 `bs3_branch_review.html`）

- [ ] **Step 1: 写 probe 脚本**（`scripts_local/probe_p5c_bs3.py`）

```python
# scripts_local/probe_p5c_bs3.py — P5c 多级三类验收(本地, gitignored)
import logging
from collections import Counter
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld, CLKline, FX, XD, ZS
from chanlun.core.recursive_branch import RecursiveBranchCalculator, LevelResult
from chanlun.core.zs_branch import ZsBranchCalculator
from chanlun.core.bs_branch import BsBranchCalculator
from chanlun.core.bs3_branch import Bs3BranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

# 真实数据:P5c 多级三类
levels = RecursiveBranchCalculator().calculate(bis, ld, "zs_wzgx_zgd", frequency="1m")
pts3 = Bs3BranchCalculator().calculate(levels)
print("=== P5c 多级三类 ===")
print(f"各级三类分布: {dict(Counter((p.level, p.bs_type) for p in pts3))}")

# 一致性:P5c L0 三类 == P5a 单级三类
res_l0 = ZsBranchCalculator(ld_provider=ld, frequency="1m", wzgx="zs_wzgx_zgd").calculate(bis)
p5a = [p for p in BsBranchCalculator().calculate(res_l0, bis) if p.bs_type in ("3buy", "3sell")]
p5c_l0 = [p for p in pts3 if p.level == 0]
print(f"P5a L0 三类={len(p5a)} | P5c L0 三类={len(p5c_l0)}")
p5a_anchors = sorted((p.bs_type, p.anchor_fx.k.k_index) for p in p5a)
p5c_anchors = sorted((p.bs_type, p.anchor_fx.k.k_index) for p in p5c_l0)
print(f"逐点一致: {p5a_anchors == p5c_anchors}")
print(f"L1+ 三类(扩张三买)={[(p.level, p.bs_type) for p in pts3 if p.level >= 1]} (L1 pending→预期空)")


# 受控演示:造 L1 done 中枢 + units → L1 扩张三买
def _fx(kidx, val, ftype):
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(idx, _type, sv, ev):
    if _type == "up":
        s, e = _fx(idx, sv, "di"), _fx(idx + 1, ev, "ding")
    else:
        s, e = _fx(idx, sv, "ding"), _fx(idx + 1, ev, "di")
    xd = XD(start=s, end=e, _type=_type, index=idx); xd.done = True
    xd.zs_high, xd.zs_low = max(sv, ev), min(sv, ev); return xd


def _make_zs(core, zd, zg, end):
    z = ZS(zs_type="xd", start=None); z.lines = list(core); z.zd, z.zg = zd, zg
    z._bounds_dirty = True; z.update_boundaries(); z.end = end; return z


core = [_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)]
leave = _seg(3, "up", 8, 14); retest = _seg(4, "down", 14, 10)
z1 = _make_zs(core, 6, 9, leave)
demo = [LevelResult(level=1, zss=[z1], done_divergence=[None], zslxs=[], upgrade_idx=[], units=core + [leave, retest])]
demo_pts = Bs3BranchCalculator().calculate(demo)
print("=== 受控演示(L1 扩张三买) ===")
for p in demo_pts:
    print(f"  L{p.level} {p.bs_type} @k{p.anchor_fx.k.k_index} val={p.anchor_fx.val} (中枢上沿之上回抽)")

# 出图:受控演示 L1 中枢 + 扩张三买
fig = go.Figure()
fig.add_shape(type="rect", x0=0, x1=5, y0=z1.zd, y1=z1.zg, line=dict(color="red", width=2),
              fillcolor="rgba(255,0,0,0.08)")
fig.add_annotation(x=2.5, y=z1.zg, text="L1 中枢核心[ZD,ZG]", showarrow=False, yshift=10)
for p in demo_pts:
    fig.add_trace(go.Scatter(x=[p.anchor_fx.k.k_index], y=[p.anchor_fx.val], mode="markers+text",
                             marker=dict(symbol="triangle-up", color="lime", size=18, line=dict(width=1, color="black")),
                             text=[f"L{p.level} {p.bs_type}"], textposition="top center", showlegend=False))
fig.update_layout(title="P5c 扩张三买(受控演示:▲=L1中枢上沿之上回抽不破ZG)",
                  xaxis=dict(title="时间(K线序号)"), yaxis=dict(title="价位"), height=420)
fig.write_html("bs3_branch_review.html")
print("written bs3_branch_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p5c_bs3.py`
Expected: P5c L0 三类逐点 == P5a（一致性 True）；L1 pending → L1+ 三类空（负向）；受控演示 L1 扩张三买；生成 `bs3_branch_review.html`。

- [ ] **Step 3: gitignore 审阅图 + commit**（`.gitignore` 在 `bs2_branch_review.html` 后加 `bs3_branch_review.html`）

```bash
git add .gitignore
git commit -m "chore: gitignore P5c 审阅图 bs3_branch_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `bs3_branch_review.html` 交付用户，审：
- 受控演示：L1 扩张三买（▲）是否落在 L1 中枢核心框上沿之上的回抽低点。
- 真实数据一致性：P5c L0 三类与 P5a 逐点相同（True）；L1 pending → 扩张三买空（数据限制负向）。
- **不通过 → 诊断口径，按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2 LevelResult units + Bs3 接口→Task1/2；§3 units 填充→Task1；§4 多级三类→Task2；§5 口径→Task2 测试；§6 测试+验收(L0=P5a 一致性)→Task2/3。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`LevelResult(...,units=)`、`Bs3BranchCalculator.calculate(levels)→List[BuySellPoint]`、复用 `BsBranchCalculator._third_class(zr, lr.units)`、`ZsBranchResult` 构造一致。
- **执行注意**：Task1 units 默认 [] 保下游不破；Task2 复用 P5a `_third_class`（跨类调 `_` 方法，DRY）；probe 验 L0 逐点一致性。
