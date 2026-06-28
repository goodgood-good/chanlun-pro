'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const MacdStats = require('../macd_stats.js');

test('module 可被 require 且导出纯函数', () => {
  assert.ok(MacdStats, 'MacdStats 应被导出');
  assert.equal(typeof MacdStats._internal.computeStats, 'function');
  assert.equal(typeof MacdStats._internal.smartSearch, 'function');
});
