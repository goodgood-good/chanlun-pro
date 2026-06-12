# 子项目④a（P4a）设计：走势类型划分（基于 zs_branch 内联背驰）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md`（§2 走势类型 / §6 坍缩 / §7 区间套）。
> 上游 `zs_branch.py`(P1 中枢 + P3 内联背驰)。蓝本 = 旧 `zslx_calculator.py`(基于 ZsCalculator，**不动它**)。
> `*.md` 被项目 gitignore，本地文件。

---

## 0. 范围

**P4 整体拆分**：P4a 走势类型划分 → P4b 递归装配 → P4c 背驰贯通（本文只做 P4a）。

**P4a 含**：
- 把 zs_branch 的 **L0 已完成中枢序列**（`done_zss` + `done_divergence`）切成走势类型 `List[ZSLX]`。
- 双信号边界：**背驰**（复用 `done_divergence`，不重判）+ **方向断裂**（`classify_rel` 本体包络口径）。
- 盘整(1中枢)/趋势(≥2依次同向中枢) 分类 + 方向。
- 输出 `ZSLX` 填好字段（含 `zs_high/zs_low`），为 P4b 喂回 zs_branch 备用。

**P4a 不含（明确留后）**：
- **右边缘 live 多假设走势类型 → 后续**（2026-05-30 brainstorm 拍板）：P4a 只用 `done_zss`，末个走势类型 `done=False`；zs_branch 的 `live`(H1/H2)分支 + provisional 背驰对走势类型的影响不处理。
- **递归装配（喂回 zs_branch 得 L1）→ P4b**；**背驰嵌套贯通 → P4c**；**中枢升级(expand 实体化)→ P4b/后续**。
- **不接 CL、不动旧 `zslx_calculator`/`recursive_calculator`**（并存重做，零回归）。

---

## 1. 目标与产物

`ZslxBranchCalculator.calculate(done_zss, done_divergence)` → `List[ZSLX]`。走势类型 = L1 的构成段（`ZSLX` 是 `LINE` 子类，带 `zs_high/zs_low`，可喂回 `zs_branch` 当输入段 → P4b 递归）。

**三位一体延续**：走势类型的**终结由背驰宣告**（§宪法）——而背驰已由 P3 在中枢完成点内联算好（`done_divergence`），P4a 直接消费、不再像旧 `zslx._wt_beichi` 自跑 `beichi_qs/pz`。

---

## 2. 模块与接口

新建 `src/chanlun/core/zslx_branch.py`（孤立、零依赖 CL）：

```python
from chanlun.core.cl_interface import LINE, ZS, ZSLX
from chanlun.core.zs_branch import classify_rel, DivergenceResult

class ZslxBranchCalculator:
    def calculate(
        self,
        done_zss: List[ZS],
        done_divergence: List[Optional[DivergenceResult]],   # 与 done_zss 索引对齐
    ) -> List[ZSLX]: ...
```

- 仅依赖 `zs_branch.classify_rel`（本体包络相邻中枢关系）+ `cl_interface` 数据类。**不**依赖 `beichi_calculator`（背驰已在 `done_divergence` 里）。
- 无状态，每次全量重算（同旧 `ZslxCalculator`）。

---

## 3. 状态机（双信号，钉死）

蓝本 = 旧 `ZslxCalculator.calculate`（§2 读过），两处关键替换：① 方向信号 `is_qs`→`classify_rel`；② 背驰信号 `_wt_beichi`(自判)→`done_divergence[i]`(查表)。

