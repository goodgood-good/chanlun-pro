# Through-Core Center Transition Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the strict center state machine so a return segment that crosses the whole core becomes the new opposite-direction leaving segment, eliminating historical `ongoing` centers and restoring the missed third-class points for `SH.513100`.

**Architecture:** Keep the existing five-unit seed, `TrendCenter` model, scanner, serializer, and chart schema. Change only the transition that handles an existing `pending_leave_unit`: when the accepted return also ends outside the opposite side of `[ZD, ZG]`, append it to the body and preserve it as the new pending leave. Lock the semantics with symmetric transition tests, scanner ownership/invariant tests, and recursive trend coverage before validating real QMT-derived 1m/5m/30m output.

**Tech Stack:** Python 3.10 dataclasses, pytest, strict recursive Chanlun structure engine, Flask chart service, Node.js built-in test runner, TradingView chart runtime, Playwright CLI.

## Global Constraints

- Work in the current `codex/recursive-five-segment-screening` checkout; do not create a worktree.
- Use PowerShell only. Run every pytest target as a separate process and every Node test file separately with `--test-reporter=tap`.
- Follow RED -> GREEN: prove the new symmetric transition, scan semantics, and recursive ownership assertions fail before changing production code.
- Completion remains third-class-point-only. Do not add timeout completion, `abandoned`, UI filtering, or a new lifecycle state.
- Keep five-unit establishment, U1/U5 positive overlap, `[ZD, ZG]`, boundary inclusion, event/schema versions, and frontend styles unchanged.
- A crossing return remains a body/extension unit. If it exits the opposite side, it becomes `pending_leave_unit` and emits the corresponding breakout-watch event.
- Each structural level may expose at most one `ongoing` formal center, and that center may exist only at the locked tail.
- Source fingerprint invalidation is sufficient; do not manually delete caches or bump the chart schema.
- Preserve all unrelated dirty and untracked files. Stage only files named by the current task.
- Every verified implementation unit gets a Chinese commit with trailer `Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Do not push, merge, discard, or alter `pre`.
- After every edit, independently read the changed anchors. After every commit, independently verify HEAD, committed paths, trailer, and dirty state.

---

## Task 1: Lock the corrected transition and scanner semantics in RED tests

**Files:**

- Modify: `tests/core/strict_structure/test_center_transitions.py`
- Modify: `tests/core/strict_structure/test_center_scan.py`
- Modify: `tests/core/strict_structure/test_recursive_engine.py`

- [ ] **Step 1: Add the symmetric transition tests**

Append these tests to `test_center_transitions.py`:

```python
def test_return_crossing_core_becomes_opposite_down_leave_then_completes():
    value = _ongoing_up_center()
    crossed = unit(5, "down", 130, 95)
    pending, watch = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is crossed
    assert pending.extension_units == (crossed,)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_DOWN

    ret = unit(6, "up", 95, 100)
    completed, completion = advance_center(pending, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_direction == "down"
    assert completed.completion_leave_unit is crossed
    assert completed.completion_return_unit is ret
    assert completion.kind is CenterEventKind.COMPLETED_DOWN


def test_return_crossing_core_becomes_opposite_up_leave_then_completes():
    value = _ongoing_down_center()
    crossed = unit(5, "up", 80, 115)
    pending, watch = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is crossed
    assert pending.extension_units == (crossed,)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP

    ret = unit(6, "down", 115, 110)
    completed, completion = advance_center(pending, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_direction == "up"
    assert completed.completion_leave_unit is crossed
    assert completed.completion_return_unit is ret
    assert completion.kind is CenterEventKind.COMPLETED_UP
```

- [ ] **Step 2: Replace the two abandoned-center scanner expectations**

Add a shared helper in `test_center_scan.py`:

```python
def _direction_flip_then_later_center():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
        unit(7, "down", 100, 96),
        unit(8, "up", 96, 99),
        unit(9, "down", 99, 97),
        unit(10, "up", 97, 105),
        unit(11, "down", 105, 101),
    )
```

Replace `test_scan_preserves_ongoing_center_when_later_geometry_cannot_extend` with:

```python
def test_scan_completes_after_through_core_direction_flip():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
    )
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.completion_direction == "down"
    assert center.completion_leave_unit is values[5]
    assert center.completion_return_unit is values[6]
