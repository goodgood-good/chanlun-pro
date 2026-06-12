# 子项目④c（P4c）设计：背驰贯通（自底向上 BUILD 嵌套背驰森林）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` **§7 区间套**（§7.1 第27课嵌套性 / §7.2 双向闭环 / §7.3 强度标注）。
> 上游：`zs_branch.py`(P1+P3，`DivergenceResult` 内联背驰) + `recursive_branch.py`(P4b，`LevelResult` 多级树)。
> 蓝本对照旧 `recursive_calculator.py` 的区间套思路（**不动它**）。`*.md` 被 gitignore，本地文件。

---

## 0. 范围

**含（MVP）**：
- 新建 `src/chanlun/core/beichi_nest.py`：把 `RecursiveBranchCalculator` 产出的多级 `LevelResult` 里**各级已固化（done）背驰段**，**自底向上 BUILD** 成嵌套森林（宪法 §7.2「贯通到顶 BUILD：从 1m 把各级背驰段造出嵌套结构」）。
- `NestedDivergence` 森林节点（`level`/`zs_index`/`divergence`/`children`）。
- 嵌套口径（已与用户抠定）：
  - **段 = 背驰离开段 `leave_seg`（c）的 K 线时间区间**（§7.1「找针对最后中枢的背驰段」）。
  - **匹配 = 严格时间包含 + 同向**：次级别 c 的 `[start_k,end_k]` 完全落在高级别 c' 区间内，且 `leave_seg._type` 相同。
  - **只相邻级别挂**（L_k → L_{k+1}）。
- 孤立、旁路、**不污染 `LevelResult`/`DivergenceResult`、不接 CL、不动旧 `recursive_calculator`**（并存重做、零回归）。

**不含（明确留后）**：
- **右边缘 provisional/live 背驰贯通**（§7.2「BUILD 在右边缘造出高级别 provisional」）→ 后续，与 P6 右边缘实时定位一起。MVP 仅 `provisional=False` 的已固化背驰。
- **每级换周期 MACD**（L1→5m、L2→30m，P4b 亦留后）→ 后续。MVP 复用同一 `ld_provider`，背驰由 P3/P4b 算好、本卷不重算。
- **跨级跳级挂载**（L_k 直挂 L_{k+2}）→ 后续。MVP 只相邻级别。
- **区间套 READ**（自顶向下：标嵌套深度 / 是否套住 / 可操作性门限，宪法 §7.3）→ **P6**。
- 非常规背驰（小转大）的嵌套口径 → 后续。

---

## 1. 目标与产物

`BeichiNestCalculator.calculate(levels: List[LevelResult]) -> List[NestedDivergence]`（顶层森林）。无状态、全量重算。是 **P6 区间套 READ** 的输入（P6 自顶向下读这片森林，标每个买卖点信号的嵌套深度/是否套住）。

**核心原文依据**：
- §7.1（第27课）**嵌套性铁律**：「次级别背驰段一定**在**大级别背驰段**里**且区间更小，逐级收缩。」→ 这是 BUILD 的挂载判据，也是出图验收的判据。
- §7.2 **双向闭环**：贯通到顶（BUILD，本卷）造嵌套结构，区间套（READ，P6）才有东西自顶向下套。

---

## 2. 模块与接口

新建 `src/chanlun/core/beichi_nest.py`：

```python
from dataclasses import dataclass, field
from typing import List

from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult


@dataclass
class NestedDivergence:
    """嵌套背驰森林的一个节点：一段已固化背驰 + 被它严格包含+同向的次级别背驰。"""
    level: int                                  # 背驰所在级别 (0=L0)
    zs_index: int                               # 该级 LevelResult.zss / done_divergence 中的索引(回溯定位)
    divergence: DivergenceResult                # 背驰本体(P3/P4b 已算,自带 leave_seg 时间区间)
    children: List["NestedDivergence"] = field(default_factory=list)  # 被严格包含+同向的次级别背驰


class BeichiNestCalculator:
    def calculate(self, levels: List[LevelResult]) -> List[NestedDivergence]: ...
```

依赖 `recursive_branch.LevelResult`、`zs_branch.DivergenceResult`。**不**依赖 CL。无状态、全量重算。

---

## 3. BUILD 算法（钉死）

```python
def calculate(self, levels: List[LevelResult]) -> List[NestedDivergence]:
    """各级 done 背驰段自底向上 BUILD 成嵌套森林。"""
    if not levels:
        return []

    # 1. 每级抽出「已固化 + is_beichi」的背驰 → 叶节点(per_level[k] = L_k 的节点列表)
    per_level: List[List[NestedDivergence]] = []
    for lr in levels:
        nodes: List[NestedDivergence] = []
        for zi, dv in enumerate(lr.done_divergence):
            if dv is not None and dv.is_beichi and not dv.provisional:   # 仅已坐实背驰
                nodes.append(NestedDivergence(level=lr.level, zs_index=zi, divergence=dv))
        per_level.append(nodes)

    # 2. 自底向上:相邻级别 (k → k+1),把 L_k 节点挂到 严格时间包含+同向 的 L_{k+1} 节点
    attached: set = set()                       # 已被挂为 child 的 L_k 节点 id
    for k in range(len(per_level) - 1):
        for lo in per_level[k]:
            parent = self._find_parent(lo, per_level[k + 1])
            if parent is not None:
                parent.children.append(lo)
                attached.add(id(lo))

    # 3. 顶层森林 = 所有未被挂载的节点(最高级别 + 断链的低级别各成树根)
    return [n for nodes in per_level for n in nodes if id(n) not in attached]
```