```python
def calculate(self, done_zss, done_divergence):
    if not done_zss:
        return []
    wts: List[ZSLX] = []
    cur: Optional[List[ZS]] = [done_zss[0]]
    cur_start = 0                      # cur 第一个中枢在 done_zss 的索引
    cur_dir: Optional[str] = None      # "trend_up" | "trend_down"（趋势方向）| None（单中枢未定）

    for i in range(1, len(done_zss)):
        zi = done_zss[i]
        if cur is None:                # 上一走势类型被背驰终结，zi 另起
            cur, cur_start, cur_dir = [zi], i, None
            continue

        rel = classify_rel(cur[-1], zi)            # "trend_up" | "trend_down" | "expand"
        # 只有【方向反转】(上涨↔下跌)才切；expand(中枢扩张/本体相交)不切——按走势级别
        # 延续定理一(第20课)延续，升级(L1 中枢)实体化留 P4b；同向(rel==cur_dir)亦延续。
        reverse = (cur_dir is not None
                   and rel in ("trend_up", "trend_down") and rel != cur_dir)

        if reverse:
            wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
            cur, cur_start, cur_dir = [zi], i, None
        else:
            cur.append(zi)
            if cur_dir is None and rel in ("trend_up", "trend_down"):
                cur_dir = rel                      # 趋势方向坐实(只认 trend_*，expand 不写入)

        # 背驰边界（仅非方向反转时；zi 刚并入 cur，其背驰 = done_divergence[i]）
        if not reverse and cur is not None:
            dv = done_divergence[i]
            if dv is not None and dv.is_beichi:
                wts.append(self._finalize(cur, cur_start, cur_dir, done=True))
                cur, cur_dir = None, None          # 被背驰终结，下一个另起

    if cur is not None:
        wts.append(self._finalize(cur, cur_start, cur_dir, done=False))
    return wts
```

**已知边界缺口（MVP 接受，留后）**：`boundary` 另起时 `zi` 成新 `cur[-1]` 但**当轮不查它自己的背驰**（同旧 `zslx`）；单中枢在另起当轮的盘整背驰可能漏标，待 P4c/后续精化。

---

## 4. 分类与方向（`_finalize`）

```python
def _finalize(self, zss, start_idx, cur_dir, done) -> ZSLX:
    if cur_dir in ("trend_up", "trend_down"):
        direction = "up" if cur_dir == "trend_up" else "down"
        zslx_type = "上涨" if direction == "up" else "下跌"
    else:
        # 盘整：单中枢，或仅由 expand(中枢扩张)连接、无趋势方向的多中枢(升级留 P4b)。
        # 方向 = 整段核心净位移(末中枢末核心段终点 vs 首中枢首核心段起点)。
        zslx_type = "盘整"
        direction = "up" if zss[-1].lines[-1].end.val >= zss[0].lines[0].start.val else "down"
    # 走势类型边界 = 第一中枢进入段起点 → 末中枢离开段终点（含 a/b，宪法 a+A+b）
    first = zss[0].start if zss[0].start is not None else zss[0].lines[0]
    last = zss[-1].end if zss[-1].end is not None else zss[-1].lines[-1]
    zslx = ZSLX(
        zslx_level=getattr(zss[0], "level", None),   # ZsCalculator 中枢 level；缺则 None(P4b 管理级别)
        start=first.start, end=last.end,
        start_line=first, end_line=last,
        _type=direction, index=start_idx, done=done,
    )
    zslx.zss = zss
    zslx.zslx_type = zslx_type
    # 喂回 zs_branch 备用(P4b)：ZsCalculator 靠构成段 zs_high/zs_low 判重叠
    zslx.zs_high = max(zs.gg for zs in zss)
    zslx.zs_low = min(zs.dd for zs in zss)
    return zslx
```

- `zs_high/zs_low` 取**所含中枢的包络** `[min(dd), max(gg)]`（同旧 `recursive._as_units` 口径，spec 决策 3）。
- `index` 用 `start_idx`（在 done_zss 中的起始位置；P4b 重排时再规整，同旧 `_finalize` 的 index=0 占位思路，但此处保留来源索引便于审图定位）。

---

## 5. 口径决策记录（brainstorm 2026-05-30 + 推荐）

