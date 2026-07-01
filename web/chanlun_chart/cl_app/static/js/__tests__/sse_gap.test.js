'use strict';
// sse_gap.js 的纯逻辑单测：断网恢复后 SSE 首帧领先 TV 图表多根 K 线时，
// 应判定为"断档"，让 charts.js 触发一次 resetData 全量补齐（而非单根 feed 补不齐）。
//
// 关键：用"完整快照 data.t 中真实缺失的成根数"判定，而非仅墙钟差——否则交易时段
// 边界(午休/隔夜/周末)墙钟差大但中间无缺根，会被误判为断档而每天无谓闪烁。
const { test } = require('node:test');
const assert = require('node:assert');
const SseGap = require('../sse_gap.js');

test('module 可被 require 且导出纯函数', () => {
  assert.ok(SseGap, 'SseGap 应被导出');
  assert.equal(typeof SseGap._internal.resolutionToPeriodSeconds, 'function');
  assert.equal(typeof SseGap._internal.latestBarSec, 'function');
  assert.equal(typeof SseGap._internal.detectGap, 'function');
  assert.equal(typeof SseGap._internal.countMissingBars, 'function');
  assert.equal(typeof SseGap._internal.shouldResetForGap, 'function');
  assert.equal(typeof SseGap._internal.marketLikelyTrading, 'function');
  assert.equal(typeof SseGap._internal.computeWatchdogReset, 'function');
  assert.equal(typeof SseGap._internal.resetAllowed, 'function');
  assert.equal(typeof SseGap._internal.nextBackoffLevel, 'function');
  assert.equal(typeof SseGap._internal.computeResetGate, 'function');
});

test('resolutionToPeriodSeconds: TV resolution → 单根周期秒数', () => {
  const { resolutionToPeriodSeconds: p } = SseGap._internal;
  assert.equal(p('1'), 60);             // 1m
  assert.equal(p('5'), 300);            // 5m
  assert.equal(p('30'), 1800);          // 30m
  assert.equal(p('60'), 3600);          // 60m
  assert.equal(p('1D'), 86400);         // 日
  assert.equal(p('1W'), 7 * 86400);     // 周
  assert.equal(p('1M'), 31 * 86400);    // 月(近似,不与分钟 '1' 混淆)
  assert.equal(p('bad'), 0);            // 无法解析 → 0(调用方据此退回原行为,不 reset)
  assert.equal(p(''), 0);
  assert.equal(p(null), 0);
});

test('latestBarSec: 取 t 数组末根(秒);毫秒量级自动归一', () => {
  const { latestBarSec } = SseGap._internal;
  assert.equal(latestBarSec([700, 1000, 1300]), 1300);
  assert.equal(latestBarSec([]), null);
  assert.equal(latestBarSec(null), null);
  assert.equal(latestBarSec(undefined), null);
  assert.equal(latestBarSec([1700000000000]), 1700000000); // ms → s 防御
});

test('detectGap(墙钟粗筛): 实时正常推进(领先 0 或 1 根)不触发', () => {
  const { detectGap } = SseGap._internal;
  const P = 300; // 5m
  assert.equal(detectGap(1000, 1000, P), false);        // 同根更新, gap=0
  assert.equal(detectGap(1000 + P, 1000, P), false);    // 领先 1 根(正常新 bar, feedBar 可补)
});

test('detectGap(墙钟粗筛): 领先 ≥2 根(墙钟)进入精判', () => {
  const { detectGap } = SseGap._internal;
  const P = 300;
  assert.equal(detectGap(1000 + 2 * P, 1000, P), true);   // 墙钟领先 2 根
  assert.equal(detectGap(1000 + 50 * P, 1000, P), true);  // 墙钟领先很多
});

test('detectGap: 边界与无效输入安全退化为 false', () => {
  const { detectGap } = SseGap._internal;
  const P = 300;
  assert.equal(detectGap(1000, null, P), false);  // TV 无数据(首次加载)→ 走正常 getBars
  assert.equal(detectGap(1000, 0, P), false);
  assert.equal(detectGap(1000, 2000, P), false);  // SSE 落后于 TV
  assert.equal(detectGap(5000, 1000, 0), false);  // 周期未知 → 退回原行为
  assert.equal(detectGap(null, 1000, P), false);  // SSE 无最新值
});

test('countMissingBars: 数 (tvLatest, sseLatest) 开区间内的真实成根', () => {
  const { countMissingBars: c } = SseGap._internal;
  assert.equal(c([700, 1000, 1300, 1600], 1000, 1600), 1);        // 仅 1300 落在开区间
  assert.equal(c([1000, 1300, 1600, 1900, 2200], 1000, 2200), 3); // 1300,1600,1900
  assert.equal(c([1000, 4000], 1000, 4000), 0);                   // 中间无根(时段边界)
  assert.equal(c([700, 1000], 700, 1000), 0);                     // 正常推进 1 根
  assert.equal(c(null, 1, 2), 0);
  assert.equal(c([], 1, 2), 0);
  // ms 量级逐元素归一: 中间恰 1 根
  assert.equal(c([1700000000000, 1700000300000, 1700000600000], 1700000000, 1700000600), 1);
});

