'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const Analysis = require('../chart_analysis.js');

const CLOSED_AT = 1700000600;

function center(overrides = {}) {
  return {
    schema: 'chanlun-chart-center/v4',
    render_kind: 'formal_center',
    center_id: 'center-l0-1',
    render_id: 'center-l0-1@r1@ongoing',
    body_revision: 'r1',
    structural_level: 0,
    source_kind: 'segment',
    state: 'ongoing',
    tradable: true,
    points: [
      { time: 1699997000, price_tick: 1060, price: 10.6 },
      { time: 1700000300, price_tick: 1000, price: 10.0 },
    ],
    core: { zd_tick: 1000, zg_tick: 1060, zd_price: 10.0, zg_price: 10.6 },
    envelope: { dd_tick: 980, gg_tick: 1090, dd_price: 9.8, gg_price: 10.9 },
    entry_unit_id: 'u1',
    core_unit_ids: ['u2', 'u3', 'u4'],
    initial_exit_unit_id: 'u5',
    initial_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    body_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    extension_unit_ids: [],
    pending_leave_unit_id: null,
    completion_leave_unit_id: null,
    completion_return_unit_id: null,
    completion_direction: null,
    established_market_time: 1699999000,
    established_at: 1699999300,
    completed_at: null,
    available_at: 1700000300,
    ...overrides,
  };
}

function divergence(kind, structuralLevel = 0, overrides = {}) {
  return {
    schema: 'chanlun-chart-divergence/v4',
    render_kind: 'strict_divergence',
    render_id: `${kind}-l${structuralLevel}`,
    divergence_id: `${kind}-l${structuralLevel}`,
    kind,
    direction: kind === 'trend' ? 'down' : 'up',
    structural_level: structuralLevel,
    source_kind: 'segment',
    price_basis_revision: 'price-v1',
    compare_unit_id: `compare-${kind}`,
    signal_unit_id: `signal-${kind}`,
    anchor_at: 1700000300,
    anchor_tick: kind === 'trend' ? 1020 : 1080,
    anchor_price: kind === 'trend' ? 10.2 : 10.8,
    confirmed_at: 1700000300,
    available_at: 1700000300,
    metrics: {
      price_extreme_confirmed: true,
      histogram_area_decayed: true,
      histogram_peak_decayed: true,
      dif_extreme_decayed: false,
      strength_source: 'macd',
      is_divergent: true,
    },
    tradable: true,
    points: [{ time: 1700000300, price: kind === 'trend' ? 10.2 : 10.8 }],
    ...overrides,
  };
}

function point(pointType, status, overrides = {}) {
  const buy = pointType.endsWith('buy');
  return {
    schema: 'chanlun-chart-point/v3',
    render_kind: status === 'confirmed' ? 'point_confirmed' : 'point_approaching',
    render_id: `${pointType}-${status}`,
    point_id: `${pointType}-${status}`,
    point_type: pointType,
    side: buy ? 'buy' : 'sell',
    status,
    variant: 'standard',
    structural_level: 0,
    source_kind: 'segment',
    price_basis_revision: 'price-v1',
    anchor_unit_id: `anchor-${pointType}`,
    anchor_at: 1700000000,
    confirmed_at: status === 'confirmed' ? 1700000300 : null,
    available_at: 1700000300,
    anchor_tick: buy ? 1020 : 1080,
    anchor_price: buy ? 10.2 : 10.8,
    invalidation_tick: buy ? 990 : 1110,
    invalidation_price: buy ? 9.9 : 11.1,
    center_id: pointType.startsWith('3') ? 'center-l0-1' : null,
    center_zd_tick: pointType.startsWith('3') ? 1000 : null,
    center_zd_price: pointType.startsWith('3') ? 10.0 : null,
    center_zg_tick: pointType.startsWith('3') ? 1060 : null,
    center_zg_price: pointType.startsWith('3') ? 10.6 : null,
    center_ordinal: pointType.startsWith('3') ? 1 : null,
    parent_point_id: pointType.startsWith('2') ? '1buy-confirmed' : null,
    divergence: pointType.startsWith('1') ? {
      kind: 'trend',
      direction: buy ? 'down' : 'up',
      compare_unit_id: 'compare-1',
      signal_unit_id: 'signal-1',
      price_extreme_confirmed: true,
      histogram_area_decayed: true,
      histogram_peak_decayed: true,
      dif_extreme_decayed: false,
      strength_source: 'macd',
      available_at: 1700000300,
      is_divergent: true,
    } : null,
    evidence_codes: ['strict-evidence'],
    missing_conditions: status === 'approaching' ? ['wait-lock'] : [],
    related_point_ids: [],
    evidence_revision: 'sha256:evidence',
    tradable: status === 'confirmed',
    points: [{ time: 1700000000, price_tick: buy ? 1020 : 1080, price: buy ? 10.2 : 10.8 }],
    ...overrides,
  };
}

