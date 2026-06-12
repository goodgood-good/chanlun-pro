# P5d 转折型背驰（进入段 a 趋势背驰）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐 Task 实现。步骤用 `- [ ]` checkbox 追踪。

**Goal:** 改 `zs_branch._divergence_for` 转折型分支——异向中枢(a/c 反向)判进入段 a 的趋势背驰 `is_beichi(prev_zs.end, a)`，使转折型中枢能出一类买卖点（锚转折点）。

**Architecture:** 唯一改 `_divergence_for`（异向时 `is_beichi(prev.end同向离开段, 进入段a)`，`leave_seg=a`/`kind="qs"`，守卫无prev/自比/异向）。下游 done_divergence→bs/zslx/recursive 全链自动受益、需重新验收。

**Tech Stack:** Python 3、pytest、poetry、ruff、plotly（验收）。

设计见 `docs/chanlun_core_redesign_5d_转折型背驰_design.md`。

---

## File Structure

- **Modify:** `src/chanlun/core/zs_branch.py` — `_divergence_for` 转折型分支（替换 line 292-293 的 `return None`）。
- **Test:** `tests/core/test_zs_branch.py` — 加转折型用例 + 更新现有异向测试 docstring。
- **Probe（gitignored）:** `scripts_local/probe_p5d_turn.py` — 真实数据转折型一类点 + 走势类型出图。
- **不改:** 下游模块（自动受益）/CL。

---

## Task 1: `_divergence_for` 转折型分支 + 受控测试 + 全链回归

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`
- Test: `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**（在 `tests/core/test_zs_branch.py` 的 `test_divergence_none_when_no_ld_provider_on_helper` 之后追加）

```python
# P5d: 转折型背驰（进入段 a 趋势背驰）
def _turn_zs():
    """转折型中枢:进入段 down、离开段 up(异向)。"""
    entry = _seg(10, "down", 10, 6)                  # 进入段 a (down)
    body = [_seg(11, "up", 6, 9), _seg(12, "down", 9, 6), _seg(13, "up", 6, 9)]
    zs = _make_zs(entry, body, 6, 9)
    zs.end = _seg(14, "up", 6, 12)                    # 离开段 c (up,异向 a)
    return zs, entry


def _prev_zs(end_seg):
    """前中枢,离开段 = end_seg。"""
    prev = _make_zs(_seg(0, "up", 3, 6),
                    [_seg(1, "down", 6, 3), _seg(2, "up", 3, 6), _seg(3, "down", 6, 3)], 3, 6)
    prev.end = end_seg
    return prev


def test_divergence_turn_with_prev_same_dir_judges_entry():
    """转折型 + 前中枢同向(down)离开段 → 判进入段 a 趋势背驰(leave_seg=a,compare=prev.end,kind=qs)。"""
    zs, entry = _turn_zs()
    prev = _prev_zs(_seg(4, "down", 6, 2))            # 前离开段 down,同向 entry(down)
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1), frequency="1m")
    dv = calc._divergence_for(zs, prev, live=False)
    assert dv is not None
    assert dv.leave_seg is entry                      # 背驰段 = 进入段 a
    assert dv.compare_seg is prev.end                 # 比较段 = 前中枢同向离开段
    assert dv.kind == "qs"                            # 转折=趋势背驰
    assert isinstance(dv.is_beichi, bool)


def test_divergence_turn_no_prev_none():
    """转折型 + 无前驱 → None(守卫 prev_zs None)。"""
    zs, _ = _turn_zs()
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1))
    assert calc._divergence_for(zs, None, live=False) is None


def test_divergence_turn_prev_opposite_none():
    """转折型 + 前中枢离开段异向(up,与进入段 down 反向) → None。"""
    zs, _ = _turn_zs()
    prev = _prev_zs(_seg(4, "up", 2, 6))              # 前离开段 up,异向 entry(down)
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1))
    assert calc._divergence_for(zs, prev, live=False) is None


def test_divergence_turn_self_compare_none():
    """转折型 + 前中枢离开段恰是本进入段(自比) → None。"""
    zs, entry = _turn_zs()
    prev = _prev_zs(entry)                            # prev.end is entry(自比)
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1))
    assert calc._divergence_for(zs, prev, live=False) is None
```