test('shouldResetForGap: 断网恢复(完整序列中间确有缺根)→ 需要 reset', () => {
  const { shouldResetForGap } = SseGap._internal;
  // 5m, TV 停在 1000(断网前末根), SSE 首帧完整序列推到 1000+10*300, 中间缺 9 根
  const t = [];
  for (let i = 0; i <= 10; i++) t.push(1000 + i * 300);
  assert.equal(shouldResetForGap(t, 1000, '5'), true);
});

test('shouldResetForGap: 正常推进 1 根 → 不 reset(避免闪烁)', () => {
  const { shouldResetForGap } = SseGap._internal;
  const t = [700, 1000]; // 末根 1000 比 TV(700)领先 1 根, 中间无缺根
  assert.equal(shouldResetForGap(t, 700, '5'), false);
});

test('shouldResetForGap: 交易时段边界(墙钟差大但中间无缺根)→ 不 reset', () => {
  const { shouldResetForGap } = SseGap._internal;
  // A股午休: TV 末根停在 11:30, 13:00 首根出现; 墙钟差 5400s >> 1.5*300=450s,
  // 但 11:30→13:00 数据上相邻(午休无成根) → 不应误判断档闪烁。
  const t1130 = 1700000000;
  const t1300 = t1130 + 5400;
  const t = [t1130 - 600, t1130 - 300, t1130, t1300]; // 11:20,11:25,11:30,13:00
  assert.equal(shouldResetForGap(t, t1130, '5'), false);
});

test('shouldResetForGap: 跨时段的真断网(中间确有缺根)仍 reset', () => {
  const { shouldResetForGap } = SseGap._internal;
  // TV 停在 10:00, 断网期间 10:05/10:10/10:15 成根但前端没收到 → data.t 含这些缺根
  const t1000 = 1700000000;
  const t = [t1000, t1000 + 300, t1000 + 600, t1000 + 900];
  assert.equal(shouldResetForGap(t, t1000, '5'), true);
});

test('shouldResetForGap: TV 毫秒时间戳由调用方归一后传入仍正确', () => {
  const { shouldResetForGap } = SseGap._internal;
  const tvLatestSec = Math.round(1700000000000 / 1000); // 1700000000
  const t = [1700000000, 1700000300, 1700000600, 1700000900]; // 完整序列,中间确有缺根
  assert.equal(shouldResetForGap(t, tvLatestSec, '5'), true);
});

// ── 第二轮(治本)：墙钟看门狗 + 防抖/退避 + 交易时段门控 ──

test('marketLikelyTrading: crypto 24x7 恒为交易中', () => {
  const { marketLikelyTrading: m } = SseGap._internal;
  const sat = Math.floor(Date.UTC(2024, 0, 6, 3, 0, 0) / 1000); // 周六
  assert.equal(m('currency', sat), true);
  assert.equal(m('currency_spot', sat), true);
});

test('marketLikelyTrading: A股 上午盘/午休/下午盘/收盘后/周末', () => {
  const { marketLikelyTrading: m } = SseGap._internal;
  const s = (d, h, mi) => Math.floor(Date.UTC(2024, 0, d, h, mi, 0) / 1000);
  assert.equal(m('a', s(8, 2, 0)), true);   // 周一 10:00 北京(上午盘)
  assert.equal(m('a', s(8, 4, 0)), false);  // 周一 12:00 北京(午休)
  assert.equal(m('a', s(8, 6, 0)), true);   // 周一 14:00 北京(下午盘)
  assert.equal(m('a', s(8, 7, 30)), false); // 周一 15:30 北京(收盘后)
  assert.equal(m('a', s(6, 2, 0)), false);  // 周六 10:00 北京
});

test('marketLikelyTrading: 美股 盘中/盘前/盘后/周末(ET, 1月EST)', () => {
  const { marketLikelyTrading: m } = SseGap._internal;
  const s = (d, h) => Math.floor(Date.UTC(2024, 0, d, h, 0, 0) / 1000);
  assert.equal(m('us', s(8, 15)), true);   // 周一 10:00 ET = 15:00 UTC
  assert.equal(m('us', s(8, 13)), false);  // 周一 08:00 ET(盘前)
  assert.equal(m('us', s(8, 22)), false);  // 周一 17:00 ET(盘后)
  assert.equal(m('us', s(6, 15)), false);  // 周六
});

test('marketLikelyTrading: 未知市场(24x5近似)工作日 true / 周末 false', () => {
  const { marketLikelyTrading: m } = SseGap._internal;
  const s = (d) => Math.floor(Date.UTC(2024, 0, d, 3, 0, 0) / 1000);
  assert.equal(m('futures', s(8)), true);     // 周一
  assert.equal(m('fx', s(6)), false);         // 周六
  assert.equal(m('ny_futures', s(7)), false); // 周日
});

