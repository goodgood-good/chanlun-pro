'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');

function rgb(hex) {
  const value = String(hex).replace('#', '');
  return [0, 2, 4].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16));
}

function colorDistance(left, right) {
  const a = rgb(left);
  const b = rgb(right);
  return Math.sqrt(a.reduce((sum, value, index) => sum + ((value - b[index]) ** 2), 0));
}

function declaration(pattern, label) {
  const match = source.match(pattern);
  assert.ok(match, `缺少 ${label}`);
  return match[0];
}

function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `缺少函数 ${name}`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`函数 ${name} 未闭合`);
}

function loadColorApi() {
  const script = `
    ${declaration(/const LEVEL_COLOR_CHAIN = \[[\s\S]*?\];/, '递归色链')}
    ${declaration(/const CHART_BI_INDEX = \{[^;]+\};/, '周期级别映射')}
    ${declaration(/const ELEMENT_CHAIN_OFFSET = \{[^;]+\};/, '结构级别偏移')}
    const DEFAULT_COLORS = {};
    ${extractFunction('chainColor')}
    ${extractFunction('chartBiIndex')}
    ${extractFunction('getDynamicColor')}
    this.api = { getDynamicColor };
  `;
  const context = {};
  vm.runInNewContext(script, context);
  return context.api;
}

test('笔与线段使用高对比颜色和轻量线宽层级', () => {
  const widths = source.match(/const BASE_STRUCTURE_LINE_WIDTHS = Object\.freeze\(\{\s*bis:\s*(\d+),\s*xds:\s*(\d+),\s*\}\)/);
  assert.ok(widths, '必须声明笔和线段的独立线宽');

  const { getDynamicColor } = loadColorApi();
  for (const interval of ['1', '5', '30', '1D']) {
    const penColor = getDynamicColor(interval, 'bis');
    const segmentColor = getDynamicColor(interval, 'xds');
    assert.notEqual(penColor, segmentColor, `${interval} 笔与线段不得同色`);
    assert.ok(colorDistance(penColor, segmentColor) >= 100, `${interval} 笔与线段颜色必须明显分离`);
  }

  assert.equal(Number(widths[1]), 1, '笔应保持 1px 细线');
  assert.equal(Number(widths[2]), 2, '线段应保持 2px，并只比笔粗一级');
});

test('相邻周期共享同一绝对结构级别颜色', () => {
  const { getDynamicColor } = loadColorApi();
  for (const [lower, higher] of [['1', '5'], ['5', '30'], ['30', '1D']]) {
    assert.equal(
      getDynamicColor(lower, 'xds'),
      getDynamicColor(higher, 'bis'),
      `${lower} 线段必须与 ${higher} 笔同色`,
    );
  }
});

test('实际绘制和菜单色块共用同一基础结构样式', () => {
  assert.ok(source.includes("const biLineStyle = getBaseStructureStyle(currentInterval, 'bis')"));
  assert.ok(source.includes("const xdLineStyle = getBaseStructureStyle(currentInterval, 'xds')"));
  assert.ok(source.includes('const item = baseStructureRenderItem(rawItem)'));
  assert.ok(source.includes('ChartUtils.createPathShape(this.chart, item, biLineStyle)'));
  assert.ok(source.includes('ChartUtils.createPathShape(this.chart, item, xdLineStyle)'));
  assert.ok(source.includes('shape: "path"'), '批量基础结构必须使用不会自动首尾闭合的开放路径');
  assert.ok(!source.includes('createPolylineShape('), '基础结构不得恢复会产生首尾伪连线的多边形图元');
  assert.ok(!source.includes("_shapeKind: 'polyline'"));
  assert.ok(source.includes('if (!baseStructureLineIsUnfinished(item)) return item;'));
  assert.ok(source.includes('linestyle: CHART_CONFIG.LINE_STYLES.DASHED'));
  assert.ok(source.includes('color: getDynamicColor(interval, elementType)'));
  assert.ok(source.includes('transparency: 0'), '自动结构线必须完全不透明');
});
