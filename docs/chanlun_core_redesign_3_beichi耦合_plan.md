# P3 zs_branch 实时内联背驰（H2a 耦合）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `zs_branch` 每个中枢完成点（`done_zss` 各中枢 + 右边缘 live H2）实时内联判离开段背驰，标 H2a（背驰）/H2b（无背驰），产出带背驰标注的中枢序列。

**Architecture:** 保持 P1 批处理委托结构不变（`calculate` 仍委托 `ZsCalculator` 找中枢）；新增**构造注入** `ld_provider`，在 `calculate` 算出中枢后**对每个中枢**用 `is_beichi(z.start, 离开段)` 原语直连判背驰（不复用 `beichi_qs`/`beichi_pz` 壳）。背驰结果作 H2 的**属性**（不新增分支）：live H2 挂 `ZsHypothesis.divergence`(provisional=True)，done 各中枢挂 `ZsBranchResult.done_divergence`(provisional=False)。`ld_provider=None` 时全退化为 None，**P1 行为不变**。

**Tech Stack:** Python 3 / dataclasses / pytest / poetry；依赖 `beichi_calculator.is_beichi`+`is_qs`（纯函数）、`cl_interface.Config`。

设计依据：`docs/chanlun_core_redesign_3_beichi耦合_design.md`。

---

## File Structure

- **Modify** `src/chanlun/core/zs_branch.py`：加 `DivergenceResult` dataclass、`ZsHypothesis.divergence` 字段、`ZsBranchResult.done_divergence` 字段、`ZsBranchCalculator.__init__` 构造注入、`_leave_seg`/`_is_trend`/`_divergence_for` helper、`calculate` 接线。唯一被修改的生产侧文件（仍**不接 CL 主链路**，零回归）。
- **Modify** `tests/core/test_zs_branch.py`：在文件末尾追加 P3 背驰测试段（fake ld_provider helper + 集成/边界测试）。
- **本地验收脚本**（不入库）：`scripts_local/probe_p3_review.py`（新建，临时 probe），产出 `zs_branch_review.html` 供人工审。

不改：`beichi_calculator.py`（只复用其纯函数）、`cl.py`（不接主链路）、任何生产配置。

---

## Task 1: DivergenceResult 数据结构 + H2/Result 字段（退化默认 None）

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`（imports + `ZsHypothesis`/`ZsBranchResult` 附近，约 :16、:142-158）
- Test: `tests/core/test_zs_branch.py`（文件末尾追加）

- [ ] **Step 1: 写失败测试**（追加到 `tests/core/test_zs_branch.py` 末尾）

```python
# ===========================================================================
# P3: 实时内联背驰（H2a 耦合）
# ===========================================================================
def test_divergence_result_dataclass():
    from chanlun.core.cl_interface import ZS
    seg = _seg(0, "up", 5, 8)
    dv = zs_branch.DivergenceResult(
        is_beichi=True, kind="pz", compare_seg=seg, leave_seg=seg, provisional=True
    )
    assert dv.is_beichi is True and dv.kind == "pz" and dv.provisional is True


def test_hypothesis_divergence_defaults_none():
    from chanlun.core.cl_interface import ZS
    zs = ZS(zs_type="xd", start=None)
    h = zs_branch.ZsHypothesis(zs=zs, node1="leave")
    assert h.divergence is None                      # 默认 None（退化）