并把现有 `test_divergence_none_when_entry_leave_opposite` 的 docstring 更新为：
```python
    """进入段 down、离开段 up(异向)+ 无前驱(prev=None) → None。(P5d:转折型有前驱同向段才判)"""
```
（断言不变——它传 `prev=None`，P5d 后守卫 `prev_zs None → None`，仍通过。）

- [ ] **Step 2: 跑测试验证失败**

Run: `poetry run pytest tests/core/test_zs_branch.py -k turn -q`
Expected: `test_divergence_turn_with_prev_same_dir_judges_entry` FAIL（现状异向直接 None、dv 为 None）；其余 3 个 None 用例 PASS（现状已 None）。

- [ ] **Step 3: 写实现**（`src/chanlun/core/zs_branch.py` 的 `_divergence_for`，替换 line 292-293）

把：
```python
        if a.type != c.type:                          # 异向不可比力度
            return None
```
替换为：
```python
        if a.type != c.type:                          # 转折型(进入/离开异向=趋势转折点)
            # 转折前趋势的背驰:前中枢同向离开段 vs 本中枢进入段 a(转折前趋势最后段)
            b = prev_zs.end if prev_zs is not None else None
            if (b is None or b is a or b.type != a.type
                    or b.start is None or b.end is None):
                return None                            # 无前驱/自比/异向/缺端点 → 不判
            bc = is_beichi(b, a, self.ld_provider, self.frequency)
            return DivergenceResult(
                is_beichi=bc, kind="qs",               # 转折=趋势背驰
                compare_seg=b, leave_seg=a, provisional=live,  # 背驰段=进入段 a
            )
```
（同向中继型走其后原有 `kind = "qs" if self._is_trend(...)` + `is_beichi(a, c)` 逻辑，不动。）

- [ ] **Step 4: 跑测试验证通过**

Run: `poetry run pytest tests/core/test_zs_branch.py -k turn -q`
Expected: PASS（4 turn 用例全绿）

- [ ] **Step 5: 全链回归 + ruff**

Run: `poetry run pytest tests/core/ -q`
Expected: PASS（zslx/bs/recursive/bs2/bs3 受控数据多同向、转折型不触发 → 零回归；test_zs_branch 全绿）

Run: `poetry run ruff check src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py`
Expected: All checks passed!

- [ ] **Step 6: commit**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): _divergence_for 转折型分支(异向中枢判进入段a趋势背驰=is_beichi(prev同向离开段,a))(P5d)"
```

---

## Task 2: 真实数据验收（转折型一类点 + 走势类型出图）

**Files:**
- Create（gitignored）: `scripts_local/probe_p5d_turn.py`
- Modify: `.gitignore`（加 `bs_turn_review.html`）

- [ ] **Step 1: 写 probe 脚本**（`scripts_local/probe_p5d_turn.py`）

```python
# scripts_local/probe_p5d_turn.py — P5d 转折型背驰真实数据验收(本地, gitignored)
import logging
from collections import Counter
import pandas as pd
import plotly.graph_objects as go
logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator
from chanlun.core.zslx_branch import ZslxBranchCalculator
from chanlun.core.bs_branch import BsBranchCalculator

