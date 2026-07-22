# Five-Segment Center Render Boundary Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every five-segment center rectangle start at U2 and end at the latest accepted in-core return, excluding U1, all leaving segments, and third-class completion returns.

**Architecture:** Add two derived, read-only render-boundary properties to `TrendCenter`, where all lifecycle roles are already authoritative. Reuse those properties in the strict center body serializer and active projection serializer, and advance the render-revision namespace so an already-open chart treats the new coordinates as a new render generation.

**Tech Stack:** Python 3.10 dataclasses, pytest, strict chart v4 payloads, Node.js built-in test runner, TradingView chart runtime, Playwright CLI.

## Global Constraints

- Work directly on the current branch and workspace; do not create a worktree or alter `pre`.
- Use PowerShell for commands. Run Node tests one file at a time with `--test-reporter=tap` and parse `# pass N` / `# fail N`.
- Follow RED → GREEN for every production behavior change; run exact pytest test names before whole files.
- Preserve all unrelated dirty and untracked files; stage only the files named by each task.
- Do not change `[ZD, ZG]`, five-segment establishment rules, U1/U5 overlap rules, third-class completion, colors, recursive labels, or payload schema v4.
- Every verified implementation unit gets a Chinese commit with trailer `Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Do not push, merge, discard, or move the `pre` branch.
- After every edit, independently read the changed anchors; after every commit, independently verify HEAD, commit files, trailer, and dirty state.

---

## File Map

- `src/chanlun/core/strict_structure/models.py`: owns authoritative center roles and exposes derived core-body render boundaries.
- `tests/core/strict_structure/test_models.py`: proves initial, extended, pending-leave, and completed boundary semantics.
- `src/chanlun/cl_utils/strict_chart.py`: serializes body rectangles/projections and owns the render-revision namespace.
- `tests/web/test_strict_chart_serializer.py`: proves formal and observation rectangles plus projections consume the model boundaries.
- `web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js`: unchanged regression gate for geometry replacement without duplicate shapes.
- `web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js`: unchanged regression gate for final rectangle colors/styles.

---

### Task 1: Authoritative Core-Body Time Properties

**Files:**
- Modify: `src/chanlun/core/strict_structure/models.py:319-342`
- Test: `tests/core/strict_structure/test_models.py:8-20, 191-213`

**Interfaces:**
- Consumes: `TrendCenter.core_units`, `pending_leave_unit`, `completion_leave_unit`, and `body_units`.
- Produces: `TrendCenter.core_body_start_market_time -> datetime` and `TrendCenter.core_body_end_market_time -> datetime`.

- [ ] **Step 1: Write failing model tests**

Add the import:

```python
from chanlun.core.strict_structure.center_machine import advance_center
```

Add these tests before the `CenterEvidence` test:

```python
def test_center_core_body_time_excludes_entry_and_leaving_units():
    ongoing = ongoing_center()
    completed = completed_up_center()

    assert (
        ongoing.core_body_start_market_time
        == ongoing.core_units[0].market_start
    )
    assert (
        ongoing.core_body_end_market_time
        == ongoing.initial_exit_unit.market_start
    )
    assert (
        completed.core_body_end_market_time
        == completed.completion_leave_unit.market_start
    )


def test_center_core_body_end_advances_only_after_an_accepted_reentry():
    initial = ongoing_center()
    reentry = unit(
        5,
        "down",
        initial.initial_exit_unit.end_tick,
        initial.zd_tick + 5,
    )
    extended, _event = advance_center(initial, reentry)

    assert extended.pending_leave_unit is None
    assert extended.core_body_end_market_time == reentry.market_end

    next_leave = unit(
        6,
        "up",
        reentry.end_tick,
        initial.zg_tick + 15,
    )
    leaving, _event = advance_center(extended, next_leave)

    assert leaving.pending_leave_unit is next_leave
    assert leaving.core_body_end_market_time == next_leave.market_start
```

- [ ] **Step 2: Run exact tests and verify RED**

Run separately:

```powershell
python -m pytest tests\core\strict_structure\test_models.py::test_center_core_body_time_excludes_entry_and_leaving_units -q
python -m pytest tests\core\strict_structure\test_models.py::test_center_core_body_end_advances_only_after_an_accepted_reentry -q
```

Expected: both fail with `AttributeError` for the missing `core_body_*_market_time` properties, not fixture or construction errors.

- [ ] **Step 3: Implement the minimal derived properties**

Insert after `core_units`:

```python
    @property
    def core_body_start_market_time(self) -> datetime:
        return self.core_units[0].market_start

    @property
    def core_body_end_market_time(self) -> datetime:
        leave = self.completion_leave_unit or self.pending_leave_unit
        if leave is not None:
            return leave.market_start
        return self.body_units[-1].market_end
