'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const MacdStats = require('../macd_stats.js');

test('module 可被 require 且导出纯函数', () => {
  assert.ok(MacdStats, 'MacdStats 应被导出');
  assert.equal(typeof MacdStats._internal.computeStats, 'function');
  assert.equal(typeof MacdStats._internal.smartSearch, 'function');
});

test('computeStats: 面积×2 与黄白线极值', () => {
  const { computeStats } = MacdStats._internal;
  // 4 根: hist 红2、红3、绿-1、绿-4(末根不排除)
  const times = [100, 200, 300, 400];
  const hist  = [2, 3, -1, -4];
  const dif   = [0.5, 0.9, -0.2, -0.7];
  const dea   = [0.4, 0.6, 0.1, -0.3];
  const r = computeStats(times, hist, 0, 3, { difArr: dif, deaArr: dea, excludeLast: false });
  assert.equal(r.posArea, 5);          // 2+3
  assert.equal(r.negArea, 5);          // |−1|+|−4|
  assert.equal(r.netArea, 0);          // 5−5
  assert.equal(r.posAreaX2, 10);
  assert.equal(r.negAreaX2, 10);
  assert.equal(r.netAreaX2, 0);
  assert.equal(r.posMax, 3);           // 红柱峰
  assert.equal(r.negMin, -4);          // 绿柱谷
  assert.equal(r.difMax, 0.9);
  assert.equal(r.difMin, -0.7);
  assert.equal(r.deaMax, 0.6);
  assert.equal(r.deaMin, -0.3);
});
