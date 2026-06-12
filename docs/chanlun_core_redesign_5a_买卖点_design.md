# 子项目⑤a（P5a）设计：买卖点（一类 + 三类，单级别 done）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` **§6 三类买卖点=节点坍缩信号** + **§4 节点①**（H2 坍缩=三类）+ §5 背驰。
> 上游：`zs_branch.py`(P1+P3，`ZsBranchResult`/`DivergenceResult`/`ZS`)。
> 蓝本对照旧 `bs_point_calculator.py`（**不动它**，并存重做）。`*.md` 被 gitignore，本地文件。

---

## 0. 范围

**含（MVP）**：
- 新建 `src/chanlun/core/bs_branch.py`：`BsBranchCalculator` 从 `zs_branch` 的 `ZsBranchResult`(`done_zss`+`done_divergence`) + 原始 `lines`，产**已完成（done）中枢**的**一类 + 三类**买卖点。
- **一类** = 趋势背驰（`done_divergence` 里 `is_beichi & kind=="qs"`）。原文第18/24课：一类买卖点由趋势背驰构成。
- **三类** = 离开中枢、回试**不破 ZG/ZD**（节点① H2 坍缩，第20课）。
- `BuySellPoint` 产物（不污染 ZS/LINE）。

**不含（明确留后）**：
- **二类买卖点**（次级别相应走势的第一类买点构成，买卖点定律一，原文 3562/3598）→ **P5b**。
- **中枢扩展精确实体化 + 2/3 类买点**（line20608，第57课）→ **P5b**。
- **右边缘 live 实时买卖点**（live H2 + provisional 早标不早剪，§5.4）→ 后续。
- **盘整背驰 pz 的买卖点语义** / 非常规背驰（小转大）→ 后续。
- **beichi_pz 生产对齐 / 转折型背驰**（依赖 P3 留后）→ 后续。
- **接 CL 生产链路 / 出 2 类 MACD 定律（0 轴回抽）** → 后续。
- 不接 CL、不动旧 `bs_point_calculator`（并存重做、零回归）。

---

## 1. 目标与产物

`BsBranchCalculator.calculate(zs_result: ZsBranchResult, lines: List[LINE]) -> List[BuySellPoint]`。无状态、全量重算。是策略层 / P6 区间套（标可操作性）的信号输入。

**核心原文依据**：
- 宪法 §6：三类买卖点 = 节点的坍缩信号（**一类=H2a 趋势背驰**；**三类=节点① H2 离开坍缩**：次级别离开、回试不破核心 `[ZD,ZG]`）。
- 第20课三买定理：次级别离开中枢，再以一次级别回试，低点不破 ZG（三买）/ 高点不破 ZD（三卖），**必须第一次回试**。
- 第18/24课：一类买卖点由趋势背驰构成。

---

## 2. 模块与接口

新建 `src/chanlun/core/bs_branch.py`：

```python
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
    anchor_fx: FX                             # 出图锚点(一类=c 末端极值;三类=回试段末端极值分型)
    divergence: Optional[DivergenceResult]    # 一类带背驰本体;三类为 None


class BsBranchCalculator:
    def calculate(self, zs_result: ZsBranchResult,
                  lines: List[LINE]) -> List[BuySellPoint]: ...
```

依赖 `zs_branch`(`ZsBranchResult`/`DivergenceResult`)、`cl_interface`(`LINE`/`FX`/`ZS`)。**不**依赖 CL。无状态、全量重算。

---

## 3. 一类买卖点算法

```python
def _first_class(self, zs_result) -> List[BuySellPoint]:
    out = []
    for i, dv in enumerate(zs_result.done_divergence):
        if dv is None or not dv.is_beichi or dv.kind != "qs":   # 仅趋势背驰
            continue
        c = dv.leave_seg                                        # 离开段
        z = zs_result.done_zss[i]
        if c._type == "down":                                   # 下跌趋势背驰 → 跌势衰竭
            out.append(BuySellPoint("1buy", z, c, c.end, dv))   # 锚 c.end(di 低点)
        elif c._type == "up":                                   # 上涨趋势背驰 → 涨势衰竭
            out.append(BuySellPoint("1sell", z, c, c.end, dv))  # 锚 c.end(ding 高点)
    return out
```

- 只取 `kind=="qs"`（趋势背驰）；`pz`（盘整背驰）不产一类（语义留后）。
- 方向：离开段向下→`1buy`、向上→`1sell`（背驰=该方向趋势衰竭/反转）。
- 锚点 = 离开段末端分型 `c.end`（向下段=di 低点 / 向上段=ding 高点）。

---

## 4. 三类买卖点算法

