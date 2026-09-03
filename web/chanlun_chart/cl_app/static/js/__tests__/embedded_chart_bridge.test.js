"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const Bridge = require("../embedded_chart_bridge.js");

function bridgeEnvironment(identity = { symbol: "a:SH.600000", interval: "5" }) {
  const messages = [];
  let messageListener = null;
  const chart = {
    ready: true,
    symbol: () => identity.symbol,
    resolution: () => identity.interval,
    dataReady() { return this.ready; },
    setSymbol(value) { this.lastSymbol = value; },
  };
  const parent = {
    postMessage(message, origin) { messages.push({ message, origin }); },
  };
  const manager = {
    getCurrentChartIdentity: () => ({ ...identity }),
    activityChanges: [],
    deferredRealtime: [],
    resumedRealtime: [],
    setEmbeddedChartActive(active) { this.activityChanges.push(active); },
    deferEmbeddedRealtime(requestId) { this.deferredRealtime.push(requestId); },
    resumeEmbeddedRealtime(requestId) { this.resumedRealtime.push(requestId); },
  };
  const env = {
    __CHANLUN_EMBEDDED_CHART: true,
    __cm: { "1": manager },
    chart_widgets: [{ activeChart: () => chart }],
    location: { origin: "http://127.0.0.1:9900" },
    parent,
    addEventListener(type, listener) {
      if (type === "message") messageListener = listener;
    },
    setTimeout(callback) { callback(); return 1; },
    clearTimeout() {},
  };
  return {
    env,
    chart,
    messages,
    manager,
    dispatch(data, overrides = {}) {
      messageListener({
        origin: env.location.origin,
        source: parent,
        data,
        ...overrides,
      });
    },
  };
}

test("normalizes the parent switch contract and rejects malformed values", () => {
  assert.deepEqual(
    Bridge.normalizeSwitchMessage({
      type: Bridge.SWITCH_MESSAGE,
      version: Bridge.VERSION,
      requestId: "chart-7",
      market: "A",
      code: "sh.600203",
      frequency: "5",
    }),
    {
      requestId: "chart-7",
      market: "a",
      code: "SH.600203",
      frequency: "5m",
    },
  );
  assert.equal(Bridge.normalizeSwitchMessage(null), null);
  assert.equal(Bridge.normalizeSwitchMessage({ type: Bridge.SWITCH_MESSAGE }), null);
  assert.equal(Bridge.normalizeSwitchMessage({
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId: "x",
    market: "a",
    code: "<script>",
    frequency: "5m",
  }), null);
  assert.deepEqual(Bridge.normalizeActivityMessage({
    type: Bridge.ACTIVITY_MESSAGE,
    version: Bridge.VERSION,
    active: false,
  }), { active: false });
  assert.equal(Bridge.normalizeActivityMessage({
    type: Bridge.ACTIVITY_MESSAGE,
    version: Bridge.VERSION,
    active: "false",
  }), null);
  assert.deepEqual(Bridge.normalizeRealtimeResumeMessage({
    type: Bridge.REALTIME_RESUME_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-7",
  }), { requestId: "chart-7" });
  assert.equal(Bridge.normalizeRealtimeResumeMessage({
    type: Bridge.REALTIME_RESUME_MESSAGE,
    version: Bridge.VERSION,
  }), null);
});

test("pauses and resumes embedded chart work from the parent activity contract", () => {
  const fixture = bridgeEnvironment();
  const state = Bridge.install(fixture.env);

  fixture.dispatch({
    type: Bridge.ACTIVITY_MESSAGE,
    version: Bridge.VERSION,
    active: false,
  });
  assert.equal(state.active, false);
  assert.deepEqual(fixture.manager.activityChanges, [false]);

  fixture.dispatch({
    type: Bridge.ACTIVITY_MESSAGE,
    version: Bridge.VERSION,
    active: true,
  });
  assert.equal(state.active, true);
  assert.deepEqual(fixture.manager.activityChanges, [false, true]);
  assert.equal(fixture.chart.lastSymbol, undefined);
});

test("resumes realtime only for the newest parent chart request", () => {
  const fixture = bridgeEnvironment();
  Bridge.install(fixture.env);
  fixture.dispatch({
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-20",
    market: "a",
    code: "SZ.000001",
    frequency: "5m",
  });
  assert.deepEqual(fixture.manager.deferredRealtime, ["chart-20"]);

  fixture.dispatch({
    type: Bridge.REALTIME_RESUME_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-19",
  });
  assert.deepEqual(fixture.manager.resumedRealtime, []);

  fixture.dispatch({
    type: Bridge.REALTIME_RESUME_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-20",
  });
  assert.deepEqual(fixture.manager.resumedRealtime, ["chart-20"]);
});

