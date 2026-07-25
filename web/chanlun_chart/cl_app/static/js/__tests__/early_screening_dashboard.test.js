"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const path = require("node:path");

const uiPath = path.resolve(__dirname, "../early_screening_ui.js");
const template = fs.readFileSync(
  path.resolve(__dirname, "../../../templates/early_screening.html"),
  "utf8",
);
const controllerSource = fs.readFileSync(path.resolve(__dirname, "../early_screening.js"), "utf8");
const dashboardCss = fs.readFileSync(path.resolve(__dirname, "../../css/early_screening.css"), "utf8");

function loadUi() {
  delete require.cache[require.resolve(uiPath)];
  return require(uiPath);
}

function fakeChartRoot() {
  const nodes = new Map();
  const lists = new Map();
  const documentRef = {};
  function makeNode(tagName = "div") {
    const attributes = new Map();
    const classes = new Set();
    return {
      tagName: tagName.toUpperCase(),
      ownerDocument: documentRef,
      dataset: {},
      hidden: false,
      disabled: false,
      focusCount: 0,
      textContent: "",
      children: [],
      className: "",
      classList: {
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
        },
        contains(name) { return classes.has(name); },
      },
      setAttribute(name, value) { attributes.set(name, String(value)); },
      getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
      focus() { this.focusCount += 1; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) {
        this.children = items.flatMap((item) => (
          item && item.isFragment ? item.children : [item]
        ));
      },
    };
  }
  documentRef.createElement = (tagName) => makeNode(tagName);
  documentRef.createDocumentFragment = () => ({
    isFragment: true,
    children: [],
    append(...items) { this.children.push(...items); },
  });
  const root = makeNode("section");
  root.querySelector = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, makeNode());
    return nodes.get(selector);
  };
  root.querySelectorAll = (selector) => {
    if (!lists.has(selector)) {
      if (selector === "[data-focus-frequency]") {
        lists.set(selector, ["30m", "5m", "1m"].map((frequency) => {
          const node = makeNode("button");
          node.dataset.focusFrequency = frequency;
          return node;
        }));
      } else {
        lists.set(selector, []);
      }
    }
    return lists.get(selector);
  };
  return {
    root,
    node: (selector) => root.querySelector(selector),
    list: (selector) => root.querySelectorAll(selector),
  };
}

const snapshot = {
  schema_version: "chanlun-trading-screening/v3",
  structure_version: "v3",
  available: true,
  scan_state: "complete",
  generated_at: "2026-07-20T15:00:00+08:00",
  as_of: "2026-07-20T15:00:00+08:00",
  sector_first: true,
  read_only: true,
  research_only: true,
  no_order_execution: true,
  counts_by_stage: { armed: 1, triggered: 1 },
  counts_by_point_type: {
    "1buy": 1, "2buy": 1, "3buy": 0,
    "1sell": 0, "2sell": 0, "3sell": 0,
  },
  sectors: [
    {
      sector_id: "tdx-industry:SH.880471",
      sector_name: "银行",
      eligible: true,
      hard_block: false,
      regime: "supportive",
      rank: 1,
      rank_score: 85,
      reason_codes: ["structural_ranking_only"],
      context_30m: {
        direction: "up",
        disposition: "supportive",
        dominant_point_type: "1buy",
      },
      context_5m: {
        direction: "neutral",
        disposition: "neutral",
        dominant_point_type: null,
      },
    },
  ],
  signals: [
    {
      signal_id: "signal-1",
      code: "SZ.000001",
      name: "平安银行",
      point_type: "1buy",
      side: "buy",
      tower: "bi",
      recursive_level: 0,
      lifecycle_stage: "armed",
      observed_at: "2026-07-20T14:55:00+08:00",
      sector: { sector_id: "tdx-industry:SH.880471", sector_name: "银行" },
      context_30m: { direction: "up", disposition: "supportive" },
      setup_5m: { point_type: "1buy", center_ordinal: null },
      trigger_1m: null,
      structural_stop: "9.80",
      risk_multiplier: "0.50",
      entry_allowed: false,
      exit_allowed: false,
      decision_reasons: ["one_minute_not_confirmed"],
      chart_urls: {
        "30m": "/?market=a&code=SZ.000001&frequency=30m",
        "5m": "/?market=a&code=SZ.000001&frequency=5m",
        "1m": "/?market=a&code=SZ.000001&frequency=1m",
      },
    },
    {
      signal_id: "signal-2",
      code: "SZ.000002",
      name: "万科A",
      point_type: "2buy",
      side: "buy",
      tower: "xd",
      recursive_level: 1,
      lifecycle_stage: "triggered",
      observed_at: "2026-07-20T14:58:00+08:00",
      sector: { sector_id: "tdx-industry:SH.880482", sector_name: "房地产" },
      context_30m: { direction: "neutral", disposition: "neutral" },
      setup_5m: { point_type: "2buy", center_ordinal: null },
      trigger_1m: { point_type: "1buy" },
      structural_stop: "7.50",
      risk_multiplier: "1.00",
      entry_allowed: true,
      exit_allowed: false,
      decision_reasons: [],
      chart_urls: {},
    },
  ],
  risk_limits: {},
  scan_audit: {
    completion_ratio: "1",
    sector_discovered_count: 10,
    sector_completed_count: 9,
    sector_failed_count: 1,
    sector_completion_ratio: "0.9",
  },
  data_quality: { complete: true, stale: false, failure_codes: [] },
  backtest_verdict: { live_ready: false, status: "evidence_insufficient" },
  errors: [],
};

