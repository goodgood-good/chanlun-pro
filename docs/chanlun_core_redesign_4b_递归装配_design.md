# 子项目④b（P4b）设计：递归装配（走势类型递归主链 + 独立升级标注）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md`（§1.4 递归层级 / §6 坍缩）。
> 蓝本 = 旧 `recursive_calculator.py`（基于 ZsCalculator/ZslxCalculator，**不动它**）。
> 上游 `zs_branch.py`(P1+P3 中枢+内联背驰) + `zslx_branch.py`(P4a 走势类型)。
> `*.md` 被 gitignore，本地文件。

---

## 0. 范围

**含（MVP）**：
- 新建 `recursive_branch.py`：把 L0 线段自底向上递归装配成多级层级树（L0 中枢/走势类型 → L1 → L2 → L3）。
- **主链 = 走势类型递归**（层级即定义）：`units → zs_branch → zslx_branch → _as_units → units` 逐级。
- **旁路 = 升级标注**（line 16429 解耦）：每级记录「9 段升级 / 中枢扩展候选」中枢，存进结果供 P5 消费，**不改走势类型边界**。
- `zs_branch` 参数化 `min_zs_lines`（L0=4 / L≥1=3）——唯一对上游的改动。

**不含（明确留后）**：
- **中枢扩展的精确实体化**（line 20608「一个中枢」vs 合并）+ **2/3 类买点** → **P5**（line 18932：扩展/延伸制造 2/3 买点）。
- **每级换周期 MACD**（L1 用 5m、L2 用 30m）→ 留后；MVP 所有级别复用同一 `ld_provider`。
- **背驰跨级别贯通/区间套**（自顶向下）→ P4c / P6。
- 右边缘 live 多假设的**逐级传播**（上卷到更高级别）→ 后续。但**末级**右边缘 pending 中枢（仅 H2 leave 读法）已入树展示（用户验收决策 2026-05-31，见 §3）——只记录、不上卷、不切走势类型，让层级树画到「正在形成」的高级中枢。
- 不接 CL、不动旧 `recursive_calculator`/`zslx_calculator`（并存重做、零回归）。

---

## 1. 目标与产物

`RecursiveBranchCalculator.calculate(xds, ld_provider, wzgx, frequency)` → `List[LevelResult]`（每级一条，level 0..N）。是 P5 买卖点 / P6 区间套的多级输入。

**核心原文依据（line 16429）**：「中枢扩展与走势转折之间没什么必然联系……只有背驰才和走势的转折有必然的联系。」→ 走势类型边界（转折）只由背驰/方向反转定；中枢升级（扩展/9段）是独立旁路。

---

## 2. 模块与接口

新建 `src/chanlun/core/recursive_branch.py`：

```python
from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.cl_interface import LINE, ZS, ZSLX, Config
from chanlun.core.zs_branch import ZsBranchCalculator, DivergenceResult, classify_rel
from chanlun.core.zslx_branch import ZslxBranchCalculator

@dataclass
class LevelResult:
    level: int                                       # 0 = L0
    zss: List[ZS]                                    # 本级已完成中枢
    done_divergence: List[Optional[DivergenceResult]] # 与 zss 索引对齐(本级内联背驰)
    zslxs: List[ZSLX]                                # 本级走势类型
    upgrade_idx: List[int]                           # 升级标注:本级中枢中 9段/扩展候选的索引(P5 用)

class RecursiveBranchCalculator:
    def calculate(self, xds, ld_provider, wzgx_config, frequency=None) -> List[LevelResult]: ...
```

依赖 `zs_branch`(中枢+内联背驰)、`zslx_branch`(走势类型)、`classify_rel`(扩展判定)。**不**依赖 CL。无状态、全量重算。

---

## 3. 主链递归循环（钉死）

```python
_MAX_LEVELS = 50    # 护栏；走势单元逐级收缩，正常远不及

def calculate(self, xds, ld_provider, wzgx_config, frequency=None):
    if not xds:
        return []
    results: List[LevelResult] = []
    units: List[LINE] = list(xds)
    level = 0
    while level < _MAX_LEVELS:
        # L0 构成段=线段→最小中枢 4 段(项目口径)；L≥1 构成段=走势类型→原文「3 个
        # 次级别走势类型重叠成中枢」用 3。
        min_lines = 4 if level == 0 else 3
        res = ZsBranchCalculator(
            ld_provider=ld_provider, frequency=frequency,
            wzgx=wzgx_config, min_zs_lines=min_lines,
        ).calculate(units)
        if not res.done_zss:
            # 右边缘只剩 pending 高级中枢(未被离开段确认完成)：记录其 H2(leave 读法)
            # 中枢 + live 背驰再终止，让层级树展示到右边缘「正在形成」的高级中枢
            # (不上卷:未完成无法切走势类型)。MVP「各级只用 done」在此放宽一档
            # (用户验收决策 2026-05-31，见 §0)。
            pend = [h for h in res.live if h.node1 == "leave"]
            if pend:
                results.append(LevelResult(
                    level, [h.zs for h in pend], [h.divergence for h in pend],
                    [], _mark_upgrades([h.zs for h in pend]),
                ))
            break                                    # 扫不出 done 中枢 → 终止
        zslxs = ZslxBranchCalculator().calculate(res.done_zss, res.done_divergence)
        upgrade_idx = self._mark_upgrades(res.done_zss)
        results.append(LevelResult(
            level, res.done_zss, res.done_divergence, zslxs, upgrade_idx,
        ))
        if len(zslxs) < 3:
            break                                    # 不足 3 个走势类型 → 无法构成上级中枢
        units = _as_units(zslxs)
        level += 1
    return results
```

