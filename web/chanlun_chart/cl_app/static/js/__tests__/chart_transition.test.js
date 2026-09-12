"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Transition = require("../chart_transition.js");

function element(hidden = false) {
  return {
    hidden,
    dataset: {},
    textContent: "",
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
  };
}

function fixture() {
  const nodes = {
    tv_chart_transition: element(true),
    tv_charts_area: element(false),
    tv_chart_transition_title: element(false),
    tv_chart_transition_detail: element(false),
  };
  let nextTimer = 0;
  const timers = new Map();
  const env = {
    document: { getElementById: (id) => nodes[id] || null },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  return { env, nodes, timers };
}

test("standalone chart stays covered until every switching chart is stably ready", () => {
  const { env, nodes } = fixture();
  Transition.install(env);

  assert.equal(Transition.begin({ market: "a", code: "SH.600000", expected: 2 }, env), true);
  assert.equal(nodes.tv_chart_transition.hidden, false);
  assert.equal(nodes.tv_charts_area.attributes["aria-busy"], "true");
  assert.equal(Transition.markReady({ symbol: "A:SZ.000001", managerId: "one" }, env), false);
  assert.equal(Transition.markReady({ symbol: "A:SH.600000", managerId: "one" }, env), true);
  assert.equal(nodes.tv_chart_transition.hidden, false);
  assert.equal(Transition.markReady({ symbol: "A:SH.600000", managerId: "two" }, env), true);
  assert.equal(nodes.tv_chart_transition.hidden, true);
  assert.equal(nodes.tv_charts_area.attributes["aria-busy"], "false");
});

test("a newer symbol invalidates stale ready notifications", () => {
  const { env, nodes } = fixture();
  Transition.install(env);
  Transition.begin({ market: "a", code: "SH.600000" }, env);
  Transition.begin({ market: "a", code: "SZ.000001" }, env);

  assert.equal(Transition.markReady({ symbol: "A:SH.600000", managerId: "one" }, env), false);
  assert.equal(nodes.tv_chart_transition.hidden, false);
  assert.equal(Transition.markReady({ symbol: "A:SZ.000001", managerId: "one" }, env), true);
  assert.equal(nodes.tv_chart_transition.hidden, true);
});

test('current history failures explain the missing candles immediately and ignore another symbol', () => {
  const { env, nodes, timers } = fixture();
  const handlers = new Map();
  env.addEventListener = (type, handler) => handlers.set(type, handler);
  Transition.install(env);
  Transition.begin({ market: 'us', code: 'AAPL.US' }, env);
  const fail = handlers.get('chanlun-history-error');
  fail({ detail: { symbol: 'us:TSLA.US', message: 'old error' } });
  assert.notEqual(nodes.tv_chart_transition.dataset.state, 'error');
  fail({ detail: { symbol: 'us:AAPL.US', message: '请检查行情连接后重新加载数据。' } });
  assert.equal(nodes.tv_chart_transition.dataset.state, 'error');
  assert.match(nodes.tv_chart_transition_detail.textContent, /行情连接/);
  assert.equal(timers.size, 0);
});

test('the first page reports its own history failure before any symbol switch', () => {
  const { env, nodes } = fixture();
  const handlers = new Map();
  env.addEventListener = (type, handler) => handlers.set(type, handler);
  env.__cm = { 0: { instanceId: 'first-chart', widget: {
    symbolInterval: () => ({ symbol: 'us:AAPL.US', interval: '1' }),
  } } };
  Transition.install(env);
  const fail = handlers.get('chanlun-history-error');
  fail({detail:{symbol:'us:AAPL.US',resolution:'5',managerId:'first-chart',message:'old period'}});
  assert.equal(nodes.tv_chart_transition.hidden, true);
  fail({detail:{symbol:'us:AAPL.US',resolution:'1',managerId:'first-chart',message:'暂未获取到 K 线'}});
  assert.equal(nodes.tv_chart_transition.dataset.state, 'error');
  assert.equal(nodes.tv_chart_transition_detail.textContent, '暂未获取到 K 线');
});