function snapshot(overrides = {}) {
  return {
    schema: 'chanlun-chart-structure/v4',
    symbol: 'SZ.000001',
    source_frequency: '5m',
    display_frequency: '5m',
    price_basis_revision: 'price-v1',
    structure_price_quantum: '0.01',
    strict_config_revision: 'strict-v1',
    source_closed_at: CLOSED_AT,
    structure_revision: 'sha256:structure-revision-1234567890',
    snapshot_revision: 'sha256:snapshot-revision-1234567890',
    render_revision: 'sha256:render-revision-1234567890',
    stroke_center_observations: [center({
      render_kind: 'center_observation',
      center_id: 'stroke-observation-1',
      render_id: 'stroke-observation-1@r1@ongoing',
      source_kind: 'stroke_observation',
      structural_level: 0,
      tradable: false,
    })],
    levels: [{
      structural_level: 0,
      label: '5m',
      origin: 'current_chart_recursive',
      centers: [center()],
      center_projections: [],
      current_trends: [{
        schema: 'chanlun-chart-trend/v3',
        render_kind: 'strict_trend',
        trend_id: 'trend-l0-current',
        render_id: 'trend-l0-current@forming@u6',
        structural_level: 0,
        source_kind: 'segment',
        state: 'forming',
        kind: 'trend',
        direction: 'up',
        tradable: true,
        points: [
          { time: 1699996000, price_tick: 980, price: 9.8 },
          { time: 1700000600, price_tick: 1100, price: 11.0 },
        ],
        range: { low_tick: 980, high_tick: 1100, low_price: 9.8, high_price: 11.0 },
        center_ids: ['center-l0-1'],
        constituent_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5', 'u6'],
        confirmed_at: null,
        available_at: CLOSED_AT,
      }],
      completed_trend_snapshots: [],
      confirmed_points: [
        point('1buy', 'confirmed'),
        point('2buy', 'confirmed'),
        point('3buy', 'confirmed'),
      ],
      approaching_points: [
        point('1sell', 'approaching'),
        point('2sell', 'approaching'),
        point('3sell', 'approaching'),
      ],
      divergences: [divergence('consolidation'), divergence('trend')],
    }],
    ...overrides,
  };
}

function barsResult(strict = snapshot(), overrides = {}) {
  return {
    bars: [
      { time: 1700000000000, close: 10.4, isBarClosed: true },
      { time: CLOSED_AT * 1000, close: 11.0, isBarClosed: true },
    ],
    strict_structure_mode: 'replace',
    strict_structure: strict,
    ...overrides,
  };
}

const context = {
  resolution: '5',
  symbol: 'A:SZ.000001',
  timeZone: 'Asia/Shanghai',
};