def test_result_done_divergence_defaults_empty():
    res = zs_branch.ZsBranchResult(done_zss=[], live=[], freeze_idx=0)
    assert res.done_divergence == []                 # 默认空列表（退化）
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py::test_divergence_result_dataclass -q`
Expected: FAIL —— `AttributeError: module 'chanlun.core.zs_branch' has no attribute 'DivergenceResult'`

- [ ] **Step 3: 实现**

在 `zs_branch.py` 顶部 import 段，把 `from dataclasses import dataclass` 改为：

```python
from dataclasses import dataclass, field
```

在 `ZsHypothesis` 定义（现 `:142-149`）末尾加 `divergence` 字段，并在其**前面**新增 `DivergenceResult`：

```python
@dataclass
class DivergenceResult:
    """一个中枢离开段的背驰判定（H2a=背驰 / H2b=无背驰）。"""

    is_beichi: bool                   # 是否背驰
    kind: str                         # "qs"(趋势背驰) | "pz"(盘整背驰)
    compare_seg: LINE                 # 比较段 a/b = 中枢进入段 z.start
    leave_seg: LINE                   # 离开段 c
    provisional: bool                 # 右边缘未坐实(True) / 已固化(False)


@dataclass
class ZsHypothesis:
    """右边缘的一个中枢读法（一个 live 分支）。"""

    zs: ZS                            # 该读法下的中枢对象
    node1: str                        # 节点①: "core"(H1) | "leave"(H2)
    rel_prev: Optional[str] = None    # 节点③: "trend_up"|"trend_down"|"expand"|None
    upgrade: bool = False             # 节点②: True=已达 9 段触发升级
    divergence: Optional[DivergenceResult] = None   # 节点① H2a: 离开段背驰(H1 恒 None)
```

在 `ZsBranchResult` 定义（现 `:152-158`）末尾加 `done_divergence` 字段：

```python
@dataclass
class ZsBranchResult:
    """单级别一次 calculate 的产出。"""

    done_zss: List[ZS]                # 左侧已冻结的已完成中枢
    live: List[ZsHypothesis]          # 右边缘活分支（通常 1~2 个）
    freeze_idx: int                   # 冻结边界
    done_divergence: List[Optional[DivergenceResult]] = field(default_factory=list)  # 与 done_zss 索引对齐
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q -k "divergence or done_divergence or dataclass"`
Expected: PASS（含原 `test_dataclasses_construct` 仍过 —— 它不传 `done_divergence`，走 default_factory）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): DivergenceResult 数据结构 + H2/Result 背驰字段(P3·退化默认None)"
```

---

## Task 2: ZsBranchCalculator 构造注入 ld_provider/frequency/wzgx（退化保 P1）

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`（imports + `ZsBranchCalculator` 类，约 :161-200）
- Test: `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**

```python
def test_calculator_construct_with_ld_provider():
    calc = zs_branch.ZsBranchCalculator(
        ld_provider=lambda s, e: {"hist": {"up_sum": 0, "down_sum": 0}, "dif": {"max": 0, "min": 0}},
        frequency="1m",
    )
    assert calc.ld_provider is not None
    assert calc.frequency == "1m"
    from chanlun.core.cl_interface import Config
    assert calc.wzgx == Config.ZS_WZGX_ZGD.value          # 默认 ZGD


def test_no_ld_provider_yields_none_divergence():
    """退化：无 ld_provider → done_divergence 全 None、live 各 divergence 全 None。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8),
        _seg(3, "down", 8, 5), _seg(4, "up", 5, 12),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)   # 无 ld_provider
    assert all(d is None for d in res.done_divergence)
    assert all(h.divergence is None for h in res.live)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py::test_calculator_construct_with_ld_provider -q`
Expected: FAIL —— `TypeError: ZsBranchCalculator() takes no arguments`（现 `__init__` 未定义）

- [ ] **Step 3: 实现**

在 `zs_branch.py` import 段补 `Config` 与背驰纯函数：

```python
from chanlun.core.cl_interface import LINE, ZS, Config
from chanlun.core.zs_calculator import ZsCalculator
from chanlun.core.beichi_calculator import is_beichi, is_qs, LdProvider
```

在 `ZsBranchCalculator` 类体（现 `MIN_LINES` 常量上方）加 `__init__`：

