# 子项目⑤b（P5b）设计：二类买卖点（定律一 · 次级别一类递归）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` **§6 三类买卖点**（买卖点定律一：二类=次级别相应走势第一类买点）。
> 上游：`recursive_branch.py`(P4b，多级 `LevelResult`) + `bs_branch.py`(P5a，`BuySellPoint`) + `zs_branch.py`(`DivergenceResult`)。
> 原文：定律一（clean 3562/3598：周线二买=日线一买，所有买点归结到一类）+ MACD 定律（6046：二买=第一次上 0 轴回抽）。`*.md` 被 gitignore，本地文件。

---

## 0. 范围

**含（MVP）**：
- 新建 `src/chanlun/core/bs2_branch.py`：`Bs2BranchCalculator` 从 `recursive_branch` 多级 `LevelResult` 产**二类**买卖点。
- **二类**（定律一）= **次级别 `L_{k-1}` 的第一类点**：`L_k` 一买之后、`L_{k-1}` 时间在后、低点**不破前低**的第一个一买 → `L_k` 二买；卖点对称。
- `BuySellPoint` 加**可选 `level` 字段**（默认 None，P5a 一三类不破；二类标 `level=k`）。

**不含（明确留后）**：
- **中枢扩展精确实体化的 2/3 类买点**（line20608/9980/10646：二之1/二之2、扩张三买）→ 后续，依赖 P4b 扩展实体化（留后）。
- **`L0` 二类**（无次级别，recursive 最低级=L0）。
- **三类点多级**（P5a 三类是单级；多级三类 → 后续）。
- **右边缘 live/provisional 二类**（本卷只用 done 一类点）→ 后续。
- **接 CL 生产链路 / 短差程序**（次级别一卖减仓、一买回补，3575）→ 后续。
- 不接 CL、不动旧 `bs_point_calculator`（并存重做、零回归）。

---

## 1. 目标与产物

`Bs2BranchCalculator.calculate(levels: List[LevelResult]) -> List[BuySellPoint]`（二类点，每个带 `level=k`）。无状态、全量重算。

**核心原文依据**：
- 买卖点定律一（3562/3598）：**大级别第二类买点由次一级别相应走势的第一类买点构成**；所有买点归结到第一类。
- 3566：日线第一类买点，在周线上就是第二类买点（级别差一）。
- 3544/6046：二买 = 女上位第一吻后下跌 / 第一次上 0 轴回抽确认（**回调不创新低**）。

---

## 2. 模块与接口

**改 `bs_branch.py`**（唯一上游改动，加可选字段）：
```python
@dataclass
class BuySellPoint:
    bs_type: str
    zs: ZS
    signal_seg: LINE
    anchor_fx: FX
    divergence: Optional[DivergenceResult]
    level: Optional[int] = None        # P5b:二类归属级别 L_k;P5a 一三类 None
```
默认 None → P5a 现有 positional 构造（5 参）完全不破。

**新建 `bs2_branch.py`**：
```python
from typing import List, Optional, Tuple
from chanlun.core.cl_interface import LINE
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.bs_branch import BuySellPoint
from chanlun.core.cl_interface import ZS

class Bs2BranchCalculator:
    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]: ...
```
依赖 `recursive_branch.LevelResult`、`bs_branch.BuySellPoint`、`zs_branch.DivergenceResult`。**不**依赖 CL。无状态。

---

## 3. 一类点识别

```python
def _first_points(self, level: LevelResult) -> List[Tuple[ZS, DivergenceResult, LINE]]:
    """该级**已固化**一类点：(zs, divergence, 离开段 c)。仅趋势背驰 qs、非 provisional。"""
    out = []
    for i, dv in enumerate(level.done_divergence):
        if dv is not None and dv.is_beichi and dv.kind == "qs" and not dv.provisional:
            out.append((level.zss[i], dv, dv.leave_seg))   # LevelResult 字段是 zss
    return out
```
- `c._type == "down"` = 一买（背驰底，锚 `c.end` 低点）；`"up"` = 一卖。
- 排除 `provisional`（右边缘 pending 级别的未固化背驰，本卷只用 done）。

---

## 4. 二类关联算法（钉死）

