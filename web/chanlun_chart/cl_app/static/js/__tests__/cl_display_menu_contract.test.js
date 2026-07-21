'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');

test('缠论显示菜单使用固定分组顺序和正式名称', () => {
  const titles = ['基础结构', '中枢', '走势类型', '买卖点', '背驰', '画线设置'];
  const indexes = titles.map((title) => source.indexOf(`_grpTitle('${title}'`));
  assert.ok(indexes.every((index) => index >= 0), `missing titles: ${indexes}`);
  assert.deepEqual([...indexes].sort((a, b) => a - b), indexes);

  for (const label of [
    '笔中枢',
    '中枢总开关',
    '走势类型总开关',
    '买卖点总开关',
    '背驰总开关',
    '盘整背驰',
    '趋势背驰',
    '独立周期画线',
  ]) {
    assert.ok(source.includes(label), `missing menu label: ${label}`);
  }
});

test('菜单不暴露接近触发或中枢投影复选框', () => {
  assert.equal(source.includes('接近触发（未确认）'), false);
  assert.equal(source.includes("_cbRow('center_projection'"), false);
  assert.equal(source.includes("cbId('center_projection')"), false);
});
