# P5a 买卖点（一类 + 三类，单级别 done）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 从 `zs_branch` 的 `ZsBranchResult`(done_zss+done_divergence) + 原始 lines，产已完成中枢的一类（趋势背驰）+ 三类（离开中枢回试不破 ZG/ZD）买卖点。

**Architecture:** 新建孤立 `bs_branch.py`，无状态 `BsBranchCalculator.calculate(zs_result, lines)→List[BuySellPoint]`；一类读 `done_divergence` 的 qs 背驰、三类读 done 中枢 `z.end` 离开段 + lines 紧邻回试段。不接 CL、不改上游。

**Tech Stack:** Python 3、dataclass、pytest、poetry、ruff、plotly（验收）。

设计见 `docs/chanlun_core_redesign_5a_买卖点_design.md`。

---

## File Structure

- **Create:** `src/chanlun/core/bs_branch.py` — `BuySellPoint` + `BsBranchCalculator`(`_first_class`/`_third_class`/`_next_seg`/`calculate`)。
- **Test:** `tests/core/test_bs_branch.py` — 受控 `ZsBranchResult`+fake `DivergenceResult`+受控 `lines`（`_seg`/`_make_zs` 范式）。
- **Probe（gitignored）:** `scripts_local/probe_p5a_bs.py` — 真实数据 K线+中枢框+买卖点标记，人工审。
- **不改:** `zs_branch.py`/CL/旧 `bs_point_calculator.py`。

---

## Task 1: `BuySellPoint` + 一类买卖点（`_first_class`）

**Files:**
- Create: `src/chanlun/core/bs_branch.py`
- Test: `tests/core/test_bs_branch.py`

- [ ] **Step 1: 写失败测试**（`tests/core/test_bs_branch.py`）

```python
"""tests/core/test_bs_branch.py — P5a 买卖点 TDD。

受控 ZsBranchResult + fake DivergenceResult + 受控 lines（_seg/_make_zs 范式，
绕笔划分浮点敏感）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult
from chanlun.core.bs_branch import BuySellPoint, BsBranchCalculator


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


def _dv(_type, leave_seg, kind="qs", is_beichi=True) -> DivergenceResult:
    return DivergenceResult(is_beichi=is_beichi, kind=kind,
                            compare_seg=leave_seg, leave_seg=leave_seg, provisional=False)


def _result(done_zss, done_div) -> ZsBranchResult:
    return ZsBranchResult(done_zss=list(done_zss), live=[], freeze_idx=0,
                          done_divergence=list(done_div))


# 一个标准中枢核心(本体[6,9]),不设 end → 三类不触发,只测一类
def _zs_no_end():
    return _make_zs([_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)], 6, 9)


def test_first_class_qs_down_is_1buy():
    c = _seg(5, "down", 10, 5)                       # 下跌趋势背驰离开段
    res = _result([_zs_no_end()], [_dv("down", c)])
    pts = BsBranchCalculator().calculate(res, [])
    assert len(pts) == 1
    assert pts[0].bs_type == "1buy"
    assert pts[0].anchor_fx is c.end                 # 锚离开段末端(di 低点)
    assert pts[0].divergence is not None


def test_first_class_qs_up_is_1sell():
    c = _seg(5, "up", 5, 10)
    res = _result([_zs_no_end()], [_dv("up", c)])
    pts = BsBranchCalculator().calculate(res, [])
    assert len(pts) == 1 and pts[0].bs_type == "1sell"


def test_first_class_pz_not_produced():
    c = _seg(5, "down", 10, 5)
    res = _result([_zs_no_end()], [_dv("down", c, kind="pz")])   # 盘整背驰不产一类
    assert BsBranchCalculator().calculate(res, []) == []


def test_first_class_non_beichi_not_produced():
    c = _seg(5, "down", 10, 5)
    res = _result([_zs_no_end()], [_dv("down", c, is_beichi=False)])
    assert BsBranchCalculator().calculate(res, []) == []


def test_first_class_none_divergence_skipped():
    res = _result([_zs_no_end()], [None])
    assert BsBranchCalculator().calculate(res, []) == []


def test_calculate_empty_returns_empty():
    assert BsBranchCalculator().calculate(_result([], []), []) == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_bs_branch.py -q`