test("dashboard exposes sector signal and chart workspaces", () => {
  assert.match(template, /data-schema="chanlun-trading-screening\/v3"/);
  assert.match(template, /id="es-sector-completion"/);
  assert.match(template, /id="es-scan-timing"/);
  assert.match(template, /data-workspace="sector"/);
  assert.match(template, /data-workspace="signals"/);
  assert.match(template, /data-workspace="charts"/);
  assert.match(template, /data-layout="focus"/);
  assert.match(template, /data-layout="dual"/);
  assert.match(template, /data-layout="triple"/);
  assert.match(template, /4 个自然日/);
});

test("chart workspace exposes accessible resizers before the dashboard controller boots", () => {
  const resizeScriptIndex = template.indexOf("early_screening_chart_resize.js");
  const controllerScriptIndex = template.indexOf("early_screening.js");

  assert.ok(resizeScriptIndex >= 0, "应加载图表尺寸控制器");
  assert.ok(resizeScriptIndex < controllerScriptIndex, "尺寸控制器应先于页面控制器加载");
  assert.match(template, /data-chart-grid/);
  for (const type of ["columns", "rows", "height"]) {
    assert.match(
      template,
      new RegExp(`data-chart-resizer="${type}"[^>]*role="separator"[^>]*tabindex="0"`),
    );
  }
  assert.match(template, /data-chart-size-reset/);
  assert.match(template, /data-chart-resize-status[^>]*aria-live="polite"/);
});

test("chart workspace exposes a decision path focus controls and decision evidence groups", () => {
  for (const contract of [
    'data-decision-title',
    'data-decision-detail',
    'data-decision-invalidation',
    'data-period-node="30m"',
    'data-period-node="5m"',
    'data-period-node="1m"',
    'data-focus-frequency="30m"',
    'data-focus-frequency="5m"',
    'data-focus-frequency="1m"',
    'data-layout="focus"',
    'data-layout="dual"',
    'data-layout="triple"',
    'data-evidence-group="established"',
    'data-evidence-group="missing"',
    'data-evidence-group="blocking"',
    'data-evidence-group="next"',
    'data-evidence-group="risk"',
    'data-raw-evidence',
  ]) assert.match(template, new RegExp(contract));
  assert.doesNotMatch(template, /data-evidence-30m|data-evidence-5m|data-evidence-1m/);
});

test("chart workspace puts the real chart before decisions and removes the selection placeholder", () => {
  const chartIndex = template.indexOf('class="es-chart-stage"');
  const decisionIndex = template.indexOf('class="es-decision-deck"');
  const pathIndex = template.indexOf('class="es-period-path"');
  const contentTag = template.match(/<div data-chart-content[^>]*>/)?.[0] || "";

  assert.ok(chartIndex >= 0 && chartIndex < decisionIndex);
  assert.ok(chartIndex < pathIndex);
  assert.doesNotMatch(template, /data-chart-placeholder/);
  assert.doesNotMatch(template, /从买卖点队列选择一只股票|这里会先给出交易结论/);
  assert.doesNotMatch(contentTag, /\shidden(?:\s|>)/);
  assert.doesNotMatch(dashboardCss, /\.es-chart-placeholder/);
});

