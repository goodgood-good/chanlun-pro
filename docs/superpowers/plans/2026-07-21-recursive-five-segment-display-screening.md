# Recursive Five-Segment Display and Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立以当前 K 线内部递归为唯一真值的五段中枢、分级走势/背驰显示，并修复 TDX 880 行业指数严格结构不可用导致提前选股全空的问题。

**Architecture:** 严格结构核心先把初始五段角色和进行中/完成状态固化为不可变模型，再由锁定走势逐级递归，并在同一证据快照内生成正式点与独立背驰。图表只消费 v4 原子快照，显示偏好只控制渲染；提前选股在 TDX 行业指数适配边界附加无复权价格基准，并在板块排名前执行可审计质量门。

**Tech Stack:** Python 3、dataclasses、pandas、pytest、JavaScript、Node `node:test` TAP、TradingView Charting Library、Flask、本地 TDX/QMT 适配器、Git/PowerShell。

## Global Constraints

- 权威设计为 `docs/superpowers/specs/2026-07-21-recursive-five-segment-display-screening-design.md`；冲突时以该规格和用户随后确认的要求为准。
- 当前任务分支为 `codex/recursive-five-segment-screening`；`pre` 必须始终保持 `be2245d681ed132cd573e00c1cee73101aabea52`，禁止 push、merge、discard。
- 工作区已有 108 项非本规格提交产生的改动。禁止清理或回滚；每次只暂存任务列出的文件，并在提交前核对 `git diff --cached --name-only`。
- 文件改动使用 `apply_patch`；写后用 PowerShell `Get-Content -Encoding UTF8`、`Select-String` 或 `Get-FileHash` 做独立旁路复核。所有 shell 命令均使用 PowerShell。
- Python 改动严格 RED→GREEN：先按精确测试名运行并看到预期失败，再写最小实现，再按精确测试名和逐文件回归。
- JavaScript 测试逐文件运行 `node --test --test-reporter=tap <file>`，禁止把多个文件放进同一 Node 命令；GREEN 必须解析 `# pass N` 且 `# fail 0`。
- 每个任务 GREEN 后立即提交。提交信息使用中文，最后一个 trailer 固定为 `Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 下列 `git add -- <task paths>` 只有在执行预检确认该路径的既有脏内容整体属于本规格时才可使用；若同一 tracked 文件混有无关用户 hunk，只暂存本任务 hunk并复核 cached diff；若 planned untracked 文件包含无法归属的既有内容，由于 Git 无法在父提交不存在该文件时只提交“新增部分”，必须暂停并向用户确认，不能把它悄悄并入提交。
- 正式中枢初始构件严格为五个连续、交替、锁定构件；第 2～4 段确定 `ZD/ZG`，第 1、5 段与核心必须正宽重叠，第 5 段终点必须越出核心。
- 中枢只有正式 `ongoing/completed` 两态；`forming/touch_only` 只属于预告。完成必须和三买/三卖在严格证据契约中原子一致。
- 递归显示映射固定为：1m→1m/5m/30m/日线；5m→5m/30m/日线；30m→30m/日线；日线→日线。15m/60m/周/月等未列周期只允许 L0 当前周期标签，不外推周/月级别。
- TDX 880 行业指数明确使用原生连续 `fq=none`，不能调用 qfq/hfq；未知精度或未知标的不得猜测价格基准。
- 协议版本固定为：`chanlun-structure/v3`、`chanlun-strict-evidence/v3`、`chanlun-chart-center/v4`、`chanlun-chart-structure/v4`、图表缓存 `v40`、严格 CL 缓存 `strict-v3`、选股快照 `chanlun-trading-screening/v2`、选股 `structure_version=v2`、前端显示配置 `schema_version=2`。
- 板块成功率复用 `min_scan_completion_ratio`，默认 `0.80`。基础设施失败低于门槛时使用 `incomplete_not_published` 并保留上一份有效快照；合法零候选可以发布，但禁止伪造选择。

## Execution Preflight: 既有 108 项脏工作区归属审计

实施 Task 1 前先执行只读预检，不能假设 untracked strict-structure 文件是本轮新建：

```powershell
$planPath = 'docs/superpowers/plans/2026-07-21-recursive-five-segment-display-screening.md'
$planned = Select-String -LiteralPath $planPath -Pattern '^- (?:Modify|Create|Regenerate): `([^`]+)`' |
  ForEach-Object { ($_.Matches[0].Groups[1].Value -replace ':\d.*$', '') } |
  Sort-Object -Unique
git status --short -- $planned
foreach ($path in $planned) {
  $status = git status --short -- $path
  if ($status -match '^\?\?') {
    "UNTRACKED $path SHA256=$((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash)"
    Get-Content -LiteralPath $path -Encoding UTF8
  } elseif ($status) {
    git diff -- $path
  }
}
```

逐文件标记为“本规格基础”“无关用户改动”或“混合”。只有第一类能由后续整文件 `git add`；第二类不触碰；第三类 tracked 文件使用精确 patch 暂存后以 `git diff --cached -- <path>` 复核，第三类 untracked 文件按上面的强制规则暂停。每次提交还必须确认 `git diff --cached --name-only` 只含该 Task 清单，并逐页读完 `git diff --cached`。

---

## File Responsibility Map

- `src/chanlun/core/strict_structure/models.py`：五段中枢、事件、严格点、独立背驰和快照不变量。
- `src/chanlun/core/strict_structure/center_machine.py`：五段候选几何、逐构件状态转移、批量扫描。
- `src/chanlun/core/strict_structure/level_catalog.py`（新建）：源周期到允许递归层数/显示标签的唯一 Python 映射。
- `src/chanlun/core/strict_structure/trend_assembler.py`、`recursive_engine.py`：完成走势锁定和逐级递归。
- `src/chanlun/core/strict_structure/divergence.py`（新建）：盘整背驰、趋势背驰的正式独立收集器。
- `src/chanlun/core/strict_structure/signals.py`、`strength.py`：一二三类点和可审计力度比较。
- `src/chanlun/core/strict_structure/identity.py`、`src/chanlun/core/cl.py`：v3 证据 revision、缓存和原子证据装配。
- `src/chanlun/cl_utils/strict_chart.py`：v4 图表序列化；不做结构重判。
- `web/chanlun_chart/cl_app/static/js/charts.js`：显示配置、菜单、图层 gate 和严格图形创建。
- `web/chanlun_chart/cl_app/static/js/chart_analysis.js`：v4 快照校验和分析侧栏摘要。
- `src/chanlun/exchange/price_basis.py`、`kline_precision.py`：TDX 880 无复权价格基准元数据。
- `web/chanlun_chart/cl_app/services/trading_screening_gateway.py`：行业指数加载、稳定错误分类、板块分析批次。
- `web/chanlun_chart/cl_app/services/trading_screening.py`：板块质量门、选股 v2 原子发布。

---

### Task 1: 五段中枢模型与进行中/完成状态机

**Files:**
- Modify: `src/chanlun/core/strict_structure/models.py:22-439`
- Modify: `src/chanlun/core/strict_structure/center_machine.py:18-430`
- Modify: `src/chanlun/core/strict_structure/incremental.py:1-120`
- Modify: `src/chanlun/core/strict_structure/__init__.py:1-65`
- Modify: `tests/core/strict_structure/helpers.py:1-160`
- Modify: `tests/core/strict_structure/test_models.py`
- Modify: `tests/core/strict_structure/test_center_seed.py`
- Modify: `tests/core/strict_structure/test_center_transitions.py`
- Modify: `tests/core/strict_structure/test_center_scan.py`
- Modify: `tests/core/strict_structure/test_center_relation.py`
- Modify: `tests/core/strict_structure/test_incremental_prefix.py`

**Interfaces:**
- Consumes: `ConstituentUnit` 的连续性、锁定时间、方向和整数 tick 契约。
- Produces: `establish_center(initial_units, structural_level, source_kind) -> TrendCenter | None`；`advance_center(center, item) -> tuple[TrendCenter, CenterEvent | None]`；`calculate_centers(units, structural_level, source_kind) -> CenterLevelResult`。
- Produces model: `TrendCenter.initial_units` 固定五段；`entry_unit`、`core_units`、`initial_exit_unit` 属性；`pending_leave_unit`、`completion_leave_unit`、`completion_return_unit`、`completed_at` 明确生命周期角色。

- [ ] **Step 1: 写五段成立和拒绝条件的失败测试**

先在 `tests/core/strict_structure/helpers.py` 写入 Step 4 所列的 `valid_five_up_exit()`，再由 `test_center_seed.py` 导入它，用向上离开的连续构件固定核心 `[105, 115]`，并删除“三段立即成立”的旧断言：

```python
def invalid_initial_five(mutation):
    values = {
        # U2-U4 的交集为 [121, 120]，没有正宽核心。
        "middle_has_no_positive_core": (
            unit(0, "up", 90, 120),
            unit(1, "down", 120, 100),
            unit(2, "up", 100, 130),
            unit(3, "down", 130, 121),
            unit(4, "up", 121, 140),
        ),
        # 核心为 [72, 78]，U1=[80, 90] 与它不重叠。
        "entry_has_no_positive_overlap": (
            unit(0, "up", 80, 90),
            unit(1, "down", 90, 70),
            unit(2, "up", 70, 78),
            unit(3, "down", 78, 72),
            unit(4, "up", 72, 95),
        ),
        # 核心为 [100, 110]；U4 有段内高点 110，但 U5=[80, 95] 不重叠。
        "exit_has_no_positive_overlap": (
            unit(0, "down", 130, 100),
            unit(1, "up", 100, 120),
            unit(2, "down", 120, 90),
            replace(unit(3, "up", 90, 95), high_tick=110),
            unit(4, "down", 95, 80),
        ),
        # 核心为 [105, 115]；U5 正宽重叠，但终点 110 未越过 ZG。
        "exit_endpoint_not_outside": (
            unit(0, "up", 90, 120),
            unit(1, "down", 120, 100),
            unit(2, "up", 100, 115),
            unit(3, "down", 115, 105),
            unit(4, "up", 105, 110),
        ),
    }
    return values[mutation]


def test_five_locked_units_establish_ongoing_center_with_middle_core():
    initial = valid_five_up_exit()
    value = establish_center(initial, 0, SourceKind.SEGMENT)
    assert value is not None
    assert value.state is CenterState.ONGOING
    assert value.initial_units == initial
    assert value.entry_unit is initial[0]
    assert value.core_units == initial[1:4]
    assert value.initial_exit_unit is initial[4]
    assert value.pending_leave_unit is initial[4]
    assert (value.zd_tick, value.zg_tick) == (105, 115)
    assert value.established_at == initial[4].confirmed_at


def test_three_or_four_locked_units_never_establish_formal_center():
    initial = valid_five_up_exit()
    assert establish_center(initial[:3], 0, SourceKind.SEGMENT) is None
    assert establish_center(initial[:4], 0, SourceKind.SEGMENT) is None


@pytest.mark.parametrize(
    "mutation",
    ("middle_has_no_positive_core", "entry_has_no_positive_overlap",
     "exit_has_no_positive_overlap", "exit_endpoint_not_outside"),
)
def test_initial_five_reject_each_geometric_violation(mutation):
    initial = invalid_initial_five(mutation)
    assert establish_center(initial, 0, SourceKind.SEGMENT) is None
```

测试文件从 `dataclasses` 导入 `replace`。四组夹具都必须使用上面的完整 `ConstituentUnit`，不能通过 monkeypatch 绕过模型验证；另外单独用 `low_tick/high_tick` 精确构造进入段和离开段与核心零宽相触的镜像用例。

- [ ] **Step 2: 运行精确测试并确认 RED**

```powershell
pytest tests/core/strict_structure/test_center_seed.py::test_five_locked_units_establish_ongoing_center_with_middle_core -q
if ($LASTEXITCODE -eq 0) { throw 'RED expected: old three-unit model unexpectedly passed' }
```

预期：因 `CenterState.ONGOING`、五段字段或五段签名尚不存在而失败。

- [ ] **Step 3: 将中心模型改为显式五段角色**

在 `models.py` 用以下正式状态和字段替换旧 `ESTABLISHED/EXTENDING/BREAKOUT_PENDING/DESTROYED/CLOSED_OTHER` 模型：

```python
class CenterEventKind(str, Enum):
    ESTABLISHED = "center_established"
    EXTENDED = "center_extended"
    BREAKOUT_WATCH_UP = "breakout_watch_up"
    BREAKOUT_WATCH_DOWN = "breakout_watch_down"
    COMPLETED_UP = "center_completed_up"
    COMPLETED_DOWN = "center_completed_down"


