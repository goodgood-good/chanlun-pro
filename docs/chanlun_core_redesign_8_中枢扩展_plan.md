# 中枢扩展实体化（中心定理二）实现计划

> **For agentic workers:** 用 superpowers:executing-plans 逐 Task 实现。步骤用 `- [ ]` 勾选跟踪。
> 设计见 `docs/chanlun_core_redesign_8_中枢扩展_design.md`。

**Goal:** 让新核心 `recursive_branch` 单周期产出高级别中枢（中心定理二·中枢扩展 + 9 段延伸），取代 P7 真实多周期叠加。

**Architecture:** 走势类型递归主链不动；新增 `zs_expand.py`（定理二判定 + 借次级别走势类型实体化高级别中枢），在 `recursive_branch` 上做递归扩展叠加，并入同一层级树；web 端停用 P7、高级别中枢统一从 `recursive_levels` L1/L2/L3 取。

**Tech Stack:** Python 3 / poetry / pytest；前端 charts.js（vanilla JS）+ datafeed bundle.js。测试 `poetry run pytest tests/ -q`，lint `poetry run ruff check`，JS `node --check`。

**口径锚点（全回原文）：** 定理二 line10029；扩展区间「3 走势类型重合」line31778/20029；9 段 line27278；扩展⊥转折 line16429。

**合并区间公式（核心几何）：**
```
spanning = 扩展跨越的 ≥3 个次级别走势类型(ZSLX，各 zs_high=GG / zs_low=DD)
核心区 ZG = min(w.zs_high)   ZD = max(w.zs_low)      ← 重合(overlap)，画框用此
包络   GG = max(w.zs_high)   DD = min(w.zs_low)      ← 并集，震荡外沿
```

---

## Task 1：定理二扩展判定（`ZS.can_expand_with` 精化 + `is_zs_expand`）

**Files:**
- Modify: `src/chanlun/core/cl_interface.py:628-633`（`can_expand_with`）
- Create: `src/chanlun/core/zs_expand.py`（仅 `is_zs_expand`，本 Task）
- Test: `tests/core/test_zs_expand.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_zs_expand.py
from chanlun.core.cl_interface import ZS
from chanlun.core.zs_expand import is_zs_expand


def _zs(zd, zg, dd, gg, done=True, line_num=3):
    """构造测试用中枢：直接写区间，start=None（被测函数不依赖 start）。"""
    z = ZS(zs_type="xd", start=None)
    z.zd, z.zg, z.dd, z.gg = zd, zg, dd, gg
    z.done = done
    z.line_num = line_num
    return z


def test_expand_core_separated_envelope_overlap_true():
    # 前核心[10,12]包络[9,13]；后核心[7,9]包络[8,11]：核心区分离(9<10)、包络重叠(11>=9)
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=7, zg=9, dd=8, gg=11)
    assert is_zs_expand(prev, cur) is True


def test_extend_core_overlap_false():
    # 核心区也重叠(后zg=11>前zd=10) → 延伸，非扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=9, zg=11, dd=8, gg=12)
    assert is_zs_expand(prev, cur) is False


def test_trend_envelope_separated_false():
    # 包络分离(后dd=14>前gg=13) → 趋势，非扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=15, zg=17, dd=14, gg=18)
    assert is_zs_expand(prev, cur) is False


def test_touch_closed_interval_true():
    # 闭区间触及：后gg=9 == 前dd=9 → 包络触及算重叠；核心区分离 → 扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=6, zg=8, dd=5, gg=9)
    assert is_zs_expand(prev, cur) is True


def test_not_done_false():
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=7, zg=9, dd=8, gg=11, done=False)
    assert is_zs_expand(prev, cur) is False
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `poetry run pytest tests/core/test_zs_expand.py -q`
Expected: FAIL（`ModuleNotFoundError: chanlun.core.zs_expand`）

- [ ] **Step 3: 精化 `can_expand_with`**

`src/chanlun/core/cl_interface.py` 替换 `can_expand_with`（line 628-633）：

```python
    def can_expand_with(self, other: 'ZS') -> bool:
        """中心定理二·中枢扩展：本体包络重叠(闭区间) 且 核心区分离(原文 line10029)。

        包络重叠 max(dd)<=min(gg)；核心区分离 other.zg<self.zd 或 other.zd>self.zg。
        核心区也重叠=延伸(同中枢)、包络分离=趋势——均非扩展。
        """
        if not other or not other.done:
            return False
        if None in (self.zd, self.zg, other.zd, other.zg,
                    self.dd, self.gg, other.dd, other.gg):
            return False
        envelope_overlap = max(self.dd, other.dd) <= min(self.gg, other.gg)
        core_separated = (other.zg < self.zd) or (other.zd > self.zg)
        return envelope_overlap and core_separated