```python
    def __init__(
        self,
        ld_provider: Optional[LdProvider] = None,
        frequency: Optional[str] = None,
        wzgx: str = Config.ZS_WZGX_ZGD.value,
    ):
        """``ld_provider`` 缺省时不判背驰（退化纯结构，保 P1 行为）。

        ``wzgx`` 默认 ZGD（核心区间口径，合原文「≥2 依次同向中枢」）；P3 独立，
        与生产 legacy 的 GD 默认无关。
        """
        self.ld_provider = ld_provider
        self.frequency = frequency
        self.wzgx = wzgx
```

在 `calculate` 现有 `return` 三处都补 `done_divergence`（本 Task 先全置 None 占位，Task 4 填真值）。把 `calculate` 三个 return 改为：

```python
        if not lines:
            return ZsBranchResult(done_zss=[], live=[], freeze_idx=0, done_divergence=[])
        # ...（中段不变）...
        done_div: List[Optional[DivergenceResult]] = [None] * len(done)   # Task 4 填真值
        if pending is None:
            return ZsBranchResult(
                done_zss=done, live=[], freeze_idx=len(lines), done_divergence=done_div
            )
        prev = done[-1] if done else None
        return ZsBranchResult(
            done_zss=done,
            live=self._fork_pending(pending, prev),
            freeze_idx=self._line_index(pending.lines[0], lines),
            done_divergence=done_div,
        )
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q`
Expected: PASS（P1 既有 24 测试 + 本 Task 新测试全绿；P1 测试用 `ZsBranchCalculator()` 无参，`ld_provider` 默认 None）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): 构造注入 ld_provider/frequency/wzgx + done_divergence 占位(P3·退化保P1)"
```

---

## Task 3: 背驰判定 helper（_leave_seg / _is_trend / _divergence_for）+ 边界单测

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`（`ZsBranchCalculator` 内新增方法）
- Test: `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**（直接单测 `_divergence_for`，避开集成不确定性，确定性复现边界）

```python
def _ld(up_sum, down_sum, dif_max, dif_min):
    return {"hist": {"up_sum": up_sum, "down_sum": down_sum},
            "dif": {"max": dif_max, "min": dif_min}}


def test_divergence_none_when_entry_leave_opposite():
    """进入段 down、离开段 up（异向）→ 不可比力度 → None。"""
    entry = _seg(0, "down", 10, 8)
    body = [_seg(1, "up", 5, 8), _seg(2, "down", 8, 5), _seg(3, "up", 5, 8)]
    zs = _make_zs(entry, body, 5, 8)
    zs.end = _seg(4, "up", 5, 12)                     # 离开段 up，异向 entry
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1))
    assert calc._divergence_for(zs, None, live=False) is None


def test_divergence_none_when_no_entry_segment():
    """开头中枢无进入段（z.start=None）→ None。"""
    body = [_seg(1, "up", 5, 8), _seg(2, "down", 8, 5), _seg(3, "up", 5, 8)]
    zs = _make_zs(None, body, 5, 8)
    zs.end = _seg(4, "up", 5, 12)
    calc = zs_branch.ZsBranchCalculator(ld_provider=lambda s, e: _ld(1, 1, 1, 1))
    assert calc._divergence_for(zs, None, live=False) is None