| 决策 | 取定 | 依据 |
|------|------|------|
| 边界判据 | **只【方向反转】(上涨↔下跌)切** + 背驰切；同向/expand 延续 | classify_rel 本体包络；见下「expand 原文论证」|
| 背驰边界信号源 | **`done_divergence[i].is_beichi`**(直接消费 P3 内联背驰) | 三位一体、不重判；用户认可 |
| 背驰 kind 用途 | 终结**只看 `is_beichi`**，`kind`(qs/pz) 仅作标注 | done_divergence.kind 是中枢级(全局前中枢)算的，与走势类型 cur 内部趋势/盘整未必一致；MVP 不强对齐 |
| **expand(中枢扩张)** | **不切(延续)**，升级(L1 中枢)实体化留 P4b | 走势级别延续定理一(第20课)；见下论证 |
| 仅扩张连接的多中枢 | 无趋势方向(cur_dir=None) → **盘整**，方向取整段净位移 | expand 不是趋势(中枢重叠违反趋势定义)，归盘整、升级留 P4b |
| 右边缘 | **MVP 基于 done_zss**(末个 done=False)，live 多假设留后 | 聚焦增量；同旧 zslx |

**expand 不切的原文论证（第20课，2026-05-30 真实数据验收订正）**：

- **中心定理二**：中枢「核心区间分离、本体相交」(`后ZD>前ZG 且后DD≤前GG`) = **形成高级别走势中枢**（扩张），明确**不是趋势**。
- **走势级别延续定理一**：「在更大级别走势中枢产生前，该级别走势类型将延续」——故 expand 时 L0 走势类型**延续、不切**。
- **递归约束**：L1 中枢 = **3 个方向交替的 L0 走势类型**（中枢三段 A、C 同向、B 反向，line 10012-20）重叠 → 要求 L0 走势类型序列**方向交替**；若把 expand 当边界切出「上涨接上涨」，P4b 永远拼不出 L1 中枢。
- **分层**：扩张产生的 L1 中枢（含 3 个子走势类型，原文「没有至少 3 个连续次级别走势类型重叠是扩张不了高级别中枢的」）是 **P4b** 的事；P4a 不在 L0 中枢层面"并入"或实体化升级，只产**方向交替**的 L0 走势类型。
- **教训**：brainstorm 初版「expand 当边界」是口径错误，被真实数据验收（zs3/zs4 expand 切出"上涨接上涨"）暴露、回原文（第20课）订正。

---

## 6. 测试 + 验证

**TDD（`tests/core/test_zslx_branch.py` 新建）**：
- 受控中枢序列单测（复用/参照 `test_zs_branch.py` 的 `_seg`/`_make_zs` 范式造 `done_zss` + 构造 `done_divergence`）：
  - 单中枢 → 盘整；≥2 依次同向 → 趋势(上涨/下跌)。
  - 方向断裂(trend_up→trend_down / trend→expand) → 切两个走势类型。
  - 背驰边界(`done_divergence[i].is_beichi=True`) → 终结、另起。
  - 末个 `done=False`。
  - `zs_high/zs_low` = 中枢包络。
- 边界：空 `done_zss` → `[]`；单中枢序列 → 1 个盘整(done=False)。

**真实数据出图（验收，沿用 P1/P3）**：
- fixture `a_SH_513100_1m.parquet` → CL → get_bis() → `ZsBranchCalculator` → `ZslxBranchCalculator` → Plotly HTML：在 P3 图上叠加**走势类型分段**（不同色块/分隔线 + 盘整/上涨/下跌 + done 标记），人工审走势类型边界是否落在背驰/方向断裂处。

---

## 7. 留后清单

- 右边缘 live 多假设走势类型 + provisional 终结 → 后续。
- P4b 递归装配（ZSLX 喂回 zs_branch 得 L1 中枢 → 多级）。
- P4c 背驰嵌套贯通（§7.2 BUILD）。
- 中枢升级(expand 实体化)、单中枢另起当轮盘整背驰漏标精化。
- 走势类型级背驰 kind 与中枢级 kind 的精确对齐（若 P4b/P5 需要）。
