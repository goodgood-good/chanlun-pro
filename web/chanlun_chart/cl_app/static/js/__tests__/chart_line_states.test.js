'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const LineStates = require('../chart_line_states.js');

test('stroke continuation does not imply irreversible completion', () => {
  const current = { kind: 'bi', state: 'continued', locked: false, linestyle: '0' };
  assert.equal(LineStates.stateOf(current), 'continued');
  assert.equal(LineStates.stateOf({ kind: 'bi', state: 'locked', locked: true }), 'continued');
  assert.equal(LineStates.stateOf({ kind: 'bi', state: ' LOCKED ', linestyle: '0' }), 'continued');
  assert.equal(LineStates.stateOf({ kind: 'bi', linestyle: '0' }), 'continued');
  assert.equal(LineStates.stateOf({ kind: 'xd', state: 'locked', locked: true }), 'locked');
  assert.equal(LineStates.normalizeBaseStructureLine(current).linestyle, '0');
});

test('completed strokes require the explicit final-completion contract', () => {
  const completed = {kind: 'bi', state: 'locked', locked: true, completion_is_final: true};
  assert.equal(LineStates.stateOf(completed), 'locked');
  assert.equal(LineStates.stateOf({...completed, completion_is_final: false}), 'continued');
  assert.equal(LineStates.stateOf({...completed, selection_pending: true}), 'forming');
  assert.notEqual(LineStates.renderKey('same', completed),
    LineStates.renderKey('same', {...completed, completion_is_final: false}));
});

test('unresolved stroke choices remain dashed even with an old continuation flag', () => {
  const pending = { kind: 'bi', state: 'continued', locked: true, selection_pending: true, linestyle: '0' };
  assert.equal(LineStates.stateOf(pending), 'forming');
  assert.equal(LineStates.normalizeBaseStructureLine(pending).linestyle, '2');
  assert.equal(LineStates.renderKey('same-points', pending), 'same-points::forming');
});

test('conditional segments from every pending range stay visible and dashed', () => {
  const pending = [1, 2, 3].map(index => ({kind: 'xd', component_index: index,
    observation_scope_id: `range-${index}`, selection_pending: true,
    state: 'formed', locked: true, linestyle: '0'}));
  const result = LineStates.uniqueRenderList(pending);
  assert.equal(result.length, 3);
  assert.deepEqual(result.map(item => item.component_index), [1, 2, 3]);
  assert.ok(result.every(item => LineStates.stateOf(item) === 'forming' && item.linestyle === '2'));
});

function line(state, linestyle) {
  return { state, linestyle, points: [{ time: 1, price: 1 }, { time: 2, price: 2 }] };
}

test('结构线明确区分形成中、几何已成形和已审计锁定', () => {
  assert.equal(LineStates.stateOf(line('forming', '1')), 'forming');
  assert.equal(LineStates.stateOf(line('formed', '0')), 'formed');
  assert.equal(LineStates.stateOf({ ...line('formed', '0'), locked: true }), 'locked');
  assert.equal(LineStates.stateOf({ linestyle: '0' }), 'locked');
});

test('只裁掉较早的形成中尾段，待锁定线段完整保留', () => {
  const formedA = line('formed', '0');
  const formedB = line('formed', '0');
  const formingA = line('forming', '1');
  const formingB = line('forming', '1');

  assert.deepEqual(
    LineStates.uniqueRenderList([formedA, formingA, formedB, formingB]),
    [formedA, formedB, { ...formingB, linestyle: '2' }],
  );
});

test('形成中结构统一改成虚线且旧缓存中的已成形虚线恢复为实线', () => {
  const legacyFormed = line('formed', '2');
  const forming = line('forming', '1');

  assert.equal(LineStates.normalizeBaseStructureLine(legacyFormed).linestyle, '0');
  assert.equal(LineStates.normalizeBaseStructureLine(forming).linestyle, '2');
  assert.equal(
    LineStates.normalizeBaseStructureLine({ linestyle: '2' }).linestyle,
    '2',
    '没有基础结构状态的其他虚线不得被改写',
  );
});

test('多笔待定尾部按顺序完整显示，不裁成最后一笔', () => {
  const locked = { ...line('locked', '0'), kind: 'bi' };
  const first = { ...line('forming', '1'), kind: 'bi', points: [{ time: 2, price: 2 }, { time: 3, price: 1 }] };
  const second = { ...line('forming', '1'), kind: 'bi', points: [{ time: 3, price: 1 }, { time: 4, price: 3 }] };
  assert.deepEqual(LineStates.uniqueRenderList([locked, first, second]), [
    locked, { ...first, linestyle: '2' }, { ...second, linestyle: '2' },
  ]);
});

test('状态进入渲染身份使同一端点的锁定翻转能够刷新线型', () => {
  assert.equal(LineStates.renderKey('same-geometry', line('formed', '0')), 'same-geometry::formed');
  assert.equal(LineStates.renderKey('same-geometry', line('locked', '0')), 'same-geometry::locked');
});