Expected: FAIL（`ModuleNotFoundError: bs_branch`）

- [ ] **Step 3: 写实现**（`src/chanlun/core/bs_branch.py`）

```python
"""bs_branch.py — P5a 缠论买卖点（一类 + 三类，单级别 done）。

从 zs_branch 的 ZsBranchResult(done_zss+done_divergence) + 原始 lines，产已完成
中枢的一类(趋势背驰,宪法 §6/第18·24课)+三类(离开中枢回试不破 ZG/ZD,节点① H2
坍缩,第20课)买卖点。孤立、不接 CL、不改上游、不动旧 bs_point_calculator。
设计见 docs/chanlun_core_redesign_5a_买卖点_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from chanlun.core.cl_interface import LINE, FX, ZS
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult


@dataclass
class BuySellPoint:
    """一个买卖点信号。"""
    bs_type: str                              # "1buy" | "1sell" | "3buy" | "3sell"
    zs: ZS                                    # 关联中枢
    signal_seg: LINE                          # 信号段(一类=背驰离开段 c;三类=回试段)
    anchor_fx: FX                             # 出图锚点(一类=c 末端;三类=回试段末端极值)
    divergence: Optional[DivergenceResult]    # 一类带背驰本体;三类 None


class BsBranchCalculator:
    """买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, zs_result: ZsBranchResult,
                  lines: List[LINE]) -> List[BuySellPoint]:
        return self._first_class(zs_result)

    def _first_class(self, zs_result: ZsBranchResult) -> List[BuySellPoint]:
        """一类 = 趋势背驰(done_divergence 里 is_beichi & kind=='qs')。
        离开段向下→1buy(跌势衰竭)、向上→1sell;锚离开段末端极值。"""
        out: List[BuySellPoint] = []
        for i, dv in enumerate(zs_result.done_divergence):
            if dv is None or not dv.is_beichi or dv.kind != "qs":   # 仅趋势背驰
                continue
            c = dv.leave_seg
            z = zs_result.done_zss[i]
            if c._type == "down":
                out.append(BuySellPoint("1buy", z, c, c.end, dv))
            elif c._type == "up":
                out.append(BuySellPoint("1sell", z, c, c.end, dv))
        return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_bs_branch.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: ruff + commit**

Run: `poetry run ruff check src/chanlun/core/bs_branch.py tests/core/test_bs_branch.py`
Expected: All checks passed!

```bash
git add src/chanlun/core/bs_branch.py tests/core/test_bs_branch.py
git commit -m "feat(core/bs_branch): BuySellPoint + 一类买卖点(_first_class 读 qs 趋势背驰)(P5a)"
```

---

## Task 2: 三类买卖点（`_third_class`/`_next_seg`）+ `calculate` 组合 + 全套回归

**Files:**
- Modify: `src/chanlun/core/bs_branch.py`
- Test: `tests/core/test_bs_branch.py`

- [ ] **Step 1: 写失败测试**（追加到 `tests/core/test_bs_branch.py` 末尾）

```python
# 中枢本体核心[6,9],带离开段 end → 测三类
def _zs_with_leave(leave):
    return _make_zs([_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)], 6, 9, end=leave)


def test_third_class_up_retest_holds_zg_is_3buy():
    leave = _seg(3, "up", 8, 14)                      # 向上离开(冲出 ZG=9)
    retest = _seg(4, "down", 14, 10)                  # 回试向下,低点 10 >= ZG=9 不破
    z = _zs_with_leave(leave)
    lines = [_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9), leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [None]), lines)
    assert len(pts) == 1
    assert pts[0].bs_type == "3buy"
    assert pts[0].anchor_fx is retest.end            # 锚回试段末端(di 低点)
    assert pts[0].divergence is None


