# 子项目⑤c（P5c）设计：扩展买点（= 多级三类 / 扩张三买）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` §6（第三类买卖点）+ §3.6（中心定理）。
> 上游：`recursive_branch.py`(P4b，多级 `LevelResult`) + `bs_branch.py`(P5a，`BsBranchCalculator._third_class` + `BuySellPoint`) + `zs_branch.py`(`ZsBranchResult`)。
> 原文：10646（中枢扩张/新生 → 中枢之上买点=第三类买点）；10027（3 买卖点后中枢扩展成高级别中枢）。`*.md` 被 gitignore。

---

## 0. 范围

**含（MVP）**：
- 改 `recursive_branch.py`：`LevelResult` 加 `units` 字段（该级输入段序列：L0=bis、L_k=喂回的 L_{k-1} 走势类型）。**唯一上游改动**。
- 新建 `src/chanlun/core/bs3_branch.py`：`Bs3BranchCalculator` 对**各级** `LevelResult` 出三类买卖点（**复用 P5a `BsBranchCalculator._third_class`**），标 `level=k`。
- **扩张三买 = L1+ 中枢的三类点**（10646）。

**关键依据（probe 逐位验证）**：「中枢扩展精确实体化」非缺失——扩张升级形成的高级别中枢 **= recursive 递归装配出的 L1 中枢**，其核心区间 `zd/zg` 的算法（3 段次级别走势类型的 `[max(DD), min(GG)]`，DD/GG=ZSLX 包络）与原文/用户口径**逐位相等**（实测 L1 中枢 `zd/zg=[2.085,2.095]` == `[max(DD),min(GG)]`）。故扩展买点无需新实体化，只需把三类买卖点逐级跑。

**不含（明确留后）**：
- **pending 中枢三类**（pending 中枢无 `z.end` 离开段，`_third_class` 自然跳过）→ 数据限制，非 bug。
- **多级一类标注**（一类已在各级 `done_divergence`；多级一类如需独立标 → 后续）。
- **二之2类独立标记**（≈ P5b 的 L1 二买，已覆盖；扩张特异性体现在 L1 中枢由扩张实体化、recursive 已处理）。
- **接 CL 生产链路** → 后续。
- 不接 CL、不动旧 `bs_point_calculator`（并存重做、零回归）。

---

## 1. 目标与产物

`Bs3BranchCalculator.calculate(levels: List[LevelResult]) -> List[BuySellPoint]`（各级三类点，带 `level=k`）。无状态、全量重算。

**原文依据**：10646（中枢扩张/新生在中枢之上的买点=第三类买点）+ 第三类买卖点定理（10031，P5a 已实现单级）。多级化 = 把三类应用到 recursive 的每级中枢（L1+ = 扩张升级中枢）。

---

## 2. 模块与接口

**改 `recursive_branch.py`**（唯一上游改动）：
```python
@dataclass
class LevelResult:
    level: int
    zss: List[ZS]
    done_divergence: List[Optional[DivergenceResult]]
    zslxs: List[ZSLX]
    upgrade_idx: List[int] = field(default_factory=list)
    units: List[LINE] = field(default_factory=list)   # P5c:该级输入段序列(回试段定位)
```
`calculate` 在 done 分支与 pending 分支 append `LevelResult` 时填 `units=list(units)`（循环里 `units` 变量现成）。默认 `[]` → P4c/P5b/P6（读 zss/done_divergence，不碰 units）不破；现有 `test_recursive_branch` positional 构造不破。

**新建 `bs3_branch.py`**：
```python
from typing import List
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import ZsBranchResult
from chanlun.core.bs_branch import BuySellPoint, BsBranchCalculator

class Bs3BranchCalculator:
    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]: ...
```
依赖 `recursive_branch.LevelResult`、`zs_branch.ZsBranchResult`、`bs_branch`(BuySellPoint + `_third_class`)。不依赖 CL。无状态。

---

## 3. recursive `units` 改动（钉死）

`recursive_branch.calculate` 循环：`units` 是「喂给本级算中枢」的输入段（L0=`list(xds)`、L_k=`_as_units(zslxs)`）。append `LevelResult` 时存它——本级中枢的 `lines`/`z.end` 即来自这批 units（对象身份一致，`_next_seg` 的 `is` 可匹配）。

