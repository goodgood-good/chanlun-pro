'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const MacdStats = require('../macd_stats.js');

const M = { 'd': '1d', '1d': 'd', 'w': '1w', '1w': 'w', 'm': '1m', '1m': 'm', '1440': '1d', '240': '4h' };

test('C14: 5m 图不命中 15m 条目(endsWith 过匹配根因)', () => {
  const { intervalKeyMatch } = MacdStats._internal;
  // key = ticker + resolution 无分隔; 5m 图 itv='5' 不应命中 '...15'
  assert.equal(intervalKeyMatch('us:qqq.us15', 'us:qqq.us', '5', M), false);
  assert.equal(intervalKeyMatch('us:qqq.us5', 'us:qqq.us', '5', M), true);
  assert.equal(intervalKeyMatch('us:qqq.us15', 'us:qqq.us', '15', M), true);
});

test('C14: ticker 以数字结尾也精确(基于 code 长度切片, 非数字边界)', () => {
  const { intervalKeyMatch } = MacdStats._internal;
  assert.equal(intervalKeyMatch('a:sh.0000015', 'a:sh.000001', '5', M), true);
  assert.equal(intervalKeyMatch('a:sh.00000115', 'a:sh.000001', '5', M), false); // 15m 键
  assert.equal(intervalKeyMatch('a:sh.00000115', 'a:sh.000001', '15', M), true);
});

test('C14: 日线精确, 不命中 2d 条目', () => {
  const { intervalKeyMatch } = MacdStats._internal;
  assert.equal(intervalKeyMatch('a:sh.0000011d', 'a:sh.000001', 'd', M), true);
  assert.equal(intervalKeyMatch('a:sh.000001d', 'a:sh.000001', 'd', M), true);
  assert.equal(intervalKeyMatch('a:sh.0000012d', 'a:sh.000001', 'd', M), false);
});

test('C14: chanlun-freq 形式(5m)键仍匹配', () => {
  const { intervalKeyMatch } = MacdStats._internal;
  assert.equal(intervalKeyMatch('us:qqq.us5m', 'us:qqq.us', '5', M), true);
});