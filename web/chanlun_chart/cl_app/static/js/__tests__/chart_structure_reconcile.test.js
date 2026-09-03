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

function strictTrend(overrides = {}) {
  return {
    render_kind: 'strict_trend',
    trend_id: 'trend-1',
    render_id: 'trend-1@locked',
    structural_level: 0,
    state: 'locked',
    direction: 'down',
    points: [
      { time: 100, price: 11 },
      { time: 500, price: 9 },
    ],
    ...overrides,
  };
}

function chartContext(chartInstanceId, interval = '5m') {
  return {
    chartInstanceId,
    symbol: 'SH.600519',
    interval,
    price_basis_revision: 'raw-test',
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

test('calendar evidence times map to deterministic TradingView coordinates', () => {
  const dailyClose = Date.UTC(2025, 11, 16, 7) / 1000;
  const weeklyClose = Date.UTC(2017, 2, 5, 7) / 1000;
  const monthlyClose = Date.UTC(2026, 6, 31, 7) / 1000;

  assert.equal(
    Reconcile.chartTimeCoordinate(dailyClose, 'd'),
    Date.UTC(2025, 11, 16) / 1000,
  );
  assert.equal(
    Reconcile.chartTimeCoordinate(weeklyClose, 'w'),
    Date.UTC(2017, 1, 27) / 1000,
  );
  assert.equal(
    Reconcile.chartTimeCoordinate(monthlyClose, 'm'),
    Date.UTC(2026, 6, 1) / 1000,
  );
});

test('close-labeled intraday evidence maps to its opening chart coordinate', () => {
  const closeTime = Date.UTC(2026, 6, 30, 3, 30) / 1000;
  assert.equal(
    Reconcile.chartTimeCoordinate(closeTime, '5m', true),
    closeTime - 300,
  );
  assert.equal(
    Reconcile.chartTimeCoordinate(closeTime, '5m', false),
    closeTime,
  );

  const item = strictCenter({
    points: [
      { time: closeTime - 300, price: 11 },
      { time: closeTime, price: 10 },
    ],
  });
  const rendered = Reconcile.itemToChartCoordinates(item, '5m', true);
  assert.deepEqual(
    rendered.points.map((point) => point.time),
    [closeTime - 600, closeTime - 300],
  );
  assert.deepEqual(
    item.points.map((point) => point.time),
    [closeTime - 300, closeTime],
    'audit evidence must remain immutable',
  );
});

test('render-coordinate conversion never mutates strict audit evidence', () => {
  const rawClose = Date.UTC(2026, 6, 31, 7) / 1000;
  const item = strictCenter({
    points: [
      { time: rawClose, price: 11 },
      { time: rawClose, price: 10 },
    ],
  });

  const rendered = Reconcile.itemToChartCoordinates(item, 'm');

  assert.deepEqual(
    rendered.points.map((point) => point.time),
    [Date.UTC(2026, 6, 1) / 1000, Date.UTC(2026, 6, 1) / 1000],
  );
  assert.deepEqual(item.points.map((point) => point.time), [rawClose, rawClose]);
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

test('render planning clips crossing centers to actual visible bar coordinates', () => {
  const item = strictCenter();
  const plan = Reconcile.planReconcile(
    [],
    [item],
    { from: 50, to: 600, barTimes: [50, 100, 200, 300, 400, 500, 600] },
    { from: 175, to: 450 },
  );

  assert.deepEqual(
    plan.createItems[0].points.map((point) => point.time),
    [200, 400],
  );
  assert.deepEqual(item.points.map((point) => point.time), [100, 500]);
});

test('right unloaded boundary clips only the render copy', () => {
  const item = strictCenter();
  const clipped = Reconcile.clipToLoadedRange(item, { from: 50, to: 400 });
  assert.deepEqual(clipped.points.map((point) => point.time), [100, 400]);
  assert.deepEqual(item.points.map((point) => point.time), [100, 500]);
});

test('strict movement lines never move an unloaded market anchor', () => {
  const item = strictTrend();

  assert.equal(
    Reconcile.clipToLoadedRange(item, { from: 200, to: 600 }),
    null,
  );
  assert.deepEqual(item.points, [
    { time: 100, price: 11 },
    { time: 500, price: 9 },
  ]);
});

test('fully visible strict movement keeps both exact anchors', () => {
  const item = strictTrend();
  const plan = Reconcile.planReconcile(
    [],
    [item],
    { from: 50, to: 600, barTimes: [50, 100, 200, 300, 400, 500, 600] },
    { from: 50, to: 600 },
  );

  assert.equal(plan.createItems.length, 1);
  assert.deepEqual(plan.createItems[0].points, item.points);
});

test('crossing strict movement waits until both anchors enter the real-bar viewport', () => {
  const item = strictTrend();
  const plan = Reconcile.planReconcile(
    [],
    [item],
    { from: 50, to: 600, barTimes: [50, 100, 200, 300, 400, 500, 600] },
    { from: 200, to: 450 },
  );

  assert.deepEqual(plan.createItems, []);
  assert.deepEqual(plan.desiredItems, []);
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
  assert.equal(plan.desiredItems.length, 1);
});

test('走势方向资格变化会触发图形替换', () => {
  const candidate = {
    render_kind: 'strict_trend',
    trend_id: 'trend-1',
    render_id: 'trend-1@forming@u9@geometric_candidate',
    structural_level: 0,
    state: 'forming',
    direction: 'down',
    semantic_direction: 'down',
    direction_status: 'geometric_candidate',
    points: [{ time: 100, price: 11 }, { time: 500, price: 9 }],
  };
  const formal = {
    ...candidate,
    render_id: 'trend-1@forming@u9@formal',
    direction_status: 'formal',
  };

  assert.notEqual(
    Reconcile.geometryFingerprint(candidate),
    Reconcile.geometryFingerprint(formal),
  );
});

test('duplicate retained entities are removed and rebuilt as one logical shape', () => {
  const item = strictCenter();
  const plan = Reconcile.planReconcile(
    [
      {
        logicalKey: 'formal_center:c1',
        renderKey: item.render_id,
        geometryFingerprint: Reconcile.geometryFingerprint(item),
        id: 'duplicate-a',
      },
      {
        logicalKey: 'formal_center:c1',
        renderKey: item.render_id,
        geometryFingerprint: Reconcile.geometryFingerprint(item),
        id: 'duplicate-b',
      },
    ],
    [item],
    { from: 0, to: 1000 },
    { from: 0, to: 1000 },
  );

  assert.deepEqual(plan.removeIds, ['duplicate-a', 'duplicate-b']);
  assert.equal(plan.createItems.length, 1);
  assert.equal(plan.createItems[0].logicalKey, 'formal_center:c1');
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

test('pending movement identity uses its stable partition id', () => {
  assert.equal(
    Reconcile.logicalKey({
      render_kind: 'pending_movement',
      partition_id: 'sha256:pending-1',
    }),
    'pending_movement:sha256:pending-1',
  );
});

test('forming center preview identity uses its stable preview id', () => {
  assert.equal(
    Reconcile.logicalKey({
      render_kind: 'center_preview',
      center_id: 'preview-1',
      preview_id: 'preview-1',
    }),
    'center_preview:preview-1',
  );
});