test("dashboard has six independent point filters", () => {
  for (const point of ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"]) {
    assert.match(template, new RegExp(`data-point-type="${point}"`));
  }
});

test("normalizeSnapshot accepts only the new read-only schema", () => {
  const Ui = loadUi();
  const normalized = Ui.normalizeSnapshot(snapshot);

  assert.equal(normalized.schema_version, "chanlun-trading-screening/v3");
  assert.equal(normalized.signals.length, 2);
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, schema_version: "chanlun-early-screening/v13" }),
    /snapshot_schema_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, no_order_execution: false }),
    /snapshot_boundary_invalid/,
  );
});

test("filters preserve independent point lifecycle sector and query choices", () => {
  const Ui = loadUi();
  const signals = Ui.normalizeSnapshot(snapshot).signals;

  assert.deepEqual(
    Ui.filterSignals(signals, { pointType: "1buy" }).map((row) => row.signal_id),
    ["signal-1"],
  );
  assert.deepEqual(
    Ui.filterSignals(signals, { lifecycle: "triggered", query: "万科" })
      .map((row) => row.signal_id),
    ["signal-2"],
  );
  assert.deepEqual(
    Ui.filterSignals(signals, { sectorId: "tdx-industry:SH.880471" })
      .map((row) => row.signal_id),
    ["signal-1"],
  );
});

test("chart selection defaults to a queue signal and survives an empty filter", () => {
  const Ui = loadUi();
  const [first, second] = snapshot.signals;

  assert.equal(Ui.resolveSelectedSignalId(null, snapshot.signals, snapshot.signals), "signal-1");
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [], snapshot.signals), "signal-1");
  assert.equal(Ui.resolveSelectedSignalId("removed", [], snapshot.signals), "signal-1");
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [second], snapshot.signals), "signal-2");
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [], []), null);
  assert.equal(Ui.resolveSelectedSignalId(null, [first], snapshot.signals), "signal-1");
});

test("signals group by native sector without price-change logic", () => {
  const Ui = loadUi();
  const grouped = Ui.groupSignalsBySector(snapshot.signals);

  assert.deepEqual(Object.keys(grouped), [
    "tdx-industry:SH.880471",
    "tdx-industry:SH.880482",
  ]);
  assert.equal(grouped["tdx-industry:SH.880471"][0].signal_id, "signal-1");
});

test("chart URLs normalize legacy frequency links and fill all three intervals", () => {
  const Ui = loadUi();

  assert.deepEqual(Ui.chartUrlsForSignal(snapshot.signals[0]), {
    "30m": "/?market=a&code=SZ.000001&layout=single&intervals=30&chart_sidebar=collapsed&default_study=MACD_HTF",
    "5m": "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
    "1m": "/?market=a&code=SZ.000001&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF",
  });
  assert.deepEqual(Ui.chartUrlsForSignal(snapshot.signals[1]), {
    "30m": "/?market=a&code=SZ.000002&layout=single&intervals=30&chart_sidebar=collapsed&default_study=MACD_HTF",
    "5m": "/?market=a&code=SZ.000002&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
    "1m": "/?market=a&code=SZ.000002&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF",
  });
});

test("chart URLs keep fragments and never duplicate the requested MACD_HTF study", () => {
  const Ui = loadUi();
  const urls = Ui.chartUrlsForSignal({
    code: "SH.600000",
    chart_urls: {
      "30m": "/?market=a&code=SH.600000&layout=single&intervals=30&default_study=MACD_HTF#main",
      "5m": "/?market=a&code=SH.600000&layout=single&intervals=5&chart_sidebar=expanded#setup",
      "1m": "/?market=a&code=SH.600000&layout=single&intervals=1#trigger",
    },
  });

  assert.equal(
    urls["30m"],
    "/?market=a&code=SH.600000&layout=single&intervals=30&default_study=MACD_HTF&chart_sidebar=collapsed#main",
  );
  assert.equal(
    urls["5m"],
    "/?market=a&code=SH.600000&layout=single&intervals=5&chart_sidebar=expanded&default_study=MACD_HTF#setup",
  );
  assert.equal(
    urls["1m"],
    "/?market=a&code=SH.600000&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF#trigger",
  );
  for (const url of Object.values(urls)) {
    assert.equal((url.match(/default_study=MACD_HTF/g) || []).length, 1);
  }
});

