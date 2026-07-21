'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const Reconcile = require('../chart_structure_reconcile.js');

function strictCenter(overrides = {}) {
  return {
    render_kind: 'formal_center',
    center_id: 'c1',
    render_id: 'c1@1@extending',
    structural_level: 0,
    points: [
      { time: 100, price: 11 },
      { time: 500, price: 10 },
    ],
    ...overrides,
  };
}

function chartContext(chartInstanceId, interval = '5m') {
  return {
    chartInstanceId,
    symbol: 'SH.600519',
    interval,
    price_basis_revision: 'raw-v1',
  };
}

test('formal center identity uses render_id instead of clipped points', () => {
  const item = strictCenter({ render_id: 'c1@2@extending' });
  assert.equal(Reconcile.renderKey(item), 'c1@2@extending');
  assert.equal(
    Reconcile.renderKey({ ...item, points: [{ time: 999, price: 10 }] }),
    'c1@2@extending',
  );
});

test('bar milliseconds are converted exactly once to epoch seconds', () => {
  assert.equal(Reconcile.barTimeMsToEpochSeconds(1784511000000), 1784511000);
  assert.throws(
    () => Reconcile.barTimeMsToEpochSeconds(1784511000),
    /bar time must be milliseconds/,
  );
});

test('visible range shrink never changes center source geometry', () => {
  const item = strictCenter();
  assert.deepEqual(
    Reconcile.clipToLoadedRange(item, { from: 50, to: 600 }).points,
    item.points,
  );
  assert.deepEqual(
    Reconcile.clipToLoadedRange(item, { from: 200, to: 600 }).points.map((point) => point.time),
    [200, 500],
  );
  assert.deepEqual(item.points.map((point) => point.time), [100, 500]);
});

test('right unloaded boundary clips only the render copy', () => {
  const item = strictCenter();
  const clipped = Reconcile.clipToLoadedRange(item, { from: 50, to: 400 });
  assert.deepEqual(clipped.points.map((point) => point.time), [100, 400]);
  assert.deepEqual(item.points.map((point) => point.time), [100, 500]);
});

test('body revision replaces exactly one prior entity', () => {
  const plan = Reconcile.planReconcile(
    [{
      logicalKey: 'formal_center:c1',
      renderKey: 'c1@1@extending',
      geometryFingerprint: 'old',
      id: 'old',
    }],
    [strictCenter({ render_id: 'c1@2@extending' })],
    { from: 0, to: 1000 },
    { from: 100, to: 900 },
  );
  assert.deepEqual(plan.removeIds, ['old']);
  assert.equal(plan.createItems.length, 1);
});

test('same ids in two chart instances never share ownership scope', () => {
  const item = strictCenter();
  assert.notEqual(
    Reconcile.scopeKey(chartContext('chart-a'), item),
    Reconcile.scopeKey(chartContext('chart-b'), item),
  );
});

test('stale epochs are rejected after a newer reconcile or disposal', () => {
  const epoch = new Reconcile.ReconcileEpoch();
  const first = epoch.next('scope');
  const second = epoch.next('scope');
  assert.equal(epoch.current('scope', first), false);
  assert.equal(epoch.current('scope', second), true);
  epoch.dispose();
  assert.equal(epoch.current('scope', second), false);
  assert.throws(() => epoch.next('scope'), /disposed/);
});

test('divergence identity uses its stable divergence id', () => {
  assert.equal(
    Reconcile.logicalKey({ render_kind: 'strict_divergence', divergence_id: 'divergence-1' }),
    'strict_divergence:divergence-1',
  );
});
