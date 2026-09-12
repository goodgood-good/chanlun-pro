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



















test('native centers remain solid and visually distinguish completion',()=>{
 const api=loadStyleApi();
 const ongoing=api.getCenterVisualStyle('formal',{state:'ongoing'});
 const complete=api.getCenterVisualStyle('formal',{state:'completed',third_class_confirmed:true});
 assert.equal(ongoing.linestyle,0);assert.equal(complete.linestyle,0);
 assert.ok(complete.transparency<ongoing.transparency);
});