```

Replace `test_scan_can_find_new_center_after_an_abandoned_ongoing_center` with:

```python
def test_scan_reuses_direction_flip_completion_return_for_next_center():
    values = _direction_flip_then_later_center()
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert [item.state for item in result.centers] == [
        CenterState.COMPLETED,
        CenterState.COMPLETED,
    ]
    assert result.centers[0].completion_return_unit is values[6]
    assert result.centers[1].entry_unit is values[6]
```

Add the scanner invariant test:

```python
@pytest.mark.parametrize(
    "values",
    (valid_five_up_exit(), _direction_flip_then_later_center()),
)
def test_scan_has_at_most_one_ongoing_center_and_only_at_locked_tail(values):
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    ongoing = [
        center for center in result.centers if center.state is CenterState.ONGOING
    ]
    assert len(ongoing) <= 1
    if ongoing:
        assert ongoing[0] is result.centers[-1]
        assert ongoing[0].body_units[-1] is values[result.locked_unit_count - 1]
```

- [ ] **Step 3: Replace the recursive test that legitimizes abandonment**

Rename the recursive test to `test_direction_flip_completion_keeps_all_centers_in_trends`, keep its input, and change the terminal assertions to:

```python
    assert len(level.center_result.centers) == 2
    assert all(
        center.state is CenterState.COMPLETED
        for center in level.center_result.centers
    )
    assert len(level.trend_types) == 1
    assert level.trend_types[0].centers == level.center_result.centers
