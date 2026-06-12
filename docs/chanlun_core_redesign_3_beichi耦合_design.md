# 子项目③（P3）设计：zs_branch 实时内联背驰（H2a 耦合）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md`（宪法 §4 节点① / §5 背驰口径）。
> 背驰内核见 `chanlun_core_redesign_2_beichi_design.md`（`beichi_calculator.py`）。
> 本文是 P3 的设计/验收依据。`*.md` 被项目 gitignore，本地文件。

---

## 0. 范围

**含**：
- 给 `zs_branch` 的**每个中枢完成点**（`done_zss` 各中枢 + 右边缘 `live` 的 H2 读法）实时判**离开段背驰** → 定 **H2a**（背驰）/ **H2b**（无背驰）。
- 常规背驰（MACD 力度，复用 `is_beichi` 原语）。
- 趋势 / 盘整背驰**自动选择**（按相邻中枢是否同向）。
- 趋势门槛在 L0 的落地（近似口径，见 §7）。

**不含（明确留后）**：
- **非常规背驰（小转大）→ P4**（依赖次级别/笔级别中枢，本级 L0 纯线段输入看不到，2026-05-30 brainstorm 拍板）。
- **精确三买卖（回试不破 ZG/ZD）→ P5**（那是买卖点核心，P3 重复做会割裂）。
- **`beichi_pz` 生产对齐 → 随买卖点链路 P5**（P3 内联不走 `beichi_pz`，见 §3；生产 legacy 买卖点已决定 P5 从零重做）。
- 真增量逐段引擎（P1 顶部注释已留后；P3 用"批处理每次重跑"模拟实时）。

---

## 1. 目标与产物

`zs_branch` 产出 = **带背驰标注的中枢序列**，供 P4 递归 / P5 买卖点消费：
- **H2a（背驰）** = 走势类型完成点 = 1类买卖点候选 + 高级别 provisional token 源（token 贯通在 P4）。
- **H2b（无背驰）** = 中枢完成、走势类型延续。

**三位一体**（中枢/走势/背驰相互递归）：中枢完成 ⟺ 判离开段背驰 ⟺ 该处走势类型是否终结，是**同一个当下判断**，故背驰**内联**进中枢完成、不做事后独立 pass。

---

## 2. 接口（构造注入力度）

```python
class ZsBranchCalculator:
    def __init__(
        self,
        ld_provider: Optional[LdProvider] = None,   # FX,FX -> ld 力度字典
        frequency: Optional[str] = None,            # 透传 is_beichi 决定黄白线口径
        wzgx: str = Config.ZS_WZGX_ZGD.value,       # 趋势判定口径(见 §5)
    ): ...
    def calculate(self, lines: List[LINE]) -> ZsBranchResult: ...
```

- `ld_provider` 复用 `beichi_calculator.LdProvider`。
- **退化**：`ld_provider is None` → 不判背驰，所有 `divergence=None`，**P1 行为完全不变**（保护 P1 的 24 个零依赖单测）。
- `wzgx` 默认 `ZS_WZGX_ZGD`（核心区间口径，合原文「≥2 依次同向中枢」）；**P3 独立**，与生产 legacy 的 `GD` 默认无关。

---

## 3. 背驰判定核心（is_beichi 原语直连，不复用 beichi_qs/pz 壳）

**关键观察**：对任一中枢 Z，盘整背驰 `b:a` 与趋势背驰 `c:b` 的**计算完全一致** = `is_beichi(Z.start, 离开段)`：
- 盘整 `a+A+b`：`a`=进入段=`Z.start`，`b`=离开段 → 比 `b:a` = `is_beichi(Z.start, 离开段)`。
- 趋势 `a+A+b+B+c`：在末中枢 Z 上，`b`=连接段=进入末中枢的段=`Z.start`，`c`=离开段 → 比 `c:b` = `is_beichi(Z.start, 离开段)`。

二者**只在语义标签（kind）与门槛上不同**，计算同一。故 P3 **直连 `is_beichi` 原语**，不复用 `beichi_qs`/`beichi_pz`：
- 那两个壳为生产链路「中枢列表 zss + 其后的 now_seg」设计，而 P3 判的是「中枢**自己的**离开段」（离开段在中枢本体之后、但属于该中枢的完成判定），语义不同。
- 且 `beichi_pz` 找「中枢内（除末段外）最近同向段」作比较——**比错了段**（宪法 §5 要比进入段 `z.start`）。P3 直连 `is_beichi(Z.start, c)` 即正确口径。

P3 仅依赖 `beichi_calculator` 的两个**纯函数**：`is_beichi`（背驰原语）、`is_qs`（趋势方向，§5）。

判定伪码（对每个已完成中枢 Z）：
```
a = Z.start;  c = leave_seg(Z)                  # §4 提取
if a is None or c is None or a.type != c.type:  # 开头中枢无进入段 / 不同向
    divergence = None
else:
    kind = "qs" if _is_trend(prev_zs, Z) else "pz"   # §5
    bc   = is_beichi(a, c, ld_provider, frequency)
    divergence = DivergenceResult(bc, kind, a, c, provisional=<§8>)
```

---

## 4. 离开段 / 比较段提取口径（钉死）

- **比较段 a** = `Z.start`（进入段/连接段）。`correct_entry` 已把真进入段提到 `z.start`，口径一致。
- **离开段 c**：
  - **done 中枢**：`correct_exit` 触发时离开段已剥到 `z.end` → `c = z.end`。`z.end is None`（最小 3 段未剥）时 → `c =` 原始 `lines` 序列中该中枢末段之后的第一段（中枢间连接段）。
  - **live H2 读法**：`c = zs.lines[-1]`（最后一段即候选离开段），本体 = `lines[:-1]`。