```

Do not mutate `body_start_market_time`, `last_touch_market_time`, `body_units`, identities, or evidence fields.

- [ ] **Step 4: Verify GREEN and the model regression file**

Run each exact test again, then:

```powershell
python -m pytest tests\core\strict_structure\test_models.py -q
```

Expected: both exact tests pass and the whole file reports zero failures.

- [ ] **Step 5: Verify and commit Task 1**

Run:

```powershell
git diff --check -- src/chanlun/core/strict_structure/models.py tests/core/strict_structure/test_models.py
git diff -- src/chanlun/core/strict_structure/models.py tests/core/strict_structure/test_models.py
```

Stage only the two Task 1 files and commit:

```powershell
git add -- src/chanlun/core/strict_structure/models.py tests/core/strict_structure/test_models.py
git commit -m "中枢：定义核心本体时间边界" -m "Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Expected: HEAD advances once and only the two target files appear in the commit.

---

### Task 2: Serialize Core-Body Geometry and Projection

**Files:**
- Modify: `src/chanlun/cl_utils/strict_chart.py:44-112, 143-179, 568-572`
- Test: `tests/web/test_strict_chart_serializer.py:220-334`

**Interfaces:**
- Consumes: `TrendCenter.core_body_start_market_time` and `TrendCenter.core_body_end_market_time` from Task 1.
- Produces: v4 center `points` whose time range excludes entry/leaving segments; ongoing projections beginning at the same body right edge; a `chanlun-chart-render/v5` revision generation.

- [ ] **Step 1: Write failing serializer tests**

Replace the time assertions in `test_chart_times_are_utc_epoch_seconds_and_reject_naive_datetime` with:

```python
    assert payload["points"][0]["time"] == int(
        center.core_units[0].market_start.timestamp()
    )
    assert payload["points"][1]["time"] == int(
        center.initial_exit_unit.market_start.timestamp()
    )
```

Keep the timezone rejection, but use `center.core_units[0].market_start.replace(tzinfo=None)` as its input.

Change the active projection test to an unextended center and assert a continuous boundary:

```python
def test_active_projection_starts_at_core_body_end() -> None:
    center = _center()
    source_closed_at = BASE + timedelta(hours=6)

    body = strict_center_to_chart_dict(center)
    projection = active_center_projection_to_chart_dict(
        center,
        source_closed_at,
    )

    expected_end = int(center.initial_exit_unit.market_start.timestamp())
    assert body["points"][1]["time"] == expected_end
    assert projection["points"][0]["time"] == expected_end
    assert projection["render_kind"] == "center_projection"
    assert projection["tradable"] is False
    assert projection["points"][1]["time"] == int(source_closed_at.timestamp())
    assert projection["core"] == body["core"]
```

Extend the observation test with:

```python
    assert payload["points"][0]["time"] == int(
        _center(
            source_kind=SourceKind.STROKE_OBSERVATION
        ).core_units[0].market_start.timestamp()
    )
```

Add a completed-center regression:

```python
def test_completed_center_body_stops_before_leave_and_completion_return() -> None:
    center = completed_up_center()
    payload = strict_center_to_chart_dict(center)

    assert payload["points"][1]["time"] == int(
        center.completion_leave_unit.market_start.timestamp()
    )
    assert payload["points"][1]["time"] < int(
        center.completion_return_unit.market_end.timestamp()
    )
    assert payload["completed_at"] == int(center.completed_at.timestamp())
```

- [ ] **Step 2: Run exact serializer tests and verify RED**

Run each named test separately:

```powershell
python -m pytest tests\web\test_strict_chart_serializer.py::test_chart_times_are_utc_epoch_seconds_and_reject_naive_datetime -q
python -m pytest tests\web\test_strict_chart_serializer.py::test_active_projection_starts_at_core_body_end -q
python -m pytest tests\web\test_strict_chart_serializer.py::test_completed_center_body_stops_before_leave_and_completion_return -q
```

Expected: each geometry test fails because the old serializer returns U1/U5 times. No test may fail from a missing fixture or invalid center.

- [ ] **Step 3: Implement minimal serializer changes**

In `_center_payload`, replace the two time sources:

```python
                "time": aware_datetime_to_epoch_seconds(
                    center.core_body_start_market_time
                ),
```

and:

```python
                "time": aware_datetime_to_epoch_seconds(
                    center.core_body_end_market_time
                ),
```

In `active_center_projection_to_chart_dict`, replace `last_touch_market_time` with:

```python
    touched_epoch = aware_datetime_to_epoch_seconds(
        center.core_body_end_market_time
    )
```