test("analysis layout switch uses task semantics and migrates legacy values", () => {
  const Ui = loadUi();
  const root = { dataset: { currentLayout: "focus" } };

  assert.equal(Ui.setChartLayout(root, "dual"), "dual");
  assert.equal(root.dataset.layout, "dual");
  assert.equal(root.dataset.currentLayout, "dual");
  assert.equal(Ui.setChartLayout(root, "quad"), "triple");
  assert.equal(root.dataset.layout, "triple");
  assert.equal(Ui.setChartLayout(root, "single"), "focus");
  assert.equal(Ui.setChartLayout(root, "unknown"), "focus");
  assert.equal(root.dataset.currentLayout, "focus");
});

test("evidence drawer synchronizes its accessible state", () => {
  const Ui = loadUi();
  const view = fakeChartRoot();

  assert.equal(Ui.setEvidencePanelOpen(view.root, true), true);
  assert.equal(view.root.dataset.evidenceOpen, "true");
  assert.equal(view.node("[data-evidence-toggle]").getAttribute("aria-expanded"), "true");
  assert.equal(view.node("[data-evidence-panel]").getAttribute("aria-hidden"), "false");

  assert.equal(Ui.setEvidencePanelOpen(view.root, false), false);
  assert.equal(view.root.dataset.evidenceOpen, "false");
  assert.equal(view.node("[data-evidence-toggle]").getAttribute("aria-expanded"), "false");
  assert.equal(view.node("[data-evidence-panel]").getAttribute("aria-hidden"), "true");
});

test("theater mode synchronizes the workspace body and toggle state", () => {
  const Ui = loadUi();
  const view = fakeChartRoot();
  const body = view.node("body");

  assert.equal(Ui.setTheaterMode(view.root, body, true), true);
  assert.equal(view.root.dataset.theaterMode, "true");
  assert.equal(view.node("[data-theater-toggle]").getAttribute("aria-pressed"), "true");
  assert.equal(view.node("[data-theater-label]").textContent, "退出影院");
  assert.equal(body.classList.contains("es-theater-open"), true);

  assert.equal(Ui.setTheaterMode(view.root, body, false), false);
  assert.equal(view.root.dataset.theaterMode, "false");
  assert.equal(view.node("[data-theater-toggle]").getAttribute("aria-pressed"), "false");
  assert.equal(view.node("[data-theater-label]").textContent, "影院模式");
  assert.equal(body.classList.contains("es-theater-open"), false);
});

test("dashboard CSS implements focus dual triple and responsive evidence layouts", () => {
  assert.match(dashboardCss, /\.es-analysis-grid\s*\{/);
  assert.match(dashboardCss, /data-layout="focus"\]\[data-focused-frequency="30m"\]/);
  assert.match(dashboardCss, /data-layout="focus"\]\[data-focused-frequency="5m"\]/);
  assert.match(dashboardCss, /data-layout="focus"\]\[data-focused-frequency="1m"\]/);
  assert.match(dashboardCss, /data-layout="dual"/);
  assert.match(dashboardCss, /data-layout="triple"/);
  assert.match(dashboardCss, /\.es-period-path\s*\{/);
  assert.match(dashboardCss, /\.es-period-path button > span\s*\{[^}]*font-size:\s*14px/s);
  assert.match(dashboardCss, /\.es-period-path button > small\s*\{[^}]*font-size:\s*14px/s);
  assert.match(dashboardCss, /\.es-period-path button > em\s*\{[^}]*font-size:\s*13px/s);
  assert.match(dashboardCss, /\.es-evidence-panel\s*\{/);
  assert.match(dashboardCss, /data-signal-side="sell"\].*data-tone="action"/);
  assert.doesNotMatch(dashboardCss, /data-layout="single"|data-layout="split"|data-layout="quad"/);
});