**动态终止**（MVP）：扫不出中枢 / 走势类型 < 3 / 触 `_MAX_LEVELS`。不硬封顶 L3（宪法 4 层是「级别命名」，结构上由数据决定到几层；硬封顶留后如需要）。

---

## 4. zs_branch 参数化 `min_zs_lines`（唯一上游改动）

`zs_branch.ZsBranchCalculator` 现 `MIN_LINES=4` 是类常量，calculate 在 3 处用（ZsCalculator 的 `min_zs_lines` + `correct_entry` ×2）。改为构造参数：

```python
def __init__(self, ld_provider=None, frequency=None,
             wzgx=Config.ZS_WZGX_ZGD.value, min_zs_lines=4):
    ...
    self.min_zs_lines = min_zs_lines
```
calculate 内 `self.MIN_LINES` → `self.min_zs_lines`（3 处）。`MIN_LINES=4` 类常量保留作默认引用/文档。**默认 4 → L0 与现有 P1/P3 行为完全不变**（现有测试用 `ZsBranchCalculator(...)` 不传 min_zs_lines → 4）。

---

## 5. 背驰力度跨级别（MVP）

所有级别**复用同一 `ld_provider`**（如 `lambda s,e: query_macd_ld(cd,s,e)`，1m K线 + htf MACD）。`ld_provider(FX, FX)` 按分型的 K 线时间算 MACD；L1+ 走势类型的 `start/end` 仍是 L0 的 FX，故同一 provider 可算其跨度力度（区间随级别变大）。**每级换周期 MACD（L1→5m…）精确口径留后。**

---

## 6. 升级标注（旁路，不改走势类型）

```python
@staticmethod
def _mark_upgrades(done_zss: List[ZS]) -> List[int]:
    """本级中枢中「9 段升级 / 中枢扩展候选」的索引（line 16429 解耦：仅标注、
    不改走势类型；实体化与 2/3 类买点留 P5）。"""
    out = []
    for i, z in enumerate(done_zss):
        if len(z.lines) >= 9:                        # 9 段升级(第33课：9 段=3 次级走势类型)
            out.append(i)
        elif i > 0 and classify_rel(done_zss[i - 1], z) == "expand":  # 中枢扩展(中心定理二本体相交)
            out.append(i)
    return out
```
MVP 只记录索引；扩展的精确机制（单中枢级别升级 vs N 中枢合并，line 20608）和 2/3 类买点在 P5 细化。

---

## 7. `_as_units`（走势类型 → 下一级输入段）

```python
def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """ZSLX 喂回 zs_branch 当输入段。zslx_branch._finalize 已填 zs_high/zs_low
    (中枢包络 max(gg)/min(dd))；此处只重排 index 为连续 0,1,2…(ZsCalculator 靠
    index 定位)。"""
    for i, zslx in enumerate(zslxs):
        zslx.index = i
    return zslxs
```
比旧 `recursive._as_units` 简单——`zs_high/zs_low` P4a 已填，无需重算。

---

## 8. 测试 + 验证

**TDD（`tests/core/test_recursive_branch.py` 新建）**：
- 受控线段序列(沿用 `_seg` 范式 + fake `ld_provider`)造**两级**结构：L0 切出 ≥3 个方向交替走势类型 → 喂回应得 ≥1 个 L1 中枢。断言 `results[0].level==0`、`results[1].level==1`、L1 中枢由 L0 走势类型构成。
- 终止：空输入→`[]`；L0 走势类型 <3 → 只 1 级。
- `min_zs_lines`：L0 用 4、L≥1 用 3（构造 3 走势类型重叠成 L1 中枢）。
- 升级标注：造 1 个 ≥9 段中枢 + 1 对 expand 中枢，断言 `upgrade_idx` 命中。
- **zs_branch 参数化回归**：`ZsBranchCalculator()` 默认仍 min_zs_lines=4，P1/P3 全部测试不破。

**真实数据出图（验收，沿用 P1/P3/P4a）**：
- fixture `a_SH_513100_1m.parquet` → CL → get_bis() → `RecursiveBranchCalculator` → Plotly：分层画各级中枢框 + 走势类型色块（L0/L1/L2…用不同行或透明度），标升级候选，人工审递归层级是否合理（L1 中枢确由 L0 走势类型重叠构成）。

---

## 9. 留后清单

- 中枢扩展精确实体化（单中枢升级 vs 合并，line 20608/第57课）+ 2/3 类买点 → **P5**。
- 每级换周期 MACD（精确背驰力度）→ 后续。
- 背驰跨级别贯通（自底向上 BUILD）→ **P4c**；区间套（自顶向下 READ）→ **P6**。
- 右边缘 live 多假设逐级传播 → 后续。
- 硬封顶 L3（日线）如需要 → 后续。