def test_third_class_up_retest_breaks_zg_none():
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 7)                   # 低点 7 < ZG=9 破 → 不产
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    assert BsBranchCalculator().calculate(_result([z], [None]), lines) == []


def test_third_class_down_retest_holds_zd_is_3sell():
    leave = _seg(3, "down", 6, 2)                     # 向下离开(跌破 ZD=6)
    retest = _seg(4, "up", 2, 5)                      # 回试向上,高点 5 <= ZD=6 不破
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [None]), lines)
    assert len(pts) == 1 and pts[0].bs_type == "3sell"


def test_third_class_no_retest_seg_none():
    leave = _seg(3, "up", 8, 14)
    z = _zs_with_leave(leave)
    lines = [leave]                                   # leave 是末段,无下一段
    assert BsBranchCalculator().calculate(_result([z], [None]), lines) == []


def test_first_and_third_coexist():
    # 同一中枢:向上离开段 qs 背驰(→1sell) + 回试不破 ZG(→3buy),两点并存
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 10)
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [_dv("up", leave)]), lines)
    assert {p.bs_type for p in pts} == {"1sell", "3buy"}
```

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_bs_branch.py -q`
Expected: FAIL（三类测试失败：calculate 当前只产一类，3buy/3sell 用例 assert 不满足）

- [ ] **Step 3: 写实现**（修改 `bs_branch.py`：改 `calculate`、加 `_third_class`/`_next_seg`）

把 `calculate` 改为：
```python
    def calculate(self, zs_result: ZsBranchResult,
                  lines: List[LINE]) -> List[BuySellPoint]:
        return self._first_class(zs_result) + self._third_class(zs_result, lines)
```

在 `_first_class` 之后追加：
```python
    def _third_class(self, zs_result: ZsBranchResult,
                     lines: List[LINE]) -> List[BuySellPoint]:
        """三类 = 离开中枢、第一次回试不破核心 ZG/ZD(第20课)。
        向上离开 & 回试低点 >= ZG → 3buy;向下离开 & 回试高点 <= ZD → 3sell。"""
        out: List[BuySellPoint] = []
        for z in zs_result.done_zss:
            leave = z.end                                          # 离开段(correct_exit 剥出)
            if leave is None:
                continue
            retest = self._next_seg(leave, lines)                  # 紧邻下一段 = 第一次回试
            if retest is None:                                     # 离开到右边缘、无回试 → 不产
                continue
            if leave._type == "up" and retest.end.val >= z.zg:     # 回试低点不破 ZG
                out.append(BuySellPoint("3buy", z, retest, retest.end, None))
            elif leave._type == "down" and retest.end.val <= z.zd:  # 回试高点不破 ZD
                out.append(BuySellPoint("3sell", z, retest, retest.end, None))
        return out

    @staticmethod
    def _next_seg(leave: LINE, lines: List[LINE]) -> Optional[LINE]:
        """离开段在 lines 中的紧邻下一段(按对象身份;leave 是 ZsCalculator 输入段之一)。"""
        for k, ln in enumerate(lines):
            if ln is leave:
                return lines[k + 1] if k + 1 < len(lines) else None
        return None
```

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_bs_branch.py -q`
Expected: PASS（12 passed）

- [ ] **Step 5: 全套回归 + ruff**

Run: `poetry run pytest tests/core/ -q`
Expected: PASS（既有 264 + 新增 → 全绿，零回归）

Run: `poetry run ruff check src/chanlun/core/bs_branch.py tests/core/test_bs_branch.py`
Expected: All checks passed!

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/bs_branch.py tests/core/test_bs_branch.py
git commit -m "feat(core/bs_branch): 三类买卖点(_third_class 离开中枢回试不破 ZG/ZD)+calculate 合并一三类(P5a)"
```

---

## Task 3: 真实数据出图验收

**Files:**
- Create（gitignored）: `scripts_local/probe_p5a_bs.py`
- Modify: `.gitignore`（加 `bs_branch_review.html`）

