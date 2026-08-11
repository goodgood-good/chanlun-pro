'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const Analysis = require('../chart_analysis.js');

const CLOSED_AT = 1700000600;
const DAILY_BAR_AT = 1784649600;
const DAILY_CLOSE_AT = 1784703600;

function center(overrides = {}) {
  return {
    schema: 'chanlun-chart-center',
    render_kind: 'formal_center',
    center_id: 'center-l0-1',
    render_id: 'center-l0-1@r1@ongoing',
    body_revision: 'r1',
    structural_level: 0,
    source_kind: 'segment',
    state: 'ongoing',
    tradable: false,
    completion_phase: 'AWAITING_SAME_LEVEL_RETURN',
    completion_point_type: null,
    expected_completion_point_type: '3buy',
    completion_point_status: null,
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
    pending_leave_unit_id: 'u5',
    completion_leave_unit_id: null,
    completion_return_unit_id: null,
    completion_direction: null,
    entering_segment: {
      unit_id: 'u1', direction: 'up', start_time: 1699996400, end_time: 1699997000,
      start_tick: 980, end_tick: 1080, start_price: 9.8, end_price: 10.8,
    },
    leaving_segment: {
      unit_id: 'u5', direction: 'up', start_time: 1699999700, end_time: 1700000300,
      start_tick: 1000, end_tick: 1090, start_price: 10.0, end_price: 10.9,
    },
    completion_return_segment: null,
    established_market_time: 1699999000,
    established_at: 1699999300,
    completed_at: null,
    available_at: 1700000300,
    ...overrides,
  };
}

function centerPreview(overrides = {}) {
  return center({
    render_kind: 'center_preview',
    center_id: 'preview-l0-1',
    preview_id: 'preview-l0-1',
    render_id: 'preview-l0-1@forming@1700000600',
    body_revision: 0,
    state: 'forming',
    tradable: false,
    established_at: null,
    completed_at: null,
    ...overrides,
  });
}

function divergence(kind, structuralLevel = 0, overrides = {}) {
  return {
    schema: 'chanlun-chart-divergence',
    render_kind: 'strict_divergence',
    render_id: `${kind}-l${structuralLevel}`,
    divergence_id: `${kind}-l${structuralLevel}`,
    kind,
    direction: kind === 'trend' ? 'down' : 'up',
    structural_level: structuralLevel,
    source_kind: 'segment',
    price_basis_revision: 'price-test',
    compare_unit_id: `compare-${kind}`,
    signal_unit_id: `signal-${kind}`,
    comparison_width: 3,
    compare_leg_unit_ids: ['a1', 'a2', `compare-${kind}`],
    signal_leg_unit_ids: ['c1', 'c2', `signal-${kind}`],
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
      strength_decay_count: 2,
      is_strong_divergent: true,
    },
    tradable: true,
    points: [{ time: 1700000300, price: kind === 'trend' ? 10.2 : 10.8 }],
    ...overrides,
  };
}