def test_divergence_none_when_no_ld_provider_on_helper():
    body = [_seg(1, "up", 5, 8), _seg(2, "down", 8, 5), _seg(3, "up", 5, 8)]
    zs = _make_zs(_seg(0, "up", 5, 8), body, 5, 8)
    zs.end = _seg(4, "up", 5, 12)
    calc = zs_branch.ZsBranchCalculator()            # 无 ld_provider
    assert calc._divergence_for(zs, None, live=False) is None
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py::test_divergence_none_when_entry_leave_opposite -q`
Expected: FAIL —— `AttributeError: 'ZsBranchCalculator' object has no attribute '_divergence_for'`

- [ ] **Step 3: 实现**（在 `ZsBranchCalculator` 内，`_fork_pending` 上方加三个方法）

```python
    @staticmethod
    def _leave_seg(zs: ZS, live: bool) -> Optional[LINE]:
        """中枢离开段 c：live H2 取末段 lines[-1]；done 中枢取剥出的 z.end
        （correct_exit 已把定向冲出的离开段剥到 z.end），z.end 缺失时退化用末段。"""
        if live:
            return zs.lines[-1] if zs.lines else None
        if zs.end is not None:
            return zs.end
        return zs.lines[-1] if zs.lines else None

    def _is_trend(self, prev_zs: Optional[ZS], zs: ZS, leave: LINE) -> bool:
        """Z 与前一中枢是否依次同向构成趋势，且趋势方向 == 离开段方向。

        use_core_envelope=True：趋势比较用前 3 段本体（剔离开段远摆，宪法 §3.5）。
        无前中枢 → 非趋势（按盘整背驰处理）。
        """
        if prev_zs is None:
            return False
        d = is_qs(prev_zs, zs, self.wzgx, use_core_envelope=True)
        return d is not None and d == leave.type

    def _divergence_for(
        self, zs: ZS, prev_zs: Optional[ZS], live: bool
    ) -> Optional[DivergenceResult]:
        """对中枢 Z 判离开段背驰（is_beichi 原语直连）。

        a = 进入段 z.start（趋势时即连接段 b）；c = 离开段。盘整 b:a 与趋势 c:b
        在每个中枢上计算同一 = is_beichi(z.start, 离开段)，仅 kind 标签不同（宪法 §3）。
        无 ld_provider / 无进入段 / 进入段与离开段异向 → None。
        """
        if self.ld_provider is None:
            return None
        a = zs.start
        c = self._leave_seg(zs, live)
        if a is None or a.start is None or a.end is None or c is None:
            return None
        if a.type != c.type:                          # 异向不可比力度
            return None
        kind = "qs" if self._is_trend(prev_zs, zs, c) else "pz"
        bc = is_beichi(a, c, self.ld_provider, self.frequency)
        return DivergenceResult(
            is_beichi=bc, kind=kind, compare_seg=a, leave_seg=c, provisional=live
        )
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q -k "divergence_none"`
Expected: PASS（3 个边界单测全绿）

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): _divergence_for 背驰判定helper(is_beichi直连)+边界(异向/无进入段/无provider)"
```

---

## Task 4: calculate 接线 done + live H2 背驰（集成：盘整 live / 盘整 done / 趋势 qs）

**Files:**
- Modify: `src/chanlun/core/zs_branch.py`（`calculate` 接线）
- Test: `tests/core/test_zs_branch.py`

- [ ] **Step 1: 写失败测试**（fake ld：所有段强力度 area=200，指定离开段弱 area=40 触发衰竭）