test('summary consumes only the authoritative strict snapshot and ignores contradictory legacy arrays', () => {
  const strictOnly = Analysis.summarizeChartData(barsResult(), context);
  const poisoned = Analysis.summarizeChartData(barsResult(snapshot(), {
    bi_zss: [{ zd: -999, zg: 999 }],
    xd_zss: [{ zd: -888, zg: 888 }],
    recursive_levels: [{ level: 0, zss: [{ zd: -777, zg: 777 }] }],
    mmds: [{ text: 'legacy-only-buy' }],
    bcs: [{ text: 'legacy-only-divergence' }],
    bis: [{ points: [{ price: -1 }, { price: -2 }] }],
    xds: [{ points: [{ price: 1 }, { price: 2 }] }],
  }), context);

  assert.deepEqual(poisoned, strictOnly);
  assert.equal(strictOnly.state, 'ready');
  assert.equal(strictOnly.trends[0].directionLabel, '向上');
  assert.equal(strictOnly.formalCenters[0].tradable, true);
  assert.equal(strictOnly.observations[0].tradable, false);
  assert.equal(strictOnly.observations[0].qualification, '观察证据，不可直接交易');
  assert.deepEqual(strictOnly.divergences.map((item) => item.label).sort(), ['盘整背驰', '趋势背驰']);
  assert.equal(strictOnly.divergences.every((item) => item.levelLabel === '5m'), true);
});

test('all six buy and sell point classes stay independent across confirmed and approaching evidence', () => {
  const summary = Analysis.summarizeChartData(barsResult(), context);

  assert.deepEqual(summary.pointCounts, {
    '1buy': { confirmed: 1, approaching: 0 },
    '2buy': { confirmed: 1, approaching: 0 },
    '3buy': { confirmed: 1, approaching: 0 },
    '1sell': { confirmed: 0, approaching: 1 },
    '2sell': { confirmed: 0, approaching: 1 },
    '3sell': { confirmed: 0, approaching: 1 },
  });
  assert.equal(summary.confirmedPoints.find((item) => item.pointType === '3buy').centerOrdinal, 1);
  assert.equal(summary.approachingPoints.find((item) => item.pointType === '2sell').missingConditions[0], 'wait-lock');
  assert.match(summary.confirmedPoints.find((item) => item.pointType === '1buy').evidenceText, /MACD/);
  assert.equal(summary.bc.label, '盘整背驰');
  assert.equal(summary.bc.levelLabel, '5m');
});

test('unavailable or context-mismatched strict data reports synchronization failure without legacy fallback', () => {
  const unavailable = Analysis.summarizeChartData({
    bars: [{ time: CLOSED_AT * 1000, close: 11 }],
    strict_structure_mode: 'unavailable',
    strict_structure_error: { code: 'strict_evidence_invalid' },
    bi_zss: [{ zd: 1, zg: 2 }],
    mmds: [{ text: '3buy' }],
  }, context);
  assert.equal(unavailable.state, 'unavailable');
  assert.equal(unavailable.formalCenters.length, 0);
  assert.equal(unavailable.confirmedPoints.length, 0);
  assert.match(unavailable.statusDetail, /strict_evidence_invalid/);

  const mismatch = Analysis.summarizeChartData(
    barsResult(snapshot({ display_frequency: '30m' })),
    context,
  );
  assert.equal(mismatch.state, 'syncing');
  assert.equal(mismatch.formalCenters.length, 0);
  assert.match(mismatch.statusDetail, /周期/);
});

test('unchanged transport may reuse only the manager-provided strict snapshot', () => {
  const summary = Analysis.summarizeChartData({
    bars: barsResult().bars,
    strict_structure_mode: 'unchanged',
    bi_zss: [{ zd: 1, zg: 2 }],
  }, { ...context, cachedStrictSnapshot: snapshot() });

  assert.equal(summary.state, 'ready');
  assert.equal(summary.formalCenters.length, 1);
  assert.equal(summary.structureRevision, 'sha256:structure-revision-1234567890');
});
