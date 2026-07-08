'use strict';
// R1-C8 接线回归: 缠论显示菜单「全选/全清」不得误触「独立周期画线」开关——它是画线
// 存储模式切换(共享 all key vs 按周期 key)而非显示项, 误触会切换画线数据源致刷新后
// 手画图形被感知为「全部丢失」(数据仍在另一个 key 里)。jQuery 菜单 DOM 交互超出本
// harness 范围, 按 test_open_dedup 先例用源码接线扫描钉死两处循环的排除子句。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

test('全选/全清循环必须排除独立周期画线开关(R1-C8)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  const excluded = src.match(/input\[type="checkbox"\]'\)\.not\('#' \+ indCbId\)\.each/g) || [];
  assert.strictEqual(excluded.length, 2,
    '全选与全清两处循环都必须 .not(indCbId) 排除, 实际=' + excluded.length);
  const bare = src.match(/input\[type="checkbox"\]'\)\.each/g) || [];
  assert.strictEqual(bare.length, 0,
    '不得残留未排除独立画线开关的裸 checkbox 遍历, 实际=' + bare.length);
});