```python
def _table_all(lines, weak_pairs):
    """所有段 ld 强(area=200,dif 含回抽0轴)；weak_pairs 列出的(start_val,end_val)段弱(area=40)→ 柱子衰竭。"""
    t = {(ln.start.val, ln.end.val): _ld(200, 200, 20, -20) for ln in lines}
    for p in weak_pairs:
        t[p] = _ld(40, 40, 4, -4)
    return t


def _provider(table):
    return lambda start_fx, end_fx: table[(start_fx.val, end_fx.val)]


def test_live_h2_pz_divergence():
    """单中枢右边缘：进入段 up(强) → 候选离开段 up(弱) → live H2 盘整背驰、provisional。"""
    lines = [
        _seg(0, "up", 5, 8),
        _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 12),                         # 候选离开段：同向 up、创新高(12>8)、弱
    ]
    table = _table_all(lines, weak_pairs=[(5, 12)])
    res = zs_branch.ZsBranchCalculator(ld_provider=_provider(table)).calculate(lines)
    h2 = next(h for h in res.live if h.node1 == "leave")
    assert h2.divergence is not None
    assert h2.divergence.is_beichi is True
    assert h2.divergence.kind == "pz"                 # 无前中枢 → 盘整
    assert h2.divergence.provisional is True          # 右边缘未坐实
    assert h2.divergence.leave_seg is lines[4]
    assert h2.divergence.compare_seg is lines[0]
    h1 = next(h for h in res.live if h.node1 == "core")
    assert h1.divergence is None                      # H1 不挂背驰


def test_done_zhongshu_pz_divergence_not_provisional():
    """中枢完成（后随新结构）：done 中枢盘整背驰、provisional=False，离开段=z.end。"""
    lines = [
        _seg(0, "up", 5, 8),
        _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 12),                         # 中枢1 离开段（剥到 z.end）
        _seg(5, "down", 12, 11), _seg(6, "up", 11, 14),
        _seg(7, "down", 14, 11), _seg(8, "up", 11, 14),   # 中枢2 → 中枢1 done
    ]
    table = _table_all(lines, weak_pairs=[(5, 12)])
    res = zs_branch.ZsBranchCalculator(ld_provider=_provider(table)).calculate(lines)
    assert len(res.done_zss) == 1
    assert len(res.done_divergence) == 1
    dv = res.done_divergence[0]
    assert dv is not None
    assert dv.is_beichi is True and dv.kind == "pz"
    assert dv.provisional is False                    # 已坐实
    assert dv.leave_seg is lines[4]                   # z.end = 离开段
    assert dv.compare_seg is lines[0]                 # z.start = 进入段


def test_done_zhongshu_qs_divergence():
    """两个同向中枢趋势：对中枢2 判趋势背驰 kind='qs'，c=中枢2离开段、b=中枢2进入段。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 19),                         # 中枢1离开 = 中枢2进入（连接段 b）
        _seg(5, "down", 19, 16), _seg(6, "up", 16, 19), _seg(7, "down", 19, 16), _seg(8, "up", 16, 19),
        _seg(9, "up", 16, 30),                        # 中枢2离开段 c（弱）
        _seg(10, "down", 30, 27), _seg(11, "up", 27, 30), _seg(12, "down", 30, 27),  # 中枢3雏形(不成立)
    ]
    table = _table_all(lines, weak_pairs=[(16, 30)])  # 仅中枢2离开段弱
    res = zs_branch.ZsBranchCalculator(ld_provider=_provider(table)).calculate(lines)
    assert len(res.done_zss) == 2
    dv2 = res.done_divergence[1]
    assert dv2 is not None
    assert dv2.kind == "qs"                           # 中枢1在前、同向 up → 趋势
    assert dv2.is_beichi is True
    assert dv2.compare_seg is lines[4]                # b = 中枢2进入段
    assert dv2.leave_seg is lines[9]                  # c = 中枢2离开段
    assert dv2.provisional is False
```

- [ ] **Step 2: 运行验证失败**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py::test_live_h2_pz_divergence -q`
Expected: FAIL —— `h2.divergence is None`（`calculate` 尚未接线，仍全 None 占位）

- [ ] **Step 3: 实现**（`calculate` 把 Task 2 的 `[None]*len(done)` 占位换成真判，并对 live H2 填 divergence）

把 Task 2 写的 `done_div = [None] * len(done)` 一行换成：

```python
        done_div: List[Optional[DivergenceResult]] = [
            self._divergence_for(z, done[i - 1] if i > 0 else None, live=False)
            for i, z in enumerate(done)
        ]
```

并把 `pending is not None` 分支的 `return` 改为先填 live H2 背驰再返回：

```python
        prev = done[-1] if done else None
        live = self._fork_pending(pending, prev)
        for h in live:
            if h.node1 == "leave":                    # 仅 H2（离开读法）判背驰
                h.divergence = self._divergence_for(h.zs, prev, live=True)
        return ZsBranchResult(
            done_zss=done,
            live=live,
            freeze_idx=self._line_index(pending.lines[0], lines),
            done_divergence=done_div,
        )
```

- [ ] **Step 4: 运行验证通过**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q -k "pz_divergence or qs_divergence"`
Expected: PASS（盘整 live / 盘整 done / 趋势 qs 三个集成测试全绿）