`_find_parent`（嵌套口径，§4）：在 `hi_nodes` 中找**唯一**严格时间包含 `lo` 且同向者；多个候选取**时间跨度最小**（最内层）；无则 None（断链 → lo 自成顶层树根）。

**为何「未挂载即树根」**：被 L_{k+1} 挂走的 L_k 节点是 children、不是根；最高级别节点无更高级可挂、恒为根；断链的低级节点（上一级无包含者）也是根（§7.1 意义下「未被套住」，可操作性留 P6 READ 裁）。森林 = 所有嵌套链的顶。

---

## 4. 嵌套口径（与用户抠定）

```python
@staticmethod
def _span(dv: DivergenceResult) -> tuple:
    """背驰段 = 离开段 c 的 K 线序号区间 [start_k, end_k]。
    leave_seg 是 LINE(XD 或 ZSLX),start/end 是 FX,FX.k 是代表 K 线、.k_index 是序号。
    P3 _divergence_for 已守卫 leave_seg.start/end 非 None。"""
    c = dv.leave_seg
    return (c.start.k.k_index, c.end.k.k_index)


def _find_parent(self, lo: NestedDivergence, hi_nodes: List[NestedDivergence]):
    lo_s, lo_e = self._span(lo.divergence)
    lo_dir = lo.divergence.leave_seg._type
    best, best_w = None, None
    for hi in hi_nodes:
        if hi.divergence.leave_seg._type != lo_dir:          # 同向
            continue
        hi_s, hi_e = self._span(hi.divergence)
        if hi_s <= lo_s and lo_e <= hi_e:                    # 严格时间包含(边界可贴合)
            w = hi_e - hi_s
            if best_w is None or w < best_w:                 # 取最内层(跨度最小)
                best, best_w = hi, w
    return best
```

- **段时间区间**：`leave_seg` 首尾分型的 K 线序号 `k_index`。
- **严格时间包含**：`hi_s <= lo_s and lo_e <= hi_e`。允许**边界贴合**（c 常是 c' 的最后一个子段，`lo_e == hi_e` 同一转折点）；只要 lo 不超出 hi。
- **同向**：`leave_seg._type` 相同（都 up 背驰 / 都 down 背驰）。
- **最内层**：同级 c' 时间互不重叠，正常至多一个包含；防御性地取跨度最小者。

---

## 5. 边界处置

| 情形 | 处置 |
|---|---|
| 一个 lo 被多个同向 hi 包含 | 取**最内层**（跨度最小）。同级背驰段时间本互不重叠，此为防御。 |
| lo 无 hi 包含（断链） | lo 成**顶层单节点树**（§7.1 未被套住，可操作性留 P6 READ 判）。 |
| 跳级（L_k 被 L_{k+2} 含、L_{k+1} 无对应） | MVP **只相邻挂**；L_k 断链为顶层，不跨级挂。跨级留后。 |
| 右边缘 provisional 背驰 | MVP `not dv.provisional` 过滤掉（含 P4b pending LevelResult 的 provisional 背驰）。留后。 |
| 某级无 is_beichi 背驰 | 该级节点列表空；不影响相邻级别挂载（自然断链）。 |

---

## 6. 测试 + 验证

**TDD（`tests/core/test_beichi_nest.py` 新建）**——受控 `LevelResult`（fake `DivergenceResult` + 受控段时间区间，沿用 `_seg` 范式造 `leave_seg`，绕浮点敏感）：
- **基本嵌套**：L0 背驰段 `[3,5]`、L1 背驰段 `[1,8]`（同向、严格含 `[3,5]`）→ 断言 L0 挂进 L1 的 `children`，顶层森林只剩 L1 根。
- **同向过滤**：L0 `[3,5]` up、L1 `[1,8]` down → 不挂，两者皆顶层。
- **非包含不挂**：L0 `[3,9]`、L1 `[1,8]`（lo 超出 hi 右界）→ 不挂。
- **最内层**：L0 `[4,5]` 同时落在 L1a `[1,8]`、L1b `[3,6]` → 挂 L1b（跨度小）。（注：人为构造同级重叠以测最内层逻辑。）
- **断链成根**：L0 背驰无 L1 父 → L0 自成顶层。
- **provisional 排除**：`provisional=True` 的背驰不入森林。
- **空输入**：`calculate([]) == []`；无背驰级别 → 空森林。
- **三级链**：L0→L1→L2 逐级严格包含同向 → 断言 L2 根、其 child=L1、L1 child=L0（深度 3 链）。

**真实数据出图（验收，沿用 P1/P3/P4a/P4b）**：
fixture `a_SH_513100_1m.parquet` → CL → `get_bis()` → `RecursiveBranchCalculator` → `BeichiNestCalculator` → Plotly：各级背驰段画成**按级别分行的时间横条**（L0 最下、L_n 最上），嵌套用框/连线显示「次级别 c 落在高级别 c' 内」，标方向。人工审 §7.1 嵌套是否成立（高级别背驰段确实框住其内的次级别同向背驰段）。

---

## 7. 留后清单

- **右边缘 provisional/live 背驰贯通**（§7.2 造高级别 provisional，右边缘实时定位的关键）→ 后续，与 P6 右边缘一起。
- **每级换周期 MACD**（精确力度，L1→5m…）→ 后续。
- **跨级跳级挂载** → 后续；MVP 只相邻。
- **区间套 READ**（自顶向下标嵌套深度 / 是否套住 / 可操作性门限）→ **P6**。
- **非常规背驰（小转大）的嵌套** → 后续。
