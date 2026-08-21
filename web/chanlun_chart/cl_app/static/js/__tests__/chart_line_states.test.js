'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const LineStates = require('../chart_line_states.js');

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
    [formedA, formedB, formingB],
  );
});

test('旧缓存中的已成形虚线会在浏览器端恢复为实线', () => {
  const legacyFormed = line('formed', '2');
  const forming = line('forming', '1');

  assert.equal(LineStates.normalizeBaseStructureLine(legacyFormed).linestyle, '0');
  assert.equal(LineStates.normalizeBaseStructureLine(forming).linestyle, '1');
  assert.equal(
    LineStates.normalizeBaseStructureLine({ linestyle: '2' }).linestyle,
    '2',
    '没有基础结构状态的其他虚线不得被改写',
  );
});

test('状态进入渲染身份使同一端点的锁定翻转能够刷新线型', () => {
  assert.equal(LineStates.renderKey('same-geometry', line('formed', '0')), 'same-geometry::formed');
  assert.equal(LineStates.renderKey('same-geometry', line('locked', '0')), 'same-geometry::locked');
});