> 执行注记：若某断言因 `ZsCalculator` 的进入段/离开段归属与预期略有出入而失败，先在测试里临时 `print(len(res.done_zss), [(z.zd,z.zg) for z in res.done_zss], res.done_divergence)` 看实际结构，再据实微调 `weak_pairs` 的端点键或断言索引——结构口径以引擎实际产出为准（P1 已验证）。

- [ ] **Step 5: 提交**

```bash
git add src/chanlun/core/zs_branch.py tests/core/test_zs_branch.py
git commit -m "feat(core/zs_branch): calculate 接线 done+live H2 内联背驰(P3·盘整/趋势/provisional)"
```

---

## Task 5: 全套回归（P1 + P2 + P3 全绿）

**Files:** 无新增改动，纯验证。

- [ ] **Step 1: 跑 zs_branch 全套**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/test_zs_branch.py -q 2>&1 | tail -5`
Expected: PASS（P1 的 24 + P3 新增约 11 = 全绿，0 failed）

- [ ] **Step 2: 跑 core 全套（防跨模块回归）**

Run: `cd D:/project/chanlun-pro && poetry run pytest tests/core/ -q 2>&1 | tail -5`
Expected: PASS（含 `test_beichi_calculator.py`、`test_zs_calculator.py` 等全绿）

- [ ] **Step 3: lint**

Run: `cd D:/project/chanlun-pro && poetry run ruff check src/chanlun/core/zs_branch.py`
Expected: 无 error（若有未用 import 等，清理后重跑）

- [ ] **Step 4: 提交（仅当 Step 1-3 有清理改动时）**

```bash
git add src/chanlun/core/zs_branch.py
git commit -m "chore(core/zs_branch): P3 lint 清理 + 全套回归绿"
```

---

## Task 6: 真实数据出图验收（人工审）

**Files:**
- Create（本地不入库）: `scripts_local/probe_p3_review.py`
- Output: `zs_branch_review.html`

- [ ] **Step 1: 写验收 probe 脚本**

```python
# scripts_local/probe_p3_review.py —— P3 真实数据出图验收（本地临时，不入库）
import logging
import pandas as pd
import plotly.graph_objects as go

logging.disable(logging.WARNING)
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator

CFG = {
    "chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}
df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
cd = CL("SH.513100", "1m", dict(CFG))
cd.process_klines(df)

bis = cd.get_bis()                       # 笔级别(1m 上有意义的级别)
ld_provider = lambda s, e: query_macd_ld(cd, s, e)
calc = ZsBranchCalculator(ld_provider=ld_provider, frequency="1m")
res = calc.calculate(bis)

ks = cd.get_klines()
fig = go.Figure(go.Candlestick(
    x=[k.date for k in ks], open=[k.o for k in ks], high=[k.h for k in ks],
    low=[k.l for k in ks], close=[k.c for k in ks], name="K",
))

def _box(z, dv, tag):
    x0, x1 = z.lines[0].start.k.date, (z.end or z.lines[-1]).end.k.date
    color = "rgba(220,40,40,0.18)" if (dv and dv.is_beichi) else "rgba(60,120,220,0.12)"
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=z.zd, y1=z.zg,
                  fillcolor=color, line=dict(width=1, color="gray"))
    if dv is not None:
        label = ("H2a" if dv.is_beichi else "H2b") + f"·{dv.kind}" + ("·prov" if dv.provisional else "")
        fig.add_annotation(x=x1, y=z.zg, text=f"{tag} {label}", showarrow=False,
                           font=dict(size=10, color="darkred" if dv.is_beichi else "navy"))

for i, z in enumerate(res.done_zss):
    _box(z, res.done_divergence[i], f"#{i}")
for h in res.live:
    if h.node1 == "leave":
        _box(h.zs, h.divergence, "live")

