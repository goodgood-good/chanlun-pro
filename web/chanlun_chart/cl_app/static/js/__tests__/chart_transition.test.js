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
    style: {}, children: [], handlers: {},
    appendChild(child) { this.children.push(child); },
    addEventListener(type, callback) { this.handlers[type] = callback; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
  };
}

function fixture() {
  const nodes = {
    tv_chart_transition: element(true),
    tv_charts_area: element(false),
    tv_chart_transition_title: element(false),
    tv_chart_transition_detail: element(false),
    spinner: element(false), card: element(false),
  };
  nodes.tv_chart_transition.querySelector = selector => selector.endsWith('__spinner') ? nodes.spinner : nodes.card;
  let nextTimer = 0;
  const timers = new Map();
  const env = {
    document: { getElementById: (id) => nodes[id] || null, createElement: () => element(false) },
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
  assert.equal(nodes.tv_charts_area.attributes['aria-busy'], 'false');
  assert.equal(nodes.spinner.hidden, true);
  assert.equal(nodes.spinner.style.animation, 'none');
  assert.equal(nodes.card.children[0].style.display, 'flex');
});

test('a bounded loading deadline becomes an idle error, and late matching ready can recover',()=>{
 const {env,nodes,timers}=fixture(); const state=Transition.install(env);
 state.begin({market:'us',code:'QQQ.US'});
 assert.equal(nodes.card.children[0].hidden,true);
 assert.equal(nodes.card.children[0].style.display,'none');
 const expire=[...timers.values()][0];expire();
 assert.equal(state.active,false);assert.equal(state.failed,true);
 assert.equal(nodes.spinner.style.display,'none');
 assert.equal(nodes.tv_charts_area.attributes['aria-busy'],'false');
 assert.match(nodes.tv_chart_transition_detail.textContent,/超时/);
 assert.equal(state.markReady({symbol:'us:TSLA.US',managerId:'one'}),false);
 assert.equal(state.markReady({symbol:'us:QQQ.US',managerId:'one'}),true);
 assert.equal(nodes.tv_chart_transition.hidden,true);
 assert.equal(state.failed,false);
});

test('manual retry invokes each current chart once and remains covered until ready',async()=>{
 const {env,nodes}=fixture();let calls=0;
 env.__cm={one:{widget:{symbolInterval:()=>({symbol:'us:QQQ.US',interval:'1'})},manualReloadData:async()=>{calls++;return true;}}};
 const state=Transition.install(env);state.begin({market:'us',code:'QQQ.US'});state.fail('连接上限');
 const action=state.retry();assert.equal(await state.retry(),false);
 assert.equal(await action,true);assert.equal(calls,1);
 assert.equal(nodes.tv_chart_transition.dataset.state,'loading');
 assert.equal(nodes.spinner.hidden,false);
 assert.equal(nodes.card.children[0].hidden,true);
 state.markReady({symbol:'us:QQQ.US',managerId:'one'});
 assert.equal(nodes.tv_chart_transition.hidden,true);
 state.begin({market:'us',code:'TSLA.US'});state.fail('failed');
 nodes.card.children[0].children[1].handlers.click();
 assert.equal(nodes.tv_chart_transition.hidden,true);
 assert.equal(nodes.tv_charts_area.attributes['aria-busy'],'false');
});

test('a rejected old retry cannot reopen feedback after a new symbol is ready',async()=>{
 const {env,nodes}=fixture();let reject;
 env.__cm={one:{widget:{symbolInterval:()=>({symbol:'us:QQQ.US',interval:'1'})},manualReloadData:()=>new Promise((_,r)=>{reject=r;})}};
 const state=Transition.install(env);state.begin({market:'us',code:'QQQ.US'});state.fail('failed');
 const pending=state.retry();await Promise.resolve();
 state.begin({market:'us',code:'TSLA.US'});state.markReady({symbol:'us:TSLA.US',managerId:'one'});
 reject(new Error('old failure'));await pending;
 assert.equal(nodes.tv_chart_transition.hidden,true);
 assert.equal(nodes.tv_chart_transition.dataset.state,'idle');
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
