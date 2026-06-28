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

test('bucketKeyOf: 分钟级与后端整除一致', () => {
  const { bucketKeyOf } = MacdStats._internal;
  assert.equal(bucketKeyOf(1800, '5m', 8), 6);      // floor(1800/300)
  assert.equal(bucketKeyOf(3600, '30m', 8), 2);     // floor(3600/1800)
  assert.equal(bucketKeyOf(1800000, '5m', 8), 6);   // 毫秒归一到秒
});

test('reduceToBuckets: 每桶取桶末根真值', () => {
  const { reduceToBuckets } = MacdStats._internal;
  // 5m 周期(间隔300s)→ higher 30m(每6根一桶)。两桶,桶末根 idx=5、idx=8。
  const times = [];
  for (let i = 0; i < 9; i++) times.push(i * 300);
  const arr = [1, 1.2, 1.4, 1.6, 1.8, 2.0,  /*桶1末根*/ 0.9, 1.1, 1.3 /*桶2末根*/];
  const buckets = reduceToBuckets(times, arr, 0, 8, '30m', 8);
  assert.equal(buckets.length, 2);
  assert.deepEqual(buckets[0], { idx: 5, value: 2.0 });
  assert.deepEqual(buckets[1], { idx: 8, value: 1.3 });
});

test('computeStatsHTF: 按桶末真值统计,不被 bar 数放大', () => {
  const { computeStatsHTF } = MacdStats._internal;
  const times = [];
  for (let i = 0; i < 12; i++) times.push(i * 300); // 5m→30m,2整桶(每6根)
  // 桶末根: idx5=2.0(红), idx11=-1.5(绿)。其余是插值噪声,不应计入。
  const hHist = [0.1,0.5,1.0,1.5,1.8,2.0, -0.1,-0.5,-0.9,-1.1,-1.3,-1.5];
  const hDif  = [0,0,0,0,0,0.8,  0,0,0,0,0,-0.6];
  const hDea  = [0,0,0,0,0,0.3,  0,0,0,0,0,-0.2];
  const r = computeStatsHTF(times, hHist, hDif, hDea, 0, 11, '30m', 8, { excludeLast: false });
  assert.equal(r.bucketCount, 2);
  assert.equal(r.posArea, 2.0);    // 只桶末红真值
  assert.equal(r.negArea, 1.5);    // |桶末绿真值|
  assert.equal(r.posMax, 2.0);
  assert.equal(r.negMin, -1.5);
  assert.equal(r.difMax, 0.8);
  assert.equal(r.deaMin, -0.2);
});
