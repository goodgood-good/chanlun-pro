# 子项目⑥（P6）设计：区间套（自顶向下 READ · 标可操作性）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` **§7 区间套**（§7.1 第27课定理 / §7.2 双向闭环 READ / §7.3 强度标注·已定）。
> 上游：`beichi_nest.py`(P4c，`NestedDivergence` 嵌套背驰森林)。
> 蓝本对照旧 `recursive_calculator.get_interval_nest`（**不动它**，并存重做）。`*.md` 被 gitignore，本地文件。

---

## 0. 范围

**含（MVP）**：
- 新建 `src/chanlun/core/interval_nest.py`：`IntervalNestCalculator` 消费 **P4c 嵌套背驰森林**（`List[NestedDivergence]`），**自顶向下 READ**，给每个节点标区间套属性（`depth`/`is_innermost`/`is_nested`/`operable`）。
- `NestRead` 产物。
- **可操作判据**（§7.2 精髓）= **最内层 + 被套住**（结构）；`depth` 暴露给策略层按门限细化。

**不含（明确留后）**：
- **三类点**（回试、非背驰，不在嵌套森林）的可操作性 → 后续。
- **右边缘 provisional 信号**纳入（P4c 森林只 done）→ 后续，与 P4c provisional 贯通一起。
- **跨级跳级链**（P4c 森林只相邻级别挂）→ 后续。
- **深度门限的可操作性判定**（§7.1「三重背驰 depth≥3 才精确」）→ **策略层**，非 P6。
- **关联回 `BuySellPoint` 对象 / 接 CL** → 后续（P6 标森林节点，节点 `.divergence` 可回溯对应买卖点）。
- 不接 CL、不动旧 `recursive_calculator.get_interval_nest`（并存重做、零回归）。

---

## 1. 目标与产物

`IntervalNestCalculator.calculate(forest: List[NestedDivergence]) -> List[NestRead]`。无状态、全量重算。是**策略层**「区间套可操作性门限」的输入，完成宪法 §7.2 双向闭环的 READ 半边（BUILD=P4c）。

**核心原文依据**：
- §7.1（第27课）：精确转折点 = 嵌套背驰链逐级收缩的最内层；「小级别背驰只有**落在大级别背驰段里**才有意义；三重背驰（日⊃5分⊃1分）才精确定位」。
- §7.2：买卖点可操作 ⟺ 同向、逐级时间包含的嵌套背驰链的**最内层**。
- §7.3：区间套作独立自顶向下 pass，标「嵌套深度 / 是否套住」，**可操作性交策略层按深度门限决定**。

---

## 2. 模块与接口

新建 `src/chanlun/core/interval_nest.py`：

```python
from dataclasses import dataclass
from typing import List

from chanlun.core.beichi_nest import NestedDivergence


@dataclass
class NestRead:
    """一个森林节点(=某级一类点/背驰段)的区间套 READ 标注。"""
    node: NestedDivergence     # 森林节点(其 .divergence 可回溯对应买卖点)
    depth: int                 # 嵌套链层级(顶层根=1,每下一层 +1)
    is_innermost: bool         # 无 children = 最内层(最低级别背驰)
    is_nested: bool            # depth>1 = 被更高级别套住(有祖先)
    operable: bool             # is_innermost & is_nested = 结构可操作信号


class IntervalNestCalculator:
    def calculate(self, forest: List[NestedDivergence]) -> List[NestRead]: ...
```

依赖 `beichi_nest.NestedDivergence`。**不**依赖 CL。无状态、全量重算。

---

## 3. 算法（自顶向下 DFS 标属性，钉死）

```python
def calculate(self, forest: List[NestedDivergence]) -> List[NestRead]:
    """森林每个节点标区间套属性。顶层根 depth=1,逐级向内 +1。"""
    out: List[NestRead] = []

    def _dfs(node: NestedDivergence, depth: int) -> None:
        innermost = not node.children                       # 无子 = 最内层
        nested = depth > 1                                  # 有祖先 = 被套住
        out.append(NestRead(
            node=node, depth=depth, is_innermost=innermost,
            is_nested=nested, operable=innermost and nested,
        ))
        for ch in node.children:
            _dfs(ch, depth + 1)

    for root in forest:
        _dfs(root, 1)
    return out
```

自顶向下：从森林每棵树的根（最高级别背驰）depth=1 起，DFS 向内（次级别）逐级 +1。

---

## 4. 口径要点

| 属性 | 定义 |
|---|---|
| `depth` | 嵌套链层级：顶层根=1，每往内一层 +1。= 该背驰段被套的层数（含自身）|
| `is_innermost` | `not node.children`：无次级别子节点 = 最低级别背驰（嵌套链末端）|
| `is_nested` | `depth > 1`：有祖先 = 落在更高级别背驰段里（§7.1「落在大级别背驰段里才有意义」）|
| `operable` | `is_innermost and is_nested`：**结构可操作** = 嵌套链末端 + 被逐级套住 |

- **孤立单节点**（`depth=1` 最内层但无祖先）→ `is_nested=False` → `operable=False`：孤立背驰没被套住，§7.1 意义下不可精确操作。
- **顶层根有子**（`depth=1`、有 children）→ `is_innermost=False` → 不可操作：它是高级别信号，精确点要往内找最内层。
- **深度门限**（三重 `depth≥3` 才精确，§7.1）→ **策略层**按 `depth` 判，P6 只标结构属性、不设门限（§7.3「可操作性交策略层」）。

---

## 5. 测试 + 验证

**TDD（`tests/core/test_interval_nest.py` 新建）**——受控 `NestedDivergence` 森林（fake 节点，沿用 P4c `_node` 范式或直接构造）：
- **3 级链**（L2→L1→L0）：L0 `depth=3`/innermost/nested/**operable**；L1 `depth=2`/not innermost/nested/not operable；L2 `depth=1`/not innermost/not nested/not operable。
- **孤立单节点**：`depth=1`/innermost/not nested/**not operable**（负向核心：孤立背驰不可操作）。
- **同父多子**（L1 含 2 个 L0 叶）：两 L0 都 `depth=2`/innermost/nested/operable；L1 `depth=1`/not innermost。
- **空森林** → `[]`。
- **多棵树**：森林含一棵 3 级链 + 一个孤立根 → 标注互不干扰。

**真实数据出图（验收，沿用 P1/P3/P4a/b/c/P5a）**：
fixture → CL → `bis` → `RecursiveBranchCalculator` → `BeichiNestCalculator` → `IntervalNestCalculator` → 打印各节点 `depth/operable`：
- 真实森林孤立（L0 全 depth=1）→ 全 `operable=False`，**负向验证**「无嵌套链 → 无可操作信号」。
- 复用 P4c 受控 3 级嵌套演示森林 → `IntervalNestCalculator` 标注 → Plotly 在嵌套图上标 `operable` 节点（高亮最内层可操作信号），人工审区间套可操作性判定正确。

---

## 6. 留后清单

- **三类点可操作性** → 后续（非背驰、不在嵌套森林，需另立机制）。
- **右边缘 provisional 信号** 纳入区间套 → 后续，与 P4c provisional 贯通一起。
- **跨级跳级链** → 后续（依赖 P4c 跨级挂载）。
- **深度门限可操作性策略**（三重精确）→ **策略层**消费 `depth`。
- **关联回 `BuySellPoint` / 接 CL 生产链路** → 后续。
- 至此缠论递归核心重做（P1 中枢 → P2 背驰 → P3 内联 → P4 递归贯通 → P5a 买卖点 → P6 区间套）主干闭环；剩 P5b（二类+扩展买点）+ 各留后口径。