```python
def _third_class(self, zs_result, lines) -> List[BuySellPoint]:
    out = []
    for z in zs_result.done_zss:
        leave = z.end                                          # 离开段(correct_exit 剥出)
        if leave is None:
            continue
        retest = self._next_seg(leave, lines)                  # 第一次回试段 = 离开段紧邻下一段
        if retest is None:                                     # 离开到右边缘、无回试段 → 不产
            continue
        if leave._type == "up" and retest.end.val >= z.zg:     # 向上离开,回试低点不破 ZG
            out.append(BuySellPoint("3buy", z, retest, retest.end, None))
        elif leave._type == "down" and retest.end.val <= z.zd: # 向下离开,回试高点不破 ZD
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

- **离开段** = `z.end`（correct_exit 已把定向冲出段剥到 `z.end`）。向上离开→3buy 候选、向下→3sell 候选。
- **回试段** = 离开段紧邻下一段（线段方向交替：向上离开→回试段向下，其末端 `retest.end` 是 di 低点；反之 ding 高点）。**取紧邻下一段天然满足「第一次回试」**。
- **判据**（核心区间，非本体）：向上离开时回试低点 `retest.end.val ≥ z.zg`（不破 ZG）→ `3buy`；向下离开时回试高点 `retest.end.val ≤ z.zd`（不破 ZD）→ `3sell`。
- 回试段不存在（z 末中枢、离开到右边缘）→ 该中枢三类暂不产（仅 done + 回试需坐实）。

`calculate` = `_first_class(zs_result) + _third_class(zs_result, lines)`（一类在前、三类在后；同一中枢两类可并存，独立标注）。

---

## 5. 口径要点

| 点 | 口径 |
|---|---|
| 一类范围 | 仅趋势背驰 `kind=="qs"`；盘整 `pz` 不产一类（留后）|
| 三类区间 | **核心 `[ZD,ZG]`**（`z.zd`/`z.zg`），非本体 `[DD,GG]`（第20课明文 ZG/ZD）|
| 第一次回试 | 取离开段紧邻下一段，天然「第一次」 |
| 一类 vs 三类 | 独立判：一类看离开段背驰(趋势反转)、三类看回试不破(趋势延续)；同一中枢可各自触发、并存标注、不互斥 |
| 锚点 | 一类=离开段 `c.end`；三类=回试段 `retest.end`（出图定位买卖点价位）|
| `_next_seg` | 按对象身份在 `lines` 定位（`z.end` 是 `ZsCalculator` 输入段之一）；若实测 `correct_exit` 复制了对象，退化用 `z.end.end` 的 K 线序号找下一段 |

---

## 6. 测试 + 验证

**TDD（`tests/core/test_bs_branch.py` 新建）**——受控（沿用 `_seg`/`_make_zs` 范式造 `done_zss`+`done_divergence`+`lines`，fake `DivergenceResult`）：
- **一类**：qs 背驰向下→`1buy`(锚 c.end)；qs 向上→`1sell`；`pz` 背驰→不产；`is_beichi=False`→不产；`None`→跳过。
- **三类**：向上离开+回试低点≥ZG→`3buy`(锚 retest.end)；向上离开+回试破ZG(低点<ZG)→不产；向下离开+回试高点≤ZD→`3sell`；回试段缺失(离开是 lines 末段)→不产。
- **并存**：同一中枢既 qs 背驰又回试不破 → 一类+三类两点都产。
- **空输入**：`calculate` 空 zs_result → `[]`。

**真实数据出图（验收，沿用 P1/P3/P4a/b/c）**：
fixture `a_SH_513100_1m.parquet` → CL → `get_bis()` → `zs_branch` → `bs_branch` → Plotly：K 线 + 中枢框 + 买卖点标记（`1买/1卖/3买/3卖` 不同色/形），人工审位置（一类在趋势背驰衰竭处、三买在中枢上沿之上的回抽低点、三卖对称）。

---

## 7. 留后清单

- **二类买卖点**（次级别第一类递归，定律一）+ **中枢扩展实体化的 2/3 类买点**（line20608）→ **P5b**。
- **右边缘 live 实时买卖点**（provisional 早标不早剪）→ 后续。
- **盘整背驰 pz / 非常规背驰（小转大）的买卖点** → 后续。
- **beichi_pz 生产对齐 / 转折型背驰**（依赖 P3 留后）→ 后续。
- **接 CL 生产链路 / 2 类 MACD 定律（第一次上 0 轴回抽，原文 6046）** → 后续。
- **区间套标可操作性**（消费本卷买卖点信号 + P4c 嵌套森林）→ **P6**。