test('computeWatchdogReset: 交易时段 + 画布末根严重失速 → true', () => {
  const { computeWatchdogReset: w } = SseGap._internal;
  const now = Math.floor(Date.UTC(2024, 0, 8, 2, 30, 0) / 1000); // 周一 10:30 北京
  const P = 300; // 5m
  assert.equal(w('a', now - 10 * P, now, P), true);  // 落后 10 根 = 失速
  assert.equal(w('a', now - P, now, P), false);      // 落后 1 根 = 正常
});

test('computeWatchdogReset: 非交易时段不 reset(防收盘/周末误闪)', () => {
  const { computeWatchdogReset: w } = SseGap._internal;
  const sat = Math.floor(Date.UTC(2024, 0, 6, 2, 30, 0) / 1000); // 周六
  const P = 300;
  assert.equal(w('a', sat - 1000 * P, sat, P), false); // 即便严重落后, 周末也不 reset
});

test('computeWatchdogReset: 无效输入安全退化 false', () => {
  const { computeWatchdogReset: w } = SseGap._internal;
  const now = Math.floor(Date.UTC(2024, 0, 8, 2, 30, 0) / 1000);
  assert.equal(w('a', null, now, 300), false);    // 无画布末根
  assert.equal(w('a', now - 9999, now, 0), false); // 周期未知
});

test('resetAllowed: 防抖最小间隔 + 退避放大', () => {
  const { resetAllowed } = SseGap._internal;
  assert.equal(resetAllowed(1000, null, 30, 0), true);   // 首次无上次 reset
  assert.equal(resetAllowed(1020, 1000, 30, 0), false);  // 20s < 30s
  assert.equal(resetAllowed(1031, 1000, 30, 0), true);   // 31s ≥ 30s
  assert.equal(resetAllowed(1050, 1000, 30, 1), false);  // 退避 level=1 → 间隔 60s, 50s 不够
  assert.equal(resetAllowed(1061, 1000, 30, 1), true);   // 61s ≥ 60s
});

test('nextBackoffLevel: 有效归零 / 无效递增封顶', () => {
  const { nextBackoffLevel } = SseGap._internal;
  assert.equal(nextBackoffLevel(3, true), 0);    // reset 生效(画布前进)→ 归零
  assert.equal(nextBackoffLevel(0, false), 1);   // 无效 → +1
  assert.equal(nextBackoffLevel(5, false), 6);   // 递增
  assert.equal(nextBackoffLevel(6, false), 6);   // 封顶 6
  assert.equal(nextBackoffLevel(10, false), 6);  // 超出也封顶
});

test('computeResetGate: 首次无上次 reset → 允许, 退避 0, 记账当前', () => {
  const { computeResetGate } = SseGap._internal;
  const r = computeResetGate(null, 1000, 5000, 30);
  assert.equal(r.allow, true);
  assert.equal(r.state.lastResetSec, 1000);
  assert.equal(r.state.backoffLevel, 0);
  assert.equal(r.state.lastViewLatestSec, 5000);
});

test('computeResetGate: 防抖窗口内 → 拒绝且不消耗退避(M-2)', () => {
  const { computeResetGate } = SseGap._internal;
  const prev = { lastResetSec: 1000, backoffLevel: 2, lastViewLatestSec: 5000 };
  const r1 = computeResetGate(prev, 1010, 5000, 30); // 10s < 30*2^2=120s
  assert.equal(r1.allow, false);
  assert.equal(r1.state.backoffLevel, 2);  // 被拒不抬退避
  const r2 = computeResetGate(r1.state, 1020, 5000, 30); // 再次被拒
  assert.equal(r2.allow, false);
  assert.equal(r2.state.backoffLevel, 2);  // 仍不变
  assert.equal(r2.state.lastResetSec, 1000); // 未 reset, 上次时间戳不动
});

test('computeResetGate: 防抖过 + 上次生效(画布前进) → 允许, 退避归零', () => {
  const { computeResetGate } = SseGap._internal;
  const prev = { lastResetSec: 1000, backoffLevel: 3, lastViewLatestSec: 5000 };
  // 间隔 30*2^3=240s, now=1300(300s>240) 放行; viewLatest 6000>5000 前进 → 上次生效
  const r = computeResetGate(prev, 1300, 6000, 30);
  assert.equal(r.allow, true);
  assert.equal(r.state.backoffLevel, 0);
  assert.equal(r.state.lastResetSec, 1300);
  assert.equal(r.state.lastViewLatestSec, 6000);
});

test('computeResetGate: 防抖过 + 上次无效(画布没动) → 允许, 退避 +1', () => {
  const { computeResetGate } = SseGap._internal;
  const prev = { lastResetSec: 1000, backoffLevel: 1, lastViewLatestSec: 5000 };
  // 间隔 30*2=60s, now=1100(100s>60) 放行; viewLatest 5000 没前进 → 上次无效
  const r = computeResetGate(prev, 1100, 5000, 30);
  assert.equal(r.allow, true);
  assert.equal(r.state.backoffLevel, 2);
});
