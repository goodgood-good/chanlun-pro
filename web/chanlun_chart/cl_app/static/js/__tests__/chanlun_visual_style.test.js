'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');

function styleBlock() {
  const start = source.indexOf('const SIGNAL_COLOR_THEMES = Object.freeze({');
  const end = source.indexOf('// 基础结构保留“笔细、线段粗”', start);
  assert.notEqual(start, -1, '缺少方向色主题');
  assert.notEqual(end, -1, '缺少视觉样式块结束标记');
  return source.slice(start, end);
}

function loadStyleApi(theme = 'Light') {
  const context = {
    Utils: { get_local_data: (key) => (key === 'theme' ? theme : null) },
    localStorage: { getItem: () => null },
    JSON,
    String,
    Number,
    Object,
    parseInt,
  };
  vm.runInNewContext(`
    const CHART_CONFIG = {
      LINE_STYLES: { SOLID: 0, DOTTED: 1, DASHED: 2 },
    };
    ${styleBlock()}
    this.api = {
      getSignalColor,
      getCenterVisualStyle,
      getTrendVisualStyle,
      getStrictPointVisual,
      getStrictDivergenceVisual,
    };
  `, context);
  return context.api;
}

function luminance(hex) {
  const values = [1, 3, 5].map((start) => Number.parseInt(hex.slice(start, start + 2), 16) / 255);
  const linear = values.map((value) => (
    value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  ));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(left, right) {
  const a = luminance(left);
  const b = luminance(right);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test('方向性小标记在浅色和深色画布都有足够对比度', () => {
  const light = loadStyleApi('Light');
  const dark = loadStyleApi('dark');
  for (const role of ['fractalTop', 'fractalBottom', 'buy', 'sell']) {
    assert.ok(contrast(light.getSignalColor(role), '#FFFFFF') >= 4.5, `${role} 浅色对比不足`);
    assert.ok(contrast(dark.getSignalColor(role), '#131722') >= 4.5, `${role} 深色对比不足`);
  }
});

test('中枢用线宽表达权重并用线型和透明度表达完成状态', () => {
  const api = loadStyleApi();
  const pen = api.getCenterVisualStyle('pen', { state: 'completed' });
  const frequency = api.getCenterVisualStyle('frequency', { state: 'completed' });
  const formal = api.getCenterVisualStyle('formal', { state: 'completed' });
  const ongoing = api.getCenterVisualStyle('formal', { state: 'ongoing' });
  const completedPreview = api.getCenterVisualStyle('preview', { state: 'completed' });
  const formingPreview = api.getCenterVisualStyle('preview', { state: 'forming' });
  const projection = api.getCenterVisualStyle('projection', { state: 'ongoing' });

  assert.equal(pen.linewidth, 1);
  assert.equal(frequency.linewidth, 1);
  assert.equal(formal.linewidth, 1);
  assert.equal(ongoing.linestyle, 2);
  assert.equal(formal.linestyle, 0);
  assert.equal(formal.transparency, 92);
  assert.equal(ongoing.transparency, 96);
  assert.equal(completedPreview.linewidth, 1);
  assert.equal(completedPreview.transparency, 96);
  assert.equal(formingPreview.transparency, 100);
  assert.equal(projection.linestyle, 2);
  assert.ok(formal.transparency < completedPreview.transparency, '正式中枢必须比预览更醒目');
});

test('走势类型保持同级颜色但弱于基础结构线', () => {
  const api = loadStyleApi();
  const completed = api.getTrendVisualStyle({ state: 'completed', direction_status: 'ended' });
  const forming = api.getTrendVisualStyle({ state: 'forming', direction_status: 'formal' });
  const candidate = api.getTrendVisualStyle({ state: 'completed', direction_status: 'geometric_candidate' });
  const reversal = api.getTrendVisualStyle({ state: 'forming', direction_status: 'awaiting_reversal_support' });
  const consolidation = api.getTrendVisualStyle({ state: 'forming', direction_status: 'consolidation' });
  assert.equal(completed.linewidth, 1);
  assert.equal(completed.linestyle, 2);
  assert.equal(completed.transparency, 46);
  assert.equal(forming.linestyle, 2);
  assert.equal(forming.transparency, 30);
  assert.equal(candidate.linestyle, 2);
  assert.equal(candidate.transparency, 50);
  assert.equal(reversal.linestyle, 1);
  assert.equal(reversal.transparency, 62);
  assert.equal(consolidation.linestyle, 1);
  assert.equal(consolidation.transparency, 70);
});

test('买卖点使用中文短标签、方向箭头和级别字号', () => {
  const api = loadStyleApi();
  const confirmed = api.getStrictPointVisual({
    render_kind: 'point_confirmed', formation_state: 'confirmed', structural_level: 0, level_label: '1m', point_type: '3buy', side: 'buy',
  });
  const higher = api.getStrictPointVisual({
    render_kind: 'point_confirmed', structural_level: 2, level_label: '30m', point_type: '2sell', side: 'sell',
  });
  const approaching = api.getStrictPointVisual({
    render_kind: 'point_approaching', structural_level: 0, level_label: '1m', point_type: '1buy', side: 'buy',
  });
  const geometryCandidate = api.getStrictPointVisual({
    render_kind: 'point_approaching', structural_level: 0, level_label: '5m', point_type: '3buy', side: 'buy',
    formation_state: 'geometry_ready', lock_state: 'pending',
    evidence_codes: ['provisional_center_completion', 'core_boundary_held'],
  });
  const legacyEvidenceOnly = api.getStrictPointVisual({
    render_kind: 'point_approaching', structural_level: 0, level_label: '5m', point_type: '3buy', side: 'buy',
    evidence_codes: ['provisional_center_completion', 'core_boundary_held'],
  });

  assert.equal(confirmed.text, '▲1m·三买');
  assert.equal(confirmed.fontsize, 12);
  assert.equal(confirmed.bold, true);
  assert.equal(higher.text, '▼30m·二卖');
  assert.equal(higher.fontsize, 13);
  assert.equal(approaching.text, '▲接近·1m·一买');
  assert.equal(approaching.fontsize, 11);
  assert.equal(approaching.bold, false);
  assert.equal(approaching.transparency, 45);
  assert.equal(geometryCandidate.text, '▲候选待锁·5m·三买');
  assert.equal(legacyEvidenceOnly.text, '▲接近·5m·三买');
});

test('盘整背驰和趋势背驰不再使用相同字重', () => {
  const api = loadStyleApi();
  const consolidation = api.getStrictDivergenceVisual({
    structural_level: 0, level_label: '5m', kind: 'consolidation', direction: 'down',
  });
  const trend = api.getStrictDivergenceVisual({
    structural_level: 0, level_label: '5m', kind: 'trend', direction: 'up',
  });
  assert.equal(consolidation.text, '▲5m·盘整背驰');
  assert.equal(consolidation.fontsize, 12);
  assert.equal(consolidation.bold, false);
  assert.equal(trend.text, '▼5m·趋势背驰');
  assert.equal(trend.fontsize, 13);
  assert.equal(trend.bold, true);
  assert.notEqual(consolidation.color, trend.color);
});

test('设置菜单展示实际方向色而非错误的背驰级别色', () => {
  assert.ok(source.includes("_dualSwatch(getSignalColor('buy'), getSignalColor('sell'), '向上背驰 / 向下背驰')"));
  assert.ok(source.includes("_swatch(getSignalColor(item.key.endsWith('buy') ? 'buy' : 'sell'))"));
  assert.ok(source.includes("_dualSwatch(getSignalColor('fractalTop'), getSignalColor('fractalBottom'), '顶分型 / 底分型')"));
});

test('一键画线默认使用细线并在创建事件中兜底应用', () => {
  assert.match(source, /const ONE_CLICK_DRAW_LINE_WIDTH = 1;/);
  assert.match(source, /"linetooltrendline\.linewidth": ONE_CLICK_DRAW_LINE_WIDTH/);
  assert.match(source, /"linetoolrectangle\.linewidth": ONE_CLICK_DRAW_LINE_WIDTH/);
  assert.match(source, /ov\.linewidth = ONE_CLICK_DRAW_LINE_WIDTH/);
});