test("wide chart workspace keeps decision navigation visible without squeezing charts", () => {
  assert.match(template, /data-evidence-toggle/);
  assert.match(template, /data-evidence-count/);
  assert.match(template, /data-theater-toggle/);
  assert.match(template, /id="es-structure-evidence"[^>]*data-evidence-panel/);
  assert.match(template, /data-evidence-close/);
  assert.match(template, /data-evidence-toggle[^>]*disabled/);
  assert.match(template, /data-theater-toggle[^>]*disabled/);
  assert.match(
    dashboardCss,
    /grid-template-columns:\s*clamp\(320px,\s*23vw,\s*440px\)\s+minmax\(0,\s*1fr\)/,
  );
  assert.match(
    dashboardCss,
    /\.es-chart-workspace\s*\{[^}]*grid-column:\s*2[^}]*grid-row:\s*1\s*\/\s*span\s*2/s,
  );
  assert.match(
    dashboardCss,
    /height:\s*var\(--es-chart-height,\s*clamp\(680px,\s*72vh,\s*920px\)\)/,
  );
  assert.match(
    dashboardCss,
    /data-layout="triple"[^}]*grid-template-columns:[^}]*--es-triple-primary[^}]*--es-triple-secondary/s,
  );
  assert.match(
    dashboardCss,
    /data-layout="triple"[^}]*grid-template-columns:\s*minmax\(0,\s*var\(--es-triple-primary,[^)]+\)\)\s+minmax\(0,\s*var\(--es-triple-secondary,[^)]+\)\)/s,
  );
  assert.match(
    dashboardCss,
    /data-layout="triple"\]\[data-focused-frequency="5m"\]\s+\.es-chart-card\.is-5m/,
  );
  assert.match(dashboardCss, /body\.es-theater-open\s*\{[^}]*overflow:\s*hidden/s);
  assert.match(
    dashboardCss,
    /\.es-chart-workspace\[data-theater-mode="true"\]\s*\{[^}]*position:\s*fixed[^}]*inset:/s,
  );
  assert.match(
    dashboardCss,
    /@media \(max-width:\s*700px\)[\s\S]*\.es-chart-workspace\[data-layout="focus"\]\s+\.es-chart-card iframe,[\s\S]*height:\s*var\(--es-chart-height,\s*max\(520px,\s*64vh\)\)/,
  );
});

test("chart sizing uses CSS variables responsive handles and persisted view state", () => {
  for (const variable of [
    "--es-chart-height",
    "--es-dual-primary",
    "--es-dual-secondary",
    "--es-triple-primary",
    "--es-triple-secondary",
    "--es-triple-top",
    "--es-triple-bottom",
  ]) assert.match(dashboardCss, new RegExp(variable));
  assert.match(dashboardCss, /\.es-chart-resizer\.is-columns/);
  assert.match(dashboardCss, /\.es-chart-resizer\.is-rows/);
  assert.match(dashboardCss, /\.es-chart-resizer\.is-height/);
  assert.match(
    dashboardCss,
    /@media \(max-width:\s*1100px\)[\s\S]*\.es-chart-resizer\.is-columns[\s\S]*display:\s*none/,
  );
  assert.match(controllerSource, /TradingScreeningChartResize/);
  assert.match(controllerSource, /chartSizing:\s*Resize\.normalizeSizing\(saved\.chartSizing\)/);
  assert.match(controllerSource, /chartSizing:\s*state\.chartSizing/);
  assert.match(controllerSource, /Resize\.createController/);
  assert.match(controllerSource, /resizeController\.setLayout\(state\.layout\)/);
});

