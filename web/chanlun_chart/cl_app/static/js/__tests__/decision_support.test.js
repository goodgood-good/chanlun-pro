"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const modulePath = path.resolve(__dirname, "../decision_support.js");
const templatePath = path.resolve(__dirname, "../../../templates/index.html");
const DS = require(modulePath);


function safeHtmlStub() {
  return {
    escapeText(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character]);
    },
    renderMarkdown() {
      return "<p>sanitized markdown</p>";
    },
  };
}


function makeDocument() {
  const listeners = new Map();
  const elements = new Map();
  return {
    hidden: false,
    addEventListener(name, handler) {
      listeners.set(name, handler);
    },
    removeEventListener(name) {
      listeners.delete(name);
    },
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, { innerHTML: "", textContent: "" });
      }
      return elements.get(id);
    },
    dispatch(name) {
      const handler = listeners.get(name);
      if (handler) handler({ type: name });
    },
  };
}


function makeHarness() {
  const requests = [];
  const timers = [];
  const abortControllers = [];
  const document = makeDocument();
  const chartChanges = [];

  class FakeAbortController {
    constructor() {
      this.signal = { aborted: false };
      abortControllers.push(this);
    }
    abort() {
      this.signal.aborted = true;
    }
  }

  function ajax(options) {
    const request = {
      options,
      aborted: false,
      resolve(payload) {
        if (typeof options.success === "function") {
          options.success(payload, "success", request);
        }
        if (typeof options.complete === "function") {
          options.complete(request, "success");
        }
      },
      reject() {
        if (typeof options.error === "function") {
          options.error(request, "error", new Error("failed"));
        }
        if (typeof options.complete === "function") {
          options.complete(request, "error");
        }
      },
      abort() {
        this.aborted = true;
      },
    };
    requests.push(request);
    return request;
  }

  const controller = DS.createController({
    AbortController: FakeAbortController,
    ajax,
    changeChartTicker(market, code) {
      chartChanges.push([market, code]);
    },
    clearTimeout(id) {
      if (timers[id - 1]) timers[id - 1].cleared = true;
    },
    document,
    now: () => 1_721_000_000_000,
    random: () => 0.25,
    safeHtml: safeHtmlStub(),
    setTimeout(callback, delay) {
      timers.push({ callback, delay, cleared: false });
      return timers.length;
    },
  });

  return { abortControllers, chartChanges, controller, document, requests, timers };
}


test("candidate renderer escapes every dynamic text field", () => {
  const html = DS.renderCandidate({
    event_id: 'event" onclick="alert(1)',
    market: "a",
    code: "SH.600001<script>",
    name: '<img src=x onerror="alert(1)">',
    reason_codes: ["中枢突破", "<svg onload=alert(1)>"],
  });

  assert.match(html, /^<button type="button" class="ds-candidate-button"/);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img"), false);
  assert.equal(html.includes("<svg"), false);
  assert.equal(html.includes(' onclick="alert(1)'), false);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});


test("track filter never mixes reversal and trend candidates", () => {
  const payload = {
    trend: [
      { event_id: "t1", strategy_track: "trend_continuation" },
      { event_id: "bad-r", strategy_track: "bottom_reversal" },
    ],
    reversal: [
      { event_id: "r1", strategy_track: "bottom_reversal" },
      { event_id: "bad-t", strategy_track: "trend_continuation" },
    ],
  };

  assert.deepEqual(DS.forTrack(payload, "trend").map((item) => item.event_id), ["t1"]);
  assert.deepEqual(DS.forTrack(payload, "reversal").map((item) => item.event_id), ["r1"]);
  assert.deepEqual(DS.forTrack(payload, "unknown"), []);
});