n_done_bc = sum(1 for d in res.done_divergence if d and d.is_beichi)
n_live_bc = sum(1 for h in res.live if h.node1 == "leave" and h.divergence and h.divergence.is_beichi)
print(f"done中枢={len(res.done_zss)} 其中背驰(H2a)={n_done_bc}; "
      f"live H2 背驰={n_live_bc}")
print("done_divergence:", [
    (d.kind, d.is_beichi) if d else None for d in res.done_divergence
])
fig.update_layout(title="P3 zs_branch 内联背驰验收 (a_SH_513100_1m·笔级)",
                  xaxis_rangeslider_visible=False, height=760)
fig.write_html("zs_branch_review.html")
print("written zs_branch_review.html")
```

- [ ] **Step 2: 运行 probe**

Run: `cd D:/project/chanlun-pro && PYTHONPATH=src poetry run python scripts_local/probe_p3_review.py`
Expected: 打印 `done中枢=N 其中背驰(H2a)=...`、`done_divergence: [...]`、`written zs_branch_review.html`，无异常

- [ ] **Step 3: 人工审图（交付用户）**

把 `zs_branch_review.html` 交付用户，审核重点：
- 标 **H2a（红框）** 的位置是否落在**趋势末端/盘整离开**该背驰处（不是随便一个中枢都背驰）；
- **kind**（趋势 qs / 盘整 pz）与该处前面是否有同向中枢吻合；
- **live·prov** 是否只出现在最右边缘未坐实的中枢。

- [ ] **Step 4: 据审图结论决定下一步**

- 图 OK → P3 完成，更新 memory `project_chanlun_core_redesign`（P3 实质完成、非常规背驰/精确三买卖/beichi_pz 生产对齐留后），收尾或转 P4。
- 图有问题（背驰位置/类型不对）→ 回到对应 Task 修正（多半是 `_leave_seg` 提取口径或 `_is_trend` 方向判定），补测试再验。

> 验收脚本是本地临时件，不提交（`scripts_local/` 可加进 `.gitignore` 或直接不 `git add`）。`zs_branch_review.html` 同 P1，本地审阅件、不入库。

---

## Self-Review（计划对照 spec）

**1. Spec 覆盖**：
- §2 力度构造注入 → Task 2 ✓；退化保 P1 → Task 2/5 ✓
- §3 is_beichi 直连、不复用 beichi_qs/pz → Task 3 `_divergence_for` ✓
- §4 离开段/比较段提取 → Task 3 `_leave_seg`（done→z.end / live→lines[-1]）✓
- §5 趋势/盘整 is_qs 自动选择 → Task 3 `_is_trend` ✓
- §6 H2a/H2b 不新增分支、divergence 属性 + done_divergence 载体 → Task 1 ✓
- §7 趋势门槛 L0（c≥次级别恒真、三买卖并入 provisional）→ Task 3/4 provisional 语义 ✓
- §8 provisional（done=False/live=True）→ Task 4 ✓
- §9 测试 + 真实数据出图 → Task 4/5/6 ✓
- §0 非常规背驰/精确三买卖/beichi_pz 生产对齐留后 → 不在任何 Task（正确排除）✓

**2. Placeholder 扫描**：无 TBD/TODO；每个 code step 给完整代码；Task 4 的「执行注记」是真实调试指引（非 placeholder，因主体代码完整给出）。

**3. 类型/签名一致**：`DivergenceResult` 字段（is_beichi/kind/compare_seg/leave_seg/provisional）在 Task 1 定义，Task 3/4 构造一致；`_divergence_for(zs, prev_zs, live)` 签名 Task 3 定义、Task 4 调用一致；`_leave_seg(zs, live)`/`_is_trend(prev_zs, zs, leave)` 一致；`is_beichi`/`is_qs` 签名与 `beichi_calculator.py` 实际一致（`is_beichi(a,c,ld_provider,frequency)`、`is_qs(one,two,wzgx,use_core_envelope=)`）。

无 gap，无需补 Task。