test("dashboard exposes approaching signals and honest batch coverage", () => {
  const Ui = loadUi();

  assert.equal(Ui.LIFECYCLE_LABELS.approaching, "即将确认");
  assert.match(template, /data-lifecycle="approaching"/);
  assert.equal(
    Ui.scanCoverageText({
      planned_symbol_count: 32,
      completed_symbol_count: 32,
      pending_symbol_count: 68,
    }),
    "本批 32/32 · 待分析 68",
  );
  assert.equal(
    Ui.scanCoverageText({
      planned_symbol_count: 7,
      completed_symbol_count: 7,
      pending_symbol_count: 0,
    }),
    "本批 7/7 · 全周期已覆盖",
  );
  assert.equal(
    Ui.scanCoverageText({
      planned_symbol_count: 0,
      completed_symbol_count: 0,
      pending_symbol_count: 0,
      background_full_refresh_required: true,
    }),
    "等待首批扫描",
  );
  assert.equal(
    Ui.sectorCoverageText({
      sector_discovered_count: 10,
      sector_completed_count: 7,
      sector_failed_count: 3,
      sector_completion_ratio: "0.7",
    }),
    "发现 10 · 完成 7 · 失败 3 · 成功率 70%",
  );
  assert.equal(
    Ui.selectedSectorCount({
      scan_audit: { selected_sector_count: 8 },
      sectors: Array.from({ length: 36 }, () => ({ eligible: true })),
    }),
    8,
  );
  assert.equal(
    Ui.selectedSectorCount({
      scan_audit: {},
      sectors: [{ rank: 1 }, { rank: null }, { rank: undefined }],
    }),
    1,
  );
  assert.equal(
    Ui.scanQualityText({
      available: true,
      scan_audit: { pending_symbol_count: 68, coverage_cycle_complete: false },
      data_quality: { complete: true, stale: false },
    }),
    "本批完整 · 全周期扫描中",
  );
  assert.equal(
    Ui.scanQualityText({
      available: true,
      scan_audit: { pending_symbol_count: 0, coverage_cycle_complete: true },
      data_quality: { complete: true, stale: false },
    }),
    "全周期完整",
  );
  assert.equal(
    Ui.scanTimingText({
      batch_duration_ms: 22400,
      coverage_cycle_elapsed_ms: 65700,
      coverage_cycle_batch_count: 3,
      coverage_cycle_complete: true,
    }),
    "本批 22.4秒 · 全周期 65.7秒 / 3批",
  );
  assert.equal(
    Ui.sectorEvidenceText(snapshot.sectors[0]),
    "30m 向上/支撑/一买 · 5m 震荡/中性/无主导点",
  );
  assert.match(controllerSource, /Ui\.sectorCoverageText\(audit\)/);
  assert.match(controllerSource, /Ui\.scanQualityText\(snapshot\)/);
  assert.match(controllerSource, /Ui\.scanTimingText\(audit\)/);
  assert.match(controllerSource, /后台正连续分析剩余/);
  assert.match(controllerSource, /本轮板块结构质量不足，保留上一快照/);
});

test("sector workspace puts every eligible sector first and labels scan scope", () => {
  const Ui = loadUi();
  const workspace = fakeChartRoot();
  Ui.renderSectorWorkspace(workspace.root, {
    scan_audit: { selected_sector_count: 1 },
    signals: [],
    sectors: [
      { sector_id: "unselected", sector_name: "未通过板块", rank: null },
      { sector_id: "selected", sector_name: "合格板块", rank: 1 },
    ],
  });

  assert.deepEqual(
    workspace.root.children.map((row) => row.dataset.sectorId),
    ["all", "selected", "unselected"],
  );
  assert.equal(workspace.root.children[1].dataset.shortlisted, "true");
  assert.match(workspace.root.children[1].children[1].textContent, /符合要求并进入扫描/);
  assert.equal(workspace.root.children[2].dataset.shortlisted, "false");
  assert.match(workspace.root.children[2].children[1].textContent, /未通过结构门槛/);
  assert.match(dashboardCss, /\.es-sector-row\.is-shortlisted/);
});

test("signal lifecycle selects the analysis-first default chart and honest decision", () => {
  const Ui = loadUi();

  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "observed" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "approaching" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "armed" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "triggered" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "executable" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "active" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "unknown" }), "5m");

  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[0]).title, "等待 1分钟精确触发");
  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[1]).title, "可执行复核");
  assert.deepEqual(
    Ui.decisionSummaryForSignal({ lifecycle_stage: "armed", setup_5m: {} }),
    {
      tone: "waiting",
      title: "等待 1分钟精确触发",
      detail: "等待剩余结构条件",
      invalidation: "未提供",
      structuralStop: "未提供",
      riskMultiplier: "未提供",
    },
  );
});

