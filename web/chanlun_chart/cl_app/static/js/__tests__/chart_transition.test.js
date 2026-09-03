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
    __CHANLUN_EMBEDDED_CHART: false,
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

test("embedded charts leave visibility control to their parent page", () => {
  const { env } = fixture();
  env.__CHANLUN_EMBEDDED_CHART = true;
  assert.equal(Transition.install(env), null);
  assert.equal(Transition.begin({ market: "a", code: "SH.600000" }, env), false);
});