- [ ] **Step 1: 写 probe 出图脚本**（`scripts_local/probe_p5a_bs.py`）

```python
# scripts_local/probe_p5a_bs.py — P5a 买卖点真实数据验收(本地, gitignored)
import logging
from collections import Counter
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator
from chanlun.core.bs_branch import BsBranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

res = ZsBranchCalculator(ld_provider=ld, frequency="1m", wzgx="zs_wzgx_zgd").calculate(bis)
pts = BsBranchCalculator().calculate(res, bis)

print(f"中枢={len(res.done_zss)} 买卖点={len(pts)} 分布={dict(Counter(p.bs_type for p in pts))}")
for p in pts:
    print(f"  {p.bs_type} @k{p.anchor_fx.k.k_index} val={p.anchor_fx.val:.3f} "
          f"zs[zd={p.zs.zd:.3f},zg={p.zs.zg:.3f}]")

# K线 + 中枢核心框 + 买卖点标记
fig = go.Figure()
fig.add_trace(go.Candlestick(x=list(range(len(df))), open=df["open"], high=df["high"],
                             low=df["low"], close=df["close"], name="K线"))
for z in res.done_zss:
    x0 = z.lines[0].start.k.k_index
    x1 = z.end.end.k.k_index if z.end is not None else z.lines[-1].end.k.k_index
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=z.zd, y1=z.zg,
                  line=dict(color="gray", width=1), fillcolor="rgba(128,128,128,0.12)")
style = {"1buy": ("triangle-up", "green"), "3buy": ("triangle-up", "lime"),
         "1sell": ("triangle-down", "red"), "3sell": ("triangle-down", "orange")}
for bs_type, (sym, col) in style.items():
    xs = [p.anchor_fx.k.k_index for p in pts if p.bs_type == bs_type]
    ys = [p.anchor_fx.val for p in pts if p.bs_type == bs_type]
    if xs:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name=bs_type,
                                 marker=dict(symbol=sym, color=col, size=13,
                                             line=dict(width=1, color="black"))))
fig.update_layout(title="P5a 买卖点(▲买/▼卖,深绿红=一类/浅绿橙=三类,灰框=中枢核心)",
                  xaxis_rangeslider_visible=False, height=700)
fig.write_html("bs_branch_review.html")
print("written bs_branch_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p5a_bs.py`
Expected: 打印中枢数/买卖点分布/逐点明细 + 生成 `bs_branch_review.html`。（若 df 列名非 open/high/low/close，按实际列名调整 Candlestick）

- [ ] **Step 3: gitignore 审阅图**（`.gitignore` 在 `beichi_nest_demo.html` 行后加）

```
beichi_nest_demo.html
bs_branch_review.html
```

```bash
git add .gitignore
git commit -m "chore: gitignore P5a 审阅图 bs_branch_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `bs_branch_review.html` 交付用户，审：
- **一类**（深绿▲1buy/深红▼1sell）是否落在趋势背驰衰竭处（下跌末端低点/上涨末端高点）。
- **三类**（浅绿▲3buy/橙▼3sell）是否落在中枢核心框上沿之上的回抽低点（3buy）/下沿之下反抽高点（3sell）。
- **不通过 → 诊断口径（背驰方向/回试段定位/ZG-ZD 边界），按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2 模块接口→Task1/2；§3 一类→Task1；§4 三类(`_third_class`/`_next_seg`)→Task2；§5 口径(核心区间/并存)→Task1/2 测试；§6 测试+验收→Task1/2/3。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`BuySellPoint(bs_type/zs/signal_seg/anchor_fx/divergence)`、`calculate(zs_result, lines)→List`、`_first_class(zs_result)`、`_third_class(zs_result, lines)`、`_next_seg(leave, lines)→Optional[LINE]`、fake `_dv`/`_result` 与 `DivergenceResult`/`ZsBranchResult` 构造签名一致，全程统一。
- **执行注意**：Task1 `calculate` 只调 `_first_class`（Task2 再合并三类）；probe 用 `PYTHONPATH=src`；df 列名按实际调整。