```

- [ ] **Step 4: 建 `zs_expand.py`，加 `is_zs_expand`**

```python
# src/chanlun/core/zs_expand.py
"""zs_expand.py — P8 中枢扩展实体化（中心定理二）。

走势类型递归主链之外的「中枢升级」路径：相邻中枢按定理二判扩展(本体包络重叠+
核心区分离)，借跨越的次级别走势类型实体化为高级别中枢。孤立、不改走势类型边界
(原文 line16429 扩展⊥转折)。设计见 docs/chanlun_core_redesign_8_中枢扩展_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS, ZSLX


def is_zs_expand(prev: Optional[ZS], cur: Optional[ZS]) -> bool:
    """中心定理二·中枢扩展判定（委托 ZS.can_expand_with，统一几何口径）。"""
    if prev is None or cur is None:
        return False
    return prev.can_expand_with(cur)
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `poetry run pytest tests/core/test_zs_expand.py -q`
Expected: PASS（5 passed）

- [ ] **Step 6: 提交**

```bash
git add src/chanlun/core/cl_interface.py src/chanlun/core/zs_expand.py tests/core/test_zs_expand.py
git commit
```
commit message（中文，结尾附 Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>）：
`feat(core/zs_expand): 中心定理二扩展判定 is_zs_expand + can_expand_with补核心区分离(P8)`

---

## Task 2：扩展实体化（`materialize_expansions` + `_spanning_zslxs` + `_build_expanded_zs`）

**Files:**
- Modify: `src/chanlun/core/zs_expand.py`
- Test: `tests/core/test_zs_expand.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/core/test_zs_expand.py`：

```python
from chanlun.core.cl_interface import ZSLX
from chanlun.core.zs_expand import materialize_expansions


def _zslx(zs_low, zs_high, zss):
    """构造测试用走势类型：zs_low=DD / zs_high=GG，zss=其中枢列表。"""
    w = ZSLX(zslx_level=None, start=None, end=None)
    w.zs_low, w.zs_high = zs_low, zs_high
    w.zss = list(zss)
    return w


def test_materialize_expand_three_zslx():
    # 2 个扩展中枢 z0,z1，跨越 3 个走势类型 w0,w1,w2(包络分别 [9,13][8,11][8.5,12])
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(8, 11, [z0, z1])   # 盘整走势类型含两扩展中枢
    w2 = _zslx(8.5, 12, [z1])
    out = materialize_expansions([z0, z1], [w0, w1, w2])
    assert len(out) == 1
    hi = out[0]
    # 核心区=重合：ZG=min(13,11,12)=11，ZD=max(9,8,8.5)=9
    assert hi.zg == 11 and hi.zd == 9
    # 包络=并集：GG=max(13,11,12)=13，DD=min(9,8,8.5)=8
    assert hi.gg == 13 and hi.dd == 8
    assert hi.done is True            # 跨越 3 走势类型 = 完成式
    assert hi.expanded_with == [z0, z1]


def test_materialize_forming_two_zslx():
    # 跨越仅 2 走势类型 → forming(done=False)
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(8, 11, [z1])
    out = materialize_expansions([z0, z1], [w0, w1])
    assert len(out) == 1 and out[0].done is False


def test_materialize_no_expand_skipped():
    # 趋势(包络分离)：不产升级中枢
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=15, zg=17, dd=14, gg=18)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(14, 18, [z1])
    assert materialize_expansions([z0, z1], [w0, w1]) == []


def test_materialize_extension_nine_lines():
    # 单中枢 9 段延伸：自成一组升级
    z = _zs(zd=10, zg=12, dd=9, gg=13, line_num=9)
    w = _zslx(9, 13, [z])
    out = materialize_expansions([z], [w])
    assert len(out) == 1 and out[0].expanded_with == [z]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `poetry run pytest tests/core/test_zs_expand.py -q`
Expected: FAIL（`ImportError: cannot import name 'materialize_expansions'`）

- [ ] **Step 3: 实现三函数**

追加到 `src/chanlun/core/zs_expand.py`：

```python
import copy


def _spanning_zslxs(group: List[ZS], zslxs: List[ZSLX]) -> List[ZSLX]:
    """扩展组跨越的次级别走势类型：取 .zss 含 group 任一中枢的连续 zslxs，
    不足 3 个则向两侧补满到 3（forming 由调用方按实际跨越数判定）。

    注：精确选取规则首版用「含组中枢的 zslxs + 补满到 3」，真实出图审校。
    """
    if not zslxs:
        return []
    gid = {id(z) for z in group}
    idxs = [i for i, w in enumerate(zslxs) if any(id(z) in gid for z in w.zss)]
    if not idxs:
        return []
    lo, hi = idxs[0], idxs[-1]
    while (hi - lo + 1) < 3:
        if lo > 0:
            lo -= 1
        elif hi < len(zslxs) - 1:
            hi += 1
        else:
            break
    return zslxs[lo:hi + 1]


def _build_expanded_zs(spanning: List[ZSLX], subs: List[ZS]) -> ZS:
    """走势类型列表 → 高级别中枢。核心区=重合、包络=并集；done=跨越≥3。"""
    zg = min(w.zs_high for w in spanning)    # 核心区上沿 = 重合
    zd = max(w.zs_low for w in spanning)     # 核心区下沿 = 重合
    gg = max(w.zs_high for w in spanning)    # 包络上沿 = 并集
    dd = min(w.zs_low for w in spanning)     # 包络下沿 = 并集
    z = ZS(zs_type="xd", start=spanning[0], end=spanning[-1],
           zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = list(spanning)                 # 构成段=次级别走势类型
    z.line_num = len(spanning)
    z.done = len(spanning) >= 3              # 3 走势类型(=9段)才完成(line27278)
    z.real = True
    z.expanded_with = list(subs)             # 记录子中枢链
    z._bounds_dirty = False                  # 防 update_boundaries 把 gg/dd 重算成并集覆盖核心区
    return z


def materialize_expansions(zss: List[ZS], zslxs: List[ZSLX]) -> List[ZS]:
    """检测中枢扩展(定理二)/延伸，借次级别走势类型实体化高级别中枢，按时间序返回。"""
    if not zss:
        return []
    n = len(zss)
    used = [False] * n
    results = []                              # (order_idx, ZS)
    # 扩展：相邻 is_zs_expand 连续组(≥2)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and is_zs_expand(zss[j], zss[j + 1]):
            j += 1
        if j > i:
            group = zss[i:j + 1]
            spanning = _spanning_zslxs(group, zslxs)
            if spanning:
                results.append((i, _build_expanded_zs(spanning, group)))
            for k in range(i, j + 1):
                used[k] = True
            i = j + 1
        else:
            i += 1
    # 延伸：未用过的单中枢 ≥9 段
    for k in range(n):
        if not used[k] and zss[k].is_extension_candidate(9):
            spanning = _spanning_zslxs([zss[k]], zslxs)
            if spanning:
                results.append((k, _build_expanded_zs(spanning, [zss[k]])))
    results.sort(key=lambda t: t[0])
    return [z for _, z in results]
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `poetry run pytest tests/core/test_zs_expand.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: lint**

Run: `poetry run ruff check src/chanlun/core/zs_expand.py`
Expected: 无 error

- [ ] **Step 6: 提交**

`feat(core/zs_expand): materialize_expansions 借3走势类型实体化高级别中枢(重合区间+完成度)(P8)`

---

## Task 3：递归扩展叠加进 `recursive_branch`

**Files:**
- Modify: `src/chanlun/core/recursive_branch.py`
- Test: `tests/core/test_recursive_branch.py`

- [ ] **Step 1: 写失败测试（合成 LevelResult，直测叠加函数）**

追加到 `tests/core/test_recursive_branch.py`（不造合成线段——直接喂 LevelResult 测 `_apply_expansion_overlay`，确定性、bite-sized）：

```python
def test_apply_expansion_overlay_adds_higher_level():
    from chanlun.core.recursive_branch import _apply_expansion_overlay, LevelResult
    from chanlun.core.cl_interface import ZS, ZSLX

    def _zs(zd, zg, dd, gg, done=True, line_num=3):
        z = ZS(zs_type="xd", start=None)
        z.zd, z.zg, z.dd, z.gg = zd, zg, dd, gg
        z.done = done
        z.line_num = line_num
        return z

    def _zslx(zs_low, zs_high, zss):
        w = ZSLX(zslx_level=None, start=None, end=None)
        w.zs_low, w.zs_high = zs_low, zs_high
        w.zss = list(zss)
        return w

    z0 = _zs(10, 12, 9, 13)
    z1 = _zs(7, 9, 8, 11)                       # 与 z0 核心区分离+包络重叠 → 扩展
    w0, w1, w2 = _zslx(9, 13, [z0]), _zslx(8, 11, [z0, z1]), _zslx(8.5, 12, [z1])
    results = [LevelResult(level=0, zss=[z0, z1], done_divergence=[None, None],
                           zslxs=[w0, w1, w2], upgrade_idx=[], units=[])]
    _apply_expansion_overlay(results)
    assert any(lv.level == 1 for lv in results)
    l1 = next(lv for lv in results if lv.level == 1)
    assert l1.zss and l1.zss[0].expanded_with == [z0, z1]
    assert len(l1.done_divergence) == len(l1.zss)     # 索引对齐不变量
    assert len(l1.upgrade_idx) == len(l1.zss)         # 升级标注对齐
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `poetry run pytest tests/core/test_recursive_branch.py::test_apply_expansion_overlay_adds_higher_level -q`
Expected: FAIL（`ImportError: cannot import name '_apply_expansion_overlay'`）

- [ ] **Step 3: 实现 `_apply_expansion_overlay` 并在 `calculate` 调用**

`recursive_branch.py`：`import` 区加 `from chanlun.core.zs_expand import materialize_expansions`。新增模块函数：

```python
def _apply_expansion_overlay(results: List[LevelResult]) -> None:
    """中枢扩展叠加(中心定理二,递归)：每级中枢借本级走势类型抬升，产出并入 level+1，
    与走势类型递归并入同一层级树(in-place 修改 results)。

    trend 主链多数只到 L0(Phase0)，扩展在此把单周期推到 L1/L2…。
    """
    by_level = {r.level: r for r in results}
    k = 0
    while k < _MAX_LEVELS:
        cur = by_level.get(k)
        if cur is None or not cur.zss:
            break
        expanded = materialize_expansions(cur.zss, cur.zslxs)
        if not expanded:
            break
        nxt = by_level.get(k + 1)
        if nxt is None:
            nxt = LevelResult(level=k + 1, zss=[], done_divergence=[],
                              zslxs=[], upgrade_idx=[], units=list(cur.zss))
            results.append(nxt)
            by_level[k + 1] = nxt
        # 并入(去重：trend 在 L≥1 多数为空，首版直接 append 扩展产物)
        base = len(nxt.zss)
        nxt.zss.extend(expanded)
        nxt.done_divergence.extend([None] * len(expanded))
        nxt.upgrade_idx.extend(range(base, base + len(expanded)))
        k += 1
    results.sort(key=lambda r: r.level)
```

在 `calculate` 的**最终 `return results` 之前**插入一行调用：

```python
        _apply_expansion_overlay(results)
        return results
```

> 去重：首版「直接 append」（trend 主链在 L≥1 多数为空，无冲突）。真实出图若见重叠双框，再按子段索引范围合并（设计组件 3 已标注）。

- [ ] **Step 4: 运行测试，确认通过**

Run: `poetry run pytest tests/core/test_recursive_branch.py -q`
Expected: PASS（含新用例；既有用例不破）

- [ ] **Step 5: 提交**

`feat(core/recursive_branch): 中枢扩展递归叠加(_apply_expansion_overlay)进层级树——单周期产L1/L2(P8)`

---

## Task 4：真实 fixture 集成验证

**Files:**
- Test: `tests/core/test_recursive_branch.py`（追加真实数据用例）

- [ ] **Step 1: 写测试**

```python
import pandas as pd
import pytest
from chanlun.core.cl import CL
from chanlun.core.cl_interface import Config

_CFG = {
    "chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
    "zs_wzgx": Config.ZS_WZGX_GD.value,
}


@pytest.mark.parametrize("path,name", [
    ("tests/fixtures/klines/a_SH_513100_1m.parquet", "513100"),
    ("tests/fixtures/klines/us_TSLA_US_1m.csv", "TSLA"),
])
def test_real_fixture_expansion_levels(path, name):
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    cd = CL(name, "1m", dict(_CFG))
    cd.process_klines(df)
    levels = cd.get_recursive_branch_levels()
    higher = [z for lv in levels if lv.level >= 1 for z in lv.zss if z.expanded_with]
    # 真实数据应产出 ≥1 个扩展高级别中枢（spike 实测：513100 线段中枢 3→1 扩展组）
    assert len(higher) >= 1
    for z in higher:
        assert z.zd < z.zg                       # 核心区非退化
        assert z.dd <= z.zd and z.zg <= z.gg      # 核心区 ⊂ 包络
```

- [ ] **Step 2: 运行测试**

Run: `poetry run pytest tests/core/test_recursive_branch.py -k real_fixture -q`
Expected: PASS。若某标的核心区退化(`zd>=zg`)或产 0 个 → 记录实际，按设计组件 2「走势类型 GG/DD 取法」或 `_spanning_zslxs` 调整（出图审校点）。

- [ ] **Step 3: 提交**

`test(core/recursive_branch): 真实fixture(513100/TSLA)中枢扩展集成——校扩展产出与区间非退化(P8)`

---

## Task 5：取代 P7（后端停用 + 高级别中枢改从 recursive_levels）

**Files:**
- Modify: `web/chanlun_chart/cl_app/blueprints/tv.py`（移除 `apply_higher_zs_to_chart_data` 调用）
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`（`apply_higher_zs_to_chart_data` 门控默认关；保留休眠不删）
- Modify: `src/chanlun/cl_utils.py`（确认 `recursive_levels` 含 L1/L2/L3，命名沿 FREQ 阶梯）
- Test: `tests/.../test_apply_higher_zs.py`（改为断言不再写 higher_zs，或跳过）

- [ ] **Step 1: 改测试**

把现有 `test_apply_higher_zs` 集成断言改为：默认配置下 `apply_higher_zs_to_chart_data` 返回 False、`chart_data` 不含 `higher_zs`（P7 已停用）；高级别中枢经 `recursive_levels` 提供。

- [ ] **Step 2: 运行，确认失败**

Run: `poetry run pytest tests/ -k higher_zs -q`
Expected: FAIL（当前仍写 higher_zs）

- [ ] **Step 3: 停用 P7**

- `tv.py`：删除/注释 `apply_higher_zs_to_chart_data(...)` 调用（line ~790）。
- `chart_compute.apply_higher_zs_to_chart_data`：函数体首行 `return False`（保留实现休眠，注释「P8 取代 P7，停用；保留可逆」）。
- 确认 `cl_utils` 构 `recursive_levels` 时未限制 `level==0`（应输出所有 level 的 zss）；若有限制则放开。

- [ ] **Step 4: 运行，确认通过**

Run: `poetry run pytest tests/ -k "higher_zs or recursive" -q`
Expected: PASS

- [ ] **Step 5: 提交**

`refactor(web): 停用P7真实多周期叠加——高级别中枢改由P8单周期扩展(recursive_levels)产出(P8)`

---

## Task 6：前端渲染 L1/L2/L3 + 级别开关改绑 + bump SCHEMA

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`（recursive_zss 渲染放开非 L0；级别开关从 `higher_zs_<period>` 改绑 `recursive_levels` 各级）
- Modify: `web/chanlun_chart/cl_app/services/chart_cache.py`（`_CHART_CACHE_SCHEMA_VERSION` v7→v8）

- [ ] **Step 1: charts.js — 渲染所有递归级别**

`drawChartElements` 中 recursive_zss 渲染：移除 `if((lvObj.level||0)!==0)continue;`，改为按 `lvObj.level` 取 `RECURSIVE_LEVEL_COLORS[level]`、按级别开关 `cfg['zs_L'+level]!==false` 控显隐；`_zsLevels` 计算从 `recursive_levels` 实际级别数 + `FREQ_CHAIN` 命名（L0=本周期级别、L1=链上+1…）生成中枢组 checkbox。

- [ ] **Step 2: charts.js — 移除 P7 higher_zss 渲染分支**

删除/停用 `higher_zss` 容器与 `higher_zs` 分支（P7 已停供数据）；toggle keys 用 `_zsLevels.map(L=>L.key)`。

- [ ] **Step 3: bump SCHEMA_VERSION**

`chart_cache.py`：`_CHART_CACHE_SCHEMA_VERSION = "v8"`（chart_data 高级别中枢来源变更、不进 config，需强制旧缓存失效）。

- [ ] **Step 4: JS 语法检查**

Run: `node --check web/chanlun_chart/cl_app/static/js/charts.js`
Expected: 无语法错误

- [ ] **Step 5: 提交**

`feat(charts): 渲染L1/L2/L3递归扩展中枢+级别开关改绑recursive_levels+bump SCHEMA v8(P8)`

---

## Task 7：全回归 + 真实出图验收

**Files:** 无（验证）

- [ ] **Step 1: 全套回归**

Run: `poetry run pytest tests/ -q`
Expected: 仅 3 个 pre-existing `test_exchange_lookback` 失败（QMT 配置，与 P8 无关）；其余全过。

- [ ] **Step 2: lint**

Run: `poetry run ruff check src/chanlun/core/zs_expand.py src/chanlun/core/recursive_branch.py web/chanlun_chart/cl_app/services/chart_compute.py`
Expected: 无 error

- [ ] **Step 3: 真实出图验收（人工）**

- 清 `chart_cache`（RAM+磁盘）后重启 flask、硬刷新。
- A 股 1m 图：核对 L0(1min级别·线段中枢)、L1(5min级别)、L2(30min级别) 中枢位置、扩展框区间（核心区窄于包络）、级别命名。
- 确认 P7 已下线无残留双框；2 个重叠 1min 中枢处能看到 L1 高级别中枢（用户原始诉求）。

- [ ] **Step 4: 记录验收结论**

出图正确 → 进入完成流程（finishing-a-development-branch）；区间/选段不对 → 回 Task 4 的 `_spanning_zslxs` / 走势类型 GG/DD 取法校准并复测。

---

## 风险与回退

- `_spanning_zslxs` 精确选段是最大不确定点：首版「含组中枢 zslxs + 补满到 3」，靠 Task 4 真实 fixture + Task 7 出图审校准；退路是改用扩展子中枢自身包络重合（2 中枢直算）。
- 扩展产 L≥1 中枢的 `done_divergence` 暂置 None（不评背驰）；高级别买卖点/背驰留后续，不在本计划。
- P7 代码保留休眠（`return False`），验收稳定后另行清理。