CFG = {"chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
       "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
       "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG)); cd.process_klines(df)
bis = cd.get_bis()
ld = lambda s, e: query_macd_ld(cd, s, e)

res = ZsBranchCalculator(ld_provider=ld, frequency="1m", wzgx="zs_wzgx_zgd").calculate(bis)
# 转折型中枢的 dv 现状
print("=== 转折型中枢 dv(P5d 后) ===")
for i, (z, dv) in enumerate(zip(res.done_zss, res.done_divergence)):
    a_t = z.start.type if z.start is not None else None
    c_t = z.end.type if z.end is not None else None
    if a_t is not None and c_t is not None and a_t != c_t:
        s = "None" if dv is None else ("背驰" if dv.is_beichi else "无背驰")
        print(f"  转折型中枢{i}: a={a_t} c={c_t} dv={s}")

pts = BsBranchCalculator().calculate(res, bis)
print(f"买卖点分布: {dict(Counter(p.bs_type for p in pts))}")
one = [p for p in pts if p.bs_type in ("1buy", "1sell")]
print(f"一类点={len(one)} (改前=3:1buy@k3384/1sell@k3617/1buy@k4312)")
zslxs = ZslxBranchCalculator().calculate(res.done_zss, res.done_divergence)
print(f"走势类型={len(zslxs)} (P4a 改前=4)")

# 出图:K线 + 中枢 + 一类点(深绿▲1buy/深红▼1sell)
fig = go.Figure()
fig.add_trace(go.Candlestick(x=list(range(len(df))), open=df["open"], high=df["high"],
                             low=df["low"], close=df["close"], name="K线"))
for z in res.done_zss:
    x0 = z.lines[0].start.k.k_index
    x1 = z.end.end.k.k_index if z.end is not None else z.lines[-1].end.k.k_index
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=z.zd, y1=z.zg,
                  line=dict(color="gray", width=1), fillcolor="rgba(128,128,128,0.1)")
style = {"1buy": ("triangle-up", "green"), "1sell": ("triangle-down", "red")}
for bs, (sym, col) in style.items():
    xs = [p.anchor_fx.k.k_index for p in one if p.bs_type == bs]
    ys = [p.anchor_fx.val for p in one if p.bs_type == bs]
    if xs:
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name=bs,
                                 marker=dict(symbol=sym, color=col, size=14, line=dict(width=1, color="black"))))
fig.update_layout(title="P5d 转折型背驰一类点(▲1buy/▼1sell,含转折型;灰框=中枢)",
                  xaxis_rangeslider_visible=False, height=700)
fig.write_html("bs_turn_review.html")
print("written bs_turn_review.html")
```

- [ ] **Step 2: 跑 probe**

Run: `PYTHONPATH=src poetry run python scripts_local/probe_p5d_turn.py`
Expected: 转折型中枢部分现有背驰判定（非全 None）；一类点 > 3（多出转折型一类点）；走势类型可能 > 4（转折处多切）；生成 `bs_turn_review.html`。

- [ ] **Step 3: gitignore 审阅图 + commit**（`.gitignore` 在 `bs3_branch_review.html` 后加 `bs_turn_review.html`）

```bash
git add .gitignore
git commit -m "chore: gitignore P5d 审阅图 bs_turn_review.html"
```

- [ ] **Step 4: 交付审图（人工验收）**

把 `bs_turn_review.html` 交付用户，审：
- 转折型一类点（多出的 ▲/▼）是否落在合理转折点（底/顶）。
- 走势类型划分是否更合理（转折处切）。
- 与前中继背驰一类点有无不合理重复。
- **不通过 → 诊断口径（比较段/守卫/方向），按用户反馈订正后重审。**

---

## Self-Review（写完计划自查）

- **Spec coverage**：§2/§3 _divergence_for 转折型分支→Task1；§4 全链影响→Task1 回归+Task2 验收；§5 风险(自比守卫)→Task1 测试；§6 测试+验收→Task1/2。全覆盖。
- **Placeholder scan**：无 TBD；测试与实现代码完整。
- **Type consistency**：`_divergence_for` 签名/返回不变；`DivergenceResult(is_beichi,kind,compare_seg,leave_seg,provisional)` 转折型 `leave_seg=a,compare_seg=prev.end`；helper `_seg`/`_make_zs`/`_ld`/`_turn_zs`/`_prev_zs` 一致。
- **执行注意**：Task1 守卫 `b is a` 防自比；现有异向测试 docstring 更新但断言不变（prev=None 仍 None）；下游受控测试预期不破（同向）。