- **同向前提**：`a.type == c.type` 才判（背驰是同向段间的力度比较）。

> 实现注意（最终设计已消解此顾虑）：曾担心 live H2 趋势判定需按本体 `lines[:-1]` 重算边界（H2 读法本体不含离开段），但实测**无需**——默认 ZGD 档 `is_qs` 比的是 `zd/zg`（前 3 段核心区间，H1/H2 一致、本就不含离开段）；GD 档经 `use_core_envelope=True` 比的是 `lines[:3]`（=本体）。两条路都天然剔除离开段远摆，故 `_branch_copy` 不为 H2 重算 boundaries 也正确，`_is_trend` 直接用 `zs.zd/zg` 即可（final 评审确认）。

---

## 5. 趋势 / 盘整自动选择（is_qs）

`_is_trend(prev_zs, Z)`：序列中 Z 的**前一个中枢** `prev_zs` 与 Z 是否 `is_qs(prev_zs, Z, wzgx, use_core_envelope=True)` 返回非 None 且方向与离开段 `c` 一致。
- 真 → **趋势背驰**（kind="qs"）。
- 假（无前中枢 / 不同向）→ **盘整背驰**（kind="pz"）。

`use_core_envelope=True`：用前 3 段本体包络判趋势（剔离开段远摆，only-3rd-bspoint 口径，宪法 §3.5 / 第33课）。

> `prev_zs`：done 中枢取 `done_zss[i-1]`；live H2 取 `done_zss[-1]`。

---

## 6. H2a/H2b 表达与数据结构（不新增分支）

H1↔H2 是**结构多义**（最后段是核心还是离开段，两可）；H2a↔H2b **不是多义**——给定离开段+MACD，背驰是**确定计算**。故不新增分支，作 H2 的**属性**。

```python
@dataclass
class DivergenceResult:
    is_beichi: bool                 # 是否背驰（H2a=True / H2b=False）
    kind: str                       # "qs"(趋势) | "pz"(盘整)
    compare_seg: LINE               # a/b = Z.start
    leave_seg: LINE                 # c = 离开段
    provisional: bool               # 右边缘未坐实(True) / 已固化(False)
```

载体（**不污染** `ZS`/`LINE` 生产类）：
- **live H2**：`ZsHypothesis.divergence: Optional[DivergenceResult]`（H1 恒 None）。
- **done 各中枢**：`ZsBranchResult.done_divergence: List[Optional[DivergenceResult]]`，与 `done_zss` 索引对齐。

---

## 7. 趋势门槛在 L0 的落地

宪法 §5：趋势背驰门槛 = 离开段 `c` ≥ 次级别（含一个对末中枢的第三类买卖点）。门槛拆两半，**均不改 `kind`**（`kind` 单一由 §5「相邻中枢是否同向」决定，避免双重决定源）：
- **「c ≥ 次级别」**：离开段是一条线段，天然 ≥ 笔级别 ✓（线段由 ≥3 笔构成，本就是一个次级别走势）。**L0 恒满足**，不构成约束。
- **「含一个三买卖」（回试不破 ZG/ZD）**：归入 §8 的 **`provisional`** 语义——done 中枢（`correct_exit` 判离开成功 = 离开段确已冲出区间）= 坐实 `provisional=False`；live H2 未坐实 = `provisional=True`。**精确回试判定留 P5**，P3 只用「是否坐实」近似。

---

## 8. provisional 语义（早标、不早剪）

宪法 §5：右边缘离开段未定型 → 背驰是早标签，离开段完成才坐实。
- **done 中枢**：离开段已定型（坐实）→ `provisional=False`。
- **live H2**：离开段是 pending 最后一段、右边缘可能延伸/翻成 H1 → `provisional=True`。
- 不早剪：H2a（背驰）即便 provisional 也保留在 live 池，不因背驰就坍缩掉 H1。

---

## 9. 测试 + 验证

**TDD（`tests/core/test_zs_branch.py` 扩展）**：
- 注入 fake `ld_provider`（按段返回构造好的 ld 字典），造**趋势背驰**/**盘整背驰**/**无背驰**场景，验 `divergence` 的 `is_beichi`/`kind`/`provisional`。
- 退化：`ld_provider=None` → 全 None；**P1 的 24 个零依赖测试保持绿**（走退化分支）。
- 边界：开头中枢无进入段（a=None）→ None；离开段与进入段不同向 → None。

**真实数据出图（验收，沿用 P1）**：
- fixture `tests/fixtures/klines/a_SH_513100_1m.parquet` → CL → `get_bis()` → `ZsBranchCalculator(ld_provider=<CL 的 query_macd_ld>)` → Plotly HTML。
- 每个中枢标注：H2a/H2b、kind(趋势/盘整)、provisional。**人工审**背驰位置是否落在该落的地方（趋势末端、盘整离开）。

---

## 10. 原文依据索引（宪法）

| 口径 | 宪法位置 |
|------|---------|
| H1/H2 分叉、H2a/H2b、坍缩判据 | §4 line 109–117 |
| 盘整背驰 b:a / 趋势背驰 c:b | §5 line 136–137 |
| 趋势门槛 c≥次级别 | §5 line 145 |
| 右边缘暂定（早标不早剪） | §5 line 149 |
| 三类买卖点 ↔ 节点① 对应 | 表 line 157–159 |

---

## 11. 留后清单

- 非常规背驰（小转大）→ **P4**（次级别中枢在手时）。
- 精确三买卖（回试不破 ZG/ZD）→ **P5**。
- `beichi_pz` 生产对齐（比较段改 `z.start`）→ **P5**（随买卖点链路从零重做）。
- 高级别 provisional token 贯通到顶 → **P4** 递归装配。