test("period path and evidence groups separate established missing blocking and risk facts", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[0],
    point_type: "2buy",
    context_30m: {
      direction: "up",
      disposition: "supportive",
      dominant_point_type: "1buy",
      hard_block: false,
      reason_codes: ["confirmed_buy_structure"],
    },
    setup_5m: {
      point_type: "2buy",
      status: "provisional",
      center_ordinal: 1,
      evidence_codes: ["bottom_fractal_confirmed"],
      missing_conditions: ["terminal_line_confirmed"],
      invalidation_price: null,
    },
    trigger_1m: null,
    decision_reasons: ["one_minute_not_confirmed", "lower_or_unrelated_structure_risk"],
  };

  assert.equal(Ui.reasonLabel("confirmed_buy_structure"), "买入方向结构已确认");
  assert.equal(Ui.reasonLabel("unmapped_code"), "unmapped_code（未翻译）");
  assert.deepEqual(
    Ui.periodPathForSignal(signal).map(({ frequency, state, tone, summary, boundary }) => ({
      frequency, state, tone, summary, boundary,
    })),
    [
      {
        frequency: "30m",
        state: "支持",
        tone: "supportive",
        summary: "方向 向上 · 主导 一买",
        boundary: "无硬阻断",
      },
      {
        frequency: "5m",
        state: "形成中",
        tone: "waiting",
        summary: "二买 · 笔中枢 · 递归 0 · 第 1 中枢",
        boundary: "失效价未提供",
      },
      {
        frequency: "1m",
        state: "等待",
        tone: "waiting",
        summary: "尚未取得同向精确触发",
        boundary: "结构止损 9.80",
      },
    ],
  );

  const groups = Ui.evidenceGroupsForSignal(signal);
  assert.deepEqual(groups.established, [
    "30分钟：买入方向结构已确认",
    "5分钟：底分型确认",
  ]);
  assert.deepEqual(groups.missing, [
    "5分钟：末端结构确认",
    "1分钟：尚未取得同向精确触发",
    "1分钟同向确认尚未完成",
  ]);
  assert.deepEqual(groups.blocking, ["较低或无关结构存在风险"]);
  assert.deepEqual(groups.next, ["等待 1分钟同向买卖点闭合"]);
  assert.deepEqual(groups.risk, [
    "5分钟失效价：未提供",
    "结构止损：9.80",
    "风险乘数：0.50",
  ]);
  assert.deepEqual(groups.raw, [
    "confirmed_buy_structure",
    "bottom_fractal_confirmed",
    "terminal_line_confirmed",
    "one_minute_not_confirmed",
    "lower_or_unrelated_structure_risk",
  ]);
});

test("unfinished setup evidence remains missing until the structure closes", () => {
  const Ui = loadUi();
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    setup_5m: {
      ...snapshot.signals[0].setup_5m,
      evidence_codes: ["unfinished_trend_divergence"],
      missing_conditions: [],
    },
  });

  assert.equal(groups.established.includes("5分钟：趋势背驰结构尚未闭合"), false);
  assert.equal(groups.missing.includes("5分钟：趋势背驰结构尚未闭合"), true);
  assert.equal(groups.raw.includes("unfinished_trend_divergence"), true);
});

test("manual chart focus survives polling only for the same signal", () => {
  const Ui = loadUi();
  const initial = Ui.resolveFocusState(null, snapshot.signals[0]);
  assert.deepEqual(initial, {
    signalId: "signal-1",
    frequency: "5m",
    overrideSignalId: null,
  });

  const manual = Ui.manualFocusState(initial, "signal-1", "30m");
  assert.deepEqual(manual, {
    signalId: "signal-1",
    frequency: "30m",
    overrideSignalId: "signal-1",
  });
  assert.deepEqual(Ui.resolveFocusState(manual, snapshot.signals[0]), manual);
  assert.deepEqual(Ui.resolveFocusState(manual, snapshot.signals[1]), {
    signalId: "signal-2",
    frequency: "1m",
    overrideSignalId: null,
  });

  const automaticallyTracked = Ui.resolveFocusState(initial, {
    ...snapshot.signals[0],
    lifecycle_stage: "triggered",
  });
  assert.equal(automaticallyTracked.frequency, "1m");
  assert.equal(automaticallyTracked.overrideSignalId, null);
});