```

Add `CenterState` to the existing model import.

- [ ] **Step 4: Independently read the edited test anchors**

Use `Select-String` with the three new test names, `_direction_flip_then_later_center`, `BREAKOUT_WATCH_DOWN`, and `level.trend_types[0].centers`. Expected: every anchor occurs exactly once, except direction-specific event names that may already exist in older tests.

- [ ] **Step 5: Run exact RED tests one process at a time**

```powershell
python -m pytest tests\core\strict_structure\test_center_transitions.py::test_return_crossing_core_becomes_opposite_down_leave_then_completes -q
python -m pytest tests\core\strict_structure\test_center_transitions.py::test_return_crossing_core_becomes_opposite_up_leave_then_completes -q
python -m pytest tests\core\strict_structure\test_center_scan.py::test_scan_completes_after_through_core_direction_flip -q
python -m pytest tests\core\strict_structure\test_center_scan.py::test_scan_reuses_direction_flip_completion_return_for_next_center -q
python -m pytest tests\core\strict_structure\test_center_scan.py::test_scan_has_at_most_one_ongoing_center_and_only_at_locked_tail -q
python -m pytest tests\core\strict_structure\test_recursive_engine.py::test_direction_flip_completion_keeps_all_centers_in_trends -q
```

Expected before production change: both direct transition tests fail because `pending_leave_unit` is `None`; scanner/recursive assertions expose the same stale `ongoing` state. Capture the real pytest failure text before proceeding.

---

## Task 2: Preserve a through-core return as the opposite leaving segment

**Files:**

- Modify: `src/chanlun/core/strict_structure/center_machine.py:250`
- Test: the three files changed in Task 1

- [ ] **Step 1: Implement the minimal transition change**

Replace `_append_extension_return` with:

```python
def _append_extension_return(
    center: TrendCenter,
    item: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    pending_leave = (
        item
        if _outside_in_direction(item, center.zd_tick, center.zg_tick)
        else None
    )
    return _append_body_unit(center, item, pending_leave=pending_leave)
```

This deliberately reuses `_outside_in_direction`: an ordinary re-entry ending within the core still clears the pending leave and emits `EXTENDED`; a segment ending beyond the opposite boundary becomes the new pending leave and emits the matching `BREAKOUT_WATCH_*` event.

- [ ] **Step 2: Independently inspect the production anchor**

Use `Select-String -Context 0,12` around `def _append_extension_return`. Expected: exactly one definition, one call to `_outside_in_direction`, and no unconditional `pending_leave=None` in this helper.

- [ ] **Step 3: Run the six exact tests GREEN, separately**

Repeat all six Task 1 pytest commands. Expected: each process exits 0 with one or two parametrized cases passing and no failures.

- [ ] **Step 4: Verify unchanged extension and watch behavior**

```powershell
python -m pytest tests\core\strict_structure\test_center_transitions.py::test_locked_return_into_core_extends_without_moving_core -q
python -m pytest tests\core\strict_structure\test_center_transitions.py::test_return_extension_then_new_leave_keeps_core_and_emits_watch -q
python -m pytest tests\core\strict_structure\test_center_scan.py::test_scan_emits_establish_extend_watch_complete_events_in_order -q
```

Expected: all three exit 0. The first still emits `EXTENDED`; the second and third keep the original up-leave behavior.

- [ ] **Step 5: Run the three complete affected test files**

```powershell
python -m pytest tests\core\strict_structure\test_center_transitions.py -q
python -m pytest tests\core\strict_structure\test_center_scan.py -q
python -m pytest tests\core\strict_structure\test_recursive_engine.py -q
```

Expected: each file exits 0 with no skipped failures. Do not combine file paths into one pytest command.

- [ ] **Step 6: Commit the self-contained state-machine unit**

Inspect `git diff --check` and `git diff --` for exactly these four files, then stage only them:

```powershell
git add -- src/chanlun/core/strict_structure/center_machine.py tests/core/strict_structure/test_center_transitions.py tests/core/strict_structure/test_center_scan.py tests/core/strict_structure/test_recursive_engine.py
git commit -m "中枢：保留穿越核心后的反向离开段" -m "Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Expected: HEAD advances exactly once; `git show --name-only --format=` lists only those four files. Independently verify the trailer and that pre-existing unrelated dirt remains unstaged.

---

## Task 3: Prove downstream recursive, point, divergence, cache, and chart compatibility

**Files:**

- Test only; no planned production edits

- [ ] **Step 1: Run downstream Python files individually**

```powershell
python -m pytest tests\core\strict_structure\test_trend_assembler.py -q
python -m pytest tests\core\strict_structure\test_third_class_points.py -q
python -m pytest tests\core\strict_structure\test_divergence_collector.py -q
python -m pytest tests\core\strict_structure\test_incremental_prefix.py -q
python -m pytest tests\core\strict_structure\test_models.py -q
python -m pytest tests\web\test_strict_chart_serializer.py -q
```

Expected: every process exits 0. If any assertion reflects the old abandoned-center semantics, first determine whether it contradicts the approved design; update it with a new RED proof and commit that isolated correction before continuing.

- [ ] **Step 2: Verify source-fingerprint invalidation**

Locate and run the repository test that asserts strict-chart cache keys include the strict structure source fingerprint. Expected: exit 0 and the fingerprint includes `center_machine.py`, so this change produces a distinct cache key without deletion.

- [ ] **Step 3: Run frontend files individually with structured TAP checks**

```powershell
node --test --test-reporter=tap web\chanlun_chart\cl_app\static\js\__tests__\strict_center_reconcile_integration.test.js
node --test --test-reporter=tap web\chanlun_chart\cl_app\static\js\__tests__\charts_integration.test.js
```

For each captured output, require `# fail 0` and parse `# pass N` with `N > 0`; do not accept only the process success message.

- [ ] **Step 4: Perform an adversarial diff review**

Confirm all of the following from source and tests:

- a return ending inside `[ZD, ZG]` clears pending;
- a return crossing below `ZD` becomes a down pending leave;
- a return crossing above `ZG` becomes an up pending leave;
- the crossing unit remains in `body_units` but the later completion return does not;
- event direction follows the new leave, not the old leave;
- completed center reuse still begins the next seed at the completion return;
- no serializer/UI workaround or schema change was introduced.

---

## Task 4: Recompute and verify real `SH.513100` structure

**Files:**

- Runtime verification only; no planned source edits

- [ ] **Step 1: Recompute all three levels from the real QMT-adjusted data path**

Use the same application/chart builder that serves `a:SH.513100`, request `1m`, `5m`, and `30m`, and inspect the strict recursive results rather than reconstructing centers from serialized rectangles. Expected formal center states:

```text
1m: 8 total = 7 completed + 1 ongoing
5m: 9 total = 9 completed + 0 ongoing
30m: 2 total = 2 completed + 0 ongoing
```

Also assert every level has at most one ongoing center; if present, its final body unit is the final locked unit for that level.

- [ ] **Step 2: Verify restored third-class evidence**

For 5m, expect the five previously missed completion directions in chronological order:

```text
up, up, down, up, down
```

Their point types must be:

```text
3buy, 3buy, 3sell, 3buy, 3sell
```

Across the full formal output, expect 9 completed 5m centers/third-class completions and 7 completed 1m centers/third-class completions. Confirm 30m output is unchanged.

- [ ] **Step 3: Verify prices and body boundaries did not drift**

For the latest 5m center, require:

```text
core=2200..2214
state=completed
completion_direction=down
core_body_start=2026-06-26T10:35:00+08:00
core_body_end=2026-07-01T11:20:00+08:00
completed_at=2026-07-15T10:40:00+08:00
```

Check all five initial units still positively overlap `[2200, 2214]`. This guards against accidentally changing seed/core geometry while fixing lifecycle state.

---

## Task 5: Restart the app and verify the visible 513100 chart

**Files:**

- Runtime only

- [ ] **Step 1: Restart only the verified chart process**

Resolve the process whose command line exactly contains `D:\project\chanlun-pro\web\chanlun_chart\app.py nobrowser`. Stop only that PID, independently verify port 9900 no longer has a listener, then start:

```powershell
Start-Process -FilePath 'D:\software\Python310\python.exe' -ArgumentList @('D:\project\chanlun-pro\web\chanlun_chart\app.py','nobrowser') -WorkingDirectory 'D:\project\chanlun-pro' -WindowStyle Hidden -PassThru
```

Poll for at most 30 seconds. Expected: exactly one matching process, `http://127.0.0.1:9900/livez` returns 200, and `/readyz` returns 200.

- [ ] **Step 2: Force a fresh 513100 request and inspect the payload**

Reload the active chart/session for `a:SH.513100`, request resolution `5`, and wait for strict status `ready`. The new source fingerprint must cause recomputation under a new key; do not delete old cache files.

Expected payload/render facts:

```text
5m formal centers=9
5m ongoing formal centers=0
5m center projections=0
latest formal core=2.200..2.214
latest formal color=#FF0000
latest formal linewidth=2
latest formal linestyle=0
```

- [ ] **Step 3: Verify the chart visually and inspect console/runtime errors**

Using the existing `chanlun-debug` Playwright session, confirm 5m K lines, volume, MACD, all formal center rectangles, and restored third-class markers render. Historical formal centers must be solid, with no dashed historical `ongoing` boxes. Capture a screenshot under `.playwright-cli` and require zero new console/page errors after reload.

- [ ] **Step 4: Final independent repository and service evidence**

Run separately:

```powershell
git rev-parse --short HEAD
git log -3 --format='%h %s'
git show --name-only --format= HEAD
git status --short
```

Expected: the design, plan, and implementation commits are visible; the implementation commit contains only the four intended files; only pre-existing unrelated dirt remains. Recheck both health endpoints return 200. Do not push.