test("plan and risk renderers use escaped persisted facts only", () => {
  const plan = DS.renderPlan({
    code: "SH.600001",
    name: "候选<script>alert(1)</script>",
    plan: { entry_price: 10.25, stop_price: 9.8, target_price: 12.5 },
    model_markdown: "<img src=x onerror=alert(1)>",
  });
  const risk = DS.renderRisk({
    available: true,
    daily_loss_locked: false,
    drawdown_locked: true,
    promotion_reasons: ["等待 20 日", "<script>alert(1)</script>"],
  });

  assert.match(plan, /10\.25/);
  assert.match(plan, /9\.8/);
  assert.equal(plan.includes("model_markdown"), false);
  assert.equal(plan.includes("<script>"), false);
  assert.equal(risk.includes("<script>"), false);
  assert.match(risk, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
});


test("risk renderer displays the API as_of timestamp", () => {
  const html = DS.renderRisk({
    available: true,
    as_of: "2026-07-13T10:35:00+08:00",
  });

  assert.match(html, /2026-07-13T10:35:00\+08:00/);
});


test("paper renderer is escaped, read-only, and exposes observation gates", () => {
  const html = DS.renderPaper({
    status: {
      mode: "research_paper",
      read_only: true,
      auto_order_enabled: false,
      live_order_capability: false,
      paper_observation_gate: {
        passed: false,
        trading_days: 19,
        minimum_trading_days: 20,
        executable_events: 1,
        minimum_executable_events: 30,
        reasons: ["trusted_bar_store_degraded"],
      },
      trusted_bar_store: { degraded: true },
    },
    account: {
      mode: "research_paper",
      read_only: true,
      auto_order_enabled: false,
      live_order_capability: false,
      available_buying_power: "98990.00",
      cost_basis_equity: "99990.00",
    },
    positions: {
      mode: "research_paper",
      read_only: true,
      auto_order_enabled: false,
      live_order_capability: false,
      items: [{ code: 'SH.600001<script>alert(1)</script>', shares: 100 }],
    },
    exits: {
      mode: "research_paper",
      read_only: true,
      auto_order_enabled: false,
      live_order_capability: false,
      items: [{ recommendation_payload: { action: "observe" } }],
    },
  });

  assert.match(html, /ds-paper-readonly/);
  assert.match(html, /不可作为实盘交易依据/);
  assert.match(html, /可信K线存储已降级/);
  assert.equal(html.includes("trusted_bar_store_degraded"), false);
  assert.match(html, /19/);
  assert.match(html, /30/);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.equal(html.includes("data-ds-decision"), false);
  assert.equal(html.includes("data-ds-review"), false);
  assert.match(html, /当前周期离场分析不可用/);
  assert.equal(html.includes("observe"), false);
});


test("paper renderer shows exits only for fresh complete current-cycle coverage", () => {
  const research = {
    mode: "research_paper",
    read_only: true,
    auto_order_enabled: false,
    live_order_capability: false,
  };
  const html = DS.renderPaper({
    status: {
      ...research,
      paper_observation_gate: { reasons: [] },
      trusted_bar_store: { degraded: false },
      exit_coverage: {
        complete: true,
        fresh: true,
        failure_count: 0,
        scan_code: "scan_complete",
        cycle_failure: null,
      },
    },
    account: research,
    positions: { ...research, items: [] },
    exits: {
      ...research,
      items: [{
        entry_event_id: "entry-current-1",
        recommendation_payload: { action: "observe", urgency: "none" },
      }],
    },
  });

  assert.match(html, /entry-current-1 · observe · none/);
  assert.equal(html.includes("当前周期离场分析不可用"), false);
});


test("paper refresh uses only the four read-only GET snapshots", () => {
  const h = makeHarness();

  h.controller.refreshPaper();

  assert.deepEqual(
    h.requests.map((request) => request.options.url),
    [
      "/decision-support/paper/status",
      "/decision-support/paper/account",
      "/decision-support/paper/positions",
      "/decision-support/paper/exits",
    ],
  );
  assert.ok(h.requests.every((request) => request.options.method === "GET"));
  h.requests.forEach((request) => request.resolve({
    ok: true,
    data: {
      mode: "research_paper",
      read_only: true,
      auto_order_enabled: false,
      live_order_capability: false,
      items: [],
    },
  }));
  assert.equal(h.controller.getState().paper.status.mode, "research_paper");
});


test("paper position navigation only switches a valid A-share chart", () => {
  const h = makeHarness();

  assert.equal(h.controller.selectPaperPosition("SH.600001"), true);
  assert.equal(h.controller.selectPaperPosition("US.AAPL"), false);
  assert.equal(h.controller.selectPaperPosition("SH.600001<script>"), false);
  assert.deepEqual(h.chartChanges, [["a", "SH.600001"]]);
});


test("failed risk refresh clears stale available state and fails closed", () => {
  const h = makeHarness();

  h.controller.refreshRisk();
  h.requests[0].resolve({
    ok: true,
    data: { available: true, as_of: "2026-07-13T10:35:00+08:00" },
  });
  assert.equal(h.controller.getState().risk.available, true);

  h.controller.refreshRisk();
  h.requests[1].reject();

  assert.equal(h.controller.getState().risk, null);
  assert.match(
    h.document.getElementById("ds_risk_view").innerHTML,
    /风控状态加载失败.*不可用/,
  );
  assert.match(h.document.getElementById("ds_status").textContent, /风控.*不可用/);
});


test("plan exposes only review and bounded manual-decision controls", () => {
  const html = DS.renderPlan({
    event_id: "event-1",
    event_data_fingerprint: `sha256:${"1".repeat(64)}`,
    code: "SH.600001",
  });

  assert.match(html, /data-ds-review/);
  assert.match(html, /data-ds-decision="accepted"/);
  assert.match(html, /data-ds-decision="ignored"/);
  assert.match(html, /data-ds-decision="executed_externally"/);
  assert.equal((html.match(/data-ds-decision=/g) || []).length, 3);
});


test("manual-check renderer escapes evidence and leaves every check unchecked", () => {
  const html = DS.renderManualChecks({
    event_id: "event-1",
    context_fingerprint: `sha256:${"2".repeat(64)}`,
    status: "pending",
    required_checks: [
      {
        manual_check_id: "check-a",
        prompt: "确认回抽 <script>alert(1)</script>",
        evidence_ids: ["lesson-20:p1", "chart-20-1"],
      },
      {
        manual_check_id: "check-b",
        prompt: "确认走势完成",
        evidence_ids: ["lesson-24:p2"],
      },
    ],
  });

  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.equal(/\schecked(?:\s|=|>)/.test(html), false);
  assert.equal(html.includes("<script>"), false);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /lesson-20:p1/);
  assert.match(html, /chart-20-1/);
  assert.match(html, /data-ds-manual-submit/);
});


