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

function loadUi() {
  delete require.cache[require.resolve(uiPath)];
  return require(uiPath);
}

const snapshot = {
  schema_version: "chanlun-trading-screening/v2",
  structure_version: "v2",
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
  assert.match(template, /data-schema="chanlun-trading-screening\/v2"/);
  assert.match(template, /id="es-sector-completion"/);
  assert.match(template, /data-workspace="sector"/);
  assert.match(template, /data-workspace="signals"/);
  assert.match(template, /data-workspace="charts"/);
  assert.match(template, /data-layout="single"/);
  assert.match(template, /data-layout="split"/);
  assert.match(template, /data-layout="quad"/);
  assert.match(template, /4 个自然日/);
});

test("dashboard has six independent point filters", () => {
  for (const point of ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"]) {
    assert.match(template, new RegExp(`data-point-type="${point}"`));
  }
});

test("normalizeSnapshot accepts only the new read-only schema", () => {
  const Ui = loadUi();
  const normalized = Ui.normalizeSnapshot(snapshot);

  assert.equal(normalized.schema_version, "chanlun-trading-screening/v2");
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
    "30m": "/?market=a&code=SZ.000001&layout=single&intervals=30",
    "5m": "/?market=a&code=SZ.000001&layout=single&intervals=5",
    "1m": "/?market=a&code=SZ.000001&layout=single&intervals=1",
  });
  assert.deepEqual(Ui.chartUrlsForSignal(snapshot.signals[1]), {
    "30m": "/?market=a&code=SZ.000002&layout=single&intervals=30",
    "5m": "/?market=a&code=SZ.000002&layout=single&intervals=5",
    "1m": "/?market=a&code=SZ.000002&layout=single&intervals=1",
  });
});

test("chart layout switch accepts only single split and quad", () => {
  const Ui = loadUi();
  const root = { dataset: { currentLayout: "single" } };

  assert.equal(Ui.setChartLayout(root, "split"), "split");
  assert.equal(root.dataset.layout, "split");
  assert.equal(root.dataset.currentLayout, "split");
  assert.equal(Ui.setChartLayout(root, "unknown"), "single");
  assert.equal(root.dataset.layout, "single");
  assert.equal(root.dataset.currentLayout, "single");
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
    "本批 32/32 · 待扫 68",
  );
  assert.equal(
    Ui.scanCoverageText({
      planned_symbol_count: 7,
      completed_symbol_count: 7,
      pending_symbol_count: 0,
    }),
    "本批 7/7 · 队列已覆盖",
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
    Ui.sectorEvidenceText(snapshot.sectors[0]),
    "30m 向上/支撑/一买 · 5m 震荡/中性/无主导点",
  );
  assert.match(controllerSource, /Ui\.sectorCoverageText\(audit\)/);
  assert.match(controllerSource, /本轮板块结构质量不足，保留上一快照/);
});