class CenterState(str, Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"


class CenterPreviewState(str, Enum):
    TOUCH_ONLY = "touch_only"
    FORMING = "forming"


@dataclass(frozen=True, slots=True)
class TrendCenter:
    center_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    state: CenterState
    initial_units: tuple[
        ConstituentUnit, ConstituentUnit, ConstituentUnit,
        ConstituentUnit, ConstituentUnit,
    ]
    body_units: tuple[ConstituentUnit, ...]
    extension_units: tuple[ConstituentUnit, ...]
    zd_tick: int
    zg_tick: int
    dd_tick: int
    gg_tick: int
    body_start_market_time: datetime
    established_market_time: datetime
    established_at: datetime
    last_touch_market_time: datetime
    pending_leave_unit: ConstituentUnit | None
    completion_leave_unit: ConstituentUnit | None
    completion_return_unit: ConstituentUnit | None
    completed_at: datetime | None
    available_at: datetime
    body_revision: int

    @property
    def entry_unit(self) -> ConstituentUnit:
        return self.initial_units[0]

    @property
    def core_units(self) -> tuple[ConstituentUnit, ConstituentUnit, ConstituentUnit]:
        return self.initial_units[1:4]

    @property
    def initial_exit_unit(self) -> ConstituentUnit:
        return self.initial_units[4]

    @property
    def completion_direction(self) -> Direction | None:
        return None if self.completion_leave_unit is None else self.completion_leave_unit.direction
```

`TrendCenter.__post_init__` 必须逐项验证：正式状态只有 `ONGOING/COMPLETED`；五段连续/交替/锁定；核心只等于 `initial_units[1:4]` 交集；进入与初始离开正宽重叠；初始离开终点越界；`body_units == initial_units + extension_units`；`dd_tick == min(item.low_tick for item in body_units)`、`gg_tick == max(item.high_tick for item in body_units)`；`body_revision == len(extension_units)`；进行中只能持有 pending，完成只能持有锁定的 completion leave/return 和 `completed_at == completion_return_unit.confirmed_at`。`CenterPreview` 改用独立 `CenterPreviewState.TOUCH_ONLY/FORMING`，永远不能提供正式 `center_id`。`CenterEventKind` 删除 `DESTROYED_*/CLOSED_OTHER`，观察性离开继续使用 `BREAKOUT_WATCH_*`，正式完成只使用 `COMPLETED_UP/DOWN`。`CenterEvidence.from_center()` 同步改为 `chanlun-center/v3` 和新角色 ID。

- [ ] **Step 4: 实现五段建立和逐构件状态转移**

在 `center_machine.py` 保留单一正宽判断，并用逐构件函数替换 `apply_pair/apply_pending_leave`：

```python
def _positive_overlap(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    return max(item.low_tick, zd_tick) < min(item.high_tick, zg_tick)


def _outside_in_direction(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    return (
        item.end_tick > zg_tick
        if item.direction == "up"
        else item.end_tick < zd_tick
    )


def _core(initial_units: tuple[ConstituentUnit, ...]) -> tuple[int, int]:
    middle = initial_units[1:4]
    return max(item.low_tick for item in middle), min(item.high_tick for item in middle)


def establish_center(initial_units, structural_level, source_kind):
    values = tuple(initial_units)
    if len(values) != 5 or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(values, structural_level, source_kind)
    if any(not item.locked for item in values):
        return None
    zd_tick, zg_tick = _core(values)
    if zd_tick >= zg_tick:
        return None
    if not _positive_overlap(values[0], zd_tick, zg_tick):
        return None
    if not _positive_overlap(values[4], zd_tick, zg_tick):
        return None
    if not _outside_in_direction(values[4], zd_tick, zg_tick):
        return None
    return _new_ongoing_center(
        values, structural_level, SourceKind(source_kind),
        price_basis_revision, zd_tick, zg_tick,
    )


def advance_center(center: TrendCenter, item: ConstituentUnit):
    _validate_transition_unit(center, item)
    if center.state is CenterState.COMPLETED:
        raise ValueError("completed center cannot transition")
    if not item.locked:
        raise ValueError("formal center transition must be locked")
    previous = center.body_units[-1]
    if item.start_tick != previous.end_tick or item.direction == previous.direction:
        raise ValueError("center transition must connect and alternate")

    pending = center.pending_leave_unit
    if pending is not None:
        if _positive_overlap(item, center.zd_tick, center.zg_tick):
            return _append_extension_return(center, item)
        if pending.direction == "up" and item.direction == "down" and item.low_tick >= center.zg_tick:
            return _complete_center(center, pending, item, "up")
        if pending.direction == "down" and item.direction == "up" and item.high_tick <= center.zd_tick:
            return _complete_center(center, pending, item, "down")
        raise ValueError("return geometry is neither extension nor third-class completion")

    if not _positive_overlap(item, center.zd_tick, center.zg_tick):
        raise ValueError("ongoing center unit must re-enter the core")
    return _append_body_unit(
        center,
        item,
        pending_leave=item if _outside_in_direction(item, center.zd_tick, center.zg_tick) else None,
    )
```

上述内部 helper 也只有一套明确语义：

- `_new_ongoing_center()` 用初始五段和 `chanlun-center/v3` namespace 建 ID，`body_units=initial_units`、`extension_units=()`、`pending_leave_unit=initial_exit_unit`、三个 completion 字段为空、`established_at=initial_exit_unit.confirmed_at`、`body_revision=0`，包络取五段 extrema，并返回 `CenterEventKind.ESTABLISHED` 所需的同一身份数据。
- `_append_extension_return()` 把回到核心的锁定构件追加到 `extension_units/body_units`，清空 pending，保持核心和 ID 不变，重算包络/可用时间/revision，返回 `EXTENDED`。
- `_append_body_unit(..., pending_leave=...)` 同样追加构件；若参数非空则返回方向对应的 `BREAKOUT_WATCH_*`，否则返回 `EXTENDED`。不得改变固定核心。
- `_complete_center()` 不把完成回抽放入 `body_units`；它将 pending 固化为 `completion_leave_unit`，当前构件写入 `completion_return_unit`，清空 pending，设 `state=COMPLETED`、`completed_at=item.confirmed_at`、`available_at=max(old, item.available_at)`，返回方向对应的 `COMPLETED_*`。
- `_validate_transition_unit()` 统一校验 level/source/basis、时间先后和 `unit_id` 未重复；任何调用方都不能绕过它。

把测试时间基准 `BASE` 改成带 `timezone.utc` 的时间，并在 `tests/core/strict_structure/helpers.py` 提供后续任务唯一使用的中心构造器；`test_center_seed.py` 直接导入它们，不再保留同名副本：

```python
def valid_five_up_exit(
    unit_offset=0,
    *,
    structural_level=0,
    zd_tick=105,
    zg_tick=115,
):
    return (
        unit(unit_offset, "up", zd_tick - 15, zg_tick + 5,
             structural_level=structural_level),
        unit(unit_offset + 1, "down", zg_tick + 5, zd_tick - 5,
             structural_level=structural_level),
        unit(unit_offset + 2, "up", zd_tick - 5, zg_tick,
             structural_level=structural_level),
        unit(unit_offset + 3, "down", zg_tick, zd_tick,
             structural_level=structural_level),
        unit(unit_offset + 4, "up", zd_tick, zg_tick + 15,
             structural_level=structural_level),
    )


def ongoing_center(
    unit_offset=0,
    *,
    structural_level=0,
    zd_tick=105,
    zg_tick=115,
    center_id=None,
):
    initial = valid_five_up_exit(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )
    value = establish_center(initial, structural_level, SourceKind.SEGMENT)
    assert value is not None
    return value if center_id is None else replace(value, center_id=center_id)


def completed_up_center(
    unit_offset=0,
    *,
    structural_level=0,
    zd_tick=105,
    zg_tick=115,
    return_low_tick=None,
    center_id=None,
):
    value = ongoing_center(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        center_id=center_id,
    )
    resolved_return_low = zg_tick + 5 if return_low_tick is None else return_low_tick
    ret = unit(
        unit_offset + 5,
        "down",
        zg_tick + 15,
        resolved_return_low,
        structural_level=structural_level,
    )
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert event is not None and event.kind is CenterEventKind.COMPLETED_UP
    return completed


def ongoing_down_center(
    unit_offset=0,
    *,
    structural_level=0,
    zd_tick=95,
    zg_tick=105,
    center_id=None,
):
    initial = (
        unit(unit_offset, "down", zg_tick + 15, zd_tick - 5,
             structural_level=structural_level),
        unit(unit_offset + 1, "up", zd_tick - 5, zg_tick + 5,
             structural_level=structural_level),
        unit(unit_offset + 2, "down", zg_tick + 5, zd_tick,
             structural_level=structural_level),
        unit(unit_offset + 3, "up", zd_tick, zg_tick,
             structural_level=structural_level),
        unit(unit_offset + 4, "down", zg_tick, zd_tick - 15,
             structural_level=structural_level),
    )
    value = establish_center(initial, structural_level, SourceKind.SEGMENT)
    assert value is not None
    return value if center_id is None else replace(value, center_id=center_id)


def completed_down_center(
    unit_offset=0,
    *,
    structural_level=0,
    zd_tick=95,
    zg_tick=105,
    return_high_tick=None,
    center_id=None,
):
    value = ongoing_down_center(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        center_id=center_id,
    )
    resolved_return_high = zd_tick - 5 if return_high_tick is None else return_high_tick
    ret = unit(
        unit_offset + 5,
        "up",
        zd_tick - 15,
        resolved_return_high,
        structural_level=structural_level,
    )
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert event is not None and event.kind is CenterEventKind.COMPLETED_DOWN
    return completed
```

`calculate_centers()` 从五段窗口开始；建立后逐个调用 `advance_center()`。完成回抽可作为下一候选五段的第一个构件；活动尾部只形成五段 preview，不产生正式 ID。`incremental.py` 继续通过同一 `calculate_centers()` 回放活动尾部，不维护第二套状态逻辑。

- [ ] **Step 5: 写延伸、完成、边界相触和扫描失败测试**

在 `test_center_transitions.py` 和 `test_center_scan.py` 加入：

```python
def test_locked_return_into_core_extends_without_moving_core():
    value = establish_center(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    ret = unit(5, "down", 130, 110)
    updated, event = advance_center(value, ret)
    assert updated.state is CenterState.ONGOING
    assert updated.pending_leave_unit is None
    assert updated.extension_units == (ret,)
    assert updated.center_id == value.center_id
    assert (updated.zd_tick, updated.zg_tick) == (105, 115)
    assert event.kind is CenterEventKind.EXTENDED


def test_locked_return_outside_completes_center():
    value = establish_center(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    ret = unit(5, "down", 130, 120)
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is value.initial_exit_unit
    assert completed.completion_return_unit is ret
    assert completed.completed_at == ret.confirmed_at
    assert event.kind is CenterEventKind.COMPLETED_UP


def test_return_touching_zg_is_completion_not_extension():
    value = establish_center(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    ret = unit(5, "down", 130, value.zg_tick)
    completed, _event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED


def test_scan_with_four_locked_units_has_no_formal_center():
    result = calculate_centers(valid_five_up_exit()[:4], 0, SourceKind.SEGMENT)
    assert result.centers == ()
```

同时添加向下镜像、进入段/离开段零宽接触、离开终点未越界、混合 basis、未锁定尾部、批量/增量同 ID 测试。

`test_center_relation.py::relation_center()` 也必须改用完整五段中心；保留既有“包络完全分离才是趋势、仅核心分离仍为升级”的关系语义，并新增断言延伸只改变 `dd/gg/body_revision`、不改变固定 `zd/zg`。本任务不借五段迁移偷偷改写走势关系定义。

- [ ] **Step 6: 运行 Task 1 精确测试和逐文件回归**

```powershell
$files = @(
  'tests/core/strict_structure/test_models.py',
  'tests/core/strict_structure/test_center_seed.py',
  'tests/core/strict_structure/test_center_transitions.py',
  'tests/core/strict_structure/test_center_scan.py',
  'tests/core/strict_structure/test_center_relation.py',
  'tests/core/strict_structure/test_incremental_prefix.py'
)
foreach ($file in $files) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}
```

预期：五个文件全部 PASS；没有旧三段成立断言，没有 `DESTROYED_*` 或 `CLOSED_OTHER` 正式状态残留。

- [ ] **Step 7: 独立复核并提交 Task 1**

```powershell
Select-String -Encoding UTF8 -Path 'src/chanlun/core/strict_structure/models.py' -Pattern 'initial_units','core_units','pending_leave_unit','completed_at'
rg -n 'CenterState\.(ESTABLISHED|EXTENDING|BREAKOUT_PENDING|DESTROYED|CLOSED_OTHER)' src/chanlun/core/strict_structure tests/core/strict_structure
if ($LASTEXITCODE -eq 0) { throw 'legacy formal center states remain' }
$old = git rev-parse HEAD
git add -- src/chanlun/core/strict_structure/models.py src/chanlun/core/strict_structure/center_machine.py src/chanlun/core/strict_structure/incremental.py src/chanlun/core/strict_structure/__init__.py tests/core/strict_structure/helpers.py tests/core/strict_structure/test_models.py tests/core/strict_structure/test_center_seed.py tests/core/strict_structure/test_center_transitions.py tests/core/strict_structure/test_center_scan.py tests/core/strict_structure/test_center_relation.py tests/core/strict_structure/test_incremental_prefix.py
git diff --cached --check
git commit -m '核心：建立五段中枢状态机' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
$new = git rev-parse HEAD
if ($new -eq $old) { throw 'commit did not advance HEAD' }
if ((git rev-parse pre) -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
```

---

### Task 2: 完成走势递归与第三类点原子一致性

**Files:**
- Create: `src/chanlun/core/strict_structure/level_catalog.py`
- Modify: `src/chanlun/core/strict_structure/trend_assembler.py`
- Modify: `src/chanlun/core/strict_structure/recursive_engine.py`
- Modify: `src/chanlun/core/strict_structure/signals.py`
- Modify: `src/chanlun/core/strict_structure/models.py`
- Modify: `src/chanlun/core/strict_structure/identity.py`
- Modify: `src/chanlun/core/cl.py:497-652`
- Modify: `src/chanlun/core/strict_structure/__init__.py`
- Modify: `tests/core/strict_structure/helpers.py`
- Create: `tests/core/strict_structure/test_level_catalog.py`
- Modify: `tests/core/strict_structure/test_trend_assembler.py`
- Modify: `tests/core/strict_structure/test_recursive_engine.py`
- Modify: `tests/core/strict_structure/test_third_class_points.py`
- Modify: `tests/core/strict_structure/test_approaching_points.py`
- Modify: `tests/core/strict_structure/test_evidence_revision.py`
- Modify: `tests/core/strict_structure/test_cl_strict_structure.py`
- Modify: `tests/core/strict_structure/test_cl_strict_points.py`
- Modify: `tests/core/strict_structure/test_real_recursive_prefix.py`
- Modify: `tests/core/strict_structure/test_real_prefix_stability.py`
- Modify: `tests/core/strict_structure/test_real_point_prefix.py`
- Modify: `tests/trading_system/strict_helpers.py`
- Modify: `tests/trading_system/test_structure_adapter.py`
- Create: `tests/core/golden/strict_points_v3.json`

**Interfaces:**
- Consumes: Task 1 的 `CenterState.COMPLETED`、`completion_leave_unit`、`completion_return_unit`、`completed_at`。
- Produces: `recursive_level_labels(source_frequency: str) -> tuple[str, ...]`；`StrictRecursiveEngine(max_levels=len(labels))`；`StrictStructureResult(schema_version="chanlun-structure/v3")`。
- Produces invariant: 每个正式完成中枢在 `StrictEvidenceResult` 中恰好对应一个同级三买/三卖，反向引用也必须成立。

- [ ] **Step 1: 写固定级别映射和五构件递归门槛的失败测试**

`test_level_catalog.py` 使用完整映射：

```python
import pytest
from chanlun.core.strict_structure.level_catalog import recursive_level_labels


@pytest.mark.parametrize(
    ("frequency", "expected"),
    (
        ("1m", ("1m", "5m", "30m", "日线")),
        ("5m", ("5m", "30m", "日线")),
        ("30m", ("30m", "日线")),
        ("d", ("日线",)),
        ("1D", ("日线",)),
        ("15m", ("15m",)),
        ("15", ("15m",)),
    ),
)
def test_recursive_level_labels_are_fixed_and_never_invent_week_or_month(frequency, expected):
    assert recursive_level_labels(frequency) == expected
```

在 `test_recursive_engine.py` 把所有三段夹具改为五段/完成走势夹具，并新增 `test_recursion_stops_when_fewer_than_five_locked_trends_remain`。

- [ ] **Step 2: 运行映射测试确认 RED**

```powershell
pytest tests/core/strict_structure/test_level_catalog.py::test_recursive_level_labels_are_fixed_and_never_invent_week_or_month -q
if ($LASTEXITCODE -eq 0) { throw 'RED expected: level_catalog should not exist yet' }
```

- [ ] **Step 3: 建立唯一 Python 级别目录并限制 CL 递归深度**

新建 `level_catalog.py`：

```python
from __future__ import annotations


_LABELS = {
    "1m": ("1m", "5m", "30m", "日线"),
    "5m": ("5m", "30m", "日线"),
    "30m": ("30m", "日线"),
    "d": ("日线",),
}


def _canonical_frequency(value: str) -> str:
    raw = str(value).strip()
    aliases = {"1": "1m", "5": "5m", "30": "30m", "1D": "d", "1d": "d", "day": "d"}
    if raw in aliases:
        return aliases[raw]
    return f"{raw}m" if raw.isdigit() else raw.lower()


def recursive_level_labels(source_frequency: str) -> tuple[str, ...]:
    key = _canonical_frequency(source_frequency)
    return _LABELS.get(key, (str(source_frequency).strip(),))
```

`CL.get_strict_structure_levels()` 使用 `labels = recursive_level_labels(self.get_frequency())` 和 `StrictRecursiveEngine(max_levels=len(labels))`。`recursive_engine.py` 将每层最低锁定输入从 3 改为 5，并返回 `chanlun-structure/v3`；只把 `TrendState.LOCKED` 走势转换为上一层构件。

- [ ] **Step 4: 迁移走势装配器到完成中枢角色**

`trend_assembler.py` 的完成判断统一为：

```python
def _group_is_complete(group, constituent_units):
    return (
        all(center.state is CenterState.COMPLETED for center in group)
        and all(
            center.completion_leave_unit is not None
            and center.completion_return_unit is not None
            and center.completion_leave_unit.locked
            and center.completion_return_unit.locked
            for center in group
        )
        and bool(constituent_units)
        and all(item.locked for item in constituent_units)
    )
```

同步重写所有 ownership 校验，不能只替换状态名：

- `_validate_center_references()` 要求 `body_units` 是源构件的连续切片，`completion_leave_unit is body_units[-1]`，`completion_return_unit` 紧随 body；下一中心可以从前一完成回抽同一构件开始，但不能更早。
- `_constituent_units()` 以组末中心的 `completion_leave_unit`（进行中则 `body_units[-1]`）为终点；内部中心的完成回抽自然落在连续切片内，末中心完成回抽必须排除。
- `_completion_times()` 从 `center.completed_at` 取确认时间；`group_start` 从 `initial_units[0]` 取；边界中心可见性从其 `initial_units` 取。
- `TrendType.__post_init__` 对每个中心只要求完整 `body_units`；除末中心外的 `completion_return_unit` 必须属于走势构件，末中心的完成回抽不得属于；`terminal_unit == centers[-1].completion_leave_unit`。

完成走势的 terminal unit 因此是 `completion_leave_unit`；完成回抽是边界确认见证并可作为下一走势起点，但不属于前一走势 `constituent_units`。锁定时间使用 `completed_at`。趋势 ID namespace 升到 `chanlun-trend/v3`。

- [ ] **Step 5: 写第三类点和中枢完成原子性失败测试**

把当前 `test_third_class_points.py::structure_for/engine_for` 迁到 `tests/core/strict_structure/helpers.py`。`structure_for(*centers, completed_trends=())` 必须合并每个走势的 `constituent_units` 与每个中心的 `body_units + completion_return_unit`，按 `unit_id` 去重后以 `(market_start, unit_id)` 确定性排序并验证时间不倒退，这样不能漏掉中心间 bridge；随后构造同级 `CenterLevelResult`/`StrictLevelResult`，并固定返回 `StrictStructureResult(schema_version="chanlun-structure/v3", ...)`。`engine_for()` 只包装这个结构和 `Decimal("0.01")`。然后在 `test_third_class_points.py` 加入：

```python
def test_completed_up_center_emits_exactly_one_confirmed_three_buy():
    completed = completed_up_center(return_low_tick=120, zg_tick=115)
    point = only_point(engine_for(completed).third_class_points())
    assert completed.state is CenterState.COMPLETED
    assert point.point_type == "3buy"
    assert point.center_id == completed.center_id
    assert point.anchor_unit_id == completed.completion_return_unit.unit_id
    assert point.confirmed_at == completed.completed_at


def test_return_touching_core_boundary_is_confirmed_boundary_three_buy():
    completed = completed_up_center(return_low_tick=115, zg_tick=115)
    point = only_point(engine_for(completed).third_class_points())
    assert point.point_type == "3buy"
    assert point.variant is StrictPointVariant.BOUNDARY_TOUCH
    assert point.anchor_tick == point.invalidation_tick == 115
```


在 `test_evidence_revision.py` 复用现有 `evidence_bundle()`，加入双向拒绝测试；不要引入 `strict_evidence()` 或 `three_buy_for()` 之类的第二套 helper：

```python
def test_evidence_rejects_completed_center_without_matching_third_point():
    completed = completed_up_center()
    with pytest.raises(ValueError, match="completed centers and third-class points must match"):
        evidence_bundle(structure=structure_for(completed), confirmed_points=())


def test_evidence_rejects_third_point_for_ongoing_center():
    ongoing = ongoing_center(center_id="shared-center")
    completed = completed_up_center(center_id=ongoing.center_id)
    points = engine_for(completed).third_class_points()
    assert len(points) == 1
    with pytest.raises(ValueError, match="completed centers and third-class points must match"):
        evidence_bundle(
            structure=structure_for(ongoing),
            confirmed_points=(points[0],),
        )
```

- [ ] **Step 6: 更新正式点引擎和 v3 证据不变量**

`StrictSignalEngine.third_class_points()` 只遍历 `CenterState.COMPLETED`，按 `completion_leave_unit.direction` 产生 3buy/3sell。`_approaching_third_class()` 只读取 `CenterState.ONGOING + pending_leave_unit + unlocked tail`，不再依赖 pending 枚举状态。

在 `StrictEvidenceResult.__post_init__` 加入双向计数校验；先拒绝多个点映射同一中心，再比较集合，不能用单一 set 悄悄吞掉重复三类点：

```python
completed_keys = [
    (
        center.structural_level,
        center.center_id,
        center.source_kind,
        center.completion_direction,
        center.completion_return_unit.unit_id,
        center.completed_at,
        center.zd_tick,
        center.zg_tick,
    )
    for level in self.structure.levels
    for center in level.center_result.centers
    if center.state is CenterState.COMPLETED
    and center.source_kind is not SourceKind.STROKE_OBSERVATION
]
third_keys = [
    (
        point.structural_level,
        point.center_id,
        point.source_kind,
        "up" if point.point_type == "3buy" else "down",
        point.anchor_unit_id,
        point.confirmed_at,
        point.center_zd_tick,
        point.center_zg_tick,
    )
    for point in self.confirmed_points
    if point.point_type in ("3buy", "3sell")
]
if len(third_keys) != len(set(third_keys)):
    raise ValueError("each completed center must have exactly one third-class point")
if set(completed_keys) != set(third_keys):
    raise ValueError("completed centers and third-class points must match")
```

`identity.py` 将正式证据 namespace 升到 `chanlun-strict-evidence/v3`；`CL` 的严格信号配置 revision 升到 `chanlun-strict-signals/v3+...`。

- [ ] **Step 7: 更新真实前缀和审计 golden**

`test_real_recursive_prefix.py` 与 `test_real_prefix_stability.py` 的签名从 `destroyed_at/leave_unit/return_unit` 改成 `state/completed_at/completion_leave_unit/completion_return_unit/body_revision`；稳定性仍比较同一历史前缀中的完整正式身份，不能仅删字段来让断言通过。

将 golden 路径改为 `strict_points_v3.json`、schema 改为 `chanlun-strict-points-golden/v3`。在 `test_real_point_prefix.py` 暴露无写操作的 `build_v3_golden_document()`，它从固定 fixture、固定前缀和 `require_points=True` 的运行结果返回完整 dict。先让旧 golden 断言失败，再用以下只输出 stdout 的命令生成候选：

```powershell
@'
import json
from tests.core.strict_structure.test_real_point_prefix import build_v3_golden_document
print(json.dumps(build_v3_golden_document(), ensure_ascii=False, indent=2, sort_keys=True))
'@ | python -
if ($LASTEXITCODE -ne 0) { throw 'v3 golden candidate generation failed' }
```

逐项核对候选中的 `point_id/type/level/center_id/anchor_at/confirmed_at/available_at/variant/strength_source` 后，才用 `apply_patch` 写入完整 JSON；禁止测试自动重写或环境变量自动接受。如果 1100 根样本在五段门槛下合法为零，则把 helper 的固定前缀增至该 fixture 中首次出现正式点后的最小闭合边界，并仍保留 `require_points=True`。

- [ ] **Step 8: 运行 Task 2 精确测试和逐文件回归**

```powershell
$files = @(
  'tests/core/strict_structure/test_level_catalog.py',
  'tests/core/strict_structure/test_trend_assembler.py',
  'tests/core/strict_structure/test_recursive_engine.py',
  'tests/core/strict_structure/test_third_class_points.py',
  'tests/core/strict_structure/test_approaching_points.py',
  'tests/core/strict_structure/test_evidence_revision.py',
  'tests/core/strict_structure/test_cl_strict_structure.py',
  'tests/core/strict_structure/test_cl_strict_points.py',
  'tests/core/strict_structure/test_real_recursive_prefix.py',
  'tests/core/strict_structure/test_real_prefix_stability.py',
  'tests/core/strict_structure/test_real_point_prefix.py',
  'tests/trading_system/test_structure_adapter.py'
)
foreach ($file in $files) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}
```

预期：全部 PASS；1m/5m/30m/日线递归深度不超过 4/3/2/1；所有完成中枢与三类点一一对应。

- [ ] **Step 9: 独立复核并提交 Task 2**

```powershell
rg -n 'chanlun-structure/v2|chanlun-strict-evidence/v2|chanlun-strict-signals/v2' src/chanlun/core tests/core/strict_structure
if ($LASTEXITCODE -eq 0) { throw 'old strict truth schema remains in active core' }
rg -n 'strict_points_v2\.json|chanlun-strict-points-golden/v2' tests/core/strict_structure
if ($LASTEXITCODE -eq 0) { throw 'active tests still consume v2 point golden' }
$old = git rev-parse HEAD
git add -- src/chanlun/core/strict_structure/level_catalog.py src/chanlun/core/strict_structure/trend_assembler.py src/chanlun/core/strict_structure/recursive_engine.py src/chanlun/core/strict_structure/signals.py src/chanlun/core/strict_structure/models.py src/chanlun/core/strict_structure/identity.py src/chanlun/core/cl.py src/chanlun/core/strict_structure/__init__.py tests/core/strict_structure/helpers.py tests/core/strict_structure/test_level_catalog.py tests/core/strict_structure/test_trend_assembler.py tests/core/strict_structure/test_recursive_engine.py tests/core/strict_structure/test_third_class_points.py tests/core/strict_structure/test_approaching_points.py tests/core/strict_structure/test_evidence_revision.py tests/core/strict_structure/test_cl_strict_structure.py tests/core/strict_structure/test_cl_strict_points.py tests/core/strict_structure/test_real_recursive_prefix.py tests/core/strict_structure/test_real_prefix_stability.py tests/core/strict_structure/test_real_point_prefix.py tests/core/golden/strict_points_v3.json tests/trading_system/strict_helpers.py tests/trading_system/test_structure_adapter.py
git diff --cached --check
git commit -m '核心：统一递归走势与第三类点完成语义' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
$new = git rev-parse HEAD
if ($new -eq $old) { throw 'commit did not advance HEAD' }
if ((git rev-parse pre) -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
```

---

### Task 3: 按递归级别输出盘整背驰与趋势背驰

**Files:**
- Create: `src/chanlun/core/strict_structure/divergence.py`
- Modify: `src/chanlun/core/strict_structure/models.py:815-858`
- Modify: `src/chanlun/core/strict_structure/strength.py:126-185`
- Modify: `src/chanlun/core/strict_structure/signals.py`
- Modify: `src/chanlun/core/strict_structure/identity.py`
- Modify: `src/chanlun/core/strict_structure/__init__.py`
- Modify: `src/chanlun/core/cl.py:556-652`
- Create: `tests/core/strict_structure/test_divergence_collector.py`
- Modify: `tests/core/strict_structure/test_strength.py`
- Modify: `tests/core/strict_structure/test_evidence_revision.py`
- Modify: `tests/core/strict_structure/signal_helpers.py`
- Modify: `tests/core/strict_structure/test_first_class_points.py`
- Modify: `tests/core/strict_structure/test_second_class_points.py`

**Interfaces:**
- Consumes: 完成中心的初始/最终离开段、完成趋势及 `StrengthProvider.snapshot(unit)`。
- Produces: `collect_strict_divergences(structure, strength) -> tuple[DivergenceEvidence, ...]`。
- Produces model: `DivergenceEvidence` 自带稳定 ID、级别、basis、确认时间和渲染锚点；点位可以引用同一对象，但独立集合不依赖点位存在。

- [ ] **Step 1: 写独立背驰模型和两类收集器失败测试**

`test_divergence_collector.py` 使用下面的完整结构夹具和固定力度 provider；所有构件都通过真实 `establish_center()/advance_center()`，不直接伪造中心状态：

```python
def completed_consolidation_fixture(level=0):
    value = ongoing_center(structural_level=level)
    earlier = value.initial_exit_unit
    entered = unit(5, "down", 130, 110, structural_level=level)
    value, _ = advance_center(value, entered)
    later = unit(6, "up", 110, 135, structural_level=level)
    value, watch = advance_center(value, later)
    assert watch is not None and watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    outside_return = unit(7, "down", 135, 120, structural_level=level)
    value, _ = advance_center(value, outside_return)
    assert value.state is CenterState.COMPLETED
    return structure_for(value), earlier, later


def completed_trend_fixture(level=0):
    values = (
        unit(0, "up", 90, 120, structural_level=level),
        unit(1, "down", 120, 100, structural_level=level),
        unit(2, "up", 100, 115, structural_level=level),
        unit(3, "down", 115, 105, structural_level=level),
        unit(4, "up", 105, 130, structural_level=level),
        unit(5, "down", 130, 120, structural_level=level),
        unit(6, "up", 120, 140, structural_level=level),  # inter-center bridge
        unit(7, "down", 140, 135, structural_level=level),
        unit(8, "up", 135, 160, structural_level=level),
        unit(9, "down", 160, 140, structural_level=level),
        unit(10, "up", 140, 155, structural_level=level),
        unit(11, "down", 155, 145, structural_level=level),
        unit(12, "up", 145, 170, structural_level=level),
        unit(13, "down", 170, 160, structural_level=level),
    )
    first = establish_center(values[0:5], level, SourceKind.SEGMENT)
    second = establish_center(values[8:13], level, SourceKind.SEGMENT)
    assert first is not None and second is not None
    first, _ = advance_center(first, values[5])
    second, _ = advance_center(second, values[13])
    owned = values[:13]  # 第二个中心的完成回抽只负责确认，不归前一走势。
    trend = TrendType(
        trend_id=f"trend-{level}",
        structural_level=level,
        price_basis_revision=TEST_PRICE_BASIS,
        kind=TrendKind.TREND,
        direction="up",
        state=TrendState.COMPLETE,
        centers=(first, second),
        constituent_units=owned,
        start_tick=owned[0].start_tick,
        end_tick=owned[-1].end_tick,
        low_tick=min(item.low_tick for item in owned),
        high_tick=max(item.high_tick for item in owned),
        market_start=owned[0].market_start,
        market_end=owned[-1].market_end,
        confirmed_at=second.completed_at,
        available_at=second.available_at,
    )
    return (
        structure_for(first, second, completed_trends=(trend,)),
        first.completion_leave_unit,
        second.completion_leave_unit,
    )


class FixedStrength:
    def __init__(self, values):
        self.values = values

    def snapshot(self, item):
        area, peak, dif = self.values[item.unit_id]
        return StrengthSnapshot(
            unit_id=item.unit_id,
            direction=item.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd_native",
            available_at=item.available_at,
        )


def strength_pair(earlier, later, *, decayed):
    return FixedStrength({
        earlier.unit_id: (10.0, 5.0, 4.0),
        later.unit_id: (5.0, 2.0, 2.0) if decayed else (12.0, 6.0, 5.0),
    })


def only(values):
    result = tuple(values)
    assert len(result) == 1
    return result[0]


def test_completed_consolidation_emits_level_scoped_divergence_without_point_dependency():
    structure, earlier, later = completed_consolidation_fixture(level=0)
    values = collect_strict_divergences(
        structure,
        strength_pair(earlier, later, decayed=True),
    )
    item = only(values)
    assert item.kind == "consolidation"
    assert item.structural_level == 0
    assert item.compare_unit_id == earlier.unit_id
    assert item.signal_unit_id == later.unit_id
    assert item.confirmed_at == later.confirmed_at
    assert item.divergence_id


def test_completed_trend_emits_trend_divergence_at_its_recursive_level():
    structure, earlier, later = completed_trend_fixture(level=2)
    item = only(collect_strict_divergences(
        structure,
        strength_pair(earlier, later, decayed=True),
    ))
    assert item.kind == "trend"
    assert item.structural_level == 2


def test_non_divergent_comparison_is_not_formal_evidence():
    structure, earlier, later = completed_trend_fixture(level=1)
    assert collect_strict_divergences(
        structure,
        strength_pair(earlier, later, decayed=False),
    ) == ()
```

- [ ] **Step 2: 运行精确测试确认 RED**

```powershell
pytest tests/core/strict_structure/test_divergence_collector.py::test_completed_consolidation_emits_level_scoped_divergence_without_point_dependency -q
if ($LASTEXITCODE -eq 0) { throw 'RED expected: collector should not exist yet' }
```

- [ ] **Step 3: 扩展背驰证据身份和确认字段**

`strength.py` 增加 `MacdStrengthUnavailable(ValueError)`，只用于“该构件没有足够/对齐 MACD 样本”这一业务性证据不足；非有限数、方向错配、basis/时间冲突仍使用普通 `ValueError` 并向上失败。

将 `DivergenceEvidence` 改为：

```python
@dataclass(frozen=True, slots=True)
class DivergenceEvidence:
    divergence_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    kind: Literal["trend", "consolidation"]
    direction: Direction
    compare_unit_id: str
    signal_unit_id: str
    anchor_at: datetime
    anchor_tick: int
    confirmed_at: datetime
    available_at: datetime
    price_extreme_confirmed: bool
    histogram_area_decayed: bool
    histogram_peak_decayed: bool
    dif_extreme_decayed: bool
    strength_source: Literal["macd_htf", "macd_native"]
```

`__post_init__` 必须验证非负级别、source/basis、不同构件 ID、整数 anchor tick、四个布尔条件、带时区且 `anchor_at <= confirmed_at <= available_at`，并按上述 namespace 重算 `divergence_id` 后逐字匹配；`is_divergent` 仍要求四个条件全部为 true。

`compare_divergence()` 使用 `stable_structure_id("chanlun-strict-divergence/v3", basis, level, source_kind.value, kind, direction, earlier.unit_id, later.unit_id)`；`anchor_at=later.market_end`，向上锚定 `later.high_tick`，向下锚定 `later.low_tick`，`confirmed_at=later.confirmed_at`。正式比较要求两段都锁定，因此 `confirmed_at` 不得为空。

- [ ] **Step 4: 实现独立收集器**

新模块提供两个明确比较源并统一去重：

```python
def collect_strict_divergences(structure, strength):
    by_id = {}
    for level in structure.levels:
        for center in level.center_result.centers:
            pair = _consolidation_pair(center)
            if pair is not None:
                evidence = compare_divergence(*pair, strength, kind="consolidation")
                if evidence.is_divergent:
                    by_id.setdefault(evidence.divergence_id, evidence)
        for trend in level.completed_trends:
            pair = _trend_pair(trend)
            if pair is not None:
                evidence = compare_divergence(*pair, strength, kind="trend")
                if evidence.is_divergent:
                    by_id.setdefault(evidence.divergence_id, evidence)
    return tuple(sorted(
        by_id.values(),
        key=lambda item: (item.available_at, item.structural_level, item.kind, item.divergence_id),
    ))
```

`_consolidation_pair(center)` 只处理完成中枢：信号段为 `completion_leave_unit`，比较段为 `body_units` 中位于它之前、同方向、已经越出同一固定核心的最近离开段；两者相同则不比较。`_trend_pair(trend)` 只处理 `TrendKind.TREND + TrendState.COMPLETE`，复用现有 `_comparison_unit` 语义，信号段为末中心 `completion_leave_unit`。只有 `MacdStrengthUnavailable` 可被收集器按条跳过并产出零正式证据；几何、时间或 basis 冲突继续抛错，不能被宽泛 `except Exception` 吞掉。

- [ ] **Step 5: 把独立背驰纳入 CL 原子证据和 revision**

`StrictEvidenceResult` 增加 `divergences: tuple[DivergenceEvidence, ...] = ()`，验证 ID 唯一、basis 一致、级别存在、`confirmed_at/available_at <= source_closed_at`。`formal_inputs` 和 `build_strict_evidence_revision()` 都包含 divergences。

`CL` 增加：

```python
@_strict_runtime_locked
def get_strict_divergences(self):
    from chanlun.core.strict_structure.divergence import collect_strict_divergences
    from chanlun.core.strict_structure.strength import MacdStrengthProvider
    cached = self._strict_structure_memo.get("divergences")
    if cached is not None:
        return cached
    result = collect_strict_divergences(
        self.get_strict_structure_levels(),
        MacdStrengthProvider(self),
    )
    self._strict_structure_memo["divergences"] = result
    return result
```

`get_strict_evidence()` 在构造 revision 和 `StrictEvidenceResult` 时传入同一 tuple。已有一买/弱二买仍引用 `compare_divergence()` 返回的完整证据，不再创建另一种简化背驰对象。

- [ ] **Step 6: 运行 Task 3 精确测试和逐文件回归**

```powershell
$files = @(
  'tests/core/strict_structure/test_divergence_collector.py',
  'tests/core/strict_structure/test_strength.py',
  'tests/core/strict_structure/test_evidence_revision.py',
  'tests/core/strict_structure/test_first_class_points.py',
  'tests/core/strict_structure/test_second_class_points.py'
)
foreach ($file in $files) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}
```

预期：两类背驰都能在无买卖点依赖时输出；相同构件对 ID 稳定；未锁定或非背驰不进入正式集合。

- [ ] **Step 7: 独立复核并提交 Task 3**

```powershell
Select-String -Encoding UTF8 -Path 'src/chanlun/core/strict_structure/models.py' -Pattern 'divergence_id','structural_level','confirmed_at'
$old = git rev-parse HEAD
git add -- src/chanlun/core/strict_structure/divergence.py src/chanlun/core/strict_structure/models.py src/chanlun/core/strict_structure/strength.py src/chanlun/core/strict_structure/signals.py src/chanlun/core/strict_structure/identity.py src/chanlun/core/strict_structure/__init__.py src/chanlun/core/cl.py tests/core/strict_structure/test_divergence_collector.py tests/core/strict_structure/test_strength.py tests/core/strict_structure/test_evidence_revision.py tests/core/strict_structure/signal_helpers.py tests/core/strict_structure/test_first_class_points.py tests/core/strict_structure/test_second_class_points.py
git diff --cached --check
git commit -m '核心：输出分级盘整与趋势背驰证据' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
$new = git rev-parse HEAD
if ($new -eq $old) { throw 'commit did not advance HEAD' }
if ((git rev-parse pre) -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
```

---

### Task 4: v4 图表协议、分级显示菜单与配置迁移

**Files:**
- Modify: `src/chanlun/cl_utils/strict_chart.py`
- Modify: `src/chanlun/cl_utils/__init__.py`
- Modify: `src/chanlun/cl_utils/chart_config.py`
- Modify: `src/chanlun/file_db_mixins/cl_object_cache.py:25`
- Modify: `web/chanlun_chart/cl_app/services/chart_cache.py:232`
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py:644-675`
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts:820-850`
- Regenerate: `web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`
- Modify: `web/chanlun_chart/cl_app/static/js/chart_analysis.js`
- Modify: `web/chanlun_chart/cl_app/templates/index.html:296`
- Modify: `web/chanlun_chart/cl_app/templates/options.html:479-529,748-768`
- Modify: `tests/web/test_strict_chart_serializer.py`
- Modify: `tests/web/test_strict_chart_transport.py`
- Modify: `tests/web/test_tv_chart_strict_structure.py`
- Modify: `tests/web/test_qmt_strict_chart_production_pipeline.py`
- Modify: `tests/web/test_strict_chart_options.py`
- Modify: `tests/web/test_kline_recompute_price_basis.py`
- Modify: `tests/web/test_tv_chart_macd_chanlun_align.py`
- Modify: `tests/persistence/test_strict_cl_object_cache_namespace.py`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/cl_show_config_per_resolution.test.js`
- Create: `web/chanlun_chart/cl_app/static/js/__tests__/cl_display_menu_contract.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/chart_structure_reconcile.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/drawings_toggle_guard.test.js`

**Interfaces:**
- Consumes: v3 中枢/点/背驰原子证据和 Python `recursive_level_labels()`。
- Produces: `chanlun-chart-structure/v4`，每级包含 `structural_level/label/origin="current_chart_recursive"/centers/current_trends/completed_trend_snapshots/confirmed_points/divergences`；projection 和 approaching 仍可传输为观察扩展。
- Produces frontend config: `schema_version=2`；`center_all`、`trend_all`、`point_all`、`divergence_all` 仅 gate；逐级 keys 保存子偏好；独立周期画线继续使用单独 key。

- [ ] **Step 1: 写 v4 中心角色、状态和独立背驰序列化失败测试**

把该文件现有三段 `_center()` 改成 Task 1 的五段构造，显式导入共享的 `completed_up_center()`，并让 `_evidence(*, divergences=(), level_count=1)` 同时把同一个 divergence tuple 传给 `build_strict_evidence_revision()` 和 `StrictEvidenceResult`；当 `level_count > 1` 时补齐相同 basis 的空 `StrictLevelResult`。然后加入：

```python
def test_v4_center_payload_exposes_five_roles_and_completion_state():
    center = completed_up_center()
    payload = strict_center_to_chart_dict(center)
    assert payload["schema"] == "chanlun-chart-center/v4"
    assert payload["state"] == "completed"
    assert payload["entry_unit_id"] == center.entry_unit.unit_id
    assert payload["core_unit_ids"] == [item.unit_id for item in center.core_units]
    assert payload["initial_exit_unit_id"] == center.initial_exit_unit.unit_id
    assert payload["completion_leave_unit_id"] == center.completion_leave_unit.unit_id
    assert payload["completion_return_unit_id"] == center.completion_return_unit.unit_id


def test_v4_snapshot_groups_independent_divergences_by_level():
    item = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence/v3",
            PRICE_BASIS,
            1,
            SourceKind.TREND_TYPE.value,
            "trend",
            "up",
            "L1-earlier",
            "L1-later",
        ),
        structural_level=1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=PRICE_BASIS,
        kind="trend",
        direction="up",
        compare_unit_id="L1-earlier",
        signal_unit_id="L1-later",
        anchor_at=BASE + timedelta(minutes=30),
        anchor_tick=160,
        confirmed_at=BASE + timedelta(minutes=35),
        available_at=BASE + timedelta(minutes=35),
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=True,
        dif_extreme_decayed=True,
        strength_source="macd_native",
    )
    snapshot = build_strict_structure_snapshot(
        _evidence(divergences=(item,), level_count=2),
        interval="1m",
    )
    assert snapshot["schema"] == "chanlun-chart-structure/v4"
    assert [level["label"] for level in snapshot["levels"]] == ["1m", "5m"]
    assert {level["origin"] for level in snapshot["levels"]} == {"current_chart_recursive"}
    assert snapshot["levels"][1]["divergences"][0]["kind"] == "trend"
```

- [ ] **Step 2: 运行精确 serializer 测试确认 RED**

```powershell
pytest tests/web/test_strict_chart_serializer.py::test_v4_center_payload_exposes_five_roles_and_completion_state -q
if ($LASTEXITCODE -eq 0) { throw 'RED expected: v3 chart payload unexpectedly passed' }
```

- [ ] **Step 3: 实现 v4 原子图表快照并失效旧缓存**

`strict_chart.py` 固定：

```python
CHART_STRUCTURE_SCHEMA = "chanlun-chart-structure/v4"
CHART_CENTER_SCHEMA = "chanlun-chart-center/v4"
_ACTIVE_CENTER_STATES = frozenset({CenterState.ONGOING})
```

中心 `render_id` 使用 `center_id@body_revision@state`；进行中和完成都序列化真实核心，projection 只为进行中中心生成。新增 `strict_divergence_to_chart_dict()`：`render_kind="strict_divergence"`，包含 `divergence_id/kind/direction/structural_level/confirmed_at/metrics` 和单点 `points=[{time: anchor_at, price_tick: anchor_tick}]`。序列化前取 `labels = recursive_level_labels(interval)`，逐级验证 `0 <= level.structural_level < len(labels)`，然后写入 `label=labels[level.structural_level]` 和该级 `divergences`；证据出现目录外级别时直接拒绝，不能发明周/月标签。

同时升级：

```python
# src/chanlun/file_db_mixins/cl_object_cache.py
CL_OBJECT_SCHEMA_VERSION = "strict-v3"

# web/chanlun_chart/cl_app/services/chart_cache.py
_CHART_CACHE_SCHEMA_VERSION = "v40"
```

`chart_compute.py`、UDF TypeScript、`chart_analysis.js` 和 `charts.js` 只接受 v4。`strict_structure_mode="unavailable"` 时只显示稳定错误诊断：正式中枢、走势、点和背驰全部为空，不能回退读取 `xd_zss/recursive_levels/mmds/bcs`；基础 `fx/bi/xd` 与独立的笔中枢观察层不受影响。运行 `npm run build` 从 TypeScript 重建 bundle，禁止手改压缩文件。

- [ ] **Step 4: 写显示配置 schema、固定级别目录和 gate 失败测试**

扩展 `cl_show_config_per_resolution.test.js` 暴露 `recursiveDisplayLevels`、`normalizeClShowConfig` 和纯函数 `strictItemEnabled`，加入：

```javascript
test('四种图表周期只展示已确认的递归级别', () => {
  const { api } = loadClConfigApi();
  assert.deepEqual(Array.from(api.levels('1')).map((x) => x.label), ['1m', '5m', '30m', '日线']);
  assert.deepEqual(Array.from(api.levels('5')).map((x) => x.label), ['5m', '30m', '日线']);
  assert.deepEqual(Array.from(api.levels('30')).map((x) => x.label), ['30m', '日线']);
  assert.deepEqual(Array.from(api.levels('1D')).map((x) => x.label), ['日线']);
  assert.deepEqual(Array.from(api.levels('15')).map((x) => x.label), ['15m']);
});

test('总开关只 gate 不改写子项偏好', () => {
  const { api } = loadClConfigApi();
  const cfg = { ...api.DEFAULT, center_all: false, center_L1: true };
  assert.equal(api.enabled(cfg, { render_kind: 'formal_center', structural_level: 1 }), false);
  assert.equal(cfg.center_L1, true);
  cfg.center_all = true;
  assert.equal(api.enabled(cfg, { render_kind: 'formal_center', structural_level: 1 }), true);
});

test('盘整背驰和趋势背驰按级别独立 gate', () => {
  const { api } = loadClConfigApi();
  const cfg = { ...api.DEFAULT, divergence_consolidation_L2: false, divergence_trend_L2: true };
  assert.equal(api.enabled(cfg, { render_kind: 'strict_divergence', structural_level: 2, kind: 'consolidation' }), false);
  assert.equal(api.enabled(cfg, { render_kind: 'strict_divergence', structural_level: 2, kind: 'trend' }), true);
});

test('v1 显示偏好迁移幂等且不复活旧 key', () => {
  const { api } = loadClConfigApi();
  const legacy = {
    point_confirmed: false,
    center_L1: false,
    trend_L1: true,
    zs_all: true,
    bc_L1: false,
    point_approaching: true,
    recursive_layers: true,
  };
  const once = api.normalize(legacy, '1');
  const twice = api.normalize(once, '1');
  assert.deepEqual(twice, once);
  assert.equal(once.schema_version, 2);
  assert.equal(once.point_all, false);
  assert.equal(once.center_L1, false);
  assert.equal(once.trend_L1, true);
  assert.equal(once.trend_all, true);
  for (const oldKey of ['zs_all', 'bc_L1', 'point_approaching', 'recursive_layers']) {
    assert.equal(Object.hasOwn(once, oldKey), false);
  }
});
```

`cl_display_menu_contract.test.js` 读取真实 `charts.js`，断言标题顺序为“基础结构→中枢→走势类型→买卖点→背驰→画线设置”，且不存在“接近触发”或可操作 projection checkbox。

- [ ] **Step 5: 运行 Node 精确测试确认 RED**

```powershell
$file = 'web/chanlun_chart/cl_app/static/js/__tests__/cl_show_config_per_resolution.test.js'
$out = node --test --test-reporter=tap $file 2>&1
$code = $LASTEXITCODE
$out | ForEach-Object { $_ }
if ($code -eq 0) { throw 'RED expected: new display contract unexpectedly passed' }
```

- [ ] **Step 6: 实现显示配置 v2 和固定菜单结构**

`charts.js` 默认对象使用以下正式 keys；动态级别子 key 在 normalize 时按当前目录补齐：

```javascript
const CL_SHOW_DEFAULT = {
  schema_version: 2,
  fx: true,
  bi: true,
  xd: true,
  center_observation: true,
  center_all: true,
  trend_all: false,
  point_all: true,
  point_1buy: true,
  point_2buy: true,
  point_3buy: true,
  point_1sell: true,
  point_2sell: true,
  point_3sell: true,
  divergence_all: true,
};

function recursiveDisplayLevels(interval) {
  const key = String(interval || '').trim();
  const map = {
    '1': ['1m', '5m', '30m', '日线'],
    '1m': ['1m', '5m', '30m', '日线'],
    '5': ['5m', '30m', '日线'],
    '5m': ['5m', '30m', '日线'],
    '30': ['30m', '日线'],
    '30m': ['30m', '日线'],
    '1D': ['日线'],
    '1d': ['日线'],
    'D': ['日线'],
    'd': ['日线'],
  };
  const fallback = /^\d+$/.test(key) ? `${key}m` : (key || '当前周期');
  const labels = map[key] || [fallback];
  return labels.map((label, level) => ({ label, level }));
}
```

`normalizeClShowConfig()` 对当前目录每一级明确补齐：`center_Ln=true`、`trend_Ln=true`、`divergence_consolidation_Ln=true`、`divergence_trend_Ln=true`。新用户以 `trend_all=false` 保持当前默认不展示走势；开启总开关时即可恢复所有默认子项。迁移规则：`point_confirmed -> point_all`；`center_L*` 和 `trend_L*` 保留，旧 `xd_Ln` 仅在没有 `trend_Ln` 时迁入；旧配置没有 `trend_all` 时，若任一 `trend_L*`/`xd_L*` 为 true 则设为 true，否则保持 false；旧 `zs_* / mmd / bc_* / center_projection / point_approaching / recursive_layers` 删除；没有旧背驰子偏好的用户按上述 true 初始化。存储 key 仍为 `cl_show_config_<chartId>_<resolution>`。

函数签名固定为 `normalizeClShowConfig(config, interval)`；`loadClShowConfig/saveClShowConfig/_clShowConfigBaseline/resolveClConfigForResolution` 都必须把目标 resolution 传到底，不能再用运行时 `_recMaxLevel` 决定配置形状。切换周期时只保留目标目录允许的 level keys，原周期偏好仍留在其独立 localStorage key 中。

`_levelBarItems(interval)` 的画线调色板级别也改成直接遍历 `recursiveDisplayLevels(interval)`，返回长度严格等于允许目录；不得再用 `FREQ_CHAIN + _recMaxLevel` 补出 5m 周线、30m 月线或日线周/月/年标签。`FREQ_CHAIN` 只可继续服务传统笔/线段颜色锚点，不能再决定严格递归 UI。

菜单生成器必须输出以下精确顺序和 key，不允许再从旧 `zs/mmd/bc` 名称推导：

1. `基础结构`：`分型(fx)`、`笔(bi)`、`线段(xd)`、`笔中枢(center_observation)`。
2. `中枢`（组注“由当前 K 线递归产生”）：`中枢总开关(center_all)`，再按 `recursiveDisplayLevels(interval)` 顺序输出 `${label} 中枢(center_Ln)`。
3. `走势类型`（同一递归来源）：`走势类型总开关(trend_all)`，再按同一目录输出 `${label} 走势类型(trend_Ln)`。
4. `买卖点`：`买卖点总开关(point_all)`、`一买(point_1buy)`、`二买(point_2buy)`、`三买(point_3buy)`、`一卖(point_1sell)`、`二卖(point_2sell)`、`三卖(point_3sell)`。
5. `背驰`（同一递归来源）：`背驰总开关(divergence_all)`，每一级依次输出 `${label} 盘整背驰(divergence_consolidation_Ln)`、`${label} 趋势背驰(divergence_trend_Ln)`。
6. `画线设置`：`独立周期画线`，继续使用现有独立 key/checkbox ID。

“笔中枢”不受正式 `center_all` 影响；projection 随同级中心自动 gate；正式点只绘制 `point_confirmed`；approaching 不加入 `_strictRenderGroups()`。全选/全清继续 `.not('#' + indCbId)`，所以不会改变独立周期画线。

- [ ] **Step 7: 实现分级图形 gate 和虚实样式**

将 `_strictItemEnabled` 固定为：

```javascript
function strictItemEnabled(cfg, item) {
  const level = item.structural_level;
  if (item.render_kind === 'center_observation') return cfg.center_observation !== false;
  if (item.render_kind === 'formal_center' || item.render_kind === 'center_projection') {
    return cfg.center_all !== false && cfg[`center_L${level}`] !== false;
  }
  if (item.render_kind === 'strict_trend') {
    return cfg.trend_all !== false && cfg[`trend_L${level}`] !== false;
  }
  if (item.render_kind === 'point_confirmed') {
    return cfg.point_all !== false && cfg[`point_${item.point_type}`] !== false;
  }
  if (item.render_kind === 'strict_divergence') {
    return cfg.divergence_all !== false
      && cfg[`divergence_${item.kind}_L${level}`] !== false;
  }
  return false;
}
```

图表 manager 的实例方法只委托纯函数，保证生产路径和 Node 测试调用同一判断：

```javascript
_strictItemEnabled(item) {
  return strictItemEnabled(this.cl_show_config || {}, item);
}
```

正式中枢 `state==='ongoing'` 用 DASHED，`completed` 用 SOLID；走势 `forming` 用 DASHED，其余 SOLID。背驰用 signal anchor 创建 text，文案取所在 v4 level 的真实 label，显示为 `${levelLabel}·盘整背驰` 或 `${levelLabel}·趋势背驰`，同时保留 `structural_level`，方向决定上下颜色。reconcile 的 logical key 使用 `strict_divergence:<divergence_id>`。

从 `chart_config.py` 与 `options.html` 删除六个 `chart_show_strict_*`/observation 计算无关开关；Options 只保留传统基础结构配置，严格图层偏好只在图表菜单保存。`index.html` 的分析侧栏只保留 `data-chart-layer="bi"/"xd"` 两个基础画线按钮，删除 `bi_zs/xd_zs/bi_mmd/xd_mmd/recursive` 五个会重复写严格偏好的按钮；`chart_analysis.js::LAYER_CONFIG_KEYS` 同步只保留 `bi/xd`，删除全部 `recursive_layers/zs_*/mmd_*/bc_*` 读写。分析摘要仍只读 v4 `levels`，中枢和点的审计卡片继续显示，但不再反向覆盖图表菜单的逐级子偏好。

- [ ] **Step 8: 运行 Python、TypeScript 和逐文件 Node 回归**

```powershell
$pyFiles = @(
  'tests/web/test_strict_chart_serializer.py',
  'tests/web/test_strict_chart_transport.py',
  'tests/web/test_tv_chart_strict_structure.py',
  'tests/web/test_qmt_strict_chart_production_pipeline.py',
  'tests/web/test_strict_chart_options.py',
  'tests/web/test_kline_recompute_price_basis.py',
  'tests/web/test_tv_chart_macd_chanlun_align.py',
  'tests/persistence/test_strict_cl_object_cache_namespace.py'
)
foreach ($file in $pyFiles) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}

Push-Location 'web/chanlun_chart/cl_app/static/datafeeds/udf'
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'UDF build failed' }
Pop-Location

$jsFiles = @(
  'web/chanlun_chart/cl_app/static/js/__tests__/cl_show_config_per_resolution.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/cl_display_menu_contract.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_structure_reconcile.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/drawings_toggle_guard.test.js'
)
foreach ($file in $jsFiles) {
  $out = node --test --test-reporter=tap $file 2>&1
  $code = $LASTEXITCODE
  $text = [string]::Join("`n", $out)
  $out | ForEach-Object { $_ }
  if ($code -ne 0 -or $text -notmatch '# fail\s+0') { throw "Node failed: $file" }
  $match = [regex]::Match($text, '# pass\s+(\d+)')
  if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) { throw "TAP pass count missing: $file" }
  "PARSED_PASS=$($match.Groups[1].Value) FILE=$file"
}
```

- [ ] **Step 9: 独立复核协议版本、菜单文案和提交 Task 4**

```powershell
rg -n 'chanlun-chart-structure/v3|chanlun-chart-center/v3|_CHART_CACHE_SCHEMA_VERSION = "v39"|CL_OBJECT_SCHEMA_VERSION = "strict-v2"' src web/chanlun_chart/cl_app --glob '!**/node_modules/**'
if ($LASTEXITCODE -eq 0) { throw 'old chart/cache schema remains' }
rg -n 'recursive_layers|zs_L[0-9]+|xd_L[0-9]+|mmd_L[0-9]+|bc_L[0-9]+' web/chanlun_chart/cl_app/static/js/chart_analysis.js
if ($LASTEXITCODE -eq 0) { throw 'analysis sidebar still writes legacy strict preferences' }
Select-String -Encoding UTF8 -Path 'web/chanlun_chart/cl_app/static/js/charts.js' -Pattern '中枢总开关','走势类型总开关','买卖点总开关','背驰总开关','盘整背驰','趋势背驰','画线设置','独立周期画线'
$old = git rev-parse HEAD
git add -- src/chanlun/cl_utils/strict_chart.py src/chanlun/cl_utils/__init__.py src/chanlun/cl_utils/chart_config.py src/chanlun/file_db_mixins/cl_object_cache.py web/chanlun_chart/cl_app/services/chart_cache.py web/chanlun_chart/cl_app/services/chart_compute.py web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/static/js/chart_analysis.js web/chanlun_chart/cl_app/templates/index.html web/chanlun_chart/cl_app/templates/options.html tests/web/test_strict_chart_serializer.py tests/web/test_strict_chart_transport.py tests/web/test_tv_chart_strict_structure.py tests/web/test_qmt_strict_chart_production_pipeline.py tests/web/test_strict_chart_options.py tests/web/test_kline_recompute_price_basis.py tests/web/test_tv_chart_macd_chanlun_align.py tests/persistence/test_strict_cl_object_cache_namespace.py web/chanlun_chart/cl_app/static/js/__tests__/cl_show_config_per_resolution.test.js web/chanlun_chart/cl_app/static/js/__tests__/cl_display_menu_contract.test.js web/chanlun_chart/cl_app/static/js/__tests__/chart_structure_reconcile.test.js web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis.test.js web/chanlun_chart/cl_app/static/js/__tests__/drawings_toggle_guard.test.js
git diff --cached --check
git commit -m '图表：迁移分级显示菜单与严格结构协议' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
$new = git rev-parse HEAD
if ($new -eq $old) { throw 'commit did not advance HEAD' }
if ((git rev-parse pre) -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
```

---

### Task 5: TDX 880 无复权价格基准与板块质量门

**Files:**
- Modify: `src/chanlun/exchange/kline_precision.py`
- Modify: `src/chanlun/exchange/price_basis.py`
- Modify: `web/chanlun_chart/cl_app/services/trading_screening_gateway.py`
- Modify: `web/chanlun_chart/cl_app/services/trading_screening.py`
- Modify: `web/chanlun_chart/cl_app/templates/early_screening.html:32`
- Modify: `web/chanlun_chart/cl_app/static/js/early_screening_ui.js:1-20`
- Modify: `web/chanlun_chart/cl_app/static/js/early_screening.js`
- Create: `tests/exchange/test_tdx_industry_price_basis.py`
- Modify: `tests/exchange/test_price_basis.py`
- Modify: `tests/web/test_trading_screening_gateway.py`
- Modify: `tests/web/test_trading_screening_service.py`
- Modify: `tests/web/test_trading_screening_page.py`
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/early_screening_dashboard.test.js`

**Interfaces:**
- Produces: `resolve_tdx_industry_index_quantum(code) -> Decimal | None`；`build_tdx_industry_price_basis_metadata(code, quantum) -> PriceBasisMetadata`。
- Produces: `SectorAssessmentBatch(assessments, discovered_count, completed_count, failure_counts, errors)`；`completion_ratio` 是 Decimal。
- Consumes: `TradingScreeningConfig.min_scan_completion_ratio`，默认 `Decimal("0.80")`。
- Changes protocol: `trading_screening.py::SectorCatalogGateway.native_sector_assessments(...) -> SectorAssessmentBatch`，从同目录 gateway 模块导入该类型；gateway 不反向导入 service，因此不形成循环依赖。

- [ ] **Step 1: 写真实形状 TDX 880 元数据和 `fq=none` 失败测试**

`tests/exchange/test_tdx_industry_price_basis.py`：

```python
def test_tdx_880_price_basis_is_native_continuous_and_stable():
    quantum = resolve_tdx_industry_index_quantum("SH.880302")
    assert quantum == Decimal("0.01")
    first = build_tdx_industry_price_basis_metadata("SH.880302", quantum)
    second = build_tdx_industry_price_basis_metadata("SH.880302", quantum)
    assert first == second
    assert first.provider == "tdx-industry-index"
    assert first.adjustment == "none"
    assert first.price_basis_revision.startswith("sha256:")


def test_non_880_code_never_receives_tdx_industry_price_basis():
    assert resolve_tdx_industry_index_quantum("SH.600519") is None
    assert resolve_tdx_industry_index_quantum("SZ.880302") is None
```

先把 `test_trading_screening_gateway.py` 现有 fake 改成能够真实暴露本次根因；所有原有 `_gateway()` 调用点同步改为三元解包：

```python
def _frame(*, with_metadata=True) -> pd.DataFrame:
    frame = pd.DataFrame({  # 保留当前 date/open/high/low/close/volume 两行数据
        "date": pd.to_datetime(
            ["2026-07-20T10:00:00+08:00", "2026-07-20T10:01:00+08:00"]
        ),
        "open": [10.0, 10.1],
        "high": [10.2, 10.3],
        "low": [9.9, 10.0],
        "close": [10.1, 10.2],
        "volume": [1000, 1200],
    })
    if with_metadata:
        frame.attrs["structure_price_quantum"] = "0.01"
        frame.attrs["price_basis_revision"] = "test-raw-v1"
    return frame


class RecordingExchange:
    def __init__(self, frame=None) -> None:
        self.frame = _frame() if frame is None else frame
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def klines(self, code, frequency, *, args):
        assert args["req_counts"] == 4
        self.calls.append((code, frequency, dict(args)))
        return self.frame.copy(deep=True)


class RecordingAnalyzer:
    def __init__(self) -> None:
        self.calls = []
        self.frames = []

    def __call__(self, *, code, frequency, frame, as_of):
        self.calls.append((code, frequency))
        self.frames.append(frame.copy(deep=True))
        approaching = (
            (provisional_point("2buy"),)
            if code == "SZ.000001" and frequency == "5m"
            else ()
        )
        return FrameStructureAnalysis(
            closed_at=as_of,
            direction="neutral",
            confirmed_points=(),
            provisional_points=approaching,
        )


def _gateway(*, sector_frame=None, analyzer=None, sector_code="SH.880471"):
    stock_exchange = RecordingExchange()
    sector_exchange = RecordingExchange(sector_frame)
    analyzer = analyzer if analyzer is not None else RecordingAnalyzer()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_exchange_provider=lambda: sector_exchange,
        universe_provider=lambda _exchange: (
            {"type": "stock_cn", "code": "SZ.000001", "name": "平安银行"},
            {"type": "stock_cn", "code": "SH.600000", "name": "浦发银行"},
        ),
        sector_provider=lambda: {
            "source": "tdx_880_industry_index",
            "sectors": [{
                "sector_id": f"tdx-industry:{sector_code}",
                "name": "银行",
                "kline_code": sector_code,
                "member_codes": ["000001", "600000"],
            }],
        },
        watchlist_provider=lambda: ({"code": "SZ.000001"},),
        holdings_provider=lambda: ("SH.600000",),
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("30m", 4), ("5m", 4), ("1m", 4)),
            minimum_bars_by_frequency=(("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )
    return gateway, analyzer, sector_exchange
```

现有测试同步从 `assessments = gateway.native_sector_assessments(...)` 改成 `batch = ...; assessments = batch.assessments`，并继续断言原有板块评估内容。随后加入：

```python
def test_native_sector_loader_forces_none_and_attaches_metadata_before_analysis():
    analyzer = RecordingAnalyzer()
    gateway, analyzer, exchange = _gateway(
        sector_frame=_frame(with_metadata=False),
        analyzer=analyzer,
    )
    batch = gateway.native_sector_assessments(as_of=NOW)
    assert exchange.calls
    assert {call[2]["fq"] for call in exchange.calls} == {"none"}
    assert analyzer.frames[0].attrs["price_basis_provider"] == "tdx-industry-index"
    assert analyzer.frames[0].attrs["price_basis_adjustment"] == "none"
    assert analyzer.frames[0].attrs["structure_price_quantum"] == "0.01"
    assert batch.completed_count == batch.discovered_count == 1


def test_unknown_sector_code_fails_closed_before_strict_analysis():
    gateway, analyzer, _exchange = _gateway(
        sector_frame=_frame(with_metadata=False),
        sector_code="SZ.880471",
    )
    batch = gateway.native_sector_assessments(as_of=NOW)
    assert batch.discovered_count == 1
    assert batch.completed_count == 0
    assert batch.failure_counts == (("sector_price_basis_unavailable", 1),)
    assert analyzer.calls == []
```

- [ ] **Step 2: 运行精确测试确认 RED**

```powershell
pytest tests/web/test_trading_screening_gateway.py::test_native_sector_loader_forces_none_and_attaches_metadata_before_analysis -q
if ($LASTEXITCODE -eq 0) { throw 'RED expected: production-shaped TDX frame unexpectedly passed' }
```

- [ ] **Step 3: 实现 TDX 行业指数专属价格基准**

`kline_precision.py` 增加严格代码分类：

```python
_TDX_INDUSTRY_INDEX = re.compile(r"^SH\.880\d{3}$")


def resolve_tdx_industry_index_quantum(code: str) -> Decimal | None:
    return Decimal("0.01") if _TDX_INDUSTRY_INDEX.fullmatch(str(code)) else None
```

`price_basis.py` 增加专属 builder，revision payload 的 schema 固定 `chanlun-price-basis/tdx-industry-v1`，provider/market/code/adjustment/quantum 全部入哈希，ledger 固定空数组。保留现有公开 `build_price_basis_revision(...)` 的签名和 QMT revision，抽出的内部 helper 如下：

```python
def _build_price_basis_revision(
    *,
    schema: str,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    adjustment_ledger: Sequence[Mapping[str, object]],
) -> str:
    payload = {
        "schema": schema,
        "provider": provider,
        "market": market,
        "code": code,
        "adjustment": adjustment,
        "structure_price_quantum": _canonical_quantum(structure_price_quantum),
        "adjustment_ledger": list(adjustment_ledger),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_price_basis_revision(
    *,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    adjustment_ledger: Sequence[Mapping[str, object]],
) -> str:
    return _build_price_basis_revision(
        schema="chanlun-price-basis/qmt-v1",
        provider=provider,
        market=market,
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=adjustment_ledger,
    )


def build_tdx_industry_price_basis_metadata(
    code: str,
    structure_price_quantum: Decimal,
) -> PriceBasisMetadata:
    revision = _build_price_basis_revision(
        schema="chanlun-price-basis/tdx-industry-v1",
        provider="tdx-industry-index",
        market="a",
        code=code,
        adjustment="none",
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=(),
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider="tdx-industry-index",
        adjustment="none",
    )
```

在 `test_price_basis.py::test_qmt_basis_revision_is_order_independent_and_fact_sensitive` 增加 `assert first.price_basis_revision == "sha256:a506389b93ea7f8626c8d8f41c77032fc3cf83be8294de20c609fb4ff6a98dc9"`；这是当前固定 fixture 的实测 revision，重构后必须逐字节不变。不能重命名或删除公开 `build_price_basis_revision()`，避免破坏 QMT 和现有导入方。

- [ ] **Step 4: 在行业指数加载边界强制无复权并传播 attrs**

`NativeTradingDataGateway._load_analysis()` 增加 `native_sector_index: bool = False`。为 true 时：校验实际 `code` 为 SH.880xxx；loader args 加 `fq="none"`；loader 返回后调用专属 builder 和 `attach_price_basis_metadata()`；立即断言 `structure_price_quantum/price_basis_revision/price_basis_provider/price_basis_adjustment` 四个 attrs 齐全，再进入 `_closed_frame()`。无法解析代码或元数据时抛 `SectorAnalysisUnavailable("sector_price_basis_unavailable", ...)`。股票路径不加 fq、不补猜测元数据。

`_closed_frame()` 已有 `snapshot_attrs`，保留并补测试验证裁剪后 attrs 完全一致。`native_sector_assessments()` 调用 `_load_analysis(..., native_sector_index=True)`。

- [ ] **Step 5: 写板块错误批次和 0.80 质量门失败测试**

在 gateway 定义不可变批次：

```python
@dataclass(frozen=True, slots=True)
class SectorAnalysisFailure:
    sector_id: str
    code: str
    error_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class SectorAssessmentBatch:
    assessments: tuple[SectorAssessment, ...]
    discovered_count: int
    completed_count: int
    failure_counts: tuple[tuple[str, int], ...]
    errors: tuple[SectorAnalysisFailure, ...]

    @property
    def completion_ratio(self) -> Decimal:
        if self.discovered_count == 0:
            return Decimal("0")
        return Decimal(self.completed_count) / Decimal(self.discovered_count)


def _sector_failure_document(item: SectorAnalysisFailure) -> dict[str, str]:
    return {
        "sector_id": item.sector_id,
        "code": item.code,
        "error_type": item.error_type,
        "reason": item.reason[:160],
    }
```

所有 service fake catalog 都改为返回 `SectorAssessmentBatch`，不再返回裸 tuple。`RecordingSectorCatalog` 保存可变 `batch`，默认值为一个完整的 `eligible_sector()`；`members()` 保持当前实现。新增测试直接用现有 service 构造，不使用计划外 helper：

```python
class RecordingSectorCatalog:
    def __init__(self, batch=None):
        self.batch = batch or SectorAssessmentBatch(
            assessments=(eligible_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )

    def native_sector_assessments(self, *, as_of):
        del as_of
        return self.batch

    def members(self):
        return {eligible_sector().sector_id: ("SZ.000001",)}


def test_sector_infrastructure_failures_below_gate_keep_previous_snapshot(tmp_path):
    catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(eligible_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=RecordingPlanner(),
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    previous = service.refresh_now()
    successful = tuple(
        replace(eligible_sector(), sector_id=f"TDX.88030{index}")
        for index in range(7)
    )
    failures = tuple(
        SectorAnalysisFailure(
            sector_id=f"TDX.88039{index}",
            code=f"SH.88039{index}",
            error_type="sector_price_basis_unavailable",
            reason="missing strict price basis metadata",
        )
        for index in range(3)
    )
    catalog.batch = SectorAssessmentBatch(
        assessments=successful,
        discovered_count=10,
        completed_count=7,
        failure_counts=(("sector_price_basis_unavailable", 3),),
        errors=failures,
    )
    payload = service.refresh_now()
    assert payload["scan_state"] == "incomplete_not_published"
    assert payload["sectors"] == previous["sectors"]
    assert payload["signals"] == previous["signals"]
    assert payload["scan_audit"]["sector_completion_ratio"] == "0.7"
    assert payload["data_quality"]["failure_codes"] == ["sector_scan_completion_below_threshold"]


def test_business_ineligible_sectors_count_as_completed_and_can_publish_empty(tmp_path):
    catalog = RecordingSectorCatalog(
        SectorAssessmentBatch(
            assessments=(hostile_sector(),),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )
    )
    empty_planner = lambda **_kwargs: ScanPlan(
        sectors=(),
        symbols=(),
        symbol_frequencies=(),
        full_market_history_scan=False,
        background_full_refresh_required=False,
    )
    service = TradingScreeningService(
        market_data=RecordingMarketData(),
        sector_catalog=catalog,
        engine=RecordingEngine(),
        scan_planner=empty_planner,
        cache_path=tmp_path / "snapshot.json",
        clock=lambda: AS_OF,
        notifier=None,
    )
    payload = service.refresh_now()
    assert payload["scan_state"] == "complete"
    assert payload["scan_audit"]["sector_completion_ratio"] == "1"
    assert payload["scan_audit"]["selected_sector_count"] == 0
```

- [ ] **Step 6: 实现稳定错误分类和板块质量门**

Gateway 定义单一带稳定 code 的边界异常，禁止 `native_sector_assessments()` 再从易变报错文案猜类型：

```python
class SectorAnalysisUnavailable(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
```

映射顺序固定：代码/价格基准附着失败，或调用 analyzer 前显式执行 `strict_snapshot_price_metadata(frame)` 失败→`sector_price_basis_unavailable`；空/短/过期行情→`sector_kline_unavailable`；元数据已验证后 analyzer 的严格结构几何/basis 冲突→`sector_structure_invalid`；exchange/adapter 其他异常→`sector_adapter_error`。`_load_analysis()` 在每个已知边界包装为 `SectorAnalysisUnavailable`，最外层只读取 `exc.code`；未知异常固定为 adapter error。服务端使用 `LogUtil.error` 记录 sector/frequency/provider/adjustment/error_type/raw message，对外 failure reason 截断且无堆栈。

`SectorAssessmentBatch.__post_init__` 将四个集合字段 tuple 化，验证 `0 <= completed_count <= discovered_count`、error sector ID 唯一、`failure_counts` 按 code 排序且其总数等于 `len(errors)`。`native_sector_assessments()` 返回该批次：业务结构不合格的 `SectorAssessment` 计入 completed；基础设施异常可以生成 `hard_block=True` 的展示行，但不计 completed，并进入 errors/failure_counts。达到质量门后，`rank_sectors()` 只接收 `assessment.sector_id not in {error.sector_id ...}` 的完成项，不能让基础设施失败行参与排名。

`TradingScreeningService._perform_incremental_refresh()` 在 `rank_sectors()` 前执行：

```python
sector_batch = self._sector_catalog.native_sector_assessments(as_of=as_of)
sector_ratio = sector_batch.completion_ratio
sector_audit = {
    "sector_discovered_count": sector_batch.discovered_count,
    "sector_completed_count": sector_batch.completed_count,
    "sector_failed_count": sector_batch.discovered_count - sector_batch.completed_count,
    "sector_completion_ratio": str(sector_ratio),
    "sector_failure_counts": dict(sector_batch.failure_counts),
}
if sector_ratio < self._config.min_scan_completion_ratio:
    failed = copy.deepcopy(dict(previous))
    failed["scan_state"] = "incomplete_not_published"
    failed["scan_audit"] = {**dict(failed.get("scan_audit", {})), **sector_audit}
    failed["data_quality"] = {
        "complete": False,
        "stale": True,
        "failure_codes": ["sector_scan_completion_below_threshold"],
    }
    failed["errors"] = [_sector_failure_document(item) for item in sector_batch.errors]
    return failed
assessments = sector_batch.assessments
```

达到门槛后才排名、选板块、取 members、建股票 plan。发布 payload 的 `scan_audit` 合并 sector_audit；有部分板块失败时 `data_quality.complete=False` 但可发布，failure_codes 包含 `sector_scan_partial`。

- [ ] **Step 7: 升级选股 v2 页面协议**

`SCHEMA_VERSION="chanlun-trading-screening/v2"`，`TradingScreeningConfig.structure_version="v2"`。更新模板 data-schema、`early_screening_ui.js` 常量和 tests。页面状态区展示板块发现/完成/失败/成功率；`incomplete_not_published` 明确显示“本轮板块结构质量不足，保留上一快照”，不能显示为有效 0 候选。

- [ ] **Step 8: 运行 Task 5 Python 和逐文件 Node 回归**

```powershell
$pyFiles = @(
  'tests/exchange/test_tdx_industry_price_basis.py',
  'tests/exchange/test_price_basis.py',
  'tests/web/test_trading_screening_gateway.py',
  'tests/web/test_trading_screening_service.py',
  'tests/web/test_trading_screening_page.py'
)
foreach ($file in $pyFiles) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}

$file = 'web/chanlun_chart/cl_app/static/js/__tests__/early_screening_dashboard.test.js'
$out = node --test --test-reporter=tap $file 2>&1
$code = $LASTEXITCODE
$text = [string]::Join("`n", $out)
$out | ForEach-Object { $_ }
if ($code -ne 0 -or $text -notmatch '# fail\s+0') { throw "Node failed: $file" }
$match = [regex]::Match($text, '# pass\s+(\d+)')
if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) { throw 'TAP pass count missing' }
"PARSED_PASS=$($match.Groups[1].Value)"
```

- [ ] **Step 9: 独立复核并提交 Task 5**

```powershell
Select-String -Encoding UTF8 -Path 'web/chanlun_chart/cl_app/services/trading_screening_gateway.py' -Pattern 'fq.*none','SectorAssessmentBatch','sector_price_basis_unavailable'
Select-String -Encoding UTF8 -Path 'web/chanlun_chart/cl_app/services/trading_screening.py' -Pattern 'chanlun-trading-screening/v2','sector_completion_ratio','incomplete_not_published'
$old = git rev-parse HEAD
git add -- src/chanlun/exchange/kline_precision.py src/chanlun/exchange/price_basis.py web/chanlun_chart/cl_app/services/trading_screening_gateway.py web/chanlun_chart/cl_app/services/trading_screening.py web/chanlun_chart/cl_app/templates/early_screening.html web/chanlun_chart/cl_app/static/js/early_screening_ui.js web/chanlun_chart/cl_app/static/js/early_screening.js tests/exchange/test_tdx_industry_price_basis.py tests/exchange/test_price_basis.py tests/web/test_trading_screening_gateway.py tests/web/test_trading_screening_service.py tests/web/test_trading_screening_page.py web/chanlun_chart/cl_app/static/js/__tests__/early_screening_dashboard.test.js
git diff --cached --check
git commit -m '选股：接通行业指数价格基准与质量门' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
$new = git rev-parse HEAD
if ($new -eq $old) { throw 'commit did not advance HEAD' }
if ((git rev-parse pre) -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
```

---

### Task 6: 全量回归、真实 9900 验收与敌对自审

**Files:**
- No planned code changes. Any failure returns to the owning task, adds a failing regression test, fixes minimally, reruns that task, and creates a Chinese fix commit with the required trailer.

**Interfaces:**
- Consumes: Tasks 1–5 的五个已提交单元。
- Produces: 可复核测试日志、真实 TDX/页面快照证据和最终变更边界报告。

- [ ] **Step 1: 逐文件运行全部严格核心测试**

```powershell
$files = @(rg --files tests/core/strict_structure | Where-Object { $_ -like 'test_*.py' } | Sort-Object)
foreach ($file in $files) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}
```

预期：每个文件单独 PASS；真实前缀测试非空要求不被取消；无未来时间、批量/增量和 identity 稳定测试全部通过。

- [ ] **Step 2: 逐文件运行所有受影响 Web Python 测试**

```powershell
$files = @(
  'tests/web/test_strict_chart_serializer.py',
  'tests/web/test_strict_chart_transport.py',
  'tests/web/test_tv_chart_strict_structure.py',
  'tests/web/test_chart_strict_runtime_wiring.py',
  'tests/web/test_strict_chart_runtime.py',
  'tests/web/test_qmt_strict_chart_production_pipeline.py',
  'tests/web/test_trading_screening_gateway.py',
  'tests/web/test_trading_screening_service.py',
  'tests/web/test_trading_screening_page.py',
  'tests/web/test_source_fingerprint_coverage.py'
)
foreach ($file in $files) {
  pytest $file -q
  if ($LASTEXITCODE -ne 0) { throw "pytest failed: $file" }
}
```

- [ ] **Step 3: 逐文件运行受影响 JavaScript TAP 测试**

```powershell
$files = @(
  'web/chanlun_chart/cl_app/static/js/__tests__/cl_show_config_per_resolution.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/cl_display_menu_contract.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_structure_reconcile.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/drawings_toggle_guard.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/early_screening_dashboard.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/sidebar_accessibility.test.js'
)
foreach ($file in $files) {
  $out = node --test --test-reporter=tap $file 2>&1
  $code = $LASTEXITCODE
  $text = [string]::Join("`n", $out)
  $out | ForEach-Object { $_ }
  if ($code -ne 0 -or $text -notmatch '# fail\s+0') { throw "Node failed: $file" }
  $match = [regex]::Match($text, '# pass\s+(\d+)')
  if (-not $match.Success -or [int]$match.Groups[1].Value -lt 1) { throw "TAP pass count missing: $file" }
  "PARSED_PASS=$($match.Groups[1].Value) FILE=$file"
}
```

- [ ] **Step 4: 重启 9900 并验证真实行业指数链路**

先用项目既有安全启动/停止方式重启服务；不得杀死不相关 Python 进程。然后在服务端日志和一次真实 `SH.880302` 30m/5m 分析中验证：

```text
request args.fq = none
price_basis_provider = tdx-industry-index
price_basis_adjustment = none
structure_price_quantum = 0.01
price_basis_revision = sha256:<64 hex>
strict metadata survives _closed_frame
```

读取 `D:\chanlun_pro\decision_support\trading_screening_snapshot.json`，断言 schema v2、structure v2、板块 discovered/completed/failed/ratio 字段存在，且不再出现“81 个元数据失败却 complete”的状态。若最终板块或股票为空，必须逐条确认是业务原因且质量门已通过。

- [ ] **Step 5: 用已登录浏览器验收四种 K 线菜单和图形**

使用 `browser:control-in-app-browser` 打开实际页面并验证：

1. 1m 菜单和画线调色板只有 1m/5m/30m/日线；5m 只有 5m/30m/日线；30m 只有 30m/日线；日线只有日线。
2. 分组顺序、中文名称、四个总开关、六类点、逐级盘整/趋势背驰、独立周期画线完全一致。
3. 关闭总开关再打开，子项偏好不变；全清不改变独立周期画线。
4. approaching 不作为正式图表点显示；projection 没有菜单开关但随中心自动出现。
5. 抽查一个 ongoing 中心为虚线、一个 completed 中心为实线；payload 中五段角色、核心、三类点与图形一致。
6. 提前选股页面显示可信板块成功率；达到门槛后确实进入排名和股票扫描。

- [ ] **Step 6: 最终敌对自审和 Git 边界核对**

```powershell
$legacy = rg -n 'CenterState\.(ESTABLISHED|EXTENDING|BREAKOUT_PENDING|DESTROYED|CLOSED_OTHER)|\.(seed_units|leave_unit|return_unit|destroyed_at|extension_pairs|z_units)\b' src/chanlun/core/strict_structure tests/core/strict_structure
if ($LASTEXITCODE -eq 0) { $legacy | ForEach-Object { $_ }; throw 'legacy three-unit center contract remains' }
$head = git rev-parse HEAD
$pre = git rev-parse pre
if ($pre -ne 'be2245d681ed132cd573e00c1cee73101aabea52') { throw 'pre moved' }
git log --oneline --decorate be2245d681ed132cd573e00c1cee73101aabea52..HEAD
git status --short
git diff --check be2245d681ed132cd573e00c1cee73101aabea52..HEAD
```

逐项反问并记录证据：是否有三段成枢残留；是否有周/月标签冒出；是否用原生高周期冒充递归；是否让笔中枢/approaching 进入交易；是否把部分/全部板块基础设施失败发布成合法空结果；是否错误复权行业指数；是否夹带或丢弃用户既有改动。

完成后再使用 `superpowers:verification-before-completion` 和 `superpowers:requesting-code-review`，取得独立审查结果后才可向用户声称完成。