test("evidence markdown is rendered only through SafeHtml", () => {
  let markdownCalls = 0;
  const safeHtml = safeHtmlStub();
  safeHtml.renderMarkdown = () => {
    markdownCalls += 1;
    return "<p>sanitized markdown</p>";
  };
  const controller = DS.createController({ safeHtml });

  const html = controller.renderEvidence({
    event_id: "e1<script>",
    review_markdown: "<img src=x onerror=alert(1)>",
    blockers: ["<svg onload=alert(1)>"],
  });

  assert.equal(markdownCalls, 1);
  assert.equal(html.includes("<img"), false);
  assert.equal(html.includes("<svg"), false);
  assert.match(html, /sanitized markdown/);
  assert.match(html, /&lt;svg onload=alert\(1\)&gt;/);
});


test("newer candidate response wins even if an aborted request resolves late", () => {
  const h = makeHarness();

  h.controller.refreshCandidates();
  h.controller.refreshCandidates();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[0].aborted, true);
  assert.equal(h.abortControllers[0].signal.aborted, true);

  h.requests[1].resolve({
    ok: true,
    data: {
      trend: [{ event_id: "new", strategy_track: "trend_continuation" }],
      reversal: [],
      stale: false,
    },
  });
  h.requests[0].resolve({
    ok: true,
    data: {
      trend: [{ event_id: "old", strategy_track: "trend_continuation" }],
      reversal: [],
      stale: false,
    },
  });

  assert.deepEqual(h.controller.getState().candidates.trend.map((item) => item.event_id), ["new"]);
});


test("polling requests only candidates and risk and stops while hidden", () => {
  const h = makeHarness();

  h.controller.init();
  assert.deepEqual(
    h.requests.map((request) => request.options.url),
    ["/decision-support/candidates?limit=25", "/decision-support/risk-status"],
  );
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].delay, 15_000);

  h.document.hidden = true;
  h.document.dispatch("visibilitychange");

  assert.equal(h.timers[0].cleared, true);
  assert.equal(h.requests.every((request) => request.aborted), true);
});