function point(pointType, status, overrides = {}) {
  const buy = pointType.endsWith('buy');
  return {
    schema: 'chanlun-chart-point',
    render_kind: status === 'confirmed' ? 'point_confirmed' : 'point_approaching',
    render_id: `${pointType}-${status}`,
    point_id: `${pointType}-${status}`,
    point_type: pointType,
    side: buy ? 'buy' : 'sell',
    status,
    variant: 'standard',
    structural_level: 0,
    source_kind: 'segment',
    price_basis_revision: 'price-test',
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
    schema: 'chanlun-chart-structure',
    symbol: 'SZ.000001',
    source_frequency: '5m',
    display_frequency: '5m',
    price_basis_revision: 'price-test',
    structure_price_quantum: '0.01',
    strict_config_revision: 'strict-test',
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
      completion_phase: 'NON_TRADABLE_OBSERVATION',
      completion_point_type: null,
      expected_completion_point_type: null,
      completion_point_status: null,
    })],
    levels: [{
      structural_level: 0,
      label: '5m',
      origin: 'current_chart_recursive',
      centers: [center()],
      center_previews: [centerPreview({
        points: [
          { time: 1700000400, price_tick: 1280, price: 12.8 },
          { time: CLOSED_AT, price_tick: 1200, price: 12.0 },
        ],
        core: { zd_tick: 1200, zg_tick: 1280, zd_price: 12.0, zg_price: 12.8 },
        envelope: { dd_tick: 1180, gg_tick: 1300, dd_price: 11.8, gg_price: 13.0 },
      })],
      center_projections: [],
      current_trends: [{
        schema: 'chanlun-chart-trend',
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

test('strict snapshot supplies centers and signals through one contract', () => {
  const strictOnly = Analysis.summarizeChartData(barsResult(), context);
  assert.equal(strictOnly.state, 'ready');
  assert.equal(strictOnly.trends[0].directionLabel, '向上');
  assert.equal(strictOnly.formalCenters[0].tradable, false);
  assert.equal(strictOnly.formalCenters[0].enteringSegment.direction, 'up');
  assert.equal(strictOnly.formalCenters[0].leavingSegment.direction, 'up');
  assert.equal(strictOnly.centerPreviews[0].tradable, false);
  assert.equal(strictOnly.centerPreviews[0].qualification, '形成中预览，不可直接交易');
  assert.equal(strictOnly.observations[0].tradable, false);
  assert.equal(strictOnly.observations[0].qualification, '严格笔中枢观察，不可直接交易');
  assert.equal(strictOnly.biZone.exists, true);
  assert.equal(strictOnly.xdZone.low, 12.0);
  assert.equal(strictOnly.xdZone.high, 12.8);
  assert.equal(strictOnly.xdZone.status, '\u5f62\u6210\u4e2d');
  assert.equal(strictOnly.xdZone.levelLabel, '线段中枢预览');
  assert.doesNotMatch(strictOnly.xdZone.meta, /\u4e0d\u53ef\u76f4\u63a5\u4ea4\u6613/);
  assert.equal(strictOnly.xdZone.enteringSegment, '向上 · 9.80 → 10.80');
  assert.equal(strictOnly.xdZone.leavingSegment, '向上 · 10.00 → 10.90');
  assert.deepEqual(strictOnly.divergences.map((item) => item.label).sort(), ['盘整背驰', '趋势背驰']);
  assert.equal(strictOnly.divergences.every((item) => item.levelLabel === '5m'), true);
  assert.equal(strictOnly.divergences.every((item) => item.comparisonWidth === 3), true);
  assert.equal(strictOnly.divergences.every((item) => item.compareLegUnitIds.length === 3), true);
  assert.equal(strictOnly.divergences.every((item) => item.strengthDecayCount === 2), true);
  assert.equal(strictOnly.divergences.every((item) => item.isStrongDivergent === true), true);
});

test('provisional third-class completion is reported as complete but non-tradable', () => {
  const base = snapshot();
  const completedPreview = centerPreview({
    state: 'completed',
    render_id: 'preview-l0-1@completed@u6',
    completion_leave_unit_id: 'u5',
    completion_return_unit_id: 'u6',
    completion_direction: 'down',
    pending_leave_unit_id: null,
    completion_phase: 'GEOMETRIC_THIRD_CLASS_POINT',
    completion_point_type: '3sell',
    expected_completion_point_type: '3sell',
    completion_point_status: 'provisional',
  });
  const strict = snapshot({
    levels: [{
      ...base.levels[0],
      centers: [],
      center_previews: [completedPreview],
    }],
  });

  const summary = Analysis.summarizeChartData(barsResult(strict), context);

  assert.equal(summary.centerPreviews[0].state, 'completed');
  assert.equal(summary.centerPreviews[0].tradable, false);
  assert.equal(
    summary.centerPreviews[0].qualification,
    '几何已完成，等待线段锁定，不可直接交易',
  );
  assert.equal(summary.xdZone.status, '三类卖点几何完成，待锁定');
  assert.equal(summary.xdZone.tone, 'forming');
});

test('current stroke and segment status use base geometry from the same response', () => {
  const summary = Analysis.summarizeChartData(barsResult(snapshot(), {
    bis: [{
      linestyle: '1',
      points: [
        { time: CLOSED_AT - 120, price: 10.2 },
        { time: CLOSED_AT, price: 10.8 },
      ],
    }],
    xds: [{
      linestyle: '0',
      points: [
        { time: CLOSED_AT - 300, price: 11.4 },
        { time: CLOSED_AT, price: 10.1 },
      ],
    }],
  }), context);

  assert.equal(summary.bi.text, '向上 · 形成中');
  assert.equal(summary.xd.text, '向下 · 已完成');
  assert.equal(summary.trends[0].directionLabel, '向上');
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

test('unavailable or context-mismatched strict data reports synchronization failure', () => {
  const unavailable = Analysis.summarizeChartData({
    bars: [{ time: CLOSED_AT * 1000, close: 11 }],
    strict_structure_mode: 'unavailable',
    strict_structure_error: { code: 'strict_evidence_invalid' },
  }, context);
  assert.equal(unavailable.state, 'unavailable');
  assert.equal(unavailable.formalCenters.length, 0);
  assert.equal(unavailable.centerPreviews.length, 0);
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

test('same-context unavailable reuses only the manager last-good snapshot as stale evidence', () => {
  const unavailable = Analysis.summarizeChartData({
    bars: [
      { time: CLOSED_AT * 1000, close: 11 },
      { time: (CLOSED_AT + 300) * 1000, close: 11.1 },
    ],
    strict_structure_mode: 'unavailable',
    strict_structure_error: { code: 'strict_price_metadata_unavailable' },
  }, { ...context, cachedStrictSnapshot: snapshot() });

  assert.equal(unavailable.state, 'stale');
  assert.equal(unavailable.formalCenters.length, 1);
  assert.equal(unavailable.centerPreviews.length, 1);
  assert.match(unavailable.statusDetail, /strict_price_metadata_unavailable/);

  const wrongSymbol = Analysis.summarizeChartData({
    bars: [{ time: CLOSED_AT * 1000, close: 11 }],
    strict_structure_mode: 'unavailable',
    strict_structure_error: { code: 'strict_evidence_invalid' },
  }, {
    ...context,
    symbol: 'A:SH.600519',
    cachedStrictSnapshot: snapshot(),
  });
  assert.equal(wrongSymbol.state, 'unavailable');
  assert.equal(wrongSymbol.formalCenters.length, 0);
});

test('daily summary validates strict source close against raw transport time', () => {
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const summary = Analysis.summarizeChartData(barsResult(strict, {
    times: [DAILY_CLOSE_AT * 1000],
    bars: [{ time: DAILY_BAR_AT * 1000, close: 11, isBarClosed: false }],
  }), { ...context, resolution: '1D' });

  assert.equal(summary.state, 'ready');
  assert.equal(summary.sourceClosedAt, DAILY_CLOSE_AT);
});

test('daily summary still rejects a genuinely stale raw transport time', () => {
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const summary = Analysis.summarizeChartData(barsResult(strict, {
    times: [(DAILY_CLOSE_AT - 86400) * 1000],
    bars: [{ time: DAILY_BAR_AT * 1000, close: 11, isBarClosed: false }],
  }), { ...context, resolution: '1D' });

  assert.equal(summary.state, 'syncing');
  assert.match(summary.statusDetail, /\u672b\u6839/);
});

test('unchanged transport may reuse only the manager-provided strict snapshot', () => {
  const summary = Analysis.summarizeChartData({
    bars: barsResult().bars,
    strict_structure_mode: 'unchanged',
  }, { ...context, cachedStrictSnapshot: snapshot() });

  assert.equal(summary.state, 'ready');
  assert.equal(summary.formalCenters.length, 1);
  assert.equal(summary.structureRevision, 'sha256:structure-revision-1234567890');
});