```python
# done 分支
results.append(LevelResult(
    level=level, zss=res.done_zss, done_divergence=res.done_divergence,
    zslxs=zslxs, upgrade_idx=_mark_upgrades(res.done_zss), units=list(units),
))
# pending 分支
results.append(LevelResult(
    level=level, zss=[h.zs for h in pend],
    done_divergence=[h.divergence for h in pend],
    zslxs=[], upgrade_idx=_mark_upgrades([h.zs for h in pend]), units=list(units),
))
```

---

## 4. 多级三类算法（复用 P5a `_third_class`）

```python
def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
    """各级中枢出三类买卖点(扩张三买=L1+中枢三类)。复用 P5a _third_class,标 level。"""
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

- 每级构造 `ZsBranchResult`（从 `LevelResult.zss`+`done_divergence`），用 `lr.units` 作 lines。
- `_third_class`（P5a）：中枢 `z.end` 离开 + `_next_seg`(units 紧邻下一段)回试不破核心 ZG/ZD → 3buy/3sell。
- L_k 中枢 `z.end` ∈ `lr.units`（同对象，§3 保证）→ `_next_seg` 的 `is` 匹配成功。
- 产出 `BuySellPoint` 标 `level=lr.level`（L0=0、扩张三买 L1+ = 1/2…）。

---

## 5. 口径要点

| 点 | 口径 |
|---|---|
| 三类判据 | 完全复用 P5a：核心区间 `z.zg/z.zd`、第一次回试（紧邻下一段）、向上离开→3buy/向下→3sell |
| 回试段定位 | `lr.units` 内对象身份（`z.end` 来自该级 units，§3 保证 `is` 匹配）|
| level 标注 | `level=lr.level`（L0=0；扩张三买 L1+=1/2…）|
| pending 中枢 | 无 `z.end` → `_third_class` 跳过（不产三类）|
| L0 一致性 | L0 三类应与 P5a 单级 `bs_branch` 逐点相同（同 bis、同 zs_branch 口径）|

---

## 6. 测试 + 验证

**TDD（`tests/core/test_bs3_branch.py` 新建）**——受控多级 `LevelResult`（带 `units`，`_seg`/`_make_zs` 范式）：
- **L0 三类**：L0 中枢（z.end 向上离开）+ units（含 z.end + 回试段不破 ZG）→ `3buy` `level==0`。
- **L1 三类（扩张三买）**：L1 中枢 + units（ZSLX 序列）→ `3buy` `level==1`。
- **pending 无三类**：中枢无 `z.end`（pending）→ 不产。
- **向下离开→3sell**、**回试破 ZG 不产**（复用 P5a 口径，跨级再验）。
- **空输入** → `[]`。
- **recursive units 填充**：`RecursiveBranchCalculator` 产的 `LevelResult.units` 非空、L_k 中枢 `z.end` ∈ `units`。
- **recursive 回归**：`LevelResult` 加 units 后 `test_recursive_branch` 全绿。

**真实数据出图（验收）**：
fixture → `RecursiveBranchCalculator` → `Bs3BranchCalculator` → 打印各级三类：
- **L0 三类 = P5a 的 14 个**（8 3buy + 6 3sell，逐点一致性验证）。
- **L1 中枢 pending（无 z.end）→ L1 三类=0**（数据限制负向）。
- 受控演示：造 L1 done 中枢 + units → L1 三类（扩张三买），出图标 `L1 3buy`。

---

## 7. 留后清单

- **pending 中枢三类**（右边缘实时扩张三买）→ 后续，与 live/provisional 一起。
- **多级一类独立标注** → 后续（各级 done_divergence 已含一类）。
- **二之2类独立语义**（与 P5b L1 二买的区分）→ 后续如需。
- **接 CL 生产链路** → 后续。
- 至此买卖点：一类/三类（P5a 单级）+ 二类（P5b 跨级）+ 三类多级/扩张三买（P5c）；剩 live 实时 + 接 CL。
