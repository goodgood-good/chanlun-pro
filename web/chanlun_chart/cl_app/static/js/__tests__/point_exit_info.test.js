'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const info = require('../point_exit_info.js');

test('missing targets are waiting, not a zero price or promised percentage', () => {
  const result = info.describe({point_status:'confirmed', side:'buy',
    stop_loss:{price:10.125,trigger:'lte',basis:'center_edge'},take_profit:{price:null}});
  assert.equal(result.stop,'≤ 10.125');
  assert.equal(result.profit,'待同级反向点');
  assert.match(result.lines.join(' '), /当前没有可确定的固定目标价/);
  assert.equal(info.price(null),'—');
});

test('future confirmed point prices disclose recognition time and possible losing exits', () => {
  const result = info.describe({point_status:'confirmed', side:'sell',
    stop_loss:{price:13.5,trigger:'gte'},take_profit:{price:14.5,basis:'opposite_point',point_type:'2buy',available_at:1800000000}});
  assert.equal(result.stop,'≥ 13.50');
  assert.match(result.lines.join(' '), /确认于/);
  assert.match(result.lines.join(' '), /退出可能亏损/);
  assert.match(result.lines.join(' '), /不表示已建立空头仓位/);
});

test('pending first-point prices identify provisional envelope observations and source location', () => {
  const result = info.describe({point_status:'approaching', side:'buy',
    stop_loss:{price:0.901,trigger:'lt',sources:['L22']},
    take_profit:{price:1.003,basis:'terminal_dd',sources:['L29']}},
    {L29:{title:'第29课',path:'D:/缠论/example.md',lines:'190–268'}});
  assert.equal(result.profit,'1.003（DD观察）（预案）');
  assert.match(result.lines.join(' '), /尚未确认/);
  assert.match(result.sources.join(' '), /D:\/缠论\/example.md.*190–268/);
});