test("selecting a candidate changes chart and loads event-driven detail and evidence", () => {
  const h = makeHarness();

  h.controller.selectEvent({
    event_id: "event/1",
    market: "a",
    code: "SH.600001",
  });

  assert.deepEqual(h.chartChanges, [["a", "SH.600001"]]);
  assert.deepEqual(
    h.requests.map((request) => request.options.url),
    [
      "/decision-support/events/event%2F1",
      "/decision-support/events/event%2F1/evidence",
      "/decision-support/events/event%2F1/manual-checks",
    ],
  );
});


test("older event and evidence responses cannot replace a newer selection", () => {
  const h = makeHarness();

  h.controller.selectEvent({ event_id: "old", market: "a", code: "SH.600001" });
  h.controller.selectEvent({ event_id: "new", market: "a", code: "SZ.000001" });
  assert.equal(h.requests.length, 6);
  assert.equal(h.requests[0].aborted, true);
  assert.equal(h.requests[1].aborted, true);
  assert.equal(h.requests[2].aborted, true);

  h.requests[3].resolve({ ok: true, data: { event_id: "new", state: "ready" } });
  h.requests[4].resolve({ ok: true, data: { event_id: "new", supporting: [] } });
  h.requests[5].resolve({
    ok: true,
    data: {
      event_id: "new",
      context_fingerprint: `sha256:${"2".repeat(64)}`,
      required_checks: [],
    },
  });
  h.requests[0].resolve({ ok: true, data: { event_id: "old", state: "late" } });
  h.requests[1].resolve({ ok: true, data: { event_id: "old", supporting: ["late"] } });
  h.requests[2].resolve({
    ok: true,
    data: {
      event_id: "old",
      context_fingerprint: `sha256:${"3".repeat(64)}`,
      required_checks: [],
    },
  });

  assert.equal(h.controller.getState().selectedEvent.event_id, "new");
  assert.equal(h.controller.getState().evidence.event_id, "new");
  assert.equal(h.controller.getState().manualChecks.event_id, "new");
});


test("manual-check submit requires every explicit human selection", () => {
  const h = makeHarness();
  const contextFingerprint = `sha256:${"2".repeat(64)}`;
  const record = {
    event_id: "event-1",
    context_fingerprint: contextFingerprint,
    status: "pending",
    required_checks: [
      {
        manual_check_id: "check-a",
        prompt: "确认回抽",
        evidence_ids: ["chart-a", "lesson-a"],
      },
      {
        manual_check_id: "check-b",
        prompt: "确认走势完成",
        evidence_ids: ["lesson-b"],
      },
    ],
  };

  h.controller.selectEvent({ event_id: "event-1" });
  h.requests[2].resolve({ ok: true, data: record });

  assert.throws(
    () => h.controller.submitManualChecks("event-1", {
      checkedIds: ["check-a"],
      operatorId: "test-user",
    }),
    /all manual checks must be explicitly selected/,
  );
  assert.equal(h.requests.length, 3);
});


test("manual-check submit constructs the exact bounded snapshot payload", () => {
  const h = makeHarness();
  const contextFingerprint = `sha256:${"2".repeat(64)}`;
  const record = {
    event_id: "event-1",
    context_fingerprint: contextFingerprint,
    status: "pending",
    required_checks: [
      {
        manual_check_id: "check-a",
        prompt: "确认回抽",
        evidence_ids: ["chart-a", "lesson-a"],
      },
      {
        manual_check_id: "check-b",
        prompt: "确认走势完成",
        evidence_ids: ["lesson-b"],
      },
    ],
  };

  h.controller.selectEvent({ event_id: "event-1" });
  h.requests[2].resolve({ ok: true, data: record });
  h.controller.submitManualChecks("event-1", {
    checkedIds: ["check-b", "check-a"],
    operatorId: "test-user",
  });

  assert.equal(h.requests.length, 4);
  assert.equal(h.requests[3].options.method, "POST");
  assert.equal(
    h.requests[3].options.url,
    "/decision-support/events/event-1/manual-checks",
  );
  assert.deepEqual(JSON.parse(h.requests[3].options.data), {
    manual_checks: [
      {
        manual_check_id: "check-a",
        value: true,
        operator_id: "test-user",
        recorded_at: new Date(1_721_000_000_000).toISOString(),
        event_id: "event-1",
        context_fingerprint: contextFingerprint,
        evidence_ids: ["chart-a", "lesson-a"],
      },
      {
        manual_check_id: "check-b",
        value: true,
        operator_id: "test-user",
        recorded_at: new Date(1_721_000_000_000).toISOString(),
        event_id: "event-1",
        context_fingerprint: contextFingerprint,
        evidence_ids: ["lesson-b"],
      },
    ],
  });
});