Keep the source-close ordering check, but change its error message to `source close cannot precede center core body end`.

Advance only the render-revision namespace:

```python
        "chanlun-chart-render/v5",
```

Do not change `CHART_CENTER_SCHEMA`, `CHART_STRUCTURE_SCHEMA`, price points, role fields, or completion fields.

- [ ] **Step 4: Verify GREEN and serializer regressions**

Run the three exact tests again, then:

```powershell
python -m pytest tests\web\test_strict_chart_serializer.py -q
```

Expected: all exact tests pass and the serializer file reports zero failures.

- [ ] **Step 5: Run frontend geometry gates one file at a time**

Run:

```powershell
node --test --test-reporter=tap web\chanlun_chart\cl_app\static\js\__tests__\strict_center_reconcile_integration.test.js
node --test --test-reporter=tap web\chanlun_chart\cl_app\static\js\__tests__\charts_integration.test.js
```

Expected structured summaries:

- `strict_center_reconcile_integration.test.js`: `# pass 9`, `# fail 0`.
- `charts_integration.test.js`: `# pass 46`, `# fail 0`.

- [ ] **Step 6: Verify and commit Task 2**

Run:

```powershell
git diff --check -- src/chanlun/cl_utils/strict_chart.py tests/web/test_strict_chart_serializer.py
git diff -- src/chanlun/cl_utils/strict_chart.py tests/web/test_strict_chart_serializer.py
```

Stage only the two Task 2 files and commit:

```powershell
git add -- src/chanlun/cl_utils/strict_chart.py tests/web/test_strict_chart_serializer.py
git commit -m "图表：按核心三段绘制中枢本体" -m "Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Expected: HEAD advances once and only the two target files appear in the commit.

---

### Task 3: Restart and Verify SH.513100 in the Real Chart

**Files:**
- Modify: none
- Test: live `http://127.0.0.1:9900`, authenticated Playwright session `chanlun-debug`

**Interfaces:**
- Consumes: Task 1 model boundaries and Task 2 v4 payload geometry.
- Produces: runtime evidence that the deployed chart matches the approved U2–U4 rule without regressing data visibility or color.

- [ ] **Step 1: Re-run post-commit Python gates**

Run:

```powershell
python -m pytest tests\core\strict_structure\test_models.py -q
python -m pytest tests\web\test_strict_chart_serializer.py -q
```

Expected: both files pass with zero failures after the commits exist.

- [ ] **Step 2: Restart only the verified chart process**

Resolve the Python process whose command line exactly contains `D:\project\chanlun-pro\web\chanlun_chart\app.py nobrowser`, stop that PID, independently verify port 9900 has no listener, and start:

```powershell
Start-Process -FilePath 'D:\software\Python310\python.exe' -ArgumentList @('D:\project\chanlun-pro\web\chanlun_chart\app.py','nobrowser') -WorkingDirectory 'D:\project\chanlun-pro' -WindowStyle Hidden -PassThru
```

Poll for at most 30 seconds. Expected: exactly one matching Python process and HTTP 200 from both `/livez` and `/readyz`.

- [ ] **Step 3: Verify the real five-segment payload independently**

Build the strict chart runtime from cached `SH.513100 / 5m` QMT bars and print the latest center. Expected:

```text
center_id=bf719e9493faa454a66a47a667cb3bcd7486890472a1fd79bfe35cb8f36344c9
core=2200..2214
core_body_start=2026-06-26T10:35:00+08:00
core_body_end=2026-07-01T11:20:00+08:00
state=completed
completion_direction=down
```

Cross-check that all five initial units retain positive-width overlap with `[2200, 2214]` and that `completed_at` remains `2026-07-15T10:40:00+08:00`.

- [ ] **Step 4: Verify the browser payload and shape**

Reload Playwright session `chanlun-debug`, set the active chart resolution to `5`, and wait until the strict status is `ready`. Inspect the latest formal center and its TradingView shape.

Expected:

```text
symbol=a:SH.513100
interval=5
bars=11520 or greater
visible range intersects loaded bars=true
formal center core=2.200..2.214
formal center points=2026-06-26 10:35 .. 2026-07-01 11:20
formal center color=#FF0000
linewidth=2
linestyle=0
center_projections=0
console errors after reload=0
```

Capture a screenshot in `.playwright-cli` and visually confirm K lines, volume, MACD, and the shortened red rectangle.

- [ ] **Step 5: Final repository and service evidence**

Run independently:

```powershell
git rev-parse --short HEAD
git log -3 --format='%h %s'
git status --short
```

Expected: both implementation commits are present; only pre-existing unrelated dirty files remain unstaged/uncommitted. Recheck `/livez` and `/readyz` are both 200. Do not push.