test("chart workspace renders the decision path evidence groups and active frequency", () => {
  const Ui = loadUi();
  const view = fakeChartRoot();
  const signal = {
    ...snapshot.signals[0],
    point_type: "2buy",
    context_30m: {
      direction: "up",
      disposition: "supportive",
      dominant_point_type: "1buy",
      hard_block: false,
      reason_codes: ["confirmed_buy_structure"],
    },
    setup_5m: {
      point_type: "2buy",
      status: "provisional",
      center_ordinal: 1,
      evidence_codes: ["bottom_fractal_confirmed"],
      missing_conditions: ["terminal_line_confirmed"],
      invalidation_price: "9.90",
    },
    decision_reasons: ["one_minute_not_confirmed"],
  };

  Ui.renderChartWorkspace(view.root, signal, { frequency: "5m" });

  assert.equal(view.root.dataset.focusedFrequency, "5m");
  assert.equal(view.root.dataset.signalSide, "buy");
  assert.equal(view.node("[data-decision-title]").textContent, "等待 1分钟精确触发");
  assert.equal(view.node("[data-decision-invalidation]").textContent, "9.90");
  assert.equal(view.node('[data-period-state="30m"]').textContent, "支持");
  assert.equal(view.node('[data-period-state="5m"]').textContent, "形成中");
  assert.equal(view.node('[data-period-state="1m"]').textContent, "等待");
  assert.deepEqual(
    view.node('[data-evidence-group="missing"]').children.map((node) => node.textContent),
    [
      "5分钟：末端结构确认",
      "1分钟：尚未取得同向精确触发",
      "1分钟同向确认尚未完成",
    ],
  );
  assert.equal(
    view.node("[data-chart-workbench]").getAttribute("href"),
    "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
  );
  assert.deepEqual(
    view.list("[data-focus-frequency]").map((node) => node.getAttribute("aria-pressed")),
    ["false", "true", "false"],
  );
  assert.ok(Number(view.node("[data-evidence-count]").textContent) > 0);
  assert.equal(view.node("[data-evidence-toggle]").disabled, false);
  assert.equal(view.node("[data-theater-toggle]").disabled, false);
});

test("chart workspace stays visible and clears stale charts when no signal exists", () => {
  const Ui = loadUi();
  const view = fakeChartRoot();
  view.node("[data-chart-content]").hidden = true;
  view.node("[data-decision-title]").textContent = "旧结论";
  for (const frequency of ["30m", "5m", "1m"]) {
    view.node(`[data-chart-frame="${frequency}"]`).setAttribute("src", `/stale-${frequency}`);
  }

  Ui.renderChartWorkspace(view.root, null);

  assert.equal(view.node("[data-chart-content]").hidden, false);
  assert.equal(view.node("[data-decision-title]").textContent, "数据未知");
  assert.equal(view.root.dataset.signalSide, "neutral");
  assert.deepEqual(
    ["30m", "5m", "1m"].map((frequency) => (
      view.node(`[data-chart-frame="${frequency}"]`).getAttribute("src")
    )),
    ["about:blank", "about:blank", "about:blank"],
  );
  assert.equal(view.node("[data-evidence-count]").textContent, "0");
  assert.equal(view.node("[data-evidence-toggle]").disabled, true);
  assert.equal(view.node("[data-theater-toggle]").disabled, true);
});

test("dashboard controller wires adaptive focus and manual period controls", () => {
  assert.match(
    controllerSource,
    /state\.selectedSignalId = Ui\.resolveSelectedSignalId\(\s*state\.selectedSignalId,\s*filtered,\s*state\.snapshot\.signals,?\s*\)/,
  );
  assert.match(controllerSource, /focusState:\s*Ui\.resolveFocusState\(null, null\)/);
  assert.match(controllerSource, /state\.focusState = Ui\.resolveFocusState\(state\.focusState, selected\)/);
  assert.match(
    controllerSource,
    /Ui\.renderChartWorkspace\(chartWorkspace, selected, \{\s*frequency: state\.focusState\.frequency,?\s*\}\)/,
  );
  assert.match(controllerSource, /\[data-focus-frequency\], \[data-period-node\]/);
  assert.match(controllerSource, /Ui\.manualFocusState\(/);
});

test("dashboard controller wires evidence drawer theater mode and escape priority", () => {
  assert.match(controllerSource, /evidenceOpen:\s*false/);
  assert.match(controllerSource, /theaterMode:\s*false/);
  assert.match(controllerSource, /\[data-evidence-toggle\]/);
  assert.match(controllerSource, /\[data-evidence-close\]/);
  assert.match(controllerSource, /\[data-theater-toggle\]/);
  assert.match(controllerSource, /event\.key\s*!==\s*"Escape"/);
  assert.match(controllerSource, /if\s*\(state\.evidenceOpen\)/);
  assert.match(controllerSource, /else if\s*\(state\.theaterMode\)/);
});