test("duplicate manual decision and review clicks share one bounded POST", () => {
  const h = makeHarness();
  const button = {
    disabled: false,
    setAttribute() {},
    removeAttribute() {},
  };
  const fingerprint = `sha256:${"1".repeat(64)}`;

  const firstDecision = h.controller.submitUserDecision(
    "event-1",
    "executed_externally",
    { button, event_data_fingerprint: fingerprint, note: "人工处理" },
  );
  const duplicateDecision = h.controller.submitUserDecision(
    "event-1",
    "executed_externally",
    { button, event_data_fingerprint: fingerprint, note: "人工处理" },
  );
  const firstReview = h.controller.requestReview("event-1", { force: true, button });
  const duplicateReview = h.controller.requestReview("event-1", { force: true, button });

  assert.equal(firstDecision, duplicateDecision);
  assert.equal(firstReview, duplicateReview);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[0].options.method, "POST");
  assert.equal(h.requests[0].options.contentType, "application/json; charset=UTF-8");
  assert.deepEqual(JSON.parse(h.requests[0].options.data), {
    action: "executed_externally",
    event_data_fingerprint: fingerprint,
    idempotency_key: "ds-lym6yqrk-1-40000000",
    note: "人工处理",
  });
  assert.deepEqual(JSON.parse(h.requests[1].options.data), { force: true });
});


test("manual decisions reject unsupported actions before any request", () => {
  const h = makeHarness();

  assert.throws(
    () => h.controller.submitUserDecision("event-1", "buy_now", {
      event_data_fingerprint: `sha256:${"1".repeat(64)}`,
    }),
    /unsupported manual decision/,
  );
  assert.equal(h.requests.length, 0);
});


test("template loads and initializes the module exactly once with static version", () => {
  const template = fs.readFileSync(templatePath, "utf8");

  assert.equal((template.match(/filename='js\/decision_support\.js'/g) || []).length, 1);
  assert.match(
    template,
    /filename='js\/decision_support\.js'[^\n]*static_version/,
  );
  assert.equal((template.match(/DecisionSupport\.init\(\)/g) || []).length, 1);
  assert.equal((template.match(/id="ds_manual_check_view"/g) || []).length, 1);
  assert.equal((template.match(/id="ds_paper_tab"/g) || []).length, 1);
  assert.equal((template.match(/id="ds_paper_view"/g) || []).length, 1);
  assert.match(template, /data-operator-id="\{\{ current_user\.get_id\(\)/);
});


test("browser build attaches the same API without CommonJS globals", () => {
  const source = fs.readFileSync(modulePath, "utf8");
  const sandbox = {
    SafeHtml: safeHtmlStub(),
    console: { error() {}, warn() {} },
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "decision_support.browser.js" });

  assert.equal(typeof sandbox.DecisionSupport.init, "function");
  assert.equal(typeof sandbox.DecisionSupport.renderCandidate, "function");
  assert.equal(typeof sandbox.DecisionSupport.renderManualChecks, "function");
  assert.equal(typeof sandbox.DecisionSupport.submitManualChecks, "function");
  assert.equal(typeof sandbox.DecisionSupport.submitUserDecision, "function");
});


test("module source has no broker or automatic-order integration", () => {
  const source = fs.readFileSync(modulePath, "utf8").toLowerCase();

  for (const forbidden of [
    "chanlun.trader",
    "broker_order",
    "open_buy",
    "open_sell",
    "cancel_order",
  ]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
});