```python
def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
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
def _find_second(c_k: LINE, sub) -> Optional[Tuple[ZS, DivergenceResult, LINE]]:
    """L_k 一类点 c_k 之后,次级别同向、不破前低/高的**第一个**(时间最早)一类点。"""
    t_k = c_k.end.k.k_index
    val_k = c_k.end.val
    best = None
    best_t = None
    for zs_sub, dv_sub, c_sub in sub:
        if c_sub._type != c_k._type:                 # 同向(一买配一买/一卖配一卖)
            continue
        t_sub = c_sub.end.k.k_index
        if t_sub <= t_k:                             # 必须在 L_k 一类点之后
            continue
        if c_k._type == "down" and c_sub.end.val < val_k:   # 一买:回调破前低 → 跳过
            continue
        if c_k._type == "up" and c_sub.end.val > val_k:     # 一卖:反抽破前高 → 跳过
            continue
        if best_t is None or t_sub < best_t:         # 取时间最早(第一个)
            best, best_t = (zs_sub, dv_sub, c_sub), t_sub
    return best
```

- **同向**：`L_k` 一买配 `L_{k-1}` 一买（都 `down`）；一卖配一卖。
- **时间在后**：`c_sub` 离开段末端晚于 `c_k`。
- **不破前低/高**（`≥`/`≤`）：一买的次级别回调低点 ≥ `L_k` 一买低点（不创新低）；一卖对称。
- **第一个**：取时间最早的满足者。

---

## 5. 口径要点

| 点 | 口径 |
|---|---|
| 次级别 | recursive 相邻 `L_{k-1}`（定律一「次一级别」）|
| 不破前低/高 | `≥`/`≤`（回调不创新低 = 不低于一买低点）|
| 二买实体 | **次级别一买**（zs/signal_seg/divergence 来自 `L_{k-1}`），但归属 `L_k`（`level=k`）|
| 一类点来源 | `LevelResult.done_divergence` 的 qs 背驰、**非 provisional** |
| L0 | 无二类（无次级别）|
| 锚点 | `c_sub.end`（次级别一买的离开段末端 = 二买价位）|

---

## 6. 测试 + 验证

**TDD（`tests/core/test_bs2_branch.py` 新建）**——受控多级 `LevelResult`（fake `DivergenceResult`，`_seg` 范式造 `leave_seg`）：
- **基本二买**：L1 一买（c 向下，低点 L、时间 t）+ L0 在其后一买（低点 ≥ L、时间 > t）→ 产 `2buy`（`level==1`，实体=L0 一买）。
- **破前低过滤**：L0 后续一买低点 < L → 跳过（无二买 / 取下一个不破的）。
- **取第一个**：L0 有 2 个不破前低的后续一买 → 取时间最早的。
- **同向**：L1 一买只配 L0 一买（不配一卖）。
- **L0 无二买**：单 L0 级 → `[]`。
- **卖点对称**：L1 一卖 + L0 后续一卖不破前高 → `2sell`。
- **provisional 排除**：provisional 一类点不参与。
- **空输入** → `[]`。
- **P5a 回归**：`BuySellPoint` 加 level 后，P5a `test_bs_branch` 全绿（positional 构造不破）。

**真实数据出图（验收）**：
fixture → CL → `bis` → `RecursiveBranchCalculator` → `Bs2BranchCalculator` → 打印各 `2buy/2sell`（level/锚点）：
- 真实数据 L1 无 done 背驰（无 L1 一买）→ **无 L1 二买**（负向验证「无次级别配对一类点 → 无二买」）。
- 受控演示：造 2 级一类点序列（L1 一买 + 其后 L0 一买不破前低）→ 标出 L1 二买，人工审。

---

## 7. 留后清单

- **中枢扩展实体化的 2/3 类买点**（line20608/9980/10646）→ 后续，依赖 P4b 扩展实体化。
- **右边缘 live/provisional 二类**（实时）→ 后续。
- **多级三类买卖点** → 后续（P5a 三类是单级）。
- **短差程序**（次级别一卖减仓/一买回补，3575）→ 策略层。
- **接 CL 生产链路** → 后续。
- 至此买卖点：一类/三类（P5a 单级）+ 二类（P5b 跨级）；扩展类与各 live/接 CL 口径留后。
