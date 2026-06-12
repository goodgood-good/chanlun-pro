# 子项目⑤d（P5d）设计：转折型背驰（进入段 a 趋势背驰）

> 地基见 `chanlun_core_redesign_0_中枢划分原文理论.md` §5（背驰口径）+ §6（第一类买卖点=趋势背驰）。
> 上游/改动：`zs_branch.py`(P1+P3，`_divergence_for`)。下游受益：`zslx_branch`/`bs_branch`/`recursive_branch`/`bs2_branch`/`bs3_branch`。
> 原文：第一类买点=趋势背驰式下跌构成（转折，3544）。`*.md` 被 gitignore。

---

## 0. 范围

**含（MVP）**：
- 改 `zs_branch._divergence_for` 的**转折型分支**（`a.type != c.type` 异向）：不再 `return None`，改判**进入段 a 的趋势背驰** = `is_beichi(前中枢同向离开段 prev_zs.end, 本中枢进入段 a)`。
- `leave_seg = a`（进入段=背驰段，P5a `_first_class` 自动当一类点、锚 `a.end`=转折点）；`kind="qs"`（转折=趋势背驰）。
- **唯一核心改动**；下游 `done_divergence` 自动含转折型背驰 → bs/zslx/recursive 全链受益。

**口径依据（用户抠定）**：转折型中枢 = 趋势转折点（顶/底），其进入段 a 是转折前趋势的**最后衰竭段**；a 的背驰（相对前一个同向段）= 转折前趋势的趋势背驰 = 第一类买卖点。

**不含（明确留后）**：
- **无前驱同向段的转折**（如数据开头中枢 0，`prev_zs is None`）→ None，留后。
- **转折型的盘整背驰变体**（pz）→ 后续。
- **接 CL** → 后续。
- 不接 CL、不动旧 `beichi_calculator`。

---

## 1. 目标与产物

转折型中枢（异向）产趋势背驰 `DivergenceResult`（`leave_seg=进入段 a`），使其能被 P5a `_first_class` 识别为一类买卖点（锚 `a.end`=转折点）。覆盖实测 14 中枢中 6 个转折型（此前全 None）。

**原文依据**：3544 第一类买点=背驰式下跌（转折）；§6 第一类买卖点=趋势背驰构成。

---

## 2. 改动接口（唯一）

`zs_branch._divergence_for`（现 line 292-293：`if a.type != c.type: return None`）改为转折型分支。签名/返回类型不变（`Optional[DivergenceResult]`）。`DivergenceResult` 结构不变（`leave_seg` 在转折型存进入段 a——语义=「背驰判定的那一段」，与中继型存离开段 c 统一为「背驰段」）。

---

## 3. 转折型背驰算法（钉死）

```python
# _divergence_for 内,替换原 "if a.type != c.type: return None"
if a.type != c.type:                                  # 转折型(进入/离开异向=趋势转折点)
    b = prev_zs.end if prev_zs is not None else None   # 前中枢同向离开段(趋势倒数第二段)
    if (b is None or b is a or b.type != a.type
            or b.start is None or b.end is None):
        return None                                    # 无前驱/自比/异向/缺端点 → 不判
    bc = is_beichi(b, a, self.ld_provider, self.frequency)
    return DivergenceResult(
        is_beichi=bc, kind="qs",                       # 转折=趋势背驰
        compare_seg=b, leave_seg=a, provisional=live,  # 背驰段=进入段 a
    )
# 同向(中继型)走原有逻辑:is_beichi(a, c)
```

- **比较对象** `b = prev_zs.end`（前一中枢同向离开段）；**背驰段** `a`（本中枢进入段=转折前趋势最后段）。
- **守卫**：`prev_zs None`（无前驱）/ `b is a`（防自比，prev 离开段恰是本进入段时）/ `b.type != a.type`（前离开段须与进入段同向）/ `b` 缺端点 → None。
- `kind="qs"`（转折=趋势背驰，第一类买卖点）；`leave_seg=a` 使 P5a 锚 `a.end`（a 向下→1buy 锚底/a 向上→1sell 锚顶）。

---

## 4. 全链下游影响（本卷非孤立，需重新验收）

| 下游 | 影响 |
|---|---|
| `done_divergence` | 转折型中枢现可能有 `is_beichi` 背驰（此前全 None）|
| `bs_branch._first_class`（P5a）| 转折型产一类点：`leave_seg=a`、`a.type` 判方向、锚 `a.end`=转折点。6 个转折型中有前驱同向的会出一类点 |
| `zslx_branch.calculate`（P4a）| 转折型背驰处会**切走势类型**（趋势完成）→ 走势类型划分变化 |
| `recursive_branch` | `done_divergence` 变 → 间接（各级背驰、嵌套森林、买卖点）|
| 现有受控测试 | 受控数据多为同向（转折型不触发）→ 预期不破；**真实数据输出变，需重新验收 zslx/bs** |

---

## 5. 潜在风险（验收关注）

1. **`prev.end` 与 `a` 同段致自比**：连续走势中前中枢离开段可能恰是本中枢进入段 → `is_beichi(同段,同段)` 无意义。**守卫 `b is a → None`** 已加；验收 probe 确认未误判。
2. **一类点重复**：转折型一类点（a 衰竭）与前一个中继背驰中枢的一类点可能都落在底/顶附近 → 验收看是否合理 / 是否需去重（MVP 不去重，两点都标，交策略层）。
3. **`leave_seg=a` 语义**：转折型 `leave_seg` 是进入段（中继型是离开段）。下游 `beichi_nest`（P4c）用 `leave_seg` 的 K 线时间区间做嵌套——a 有效时间区间，不破；但语义上「leave_seg=背驰段」需文档明确（已在 §2 注）。

---

## 6. 测试 + 验证

**TDD（扩 `tests/core/test_zs_branch.py` 或新 `test_zs_branch_turn.py`）**——受控（`_seg`/`_make_zs` + fake `ld_provider`）：
- **转折型+prev同向+背驰**：中枢 a/c 异向、prev_zs 离开段同向 a、ld 注入背驰力度 → `is_beichi=True`、`leave_seg is a`、`kind=="qs"`、`compare_seg is prev_zs.end`。
- **转折型+prev同向+无背驰**：ld 注入足力度 → `is_beichi=False`。
- **转折型+无prev** → None。
- **转折型+prev异向**（prev.end 与 a 异向）→ None。
- **转折型+prev.end is a**（自比）→ None。
- **中继型不变**：a/c 同向 → 走原 `is_beichi(a,c)`（回归）。

**下游回归**：`test_zslx_branch`/`test_bs_branch`/`test_recursive_branch`/`test_bs2_branch`/`test_bs3_branch` 全绿（受控同向数据不触发转折型）。

**真实数据出图（验收，沿用 P1/P3/P4a/P5a）**：
fixture → `zs_branch` → `bs_branch` → Plotly：6 个转折型中枢中有前驱同向的产一类点（标转折型 `1buy`/`1sell`，锚转折点底/顶），人工审：①转折型一类点是否落在合理转折点；②走势类型划分是否更合理（转折处切）；③与前中继背驰一类点有无不合理重复。

---

## 7. 留后清单

- **无前驱同向段的转折**（数据开头中枢 0）→ 后续（需更前上下文）。
- **转折型盘整背驰变体**（pz）→ 后续。
- **一类点去重策略**（转折型 vs 前中继背驰）→ 策略层。
- **接 CL 生产链路** → 后续。
- 至此背驰覆盖：中继型（同向，P3）+ 转折型（异向，P5d）；剩无前驱转折 + pz 变体 + 非常规（小转大）。
