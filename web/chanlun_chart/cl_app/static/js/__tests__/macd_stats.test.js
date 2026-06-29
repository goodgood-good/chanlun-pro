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
  assert.equal(bucketKeyOf(1800, '5m', 8), 6);      // 1800s → floor(1800/300)
  assert.equal(bucketKeyOf(3600, '30m', 8), 2);     // 3600s → floor(3600/1800)
  // 毫秒归一:真实量级毫秒(≥1e10)先 /1000 再整除,与对应秒级同桶
  assert.equal(bucketKeyOf(1700001000000, '5m', 8), 5666670); // 1.7e12 ms → 1.7e9 s
  assert.equal(bucketKeyOf(1700001000, '5m', 8), 5666670);    // 同值(秒级)
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

test('computeSegmentSlopes: 上行>0 下行<0 且按区间裁剪', () => {
  const { computeSegmentSlopes } = MacdStats._internal;
  const times = [];
  for (let i = 0; i < 20; i++) times.push(i * 300);
  const xds = [
    { points: [{ price: 10, time: 0 },    { price: 14, time: 300 * 4 }] },  // 上行 idx0→4, slope=(14-10)/4=1
    { points: [{ price: 14, time: 300 * 4 }, { price: 8, time: 300 * 10 }] }, // 下行 idx4→10, slope=(8-14)/6=-1
    { points: [{ price: 8, time: 300 * 16 }, { price: 9, time: 300 * 19 }] }, // 区间外(>endIdx=12)
  ];
  const r = computeSegmentSlopes(times, xds, 0, 12);
  assert.equal(r.length, 2);              // 第三根在区间外被裁掉
  assert.equal(r[0].slope, 1);
  assert.equal(r[0].dir, 'up');
  assert.equal(r[1].slope, -1);
  assert.equal(r[1].dir, 'down');
});

test('resolveHigherFreq: TV resolution → 高周期 frequency(生产场景)', () => {
  const { resolveHigherFreq } = MacdStats._internal;
  assert.equal(resolveHigherFreq('1'), '5m');    // 1m 图 → HTF 5m
  assert.equal(resolveHigherFreq('5'), '30m');   // 5m → 30m
  assert.equal(resolveHigherFreq('30'), 'd');    // 30m → 日
  assert.equal(resolveHigherFreq('1D'), 'w');    // 日 → 周
  assert.equal(resolveHigherFreq('1M'), 'y');    // 月 → 年(不与分钟 '1' 混淆)
  assert.equal(resolveHigherFreq('15'), null);   // 15m 无高周期
  assert.equal(resolveHigherFreq('60'), null);   // 60m 无高周期
});

test('computeSegmentSlopes: 生产单位 times(毫秒) vs xds(秒) 仍对齐', () => {
  const { computeSegmentSlopes } = MacdStats._internal;
  const times = [];
  for (let i = 0; i < 20; i++) times.push((1700000000 + i * 300) * 1000); // 毫秒
  const xds = [
    { points: [{ price: 10, time: 1700000000 }, { price: 14, time: 1700000000 + 300 * 4 }] }, // 秒
  ];
  const r = computeSegmentSlopes(times, xds, 0, 12);
  assert.equal(r.length, 1);
  assert.equal(r[0].slope, 1);   // (14-10)/4
  assert.equal(r[0].dir, 'up');
});

test('peakAbs: 同向峰值绝对值,无数据返回 null', () => {
  const { peakAbs } = MacdStats._internal;
  assert.equal(peakAbs(0.9, -0.2), 0.9);   // |0.9| > |−0.2|
  assert.equal(peakAbs(0.2, -0.7), 0.7);   // |−0.7| > |0.2|
  assert.equal(peakAbs(0.5, null), 0.5);   // 只一侧有数据
  assert.equal(peakAbs(null, -0.3), 0.3);
  assert.strictEqual(peakAbs(null, null), null); // 两侧皆无 → null(面板显示 "-")
});