test("only accepts same-origin messages from the embedding parent", () => {
  const fixture = bridgeEnvironment();
  Bridge.install(fixture.env);
  const switchMessage = {
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-1",
    market: "a",
    code: "SZ.000001",
    frequency: "5m",
  };

  fixture.dispatch(switchMessage, { origin: "https://invalid.example" });
  fixture.dispatch(switchMessage, { source: {} });

  assert.equal(fixture.chart.lastSymbol, undefined);
  assert.equal(fixture.messages.length, 1);
  assert.equal(fixture.messages[0].message.type, Bridge.READY_MESSAGE);
});

test("reuses the chart instance and reports matching data readiness", () => {
  const identity = { symbol: "a:SH.600000", interval: "5" };
  const fixture = bridgeEnvironment(identity);
  Bridge.install(fixture.env);

  fixture.dispatch({
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-2",
    market: "a",
    code: "SZ.000001",
    frequency: "5m",
  });

  assert.equal(fixture.chart.lastSymbol, "a:SZ.000001");
  assert.deepEqual(fixture.manager.deferredRealtime, ["chart-2"]);
  assert.deepEqual(
    fixture.messages.map((row) => row.message.type),
    [Bridge.READY_MESSAGE, "chanlun:chart-switch-accepted"],
  );

  identity.symbol = "a:SZ.000001";
  assert.equal(Bridge.notifyDataReady(identity, fixture.env), true);
  assert.deepEqual(fixture.messages.at(-1).message, {
    type: Bridge.DATA_READY_MESSAGE,
    version: Bridge.VERSION,
    market: "a",
    code: "SZ.000001",
    frequency: "5m",
    requestId: "chart-2",
  });
});

test("an already-matching chart still waits for the manager stable-ready barrier", () => {
  const fixture = bridgeEnvironment();
  let stableRequests = 0;
  fixture.manager.requestEmbeddedStableReady = () => {
    stableRequests += 1;
    return true;
  };
  Bridge.install(fixture.env);
  fixture.dispatch({
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId: "chart-3",
    market: "a",
    code: "SH.600000",
    frequency: "5m",
  });

  assert.equal(fixture.chart.lastSymbol, undefined);
  assert.equal(stableRequests, 1);
  assert.deepEqual(fixture.manager.deferredRealtime, ["chart-3"]);
  assert.equal(fixture.messages.at(-1).message.type, "chanlun:chart-switch-accepted");

  assert.equal(Bridge.notifyDataReady({ symbol: "a:SH.600000", interval: "5" }, fixture.env), true);
  assert.equal(fixture.messages.at(-1).message.type, Bridge.DATA_READY_MESSAGE);
  assert.equal(fixture.messages.at(-1).message.requestId, "chart-3");
});

test("rapid symbol requests serialize TradingView work and keep only the newest target", async () => {
  const fixture = bridgeEnvironment();
  const calls = [];
  let finishFirst;
  fixture.chart.setSymbol = (value) => {
    calls.push(value);
    if (calls.length === 1) {
      return new Promise((resolve) => { finishFirst = resolve; });
    }
    return Promise.resolve();
  };
  const state = Bridge.install(fixture.env);
  const request = (requestId, code) => fixture.dispatch({
    type: Bridge.SWITCH_MESSAGE,
    version: Bridge.VERSION,
    requestId,
    market: "a",
    code,
    frequency: "5m",
  });

  request("chart-10", "SZ.000001");
  request("chart-11", "SZ.000002");
  request("chart-12", "SZ.000003");

  assert.deepEqual(calls, ["a:SZ.000001"]);
  assert.equal(state.pending.requestId, "chart-12");
  finishFirst();
  await Promise.resolve();
  await Promise.resolve();

  // Resolving TradingView's setSymbol promise only means the request was
  // accepted. The bridge keeps the lane locked until K-lines and automatic
  // structure drawings report stable readiness.
  assert.deepEqual(calls, ["a:SZ.000001"]);
  assert.equal(state.switchInFlight, "chart-10");
  assert.equal(Bridge.notifyDataReady({
    symbol: "a:SZ.000001",
    interval: "5",
  }, fixture.env), false);

  assert.deepEqual(calls, ["a:SZ.000001", "a:SZ.000003"]);
  assert.deepEqual(
    fixture.messages
      .filter((row) => row.message.type === "chanlun:chart-switch-accepted")
      .map((row) => row.message.requestId),
    ["chart-10", "chart-12"],
  );
});
