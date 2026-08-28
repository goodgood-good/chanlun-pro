"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const { mock } = require("node:test");
const path = require("node:path");

// Pure dashboard fixtures describe one frozen review instant.  Keep implicit
// live-clock calls deterministic; boundary-crossing tests pass their own clock.
mock.timers.enable({
  apis: ["Date"],
  now: new Date("2026-07-20T14:58:30+08:00"),
});

const uiPath = path.resolve(__dirname, "../early_screening_ui.js");
const uiSource = fs.readFileSync(uiPath, "utf8");

test("screening guidance never describes structural ratios as account positions", () => {
  assert.doesNotMatch(uiSource, /段差仓|账户|持仓|仓位|组合热度只可下调/);
});
const humanReviewUiPath = path.resolve(
  __dirname,
  "../human_review_screening.js",
);
const template = fs.readFileSync(
  path.resolve(__dirname, "../../../templates/early_screening.html"),
  "utf8",
);
const controllerSource = fs.readFileSync(path.resolve(__dirname, "../early_screening.js"), "utf8");
const humanReviewSource = fs.readFileSync(
  path.resolve(__dirname, "../human_review_screening.js"),
  "utf8",
);
const markoutAuditSource = fs.readFileSync(
  path.resolve(__dirname, "../human_review_markout_audit.js"),
  "utf8",
);
const dashboardCss = fs.readFileSync(path.resolve(__dirname, "../../css/early_screening.css"), "utf8");

test("live and review requests are bounded and the review queue polls while visible", () => {
  assert.match(controllerSource, /SNAPSHOT_REQUEST_TIMEOUT_MS\s*=\s*20_000/);
  assert.match(controllerSource, /SNAPSHOT_RECOVERY_RETRY_MS\s*=\s*750/);
  assert.match(controllerSource, /new AbortController\(\)/);
  assert.match(controllerSource, /signal:\s*controller\.signal/);
  assert.match(controllerSource, /snapshot_request_timeout/);
  assert.match(controllerSource, /cache:\s*"no-cache"/);
  assert.doesNotMatch(controllerSource, /cache:\s*"no-store"/);
  assert.match(controllerSource, /response\.status === 304 \? null/);
  assert.match(controllerSource, /snapshot_not_modified_without_state/);
  assert.match(controllerSource, /for \(let attempt = 0; attempt < 2;/);
  assert.match(controllerSource, /response\.status === 401/);
  assert.match(controllerSource, /snapshot_authentication_required/);
  assert.match(controllerSource, /await waitForSnapshotRetry\(response\)/);
  assert.match(humanReviewSource, /REQUEST_TIMEOUT_MS\s*=\s*30_000/);
  assert.match(humanReviewSource, /new AbortController\(\)/);
  assert.match(humanReviewSource, /signal:\s*controller\.signal/);
  assert.match(humanReviewSource, /function schedulePoll\(\)/);
  assert.match(
    humanReviewSource,
    /document\.visibilityState === "visible" && state\.mode === "human-review"/,
  );
});

test("manual A-share attention symbols render the isolated quote price and percentage", () => {
  assert.match(controllerSource, /symbolRow\.quote_available === true/);
  assert.match(controllerSource, /symbolRow\.current_price/);
  assert.match(controllerSource, /symbolRow\.change_percent/);
  assert.match(controllerSource, /change\.toFixed\(2\)\}%/);
  assert.match(dashboardCss, /\.es-holding-card__quote/);
  assert.match(dashboardCss, /font-variant-numeric:\s*tabular-nums/);
});

function loadUi() {
  delete require.cache[require.resolve(uiPath)];
  return require(uiPath);
}

function loadHumanReviewUi() {
  delete require.cache[require.resolve(humanReviewUiPath)];
  return require(humanReviewUiPath);
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
        lists.set(selector, ["d", "30m", "5m", "1m"].map((frequency) => {
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

function currentHigherTimeframeRisk() {
  return {
    market_gate: "GREEN",
    sector_gate: "GREEN",
    symbol_gate: "GREEN",
    market_reason_codes: [],
    sector_reason_codes: [],
    symbol_reason_codes: [],
    reason_codes: [],
    sector_higher_timeframe_source_mode: "PAGE_PARITY_SAME_5M_BASE",
    sector_strict_same_5m_warmup_evidence: {
      required_daily_bar_count: 480,
      full_daily_bar_count: 480,
      suffix_daily_bar_count: 320,
      converged: true,
      reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
    },
    sector_strict_same_5m_source_coverage_evidence: sectorSameBaseCoverage({
      converged: true,
      fullCount: 480,
    }),
    sector_research_bridge_parameter_set_id: null,
  };
}

const snapshot = {
  schema: "chanlun-trading-screening",
  structure_contract_id: "physical-timeframe-recursive",
  available: true,
  scan_state: "complete",
  generated_at: "2026-07-20T15:00:00+08:00",
  as_of: "2026-07-20T15:00:00+08:00",
  sector_first: true,
  read_only: true,
  research_only: true,
  no_order_execution: true,
  counts_by_stage: { approaching: 1, triggered: 1 },
  counts_by_point_type: {
    "1buy": 1, "2buy": 1, "3buy": 0,
    "1sell": 0, "2sell": 0, "3sell": 0,
  },
  sectors: [
    {
      sector_id: "qmt-gics3:bank",
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
      lifecycle_stage: "approaching",
      observed_at: "2026-07-20T14:55:00+08:00",
      sector: { sector_id: "qmt-gics3:bank", sector_name: "银行" },
      context_30m: { direction: "up", disposition: "supportive" },
      setup_5m: { point_type: "1buy", center_ordinal: null },
      segment_difference_1m: null,
      structural_stop: "9.80",
      risk_multiplier: "0.50",
      entry_allowed: false,
      exit_allowed: false,
      decision_reasons: ["one_minute_not_confirmed"],
      higher_timeframe_risk: currentHigherTimeframeRisk(),
      chart_urls: {
        "d": "/?market=a&code=SZ.000001&layout=single&intervals=D",
        "30m": "/?market=a&code=SZ.000001&layout=single&intervals=30",
        "5m": "/?market=a&code=SZ.000001&layout=single&intervals=5",
        "1m": "/?market=a&code=SZ.000001&layout=single&intervals=1",
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
      sector: { sector_id: "qmt-gics3:real-estate", sector_name: "房地产" },
      context_30m: { direction: "neutral", disposition: "neutral" },
      setup_5m: { point_type: "2buy", center_ordinal: null },
      segment_difference_1m: {
        point_type: "1buy",
        available_at: "2026-07-20T14:58:00+08:00",
      },
      entry_execution_boundary: {
        confirmation_bar_closed_at: "2026-07-20T14:58:00+08:00",
        entry_valid_until: "2026-07-20T14:59:00+08:00",
      },
      structural_stop: "7.50",
      risk_multiplier: "1.00",
      entry_allowed: true,
      exit_allowed: false,
      decision_reasons: [],
      execution_profile: {
        segment_difference_ready: true,
        precise_execution_ready: true,
      },
      higher_timeframe_risk: currentHigherTimeframeRisk(),
      chart_urls: {
        "d": "/?market=a&code=SZ.000002&layout=single&intervals=D",
        "30m": "/?market=a&code=SZ.000002&layout=single&intervals=30",
        "5m": "/?market=a&code=SZ.000002&layout=single&intervals=5",
        "1m": "/?market=a&code=SZ.000002&layout=single&intervals=1",
      },
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
  assert.match(template, /data-schema="chanlun-trading-screening\"/);
  assert.match(template, /id="es-sector-completion"/);
  assert.match(template, /id="es-audit-locked-count"/);
  assert.match(template, /id="es-confirmed-point-distribution"/);
  assert.match(template, /id="es-candidate-point-distribution"/);
  assert.match(template, /id="es-scan-timing"/);
  assert.match(template, /id="hr-sector-receipts"/);
  assert.match(template, /id="hr-forward-scheduler-status"/);
  assert.match(template, /id="hr-qmt-runtime-status"/);
  assert.match(template, /id="hr-forward-screening-status"/);
  assert.match(template, /id="hr-forward-archive-status"/);
  assert.match(template, /id="hr-forward-delivery-status"/);
  assert.match(template, /id="es-holdings-list"/);
  assert.match(template, /id="es-holdings-declared"/);
  assert.match(template, /id="es-holdings-monitored"/);
  assert.match(template, /id="es-holdings-unsupported"/);
  assert.match(template, /id="es-us-monitor-title">美股实时监听/);
  assert.match(template, /id="es-us-monitor" class="es-us-monitor-compact"/);
  assert.match(template, /id="es-us-monitor-health-panel"/);
  assert.doesNotMatch(template, /id="es-us-monitor-list"/);
  assert.doesNotMatch(template, /class="es-us-monitor-columns"/);
  assert.match(template, /data-market="a"/);
  assert.match(template, /data-market="us"/);
  assert.match(template, /美股线索不参与板块门且会继续保留/);
  assert.match(template, /id="es-us-monitor-active"/);
  assert.match(template, /id="es-us-monitor-other"/);
  assert.match(template, /id="es-us-monitor-updated"/);
  assert.match(template, /id="es-us-monitor-notifications"/);
  assert.match(template, /<dt>待机<\/dt><dd id="es-us-monitor-other"/);
  assert.match(template, /class="es-us-monitor-compact__metrics"/);
  assert.match(template, /id="es-sector-catalog-status"/);
  assert.match(template, /data-selection-scope="sector-trigger"/);
  assert.match(template, /data-selection-scope="all-qualified"/);
  assert.match(controllerSource, /function renderManualAttention\(\)/);
  assert.match(controllerSource, /function renderUsMonitorStatus\(\)/);
  assert.doesNotMatch(controllerSource, /es-us-monitor-card__groups/);
  assert.match(controllerSource, /function renderSectorCatalogStatus\(\)/);
  assert.match(controllerSource, /CURRENT_COVERAGE_CYCLE/);
  assert.match(controllerSource, /CACHED_SECTOR_SNAPSHOT/);
  assert.match(controllerSource, /LAST_INVALIDATED_SNAPSHOT/);
  assert.doesNotMatch(controllerSource, /1分钟触发 · 5分钟结构 · 30分钟背景 · 实时监听正常/);
  assert.match(controllerSource, /A_SHARE_STRICT_DECISION_CORE/);
  assert.match(controllerSource, /非A股辅助结构雷达监听中/);
  assert.match(controllerSource, /selectionScope: savedSelectionScope/);
  assert.match(controllerSource, /searchParams\.set\("scope", requestedScope\)/);
  assert.match(controllerSource, /QMT_SECTOR_TRIGGER/);
  assert.match(controllerSource, /snapshot\.manual_attention_signals/);
  assert.match(controllerSource, /当前休市 · 开市后自动恢复实时监听/);
  assert.match(dashboardCss, /\.es-holding-card\.is-alert/);
  assert.match(dashboardCss, /\.es-us-monitor-compact\s*\{/);
  assert.match(dashboardCss, /\.es-sector-catalog-status\[data-state="preview"\]/);
  assert.match(humanReviewSource, /sector_capture_receipts/);
  assert.match(humanReviewSource, /forward_operations/);
  assert.match(humanReviewSource, /forwardOperations\.qmt_runtime/);
  assert.match(humanReviewSource, /SCHEDULED_TASK_PRINCIPAL_MISMATCH/);
  assert.match(humanReviewSource, /FORWARD_SCHEDULER_NOT_READY_FOR_PAPER/);
  assert.match(humanReviewSource, /FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER/);
  assert.match(humanReviewSource, /SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER/);
  assert.match(humanReviewSource, /尚未获得同日 QMT 盘前抓取回执/);
  assert.doesNotMatch(humanReviewSource, /09:10 后缺少同日 QMT 盘前抓取回执/);
  assert.match(humanReviewSource, /CAPTURE_MISSING_AFTER_DUE/);
  assert.match(humanReviewSource, /EVALUATION_MISSING_AFTER_DEADLINE/);
  assert.match(humanReviewSource, /DATA_READY_EVENT_MISSING/);
  assert.match(humanReviewSource, /CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED/);
  assert.match(humanReviewSource, /IMPLEMENTATION_CHANGED_SINCE_CAPTURE/);
  assert.match(humanReviewSource, /CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE/);
  assert.match(humanReviewSource, /CAPTURE_EVENT_EVIDENCE_UNPROVEN/);
  assert.match(humanReviewSource, /EVALUATION_VALUATION_EVIDENCE_MISSING/);
  assert.match(humanReviewSource, /EVALUATION_ARTIFACT_EVIDENCE_INVALID/);
  assert.match(humanReviewSource, /逐日前向交付尚未完成/);
  assert.match(humanReviewSource, /当日板块快照缺失/);
  assert.match(humanReviewSource, /当日回执缺失/);
  assert.match(humanReviewSource, /回执未证明/);
  assert.match(humanReviewSource, /回执无效/);
  assert.match(template, /系统与审计状态/);
  assert.match(template, /id="hr-position-recommendation"/);
  assert.match(template, /id="hr-tactical-execution-status"/);
  for (const removedId of [
    "hr-execution-evidence-status",
    "hr-entry-selection-evidence-status",
    "hr-portfolio-rejection-evidence-status",
    "hr-portfolio-decision-audit-status",
    "hr-portfolio-fill-decision-audit-status",
    "hr-virtual-reserved-sell-quantity",
    "hr-virtual-cancelled-count",
    "hr-virtual-operations-cancelled-count",
    "hr-paper-path-status",
    "hr-paper-path-reasons",
    "hr-paper-accounting-status",
    "hr-paper-cash-balance",
    "hr-paper-total-fees",
    "hr-paper-valuation-status",
    "hr-paper-market-value",
    "hr-paper-equity",
  ]) {
    assert.doesNotMatch(template, new RegExp(`id="${removedId}"`));
  }
  assert.match(humanReviewSource, /paperPathDecision/);
  assert.match(humanReviewSource, /HISTORICAL_SOURCE_REVIEW_ONLY/);
  assert.match(humanReviewSource, /SOURCE_SUPERSEDED_FOR_PAPER/);
  assert.match(humanReviewSource, /SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER/);
  assert.match(humanReviewSource, /CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER/);
  assert.match(humanReviewSource, /来源行情会话/);
  assert.match(humanReviewSource, /仅记录人工识别，不建立执行计划/);
  assert.match(humanReviewSource, /HIGHER_TIMEFRAME_GATE_NOT_GREEN/);
  assert.match(humanReviewSource, /NO_CAUSAL_1M_EXECUTION_BAR_REMAINS_BEFORE_TTL/);
  assert.match(humanReviewSource, /INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES/);
  assert.match(humanReviewSource, /观察记录已建立，等待后续合法 1m K 线/);
  assert.match(humanReviewSource, /在账本状态可验证前保持失败关闭/);
  assert.match(template, /只接受零订单、零成交且哈希验证通过的候选证据/);
  assert.doesNotMatch(template, /账户|现金|持仓|仓位|持有|虚拟|组合热度|硬阻断/);
  assert.doesNotMatch(uiSource, /持有跟踪|无硬阻断|硬阻断：/);
  assert.doesNotMatch(controllerSource, /snapshot\.manual_holdings|snapshot\.manual_holding_signals/);
  assert.match(humanReviewSource, /候选报告自身零订单\/零成交/);
  assert.doesNotMatch(humanReviewSource, /账户|现金|持仓|仓位|虚拟|组合热度/);
  assert.doesNotMatch(humanReviewSource, /QMT 板块先行 · 零订单\/零成交/);
  assert.match(template, /复核记录只保存中枢、走势、级别、买卖点和失效条件判断/);
  assert.match(humanReviewSource, /精确 QMT 目录/);
  assert.match(humanReviewSource, /terminal_signal_lifecycle_one_shot_enforced/);
  assert.match(humanReviewSource, /fixed_one_lot_tactical_review_only/);
  assert.match(humanReviewSource, /固定 100 股（一手）仅观察；不覆盖多手部分成交/);
  assert.match(humanReviewSource, /sectorSourceDisclosure/);
  assert.match(humanReviewSource, /sector_higher_timeframe_evidence/);
  assert.match(humanReviewSource, /原生日线研究桥（最高为琥珀色）/);
  assert.match(humanReviewSource, /严格5m历史审计/);
  assert.match(humanReviewSource, /marketSymbolHigherTimeframeDisclosure/);
  assert.match(humanReviewSource, /market_symbol_higher_timeframe_evidence/);
  assert.match(humanReviewSource, /日线高级别结构与来源可复核/);
  assert.match(humanReviewSource, /日线高级别结构可复核·来源未附/);
  assert.match(humanReviewSource, /日线高级别结构可复核·来源部分/);
  assert.match(humanReviewSource, /完成K线数量及结构映射，已按无效处理/);
  assert.match(humanReviewSource, /evidence_bar_end/);
  assert.match(humanReviewSource, /mapping_candidate_ids/);
  assert.match(humanReviewSource, /source_support/);
  assert.match(humanReviewSource, /session_evidence/);
  assert.match(humanReviewSource, /warmup_evidence/);
  assert.match(humanReviewSource, /native_daily_reconciliation_evidence/);
  assert.match(humanReviewSource, /精确检查完成，未发现缺日或 240 根网格异常/);
  assert.match(humanReviewSource, /原生日线调和：与1m派生日线重合/);
  assert.match(humanReviewSource, /sectorNameDisclosure/);
  assert.match(humanReviewSource, /POINT_IN_TIME_SAME_SESSION/);
  assert.match(humanReviewSource, /SAME_SESSION_SYMBOL_NOT_MEMBER/);
  assert.match(humanReviewSource, /排序观测/);
  assert.match(humanReviewSource, /候选股票属于该快照/);
  assert.match(
    humanReviewSource,
    /QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE/,
  );
  assert.match(humanReviewSource, /strategic_buy_entire_bar_strict_cross_enforced/);
  assert.match(humanReviewSource, /strategic_buy_no_chase_reject_independent_of_volume/);
  assert.match(humanReviewSource, /strategic_buy_five_percent_bar_volume_cap_enforced/);
  assert.match(humanReviewSource, /persistent_sell_five_percent_bar_volume_cap_enforced/);
  assert.match(humanReviewSource, /adverse_observed_bar_extreme_fill_price_enforced/);
  assert.match(humanReviewSource, /completed_bar_close_fill_timestamp_enforced/);
  assert.match(humanReviewSource, /越价即拒 \/ 整柱严格穿价 \/ 5% \/ 不利极值 \/ 收盘确认/);
  assert.match(humanReviewSource, /source_provenance_available/);
  assert.match(markoutAuditSource, /source_provenance_status/);
  assert.match(template, /id="hr-decision-core-id"/);
  assert.match(template, /id="hr-decision-source-id"/);
  assert.match(template, /id="hr-markout-cohort-status"/);
  assert.match(template, /id="hr-forward-lineage-status"/);
  assert.match(humanReviewSource, /forward_warmup_structure_lineage/);
  assert.match(humanReviewSource, /NO_QUALIFIED_SESSIONS/);
  assert.match(humanReviewSource, /等待完整前向日/);
  assert.match(humanReviewSource, /暖机谱系/);
  assert.match(humanReviewSource, /decision_source_snapshot_id/);
  assert.match(markoutAuditSource, /source_identity_status/);
  assert.match(markoutAuditSource, /mixed_sample_cohorts/);
  assert.match(markoutAuditSource, /sample_sufficient_by_horizon/);
  assert.match(markoutAuditSource, /单一已认证实现/);
  assert.match(markoutAuditSource, /已拆分，禁止合并/);
  assert.match(markoutAuditSource, /同批样本门通过/);
  assert.match(markoutAuditSource, /同批样本不足/);
  assert.match(markoutAuditSource, /源码未认证/);
  assert.match(markoutAuditSource, /价格来源未完整/);
  assert.match(humanReviewSource, /paper_observation_eligible/);
  assert.match(humanReviewSource, /feedbackMatchesLatest/);
  assert.match(humanReviewSource, /retryRequestId/);
  assert.match(humanReviewSource, /candidate\.realtime_notification === true/);
  assert.match(humanReviewSource, /mergeRealtimeNotificationQueue\(payload\.data\)/);
  assert.doesNotMatch(humanReviewSource, /补建虚拟意图/);
  assert.match(humanReviewSource, /REVIEW_ONLY/);
  assert.match(template, /data-workspace="sector"/);
  assert.match(template, /data-workspace="signals"/);
  assert.match(template, /data-workspace="charts"/);
  assert.match(template, /data-layout="focus"/);
  assert.match(template, /data-layout="dual"/);
  assert.match(template, /data-layout="triple"/);
  assert.match(controllerSource, /runtime_health/);
  assert.match(controllerSource, /后台选股扫描健康门未通过/);
  assert.match(controllerSource, /priority_monitor_ready/);
  assert.match(controllerSource, /盘中实时预警通道尚未就绪/);
  assert.match(controllerSource, /优先预警正常，候选范围仍在准备/);
  assert.match(controllerSource, /Ui\.segmentScopeText\(runtimeHealth, segmentDifferenceCount\)/);
  assert.match(controllerSource, /full_coverage_refresh_paused/);
  assert.match(controllerSource, /full_coverage_next_active_at/);
  assert.match(controllerSource, /全市场覆盖等待下一运行窗口/);
  assert.match(controllerSource, /盘中算力正用于人工关注、自选与强板块候选的实时预警/);
  assert.match(template, /id="es-preselection-status"/);
  assert.match(template, /id="es-priority-monitor-status"/);
  assert.match(template, /id="es-preselection-diagnostic"/);
  assert.match(template, /id="es-priority-monitor-diagnostic"/);
  assert.match(template, /收盘后生成候选，盘中跟踪结构变化/);
  assert.match(template, /<dt>今日候选名单<\/dt>/);
  assert.match(dashboardCss, /\.es-status-facts dd\s*\{[^}]*overflow-wrap:\s*anywhere/s);
  assert.match(uiSource, /daily_preselection_ready/);
  assert.match(uiSource, /daily_preselection_target_session/);
  assert.match(uiSource, /daily_preselection_expected_session/);
  assert.match(controllerSource, /Ui\.dailyPreselectionText\(runtimeHealth\)/);
  assert.match(
    controllerSource,
    /Ui\.priorityMonitorText\(runtimeHealth, liveOverlay, snapshot\.us_monitor\)/,
  );
  assert.match(template, /旧线段、失效、结束及旧版迁移状态均不进入当前列表/);
});

test("desktop layout aligns the signal queue with the chart below a compact sector rail", () => {
  assert.match(
    dashboardCss,
    /grid-template-areas:\s*"sector sector"\s*"signals charts"/,
  );
  assert.match(
    dashboardCss,
    /\.es-sector-list\s*\{[\s\S]*?grid-template-columns:\s*repeat\(auto-fit, minmax\(250px, 1fr\)\)/,
  );
  assert.match(template, /<details class="es-rule-note">[\s\S]*?<summary>规则与数据口径<\/summary>/);
  const sectorWorkspaceStart = template.indexOf('class="es-workspace es-sector-workspace"');
  const signalWorkspaceStart = template.indexOf('class="es-workspace es-signal-workspace"');
  const lifecycleFilters = template.indexOf('id="es-lifecycle-filters"');
  const pointFilters = template.indexOf('id="es-point-filters"');
  assert.ok(sectorWorkspaceStart < lifecycleFilters && lifecycleFilters < signalWorkspaceStart);
  assert.ok(sectorWorkspaceStart < pointFilters && pointFilters < signalWorkspaceStart);
  assert.match(dashboardCss, /\.es-global-filters\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 1fr\)/);
  assert.match(dashboardCss, /\.es-search\s*\{[^}]*margin:\s*6px 14px/s);
  assert.match(dashboardCss, /\.es-workspace__header\s*\{[^}]*min-height:\s*64px/s);
  assert.match(dashboardCss, /\.es-signal-list\s*\{[^}]*padding-top:\s*8px/s);
  assert.match(template, /<details class="es-global-filter-drawer">[\s\S]*?id="es-filter-summary"/);
  assert.match(template, /<details class="es-chart-display-settings">[\s\S]*?data-layout="focus"/);
  assert.match(template, /<details class="hr-audit-summary">[\s\S]*?class="hr-summary"/);
  assert.match(controllerSource, /sectorExpanded:\s*false/);
  assert.match(controllerSource, /\{ expanded: state\.sectorExpanded, limit: 10 \}/);
});

test("human review disclosures execute outside boot and explain sector ranking", () => {
  const Ui = loadHumanReviewUi();
  const candidate = {
    sector_name: "银行",
    sector_name_attestation: "POINT_IN_TIME_SAME_SESSION",
    sector_membership_attestation: "POINT_IN_TIME_SAME_SESSION",
    sector_name_captured_at: "2026-07-28T09:10:00+08:00",
    sector_name_entry_sha256: `sha256:${"1".repeat(64)}`,
    sector_name_catalog_revision: `sha256:${"2".repeat(64)}`,
    sector_ranking_catalog_attestation: "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH",
    sector_ranking_evidence: {
      ordinal: 2,
      rank_score: 45,
      rank_components: { five_support: 0, neutral_access: 5, thirty_support: 40 },
      horizontal_strength: "7.5",
      horizontal_rank: 1,
      strength_observed_at: "2026-07-28T09:10:00+08:00",
      strength_anchor_session: "2026-07-25",
      strength_member_count: 42,
      evidence_id: `sha256:${"3".repeat(64)}`,
      strength_source_revision: `sha256:${"4".repeat(64)}`,
      strength_evidence_revision: `sha256:${"5".repeat(64)}`,
      sector_catalog_revision: `sha256:${"2".repeat(64)}`,
    },
  };

  const name = Ui.sectorNameDisclosure(candidate);
  const ranking = Ui.sectorRankingDisclosure(candidate);
  assert.equal(name.tag, "板块归属点时可证");
  assert.match(name.line, /排序观测时点前已采集/);
  assert.equal(ranking.tag, "板块排序分项可复核");
  assert.match(ranking.lines[0], /综合第 2 名/);
  assert.match(ranking.lines[0], /thirty_support=40/);
  assert.match(ranking.lines[1], /横向强度 7.5/);
  assert.match(ranking.lines[3], /同一份QMT目录修订/);
  assert.equal(ranking.factIds.length, 4);

  const mixed = Ui.sectorRankingDisclosure({
    ...candidate,
    sector_ranking_catalog_attestation: "EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION",
  });
  assert.equal(mixed.tag, "板块排序目录未闭环");
  assert.match(mixed.lines[3], /未改配到同日其他快照/);

  const invalid = Ui.sectorRankingDisclosure({});
  assert.equal(invalid.tag, "板块排序证据无效");
  assert.match(invalid.lines[0], /当前契约要求的完整板块排序证据/);
});

test("human review shows daily diagnostics only and labels buy points diagnostic-only", () => {
  const Ui = loadHumanReviewUi();
  const warmup = {
    required_daily_bar_count: 480,
    full_daily_bar_count: 480,
    suffix_daily_bar_count: 320,
    converged: true,
    reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
  };
  const convergence = {
    status: "NON_MONOTONIC",
    observation_count: 4,
    observations: [480, 640, 800, 960].map((barCount) => ({
      bar_count: barCount,
    })),
    diagnostic_only: true,
    active_gate_unchanged: true,
  };
  const active = {
    period: "D",
    state: "FORMED",
    completed_bar_count: 24,
    evidence_bar_end: "2026-07-31T15:00:00+08:00",
    mapping_unique: true,
    mapped_center_id: "D-center-1",
    mapping_candidate_ids: ["D-center-1"],
    mapping_supply: {
      classification: "UNIQUE_MAPPING",
      point_evidence_count: 7,
      point_type_counts: {
        "1sell": 1, "2sell": 1, "3sell": 1, "3buy": 2,
      },
      in_top_interval_sell12_count: 2,
      completed_in_top_interval_sell12_count: 1,
      diagnostic_buy_point_type_counts: { "1buy": 2, "2buy": 1 },
      diagnostic_buy_point_evidence: [
        { point_id: `sha256:${"a".repeat(64)}` },
        { point_id: `sha256:${"b".repeat(64)}` },
        { point_id: `sha256:${"c".repeat(64)}` },
      ],
    },
  };
  const inactive = (period) => ({
    period,
    state: "NONE",
    completed_bar_count: 24,
    evidence_bar_end: "2026-07-31T15:00:00+08:00",
    mapping_unique: true,
    mapped_center_id: null,
    mapping_candidate_ids: [],
    mapping_supply: null,
  });
  const sectorEvidence = {
    schema: "chanlun-human-review-sector-higher-timeframe-evidence",
    source_mode: "PAGE_PARITY_SAME_5M_BASE",
    strict_same_5m_warmup_evidence: warmup,
    warmup_convergence_evidence: convergence,
    strict_same_5m_warmup_convergence_evidence: convergence,
    research_bridge_parameter_set_id: null,
    sector_id: "QMT:GICS3:bank",
    observed_at: "2026-08-01T10:00:00+08:00",
    gate: "AMBER",
    states: { M: "FORMED", W: "NONE", D: "FORMED" },
    reason_codes: ["SECTOR_MONTHLY_TOP_FORMED"],
    period_diagnostics: [inactive("M"), inactive("W"), active],
    evidence_id: `sha256:${"d".repeat(64)}`,
  };
  const sector = Ui.sectorSourceDisclosure({
    sector_higher_timeframe_evidence: sectorEvidence,
  });
  assert.match(sector.lines.join("\n"), /板块日线：顶部结构已形成 · 研究状态/);
  assert.match(sector.lines.join("\n"), /板块日线方向诊断：一买 2 \/ 二买 1/);
  assert.match(sector.lines.join("\n"), /稳定身份 3 个/);
  assert.match(sector.lines.join("\n"), /仅供人工识别，不参与卖点映射、风险门或订单/);
  assert.doesNotMatch(sector.lines.join("\n"), /月线|周线|月\/周\/日/);
  assert.match(sector.lines.join("\n"), /板块当前来源多前缀暖机诊断：非单调/);
  assert.match(sector.lines.join("\n"), /严格5m同源多前缀暖机诊断/);
  assert.match(sector.lines.join("\n"), /合格前缀 4 个（480–960 根日线）/);
  assert.match(sector.lines.join("\n"), /仅作历史稳定性审计，不参与买入放行/);
  assert.deepEqual(sector.factIds, [sectorEvidence.evidence_id]);

  const marketSymbol = Ui.marketSymbolHigherTimeframeDisclosure({
    market_symbol_higher_timeframe_evidence: {
      market: {
        gate: "AMBER",
        states: { M: "FORMED", W: "NONE", D: "FORMED" },
        reason_codes: [],
        period_diagnostics: [inactive("M"), inactive("W"), active],
        source_support: { warmup_convergence_evidence: convergence },
      },
      symbol_evidence: {
        gate: "GREEN",
        states: { M: "NONE", W: "NONE", D: "NONE" },
        reason_codes: [],
        period_diagnostics: [inactive("M"), inactive("W"), inactive("D")],
      },
      evidence_id: `sha256:${"e".repeat(64)}`,
    },
  });
  assert.match(marketSymbol.lines.join("\n"), /市场日线方向诊断：一买 2 \/ 二买 1/);
  assert.match(marketSymbol.lines.join("\n"), /市场高级别历史多前缀暖机诊断：非单调/);
  assert.match(marketSymbol.lines.join("\n"), /仅作历史稳定性审计，不参与买入放行/);
  assert.doesNotMatch(marketSymbol.lines.join("\n"), /月线|周线|月\/周\/日/);

  const invalid = Ui.sectorSourceDisclosure({});
  assert.match(invalid.lines.join("\n"), /当前契约要求的板块高级别证据/);
});

test("candidate QMT catalog gate stays visible in account-free review feedback", () => {
  const Ui = loadHumanReviewUi();
  const snapshot = {
    paper_observation_eligible: true,
    paper_observation_reason: null,
  };
  const blocked = Ui.paperPathDecision(snapshot, {
    paper_events: [],
    paper_observation_eligible: false,
    paper_observation_reason: "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY",
  });
  assert.equal(blocked.status, "REVIEW_ONLY");
  assert.match(blocked.headline, /不建立执行计划/);
  assert.match(blocked.reasons[0], /精确 QMT 目录修订尚不可用/);

  const exit = Ui.paperPathDecision(snapshot, {
    paper_events: [],
    paper_observation_eligible: true,
    latest_feedback: null,
  });
  assert.equal(exit.status, "AWAITING_HUMAN_REVIEW");
});

test("candidate origin distinguishes sector triggers from monitor supplements", () => {
  const ui = loadUi();
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: true,
    selection_sources: ["QMT_SECTOR_TRIGGER"],
  }), "板块触发");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: false,
    selection_sources: ["QMT_SECTOR_ELIGIBLE_SCOPE"],
  }), "板块范围");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: false,
    selection_sources: ["ACTIVE_WATCHLIST_MONITOR"],
  }), "自选监控");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: true,
    selection_sources: ["QMT_SECTOR_TRIGGER", "VIRTUAL_HOLDING_MONITOR"],
  }), "板块触发 + 人工关注组监控");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: false,
    selection_sources: ["HOLDING_MONITOR"],
  }), "人工关注组监控");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: false,
    selection_sources: ["DECISION_RULE_RECHECK"],
  }), "规则变更重检");
  assert.deepEqual(
    ui.evidenceGroupsForSignal({
      selection_sources: ["ACTIVE_WATCHLIST_MONITOR"],
      sector_triggered: false,
    }).established,
    ["候选来源：自选监控"],
  );
});

test("disabled formal research is presented as not required rather than unresolved", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[1],
    formal_selection_required: false,
    selection_sources: ["ACTIVE_WATCHLIST_MONITOR"],
  };
  const groups = Ui.evidenceGroupsForSignal(signal);

  assert.ok(groups.established.includes(
    "选股口径：实时技术监听不要求离线正式研究；该项不构成阻断",
  ));
  assert.equal(groups.blocking.some((line) => /正式研究/.test(line)), false);
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

test("dashboard exposes and persists the optional one-minute segment-difference filter", () => {
  assert.match(template, /id="es-segment-count"/);
  assert.match(template, /id="es-precise-count"/);
  assert.match(template, /5分钟是交易级别，决定主信号是否成立/);
  assert.match(template, /1分钟不生成独立交易信号/);
  assert.match(template, /id="es-show-current-segments"[^>]*>查看当前定位</);
  for (const state of ["all", "present", "current", "historical", "absent"]) {
    assert.match(template, new RegExp(`data-segment-state="${state}"`));
  }
  assert.match(template, /不累计历史证据/);
  assert.match(controllerSource, /segmentState:\s*\["present", "current", "historical", "absent"\]\.includes\(saved\.segmentState\)/);
  assert.match(controllerSource, /segmentState:\s*state\.segmentState/);
  assert.match(controllerSource, /Ui\.currentSegmentDifferenceReadyForSignal\(signal\)/);
  assert.match(controllerSource, /unifiedSignals\.filter\(Ui\.isCurrentSelectionSignal\)/);
  assert.match(controllerSource, /\[data-segment-state\]/);
  assert.match(controllerSource, /state\.segmentState = "all"/);
  assert.match(controllerSource, /state\.segmentState = "current"/);
  assert.match(controllerSource, /revealCurrentSegmentsAfterRender/);
  assert.match(controllerSource, /workspace\.scrollIntoView\(\{ behavior: "smooth", block: "start" \}\)/);
  assert.match(controllerSource, /正在查看当前定位/);
  assert.match(controllerSource, /state\.pointType = "all"/);
  assert.match(controllerSource, /data-screening-mode="live"/);
  assert.match(controllerSource, /action\.disabled = count === 0/);
  assert.match(controllerSource, /Ui\.currentPreciseExecutionReadyForSignal\(signal\)/);
  assert.match(controllerSource, /state\.segmentState === "current"/);
  assert.match(controllerSource, /state\.segmentState = "all"/);
});

test("precise execution readiness is stricter than one-minute segment evidence", () => {
  const Ui = loadUi();
  const base = {
    code: "SZ.000001",
    lifecycle_stage: "triggered",
    side: "buy",
    entry_allowed: true,
    exit_allowed: false,
    segment_difference_1m: {
      point_type: "1buy",
      source_frequency: "1m",
      recursive_level: 0,
    },
    entry_execution_boundary: {
      confirmation_bar_closed_at: "2026-08-20T10:01:00+08:00",
      entry_valid_until: "2026-08-20T10:02:00+08:00",
    },
    position_recommendation: {
      side: "buy",
      status: "RECOMMENDED",
      basis: "STRUCTURAL_RISK_MODEL_UPPER_BOUND",
      recommended_ratio: "0.25",
      recommended_percent: "25",
      reason_codes: ["STRUCTURAL_RISK_BUDGET_SIZED"],
    },
    execution_profile: {
      segment_difference_ready: true,
      precise_execution_ready: true,
    },
  };
  const insideBoundary = new Date("2026-08-20T10:01:30+08:00");
  const afterBoundary = new Date("2026-08-20T10:02:01+08:00");

  assert.equal(Ui.segmentDifferenceEvidenceStatusForSignal(base), "present");
  assert.equal(Ui.segmentDifferenceReadyForSignal(base, insideBoundary), true);
  assert.equal(Ui.currentSegmentDifferenceReadyForSignal(base, insideBoundary), true);
  assert.equal(Ui.currentPreciseExecutionReadyForSignal(base, insideBoundary), true);
  assert.equal(Ui.fiveMinuteTradeSignalConfirmedForSignal(base), true);
  assert.equal(
    Ui.segmentDifferenceBoundaryStatusForSignal(base, insideBoundary),
    "current",
  );
  assert.equal(Ui.preciseExecutionReadyForSignal(base, insideBoundary), true);
  assert.equal(
    Ui.segmentDifferenceBoundaryStatusForSignal(base, afterBoundary),
    "expired",
  );
  assert.equal(Ui.preciseExecutionReadyForSignal(base, afterBoundary), false);
  assert.equal(
    Ui.fiveMinuteTradeSignalConfirmedForSignal({
      ...base,
      lifecycle_stage: "invalidated",
    }),
    false,
  );
  assert.deepEqual(
    {
      status: Ui.positionRecommendationForSignal(base, afterBoundary).status,
      ratio: Ui.positionRecommendationForSignal(base, afterBoundary).recommended_ratio,
      reasons: Ui.positionRecommendationForSignal(base, afterBoundary).reason_codes,
    },
    {
      status: "BLOCKED",
      ratio: "0",
      reasons: [
        "STRUCTURAL_RISK_BUDGET_SIZED",
        "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
      ],
    },
  );
  assert.equal(
    Ui.preciseExecutionReadyForSignal({
      ...base,
      entry_allowed: false,
      execution_profile: {
        ...base.execution_profile,
        precise_execution_ready: false,
      },
    }, insideBoundary),
    false,
  );
  assert.equal(
    Ui.preciseExecutionReadyForSignal({
      ...base,
      segment_difference_1m: null,
    }, insideBoundary),
    false,
  );
});

test("persisted current notification boundary expires against the live clock", () => {
  const Ui = loadUi();
  const signal = {
    side: "buy",
    synthetic_notification_projection: true,
    notification_segment_difference_boundary_status: "current",
    notification_segment_difference_evidence_status: "present",
    notification_segment_difference_valid_until: "2026-08-20T10:02:00+08:00",
    segment_difference_1m: {
      point_type: "1buy",
      side: "buy",
      source_frequency: "1m",
      recursive_level: 0,
    },
  };

  assert.equal(
    Ui.segmentDifferenceBoundaryStatusForSignal(
      signal,
      new Date("2026-08-20T10:01:59+08:00"),
    ),
    "current",
  );
  assert.equal(
    Ui.segmentDifferenceBoundaryStatusForSignal(
      signal,
      new Date("2026-08-20T10:02:00+08:00"),
    ),
    "expired",
  );
});

test("stock selection opens with all six point channels visible", () => {
  assert.match(template, /data-selection-scope="sector-trigger"[^>]*aria-pressed="false"/);
  assert.match(template, /data-selection-scope="all-qualified"[^>]*aria-pressed="true"/);
  assert.match(template, /data-point-type="buy"[^>]*aria-pressed="false"/);
  assert.match(template, /data-point-type="sell"[^>]*aria-pressed="false"/);
  assert.match(template, /data-point-type="all"[^>]*aria-pressed="true"/);
  assert.match(controllerSource, /pointType:\s*pointFilters\.includes\(saved\.pointType\)/);
  assert.match(controllerSource, /lifecycle:\s*lifecycleFilters\.includes\(saved\.lifecycle\)/);
  assert.match(
    controllerSource,
    /CANONICAL_SIX_POINT_CHANNELS_V7_5M_TRADE_1M_PRECISION/,
  );
  assert.match(controllerSource, /value\.contract\s*!==\s*VIEW_CONTRACT/);
  assert.match(controllerSource, /localStorage\.removeItem\(STORAGE_KEY\)/);
  assert.match(controllerSource, /saved\.selectionScope\s*===\s*"sector-trigger"[\s\S]*?"all-qualified"/);
  assert.match(template, /class="es-signal-channel-bar"/);
  const quickChannels = template.match(
    /class="es-signal-channel-bar"[\s\S]*?<\/div>\s*<\/div>/,
  );
  assert.ok(quickChannels);
  for (const point of ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"]) {
    assert.match(quickChannels[0], new RegExp(`data-point-type="${point}"`));
  }
  assert.match(humanReviewSource, /alertType:\s*"all"/);
  assert.match(humanReviewSource, /reviewLane:\s*"focus"/);
  assert.match(
    template,
    /id="hr-lane-filter"[\s\S]*value="focus" selected>今日重点/,
  );
  assert.match(
    template,
    /id="hr-alert-filter"[\s\S]*value="all" selected>全部提醒/,
  );
  assert.match(
    template,
    /value="POSSIBLE_5M_TRADE_BUY">5分钟操作确认买点/,
  );
  assert.match(
    humanReviewSource,
    /POSSIBLE_5M_TRADE_SELL:\s*"5分钟操作确认卖点"/,
  );
  assert.match(
    humanReviewSource,
    /HUMAN_TREND_TYPE_CONFIRMATION_INCOMPLETE:\s*"人工尚未确认走势类型"/,
  );
  assert.match(
    humanReviewSource,
    /WARMUP_CONVERGENCE_REQUIRED_FOR_VIRTUAL_ENTRY:\s*"暖机双窗口尚未一致，不能进入新的买入观察"/,
  );
  assert.match(
    humanReviewSource,
    /EXPECTED_REVIEW_LEVEL_30M_OR_5M:\s*"旧档案：该卖点线索需人工确认是30分钟退出还是5分钟短差"/,
  );
});

test("normalizeSnapshot accepts only the new read-only schema", () => {
  const Ui = loadUi();
  const normalized = Ui.normalizeSnapshot(snapshot);

  assert.equal(normalized.schema, "chanlun-trading-screening");
  assert.equal(normalized.signals.length, 2);
  assert.equal(normalized.point_distribution.all_signals.total, 2);
  assert.equal(normalized.point_distribution.candidate.counts_by_point_type["1buy"], 1);
  assert.equal(
    normalized.point_distribution.operational_confirmed.counts_by_point_type["2buy"],
    1,
  );
  assert.equal(normalized.point_distribution.executable.total, 1);
  assert.equal(normalized.point_distribution.audit_locked.total, 0);
  assert.equal(
    Ui.pointDistributionCountText(normalized.point_distribution.operational_confirmed),
    "一买 0 · 一卖 0 · 二买 1 · 二卖 0 · 三买 0 · 三卖 0",
  );
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, schema: "chanlun-early-screening" }),
    /snapshot_schema_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, no_order_execution: false }),
    /snapshot_boundary_invalid/,
  );
});

test("normalizeSnapshot expands the compact shared signal catalog", () => {
  const Ui = loadUi();
  const fields = [
    "execution_profile",
    "higher_timeframe_risk",
    "position_recommendation",
    "sector",
    "context_30m",
    "context_d",
    "decision_reasons",
    "warmup",
  ];
  const source = snapshot.signals[0];
  const compactSignal = { ...source };
  const values = {};
  fields.forEach((field) => {
    values[field] = [source[field] === undefined ? null : source[field]];
    delete compactSignal[field];
  });
  compactSignal.signal_catalog_refs = fields.map(() => 0);
  const wireSnapshot = {
    ...snapshot,
    signals: [compactSignal],
    signal_transport: "signal-catalog-v1",
    signal_catalog: {
      schema: "chanlun-early-signals-signal-catalog-v1",
      fields,
      values,
    },
  };

  const normalized = Ui.normalizeSnapshot(wireSnapshot);

  assert.equal(normalized.signals.length, 1);
  assert.deepEqual(normalized.signals[0].higher_timeframe_risk, source.higher_timeframe_risk);
  assert.deepEqual(normalized.signals[0].sector, source.sector);
  assert.equal(normalized.signal_catalog, undefined);
  assert.equal(normalized.signal_transport, undefined);
  assert.equal(normalized.signals[0].signal_catalog_refs, undefined);
  assert.throws(
    () => Ui.normalizeSnapshot({
      ...wireSnapshot,
      signals: [{ ...compactSignal, signal_catalog_refs: [999, ...fields.slice(1).map(() => 0)] }],
    }),
    /snapshot_signal_catalog_invalid/,
  );
});

function sectorSameBaseCoverage({ converged = false, fullCount = 240 } = {}) {
  const reason = converged
    ? "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
    : "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT";
  return {
    contract_id: "chanlun-qmt-sector-same-5m-source-coverage",
    observed_at: "2026-07-20T15:00:00+08:00",
    calendar_first_session: "2023-05-04",
    first_visible_bar_at: "2025-04-29T11:05:00+08:00",
    last_visible_bar_at: "2026-07-20T15:00:00+08:00",
    first_completed_session: "2025-04-30",
    last_completed_session: "2026-07-20",
    visible_five_minute_bar_count: fullCount * 48,
    completed_daily_bar_count: fullCount,
    required_daily_bar_count: 480,
    remaining_daily_bar_count: Math.max(0, 480 - fullCount),
    missing_leading_calendar_session_count: converged ? 0 : 480,
    history_requirement_met: fullCount >= 480,
    warmup_converged: converged,
    warmup_reason_code: reason,
    boundary_status: converged
      ? "REQUIRED_HISTORY_CONVERGED"
      : "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP",
    base_frequency: "5m",
    prefix_only: true,
    data_grade: "RESEARCH_ONLY",
    live_status: "LIVE_DISABLED",
  };
}

function sellOnlyRisk({ legacy = false } = {}) {
  const currentReason = "HIGHER_TIMEFRAME_ENTRY_GATE_NOT_APPLICABLE_TO_SELL_ONLY";
  const genericLegacyReason = "HIGHER_TIMEFRAME_GATE_NOT_ATTACHED";
  const sectorLegacyReason = "HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED";
  return {
    market_gate: "UNRESOLVED",
    sector_gate: "UNRESOLVED",
    symbol_gate: "UNRESOLVED",
    new_entry_requires_all_green: false,
    market_reason_codes: [legacy ? genericLegacyReason : currentReason],
    sector_reason_codes: [legacy ? sectorLegacyReason : currentReason],
    symbol_reason_codes: [legacy ? genericLegacyReason : currentReason],
    reason_codes: legacy
      ? [genericLegacyReason, sectorLegacyReason]
      : [currentReason],
  };
}

function sellOnlySignal({ legacy = false } = {}) {
  return {
    ...snapshot.signals[0],
    signal_id: legacy ? "legacy-sell-only" : "current-sell-only",
    point_type: "3sell",
    side: "sell",
    selection_path: "INDIVIDUAL_THREE_PROGRAM",
    entry_allowed: false,
    technical_entry_allowed: false,
    higher_timeframe_risk: sellOnlyRisk({ legacy }),
  };
}

function withSellOnlyPolicy(signal) {
  return {
    ...snapshot,
    screening_policy: {
      sell_only_higher_timeframe_evidence_policy:
        "SCHEMA_COMPLETE_UNRESOLVED_WITHOUT_PROVIDER_CALL",
    },
    signals: [signal, snapshot.signals[1]],
  };
}

test("normalizeSnapshot fails closed on contradictory sector source contracts", () => {
  const Ui = loadUi();
  const bridgeRisk = {
    ...(snapshot.signals[0].higher_timeframe_risk || {}),
    sector_gate: "AMBER",
    sector_higher_timeframe_source_mode:
      "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH",
    sector_strict_same_5m_warmup_evidence: {
      required_daily_bar_count: 480,
      full_daily_bar_count: 240,
      suffix_daily_bar_count: 160,
      converged: false,
      reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
    },
    sector_strict_same_5m_source_coverage_evidence: sectorSameBaseCoverage(),
    sector_research_bridge_parameter_set_id: `sha256:${"7".repeat(64)}`,
  };
  const withRisk = (risk) => ({
    ...snapshot,
    signals: [
      { ...snapshot.signals[0], higher_timeframe_risk: risk },
      snapshot.signals[1],
    ],
  });

  assert.doesNotThrow(() => Ui.normalizeSnapshot(withRisk(bridgeRisk)));
  assert.throws(
    () => Ui.normalizeSnapshot(withRisk({ ...bridgeRisk, sector_gate: "GREEN" })),
    /snapshot_sector_source_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot(withRisk({
      ...bridgeRisk,
      sector_higher_timeframe_source_mode: "UNKNOWN_SOURCE_MODE",
    })),
    /snapshot_sector_source_invalid/,
  );
  const partial = { ...bridgeRisk };
  delete partial.sector_higher_timeframe_source_mode;
  assert.throws(
    () => Ui.normalizeSnapshot(withRisk(partial)),
    /snapshot_sector_source_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot(withRisk({
      market_gate: "GREEN",
      sector_gate: "UNRESOLVED",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: [],
      symbol_reason_codes: [],
      reason_codes: [],
    })),
    /snapshot_sector_source_invalid/,
  );
});

test("normalizeSnapshot displays explicitly unavailable sector sources as fail-closed evidence", () => {
  const Ui = loadUi();
  for (const reasonCode of [
    "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
    "QMT_SECTOR_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
  ]) {
    const unavailableRisk = {
      market_gate: "GREEN",
      sector_gate: "UNRESOLVED",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: [reasonCode],
      symbol_reason_codes: [],
      reason_codes: [reasonCode],
    };
    const normalized = Ui.normalizeSnapshot({
      ...snapshot,
      signals: [
        {
          ...snapshot.signals[0],
          entry_allowed: false,
          higher_timeframe_risk: unavailableRisk,
        },
        snapshot.signals[1],
      ],
    });
    const groups = Ui.evidenceGroupsForSignal(normalized.signals[0]);

    assert.match(groups.blocking.join(" "), /板块高级别来源尚未取得/);
    assert.match(groups.risk.join(" "), /板块高级别提供器当前不可用/);
    assert.ok(groups.raw.includes(reasonCode));
  }
});

test("normalizeSnapshot accepts the explicit ETF proxy sector exemption only", () => {
  const Ui = loadUi();
  const etfSignal = {
    ...snapshot.signals[0],
    code: "SH.513100",
    name: "纳指ETF国泰",
    selection_path: "ETF_PROXY",
    sector: {
      sector_id: "etf-proxy:SH.513100",
      sector_name: "ETF代理",
      eligible: true,
      hard_block: false,
      reason_codes: ["ETF_PROXY_SECTOR_NOT_REQUIRED"],
    },
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "UNRESOLVED",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: ["QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE"],
      symbol_reason_codes: [],
      reason_codes: ["QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE"],
    },
  };
  const withSignal = (signal) => ({
    ...snapshot,
    signals: [signal, snapshot.signals[1]],
  });

  const normalized = Ui.normalizeSnapshot(withSignal(etfSignal));
  const groups = Ui.evidenceGroupsForSignal(normalized.signals[0]);
  assert.match(groups.risk.join(" "), /ETF代理路径不要求行业板块高级别来源/);
  assert.doesNotMatch(groups.blocking.join(" "), /板块高级别来源字段不完整/);

  assert.throws(
    () => Ui.normalizeSnapshot(withSignal({ ...etfSignal, selection_path: "" })),
    /snapshot_sector_source_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot(withSignal({
      ...etfSignal,
      sector: { ...etfSignal.sector, hard_block: true },
    })),
    /snapshot_sector_source_invalid/,
  );
});

test("normalizeSnapshot accepts exact current and migrated sell-only entry-gate declarations", () => {
  const Ui = loadUi();

  for (const legacy of [false, true]) {
    const normalized = Ui.normalizeSnapshot(withSellOnlyPolicy(sellOnlySignal({ legacy })));
    const signal = normalized.signals[0];
    const groups = Ui.evidenceGroupsForSignal(signal);

    assert.match(groups.risk.join(" "), /纯卖出结构/);
    assert.match(groups.risk.join(" "), /只用于新买入核验/);
    assert.doesNotMatch(groups.blocking.join(" "), /来源字段不完整/);
    assert.equal(
      Ui.reasonLabel("HIGHER_TIMEFRAME_ENTRY_GATE_NOT_APPLICABLE_TO_SELL_ONLY"),
      "纯卖出结构不适用买入专用高级别风险门",
    );
  }
});

test("normalizeSnapshot rejects forged or contradictory sell-only exemptions", () => {
  const Ui = loadUi();
  const current = sellOnlySignal();
  const assertSourceRejected = (signal, includePolicy = true) => {
    const candidate = includePolicy
      ? withSellOnlyPolicy(signal)
      : { ...snapshot, signals: [signal, snapshot.signals[1]] };
    assert.throws(
      () => Ui.normalizeSnapshot(candidate),
      /snapshot_sector_source_invalid/,
    );
  };

  assertSourceRejected(current, false);
  assertSourceRejected({ ...current, side: "buy" });
  assertSourceRejected({ ...current, point_type: "3buy" });
  assertSourceRejected({ ...current, technical_entry_allowed: true });
  assertSourceRejected({
    ...current,
    higher_timeframe_risk: {
      ...current.higher_timeframe_risk,
      sector_gate: "GREEN",
    },
  });
  assertSourceRejected({
    ...current,
    presentation_sell_only_higher_timeframe_entry_gate: "CURRENT_EXPLICIT_REASON",
    higher_timeframe_risk: {
      ...current.higher_timeframe_risk,
      sector_reason_codes: ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"],
    },
  });
});

test("normalizeUsMonitor isolates auxiliary contract failures and recomputes US counts", () => {
  const Ui = loadUi();
  assert.equal(Ui.normalizeSnapshot(snapshot).us_monitor.available, false);

  const monitor = Ui.normalizeUsMonitor({
    schema: "chanlun-us-realtime-monitor",
    source_schema: "chanlun-attention-group-monitor",
    market: "us",
    market_scope: "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS",
    decision_mode: "STRICT_STRUCTURE_OBSERVATION_ONLY",
    auxiliary_only: true,
    full_market_screening: false,
    selection_candidates: false,
    available: true,
    ready: false,
    status: "warming_up",
    reason_code: "MULTI_TIMEFRAME_WARMUP_INCOMPLETE",
    symbols: [
      {
        market: "us", code: "QCOM.US", name: "高通", groups: ["我的关注"],
        monitoring_scope: "WATCHLIST", status: "monitoring",
      },
      {
        market: "us", code: "QQQ.US", name: "纳指100ETF", groups: ["ETF"],
        monitoring_scope: "WATCHLIST", status: "market_closed",
      },
    ],
    research_only: true,
    no_order_execution: true,
    manual_review_required: true,
  });
  assert.equal(monitor.declared_count, 2);
  assert.equal(monitor.active_count, 1);
  assert.equal(monitor.closed_count, 1);
  assert.equal(monitor.failed_count, 0);

  const malformed = Ui.normalizeUsMonitor({ ...monitor, selection_candidates: true });
  assert.equal(malformed.available, false);
  assert.equal(malformed.reason_code, "US_MONITOR_CONTRACT_INVALID");
});

test("US monitor symbols share the signal queue and bypass only A-share sector filters", () => {
  const Ui = loadUi();
  const usMonitor = {
    schema: "chanlun-us-realtime-monitor",
    source_schema: "chanlun-attention-group-monitor",
    market: "us",
    market_scope: "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS",
    decision_mode: "STRICT_STRUCTURE_OBSERVATION_ONLY",
    auxiliary_only: true,
    full_market_screening: false,
    selection_candidates: false,
    available: true,
    ready: true,
    status: "ready",
    reason_code: "READY",
    last_completed_at: "2026-08-15T10:30:00+08:00",
    symbols: [
      {
        market: "us", code: "QCOM.US", name: "高通", groups: ["我的关注"],
        monitoring_scope: "WATCHLIST", status: "monitoring",
      },
      {
        market: "us", code: "QQQ.US", name: "纳指100ETF", groups: ["人工关注组"],
        monitoring_scope: "MANUAL_ATTENTION", status: "market_closed",
      },
    ],
    research_only: true,
    no_order_execution: true,
    manual_review_required: true,
  };
  const normalized = Ui.normalizeSnapshot({ ...snapshot, us_monitor: usMonitor });
  const usRows = normalized.unified_signals.filter(
    (row) => Ui.inferSignalMarket(row) === "us",
  );

  assert.deepEqual(usRows.map((row) => row.code), ["QCOM.US", "QQQ.US"]);
  assert.ok(usRows.every((row) => row.lifecycle_stage === "monitoring"));
  assert.ok(usRows.every((row) => row.sector.sector_id === "market:us"));
  assert.match(usRows[0].chart_urls["1m"], /market=us.*code=QCOM\.US.*intervals=1/);
  assert.deepEqual(
    Ui.filterSignals(normalized.unified_signals, {
      sectorId: "qmt-gics3:bank",
    }).map((row) => row.code),
    ["SZ.000001", "QCOM.US", "QQQ.US"],
  );
  assert.deepEqual(
    Ui.filterSignals(normalized.unified_signals, { market: "us" })
      .map((row) => row.code),
    ["QCOM.US", "QQQ.US"],
  );
  assert.deepEqual(
    Ui.filterSignals(normalized.unified_signals, { source: "attention" })
      .map((row) => row.code),
    ["QQQ.US"],
  );
  assert.deepEqual(
    Ui.filterSignals(normalized.unified_signals, { reviewStage: "tracking" })
      .map((row) => row.code),
    ["QCOM.US", "QQQ.US"],
  );
});

test("pure monitor positions remain browseable without inflating 5m clue counts", () => {
  const Ui = loadUi();
  const monitorPosition = Ui.usMonitorSignal(
    {
      code: "QQQ.US",
      name: "纳指100ETF",
      monitoring_scope: "MANUAL_ATTENTION",
      status: "market_closed",
    },
    { last_completed_at: "2026-08-26T14:22:21+08:00" },
  );
  const rows = [snapshot.signals[0], monitorPosition];

  assert.deepEqual(Ui.signalQueueFacts(rows), {
    total_count: 2,
    structure_clue_count: 1,
    monitor_position_count: 1,
  });
  assert.equal(
    Ui.signalQueueCountText(rows),
    "1 条5m结构线索 · 1 个独立监听",
  );
  assert.equal(
    Ui.signalQueueCountText([monitorPosition], rows),
    "0 条5m结构线索 · 1 个独立监听 / 全部 1 条5m结构线索 · 1 个独立监听",
  );
  assert.match(controllerSource, /queueFacts\.structure_clue_count/);
  assert.match(controllerSource, /Ui\.signalQueueCountText\(filtered, selectionScopedSignals\(\)\)/);
  assert.match(template, /id="es-market-data-label"/);
  assert.match(template, /id="es-sector-scope"/);
  assert.match(template, /买卖点与监听队列/);
});

test("unmatched notification history stays in human review and cannot create a current selection", () => {
  const Ui = loadUi();
  const HumanUi = loadHumanReviewUi();
  const event = {
    schema: "chanlun-realtime-review-notification",
    notification_id: `sha256:${"a".repeat(64)}`,
    source: "CROSS_MARKET_ATTENTION_MONITOR",
    market: "us",
    code: "QCOM.US",
    name: "高通",
    side: "sell",
    point_type: "1sell",
    source_frequency: "5m",
    trade_frequency: "5m",
    segment_difference_frequency: "1m",
    segment_difference_present: false,
    signal_time: "2026-08-15T10:25:00-04:00",
    observed_at: "2026-08-15T10:25:00-04:00",
    recorded_at: "2026-08-15T22:26:00+08:00",
    structure_anchor_time: "2026-08-15T10:24:00-04:00",
    structure_confirmed_at: "2026-08-15T10:22:00-04:00",
    signal_available_at: "2026-08-15T10:25:00-04:00",
    detected_at: "2026-08-15T10:25:35-04:00",
    delivery_updated_at: "2026-08-15T22:26:00+08:00",
    delivered_at: null,
    new_stage: "triggered",
    delivery_status: "failed",
    delivery_reason: "network",
    evidence_id: "point:qcom:1sell",
    recursive_level: 0,
    anchor_time: "2026-08-15T10:24:00-04:00",
    current_price: 145.26,
    current_price_source: "realtime_tick",
    current_price_at: "2026-08-15T10:25:35-04:00",
    reference_price: 145.2,
    invalidation_price: null,
    big_direction: "down",
    mid_direction: "down",
    is_manual_attention: true,
    selection_sources: ["MANUAL_ATTENTION_MONITOR"],
    chart_urls: {
      d: "/?market=us&code=QCOM.US&layout=single&intervals=D",
      "30m": "/?market=us&code=QCOM.US&layout=single&intervals=30",
      "5m": "/?market=us&code=QCOM.US&layout=single&intervals=5",
      "1m": "/?market=us&code=QCOM.US&layout=single&intervals=1",
    },
    review_required: true,
    automated_action_authorized: false,
    real_order_transport_enabled: false,
    live_status: "LIVE_DISABLED",
  };
  const inbox = {
    schema: "chanlun-realtime-review-inbox",
    events: [event],
    event_count: 1,
    pending_review_count: 1,
    delivery_counts: { failed: 1 },
    credentials_exposed: false,
    real_account_accessed: false,
    real_order_transport_enabled: false,
    automated_order_authorized: false,
    live_status: "LIVE_DISABLED",
  };
  const notificationObservedAt = new Date("2026-08-15T10:30:00-04:00");
  const normalized = Ui.normalizeSnapshot({
    ...snapshot,
    realtime_notifications: inbox,
  });
  const clue = normalized.unified_signals.find((row) => row.code === "QCOM.US");

  assert.equal(clue, undefined);
  assert.equal(normalized.realtime_notifications.events.length, 1);
  assert.deepEqual(
    Ui.filterSignals(normalized.unified_signals, {
      market: "us", source: "notification", reviewStage: "notified",
    }),
    [],
  );
  assert.equal(
    Ui.fullDateTimeText(Ui.realtimeNotificationDisplayTime(event)),
    "2026-08-15 22:26:00",
  );

  const review = HumanUi.mergeRealtimeNotificationQueue({
    review_queue: [{ candidate_id: "formal:1", symbol: "SZ.000001" }],
  }, notificationObservedAt);
  assert.equal(review.review_queue_count, 1);
  const reviewWithInbox = HumanUi.mergeRealtimeNotificationQueue({
    review_queue: [{ candidate_id: "formal:1", symbol: "SZ.000001" }],
    realtime_notifications: inbox,
  }, notificationObservedAt);
  assert.equal(reviewWithInbox.review_queue_count, 2);
  assert.equal(reviewWithInbox.current_realtime_notification_count, 1);
  assert.equal(reviewWithInbox.focus_review_queue_count, 1);
  assert.equal(reviewWithInbox.review_queue[0].candidate_kind, "realtime_notification");
  assert.equal(reviewWithInbox.review_queue[0].market, "us");
  assert.equal(reviewWithInbox.review_queue[0].current_price, 145.26);
  assert.equal(reviewWithInbox.review_queue[0].current_price_source, "realtime_tick");
  assert.equal(
    reviewWithInbox.review_queue[0].current_price_at,
    event.current_price_at,
  );
  assert.match(
    HumanUi.realtimeNotificationPriceText(reviewWithInbox.review_queue[0]),
    /2026-08-15 22:25:35/,
  );
  assert.equal(
    reviewWithInbox.review_queue[0].entry_confirmation_bar_closed_at,
    event.structure_confirmed_at,
  );
  assert.equal(
    reviewWithInbox.review_queue[0].review_available_at,
    event.delivery_updated_at,
  );
  assert.equal(
    reviewWithInbox.review_queue[0].realtime_notification_detected_at,
    event.detected_at,
  );
  assert.equal(
    reviewWithInbox.review_queue[0].realtime_notification_setup_lock_state,
    "unknown",
  );
  assert.match(
    HumanUi.realtimeNotificationSetupLockLabel(reviewWithInbox.review_queue[0]),
    /末端结构封存状态未保存/,
  );
  assert.equal(
    HumanUi.realtimeNotificationTimeLabel(reviewWithInbox.review_queue[0]),
    "投递更新时间",
  );
  assert.equal(reviewWithInbox.review_queue[0].paper_observation_eligible, false);
  assert.equal(reviewWithInbox.review_queue[0].review_priority, 110);
  assert.equal(reviewWithInbox.review_queue[0].review_lane, "ACTIONABLE_REVIEW");

  const laterReview = HumanUi.realtimeNotificationCandidate(
    event,
    new Date("2026-08-15T10:36:00-04:00"),
  );
  assert.equal(laterReview.review_priority, 110);
  assert.equal(laterReview.review_lane, "ACTIONABLE_REVIEW");
  assert.equal(laterReview.confidence, "MEDIUM");
  assert.equal("realtime_notification_is_historical" in laterReview, false);
  const laterQueue = HumanUi.mergeRealtimeNotificationQueue(
    { review_queue: [], realtime_notifications: inbox },
    new Date("2026-08-15T10:36:00-04:00"),
  );
  assert.equal(laterQueue.current_realtime_notification_count, 1);
  assert.equal(laterQueue.focus_review_queue_count, 1);

  const degradedReview = HumanUi.mergeRealtimeNotificationQueue({
    formal_review_available: false,
    formal_review_unavailable_reason: "human_review_web_bundle_invalid",
    review_queue: [],
    realtime_notifications: inbox,
  }, notificationObservedAt);
  assert.equal(degradedReview.review_queue_count, 1);
  assert.equal(degradedReview.formal_review_queue_count, 0);
  assert.match(
    HumanUi.formalReviewUnavailableLabel(
      degradedReview.formal_review_unavailable_reason,
    ),
    /旧版程序候选归档.*等待新快照发布/,
  );

  const unsafe = HumanUi.realtimeNotificationCandidate({
    ...event,
    automated_action_authorized: true,
  }, notificationObservedAt);
  assert.equal(unsafe, null);

  const legacyWithoutCurrentPrice = HumanUi.realtimeNotificationCandidate({
    ...event,
    current_price: undefined,
    reference_price: 999,
  }, notificationObservedAt);
  assert.equal(legacyWithoutCurrentPrice.current_price, undefined);
});

test("notification history may annotate only the same still-current structure", () => {
  const Ui = loadUi();
  const current = snapshot.signals[1];
  const baseEvent = {
    schema: "chanlun-realtime-review-notification",
    notification_id: `sha256:${"b".repeat(64)}`,
    source: "A_SHARE_STRICT_DECISION_CORE",
    market: "a",
    code: current.code,
    name: current.name,
    side: current.side,
    point_type: current.point_type,
    source_frequency: "5m",
    signal_time: current.observed_at,
    signal_available_at: current.observed_at,
    observed_at: current.observed_at,
    recorded_at: current.observed_at,
    detected_at: current.observed_at,
    delivery_updated_at: current.observed_at,
    new_stage: "triggered",
    delivery_status: "delivered",
    evidence_id: "",
    recursive_level: 0,
    current_price: 8.12,
    current_price_source: "realtime_tick",
    current_price_at: current.observed_at,
    chart_urls: { ...current.chart_urls },
    review_required: true,
    automated_action_authorized: false,
    real_order_transport_enabled: false,
    live_status: "LIVE_DISABLED",
  };
  const inbox = {
    schema: "chanlun-realtime-review-inbox",
    events: [baseEvent],
    credentials_exposed: false,
    real_account_accessed: false,
    real_order_transport_enabled: false,
    automated_order_authorized: false,
    live_status: "LIVE_DISABLED",
  };

  const annotated = Ui.normalizeSnapshot({
    ...snapshot,
    realtime_notifications: inbox,
  });
  const currentRow = annotated.unified_signals.find(
    (row) => row.signal_id === current.signal_id,
  );

  assert.equal(annotated.unified_signals.length, snapshot.signals.length);
  assert.equal(currentRow.realtime_notification, true);
  assert.equal(currentRow.synthetic_notification_projection, undefined);
  assert.equal(currentRow.notification_current_price, 8.12);
  assert.equal(currentRow.notification_current_price_source, "realtime_tick");
  assert.equal(currentRow.notification_current_price_at, current.observed_at);
  assert.match(Ui.realtimeNotificationPriceText(currentRow), /2026-/);

  const invalidated = Ui.normalizeSnapshot({
    ...snapshot,
    realtime_notifications: {
      ...inbox,
      events: [{ ...baseEvent, new_stage: "invalidated" }],
    },
  });
  assert.equal(
    invalidated.unified_signals.some((row) => row.signal_id === current.signal_id),
    false,
  );
  assert.equal(invalidated.realtime_notifications.events.length, 1);
});

test("terminal lifecycle rows are excluded from the normalized current shortlist", () => {
  const Ui = loadUi();
  const normalized = Ui.normalizeSnapshot({
    ...snapshot,
    signals: [
      { ...snapshot.signals[0], lifecycle_stage: "invalidated" },
      snapshot.signals[1],
    ],
  });

  assert.deepEqual(normalized.signals.map((row) => row.signal_id), ["signal-2"]);
  assert.equal(normalized.presentation_signal_count, 1);
  assert.equal(normalized.total_qualified_signal_count, 1);
  assert.equal(normalized.counts_by_point_type["1buy"], 0);
  assert.equal(normalized.counts_by_point_type["2buy"], 1);
  assert.deepEqual(
    Ui.filterSignals([
      { ...snapshot.signals[0], lifecycle_stage: "closed" },
      snapshot.signals[1],
    ]).map((row) => row.signal_id),
    ["signal-2"],
  );
});

test("expired one-minute segment stays visible only as historical audit evidence", () => {
  const Ui = loadUi();
  const HumanUi = loadHumanReviewUi();
  const event = {
    schema: "chanlun-realtime-review-notification",
    notification_id: `sha256:${"e".repeat(64)}`,
    source: "A_SHARE_STRICT_DECISION_CORE",
    market: "a",
    code: "SH.601231",
    name: "环旭电子",
    side: "buy",
    point_type: "1buy",
    source_frequency: "5m",
    trade_frequency: "5m",
    segment_difference_frequency: "1m",
    segment_difference_present: true,
    segment_difference_status: "expired",
    segment_difference_current: false,
    segment_difference_evidence_status: "present",
    segment_difference_boundary_status: "expired",
    segment_difference_point_type: "2buy",
    segment_difference_divergence_kind: "consolidation",
    segment_difference_valid_until: "2026-08-03T11:13:00+08:00",
    setup_lock_state: "pending",
    signal_time: "2026-08-17T13:55:00+08:00",
    signal_available_at: "2026-08-17T13:55:00+08:00",
    structure_confirmed_at: "2026-08-17T13:55:00+08:00",
    detected_at: "2026-08-17T13:57:22+08:00",
    recorded_at: "2026-08-17T13:57:34+08:00",
    delivery_updated_at: "2026-08-17T13:57:34+08:00",
    delivered_at: "2026-08-17T13:57:34+08:00",
    delivery_status: "delivered",
    new_stage: "triggered",
    evidence_id: "point:601231:5m:1buy",
    recursive_level: 0,
    current_price: 29.06,
    chart_urls: { d: "/d", "30m": "/30", "5m": "/5", "1m": "/1" },
    review_required: true,
    automated_action_authorized: false,
    real_order_transport_enabled: false,
    live_status: "LIVE_DISABLED",
  };

  const clue = Ui.realtimeNotificationSignal(event);
  assert.equal(clue.setup_5m.lock_state, "pending");
  assert.equal(Ui.setupLockStateForSignal(clue), "pending");
  assert.equal(clue.segment_difference_1m.point_type, "2buy");
  assert.equal(clue.segment_difference_1m.divergence_kind, "consolidation");
  assert.equal(clue.notification_segment_difference_present, true);
  assert.equal(clue.notification_segment_difference_current, false);
  assert.equal(clue.notification_segment_difference_status, "expired");
  const oneMinute = Ui.periodPathForSignal(clue).find(
    (period) => period.frequency === "1m",
  );
  assert.equal(oneMinute.state, "历史区间套定位已过");
  assert.equal(
    oneMinute.summary,
    "二买（盘整背驰） · 1分钟历史区间套证据保留（不计入当前定位）",
  );
  assert.match(oneMinute.boundary, /现已过期；仅保留历史定位证据/);

  const candidate = HumanUi.realtimeNotificationCandidate(event);
  assert.equal(candidate.realtime_notification_segment_difference_present, true);
  assert.equal(candidate.realtime_notification_segment_difference_current, false);
  assert.equal(candidate.realtime_notification_segment_difference_status, "expired");
  assert.equal(
    candidate.realtime_notification_segment_difference_divergence_kind,
    "consolidation",
  );
  assert.equal(candidate.realtime_notification_setup_lock_state, "pending");
  assert.equal(
    HumanUi.realtimeNotificationSetupLockLabel(candidate),
    "5分钟操作确认已完成；末端结构仍会随新K更新，不影响当前复核",
  );
  assert.equal(
    HumanUi.realtimeNotificationSetupLockLabel({
      ...candidate,
      realtime_notification_setup_lock_state: "locked",
    }),
    "5分钟操作确认已完成；末端结构已封存",
  );
  assert.ok(candidate.warning_codes.includes("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"));
  assert.deepEqual(HumanUi.realtimeNotificationSegmentPeriod(candidate), [
    "历史区间套定位已过",
    "2buy（盘整背驰）区间套证据仍保留；买入精确定位窗口已过期",
    "定位窗口有效至 2026-08-03 11:13:00；5分钟信号保留，精确执行已关闭",
  ]);

  const currentEvent = {
    ...event,
    segment_difference_status: "current",
    segment_difference_current: true,
    segment_difference_boundary_status: "current",
    segment_difference_valid_until: "2026-08-17T13:58:00+08:00",
    position_recommendation: {
      side: "buy",
      status: "RECOMMENDED",
      recommended_ratio: "0.25",
      recommended_percent: "25",
      reason_codes: ["STRUCTURAL_RISK_BUDGET_SIZED"],
    },
  };
  const beforeExpiry = HumanUi.realtimeNotificationCandidate(
    currentEvent,
    new Date("2026-08-17T13:57:59+08:00"),
  );
  const afterExpiry = HumanUi.realtimeNotificationCandidate(
    currentEvent,
    new Date("2026-08-17T13:58:00+08:00"),
  );
  assert.equal(beforeExpiry.realtime_notification_segment_difference_status, "current");
  assert.equal(afterExpiry.realtime_notification_segment_difference_status, "expired");
  assert.equal(afterExpiry.position_recommendation.status, "BLOCKED");
  assert.equal(afterExpiry.position_recommendation.recommended_ratio, "0");
  assert.ok(afterExpiry.warning_codes.includes("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"));
  assert.match(
    HumanUi.positionRecommendationLabel(
      beforeExpiry,
      new Date("2026-08-17T13:58:00+08:00"),
    ),
    /定位窗口已过/,
  );
});

test("segment enrichment projection preserves the later confluence time", () => {
  const Ui = loadUi();
  const clue = Ui.realtimeNotificationSignal({
    notification_id: `sha256:${"f".repeat(64)}`,
    market: "a",
    code: "SZ.000001",
    name: "平安银行",
    side: "buy",
    point_type: "3buy",
    source_frequency: "5m",
    new_stage: "segment_enriched",
    signal_time: "2026-08-20T10:00:00+08:00",
    signal_available_at: "2026-08-20T10:00:00+08:00",
    segment_difference_frequency: "1m",
    segment_difference_present: true,
    segment_difference_status: "current",
    segment_difference_current: true,
    segment_difference_evidence_status: "present",
    segment_difference_boundary_status: "current",
    segment_difference_point_type: "1buy",
    segment_difference_divergence_kind: "trend",
    segment_difference_recursive_level: 0,
    segment_difference_available_at: "2026-08-20T09:40:00+08:00",
    delivery_status: "delivered",
    chart_urls: { d: "/d", "30m": "/30", "5m": "/5", "1m": "/1" },
  });

  assert.equal(clue.notification_signal_available_at, "2026-08-20T10:00:00+08:00");
  assert.equal(clue.segment_difference_1m.divergence_kind, "trend");
});

test("filters preserve independent point lifecycle sector and query choices", () => {
  const Ui = loadUi();
  const signals = Ui.normalizeSnapshot(snapshot).signals;
  const mixedSignals = [
    ...signals,
    { ...signals[0], signal_id: "signal-sell", point_type: "3sell", side: "sell" },
  ];

  assert.deepEqual(
    Ui.filterSignals(mixedSignals, { pointType: "buy" }).map((row) => row.signal_id),
    ["signal-1", "signal-2"],
  );
  assert.deepEqual(
    Ui.filterSignals(mixedSignals, { pointType: "sell" }).map((row) => row.signal_id),
    ["signal-sell"],
  );

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
    Ui.filterSignals(signals, { sectorId: "qmt-gics3:bank" })
      .map((row) => row.signal_id),
    ["signal-1"],
  );

  const currentSegment = signals[1];
  const expiredSegment = {
    ...currentSegment,
    signal_id: "signal-segment-expired",
    decision_reasons: ["ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"],
  };
  const unavailableSegment = {
    ...currentSegment,
    signal_id: "signal-segment-unavailable",
    decision_reasons: ["ONE_MINUTE_SEGMENT_BOUNDARY_MISSING"],
  };
  const currentSellWitness = {
    ...currentSegment,
    signal_id: "signal-sell-witness",
    point_type: "2sell",
    side: "sell",
    segment_difference_1m: {
      ...currentSegment.segment_difference_1m,
      point_type: "2sell",
    },
    entry_allowed: false,
    exit_allowed: true,
  };
  const historicalSellWitness = {
    ...currentSellWitness,
    signal_id: "signal-sell-witness-historical",
    segment_difference_1m: {
      ...currentSellWitness.segment_difference_1m,
      available_at: "2026-07-20T14:40:00+08:00",
    },
  };
  const segmentSignals = [
    signals[0],
    currentSegment,
    currentSellWitness,
    historicalSellWitness,
    expiredSegment,
    unavailableSegment,
  ];
  assert.equal(Ui.segmentDifferenceStatusForSignal(signals[0]), "absent");
  assert.equal(Ui.segmentDifferenceStatusForSignal(currentSegment), "current");
  assert.equal(Ui.segmentDifferenceStatusForSignal(expiredSegment), "expired");
  assert.equal(Ui.segmentDifferenceStatusForSignal(unavailableSegment), "unavailable");
  assert.deepEqual(
    Ui.filterSignals(segmentSignals, { segmentState: "present" })
      .map((row) => row.signal_id),
    [
      "signal-2",
      "signal-sell-witness",
      "signal-sell-witness-historical",
      "signal-segment-expired",
      "signal-segment-unavailable",
    ],
  );
  assert.deepEqual(
    Ui.filterSignals(segmentSignals, { segmentState: "current" })
      .map((row) => row.signal_id),
    ["signal-2", "signal-sell-witness", "signal-sell-witness-historical"],
  );
  assert.equal(
    Ui.segmentDifferenceReadyForSignal(
      historicalSellWitness,
      new Date("2026-07-20T14:58:30+08:00"),
    ),
    true,
  );
  assert.equal(
    Ui.currentSegmentDifferenceReadyForSignal(
      historicalSellWitness,
      new Date("2026-07-20T14:58:30+08:00"),
    ),
    true,
  );
  assert.deepEqual(
    (() => {
      const period = Ui.periodPathForSignal(historicalSellWitness).find(
        (row) => row.frequency === "1m",
      );
      return [period.state, period.summary, period.boundary];
    })(),
    [
      "卖出区间套精确定位有效",
      "二卖 · 1分钟区间套精确定位已确认",
      "卖出区间套精确位置有效；核对持有结构级别后人工复核",
    ],
  );
  assert.deepEqual(
    Ui.filterSignals(segmentSignals, { segmentState: "historical" })
      .map((row) => row.signal_id),
    ["signal-segment-expired", "signal-segment-unavailable"],
  );
  assert.deepEqual(
    Ui.filterSignals(segmentSignals, { segmentState: "absent" })
      .map((row) => row.signal_id),
    ["signal-1"],
  );
  const sameLevelRecursiveEvidence = {
    ...currentSegment,
    signal_id: "signal-1m-chart-l1",
    segment_difference_1m: {
      point_type: "1buy",
      source_frequency: "1m",
      recursive_level: 1,
    },
  };
  assert.equal(
    Ui.segmentDifferenceEvidenceStatusForSignal(sameLevelRecursiveEvidence),
    "absent",
  );
  assert.deepEqual(
    Ui.filterSignals([sameLevelRecursiveEvidence], { segmentState: "present" }),
    [],
  );
  assert.equal(
    Ui.emptySignalDetail(Ui.normalizeSnapshot(snapshot), "", { segmentState: "current" }),
    "当前有 1 个5分钟操作候选已完成有效的1分钟区间套定位（买点 1 / 卖点 0），但被其他筛选条件隐藏；点击“查看当前定位”可清除这些筛选。",
  );
});

test("review display sorting keeps confirmed setups ahead of provisional candidates", () => {
  const Ui = loadUi();
  const rows = [
    {
      signal_id: "approaching-ranked",
      code: "SZ.000003",
      point_type: "1buy",
      lifecycle_stage: "approaching",
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "armed-unranked",
      code: "SZ.000002",
      point_type: "2buy",
      lifecycle_stage: "armed",
      sector: { sector_id: "sector-2" },
    },
    {
      signal_id: "triggered-unranked",
      code: "SZ.000001",
      point_type: "3buy",
      lifecycle_stage: "triggered",
      sector: { sector_id: "sector-2" },
    },
    {
      signal_id: "armed-ranked",
      code: "SZ.000004",
      point_type: "3buy",
      lifecycle_stage: "armed",
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "armed-ranked-first-sell",
      code: "SZ.000005",
      point_type: "1sell",
      lifecycle_stage: "armed",
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "confirmed-observed",
      code: "SH.688132",
      point_type: "3buy",
      lifecycle_stage: "observed",
      setup_5m: { status: "confirmed", point_type: "3buy" },
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "provisional-formed",
      code: "SH.688132",
      point_type: "3buy",
      lifecycle_stage: "formed",
      setup_5m: { status: "provisional", point_type: "3buy" },
      sector: { sector_id: "sector-1" },
    },
  ];
  const originalIds = rows.map((row) => row.signal_id);
  const sorted = Ui.sortSignalsForReview(rows, [
    { sector_id: "sector-1", rank: 1 },
    { sector_id: "sector-2", rank: null },
  ]);

  assert.deepEqual(sorted.map((row) => row.signal_id), [
    "triggered-unranked",
    "armed-ranked-first-sell",
    "armed-ranked",
    "armed-unranked",
    "confirmed-observed",
    "provisional-formed",
    "approaching-ranked",
  ]);
  assert.deepEqual(rows.map((row) => row.signal_id), originalIds);
});

test("live selection sorts the filtered rows by review priority descending", () => {
  const Ui = loadUi();
  const rows = [
    {
      signal_id: "confirmed-low-priority",
      code: "SZ.000001",
      point_type: "1buy",
      lifecycle_stage: "triggered",
      review_priority: 25,
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "approaching-high-priority",
      code: "SZ.000002",
      point_type: "3buy",
      lifecycle_stage: "approaching",
      review_priority: 80,
      sector: { sector_id: "sector-2" },
    },
    {
      signal_id: "notification-failed",
      code: "QCOM.US",
      point_type: "2buy",
      lifecycle_stage: "triggered",
      realtime_notification: true,
      notification_delivery_status: "failed",
      sector: { sector_id: "market:us" },
    },
    {
      signal_id: "canonical-derived-priority",
      code: "SZ.000003",
      point_type: "2buy",
      side: "buy",
      lifecycle_stage: "triggered",
      entry_allowed: true,
      execution_profile: { recommendation: "READY", context_grade: "A" },
      higher_timeframe_risk: {
        market_gate: "GREEN",
        sector_gate: "GREEN",
        symbol_gate: "GREEN",
        reason_codes: [],
      },
      warmup: { converged: true, reason_codes: [] },
      selection_sources: [],
      decision_reasons: [],
      sector: { sector_id: "sector-1" },
    },
    {
      signal_id: "priority-unavailable",
      code: "AAPL.US",
      point_type: "1buy",
      lifecycle_stage: "triggered",
      sector: { sector_id: "market:us" },
    },
  ];

  assert.deepEqual(
    Ui.sortSignalsForReview(rows).map((row) => row.signal_id),
    [
      "notification-failed",
      "canonical-derived-priority",
      "approaching-high-priority",
      "confirmed-low-priority",
      "priority-unavailable",
    ],
  );
  assert.equal(Ui.reviewPriorityForSignal(rows[2]), 110);
  assert.equal(Ui.reviewPriorityForSignal(rows[3]), 85);
  assert.equal(Ui.reviewPriorityForSignal(rows[4]), null);
});

test("derived review priority keeps recommendation bands stable under rich diagnostics", () => {
  const Ui = loadUi();
  const diagnosticReasons = Array.from({ length: 40 }, (_, index) => `diagnostic_${index}`);
  const signal = (signalId, status) => ({
    signal_id: signalId,
    code: signalId,
    point_type: "2buy",
    side: "buy",
    lifecycle_stage: "triggered",
    execution_profile: {
      recommendation: status === "BLOCKED" ? "BLOCKED" : "CAUTION",
      context_grade: "B",
      advisory_reason_codes: diagnosticReasons,
      hard_block_reason_codes: status === "BLOCKED" ? diagnosticReasons : [],
    },
    position_recommendation: { status, reason_codes: diagnosticReasons },
    higher_timeframe_risk: {
      market_gate: "UNRESOLVED",
      sector_gate: "UNRESOLVED",
      symbol_gate: "UNRESOLVED",
      reason_codes: diagnosticReasons,
    },
    warmup: { converged: true, reason_codes: diagnosticReasons },
    decision_reasons: diagnosticReasons,
    selection_sources: ["QMT_SECTOR_ELIGIBLE_SCOPE"],
  });
  const rows = [
    signal("blocked", "BLOCKED"),
    signal("not-actionable", "NOT_ACTIONABLE"),
    signal("conditional", "CONDITIONAL"),
    signal("recommended", "RECOMMENDED"),
  ];

  assert.deepEqual(
    Ui.sortSignalsForReview(rows).map((row) => row.signal_id),
    ["recommended", "conditional", "not-actionable", "blocked"],
  );
  assert.ok(Ui.reviewPriorityForSignal(rows[3]) >= 70);
  assert.ok(Ui.reviewPriorityForSignal(rows[0]) <= 19);
});

test("confirmed sell review is urgent and manual attention adds account-free priority", () => {
  const Ui = loadUi();
  const signal = (signalId, sources) => ({
    signal_id: signalId,
    code: signalId,
    point_type: "2sell",
    side: "sell",
    lifecycle_stage: "triggered",
    execution_profile: { recommendation: "CAUTION", context_grade: "B" },
    position_recommendation: { status: "CONDITIONAL", reason_codes: ["MANUAL_REVIEW"] },
    higher_timeframe_risk: {
      market_gate: "UNRESOLVED",
      sector_gate: "UNRESOLVED",
      symbol_gate: "UNRESOLVED",
      reason_codes: [],
    },
    warmup: { converged: true, reason_codes: [] },
    selection_sources: sources,
    setup_5m: { available_at: "2026-08-20T14:55:00+08:00" },
    observed_at: "2026-08-20T15:00:00+08:00",
  });
  const structuralSell = signal("structural-sell", ["QMT_SECTOR_ELIGIBLE_SCOPE"]);
  const manualAttention = signal("manual-attention", ["MANUAL_ATTENTION_MONITOR"]);
  const staleStructuralSell = {
    ...structuralSell,
    signal_id: "stale-structural-sell",
    segment_difference_1m: { available_at: "2026-08-21T09:31:00+08:00" },
    observed_at: "2026-08-21T09:31:00+08:00",
  };
  const staleManualAttention = {
    ...manualAttention,
    signal_id: "stale-manual-attention",
    observed_at: "2026-08-21T09:31:00+08:00",
  };

  assert.ok(Ui.reviewPriorityForSignal(structuralSell) >= 80);
  assert.ok(Ui.reviewPriorityForSignal(manualAttention) >= 90);
  assert.ok(Ui.reviewPriorityForSignal(staleStructuralSell) >= 40);
  assert.ok(Ui.reviewPriorityForSignal(staleStructuralSell) <= 69);
  assert.ok(Ui.reviewPriorityForSignal(staleManualAttention) >= 90);
  assert.deepEqual(
    Ui.sortSignalsForReview([structuralSell, manualAttention]).map((row) => row.signal_id),
    ["manual-attention", "structural-sell"],
  );

  const laterManualAttention = {
    ...manualAttention,
    signal_id: "later-manual-attention",
    observed_at: "2026-08-21T15:00:00+08:00",
    review_priority: 97,
  };
  assert.ok(Ui.reviewPriorityForSignal(laterManualAttention) >= 90);
  assert.match(
    Ui.decisionSummaryForSignal(laterManualAttention).title,
    /5分钟二卖.*操作确认/,
  );
});

test("derived sell priority counts the formal 5m setup on same-session trading minutes", () => {
  const Ui = loadUi();
  const sell = (availableAt, observedAt) => ({
    signal_id: `${availableAt}:${observedAt}`,
    code: "SZ.000001",
    market: "a",
    point_type: "2sell",
    side: "sell",
    lifecycle_stage: "triggered",
    setup_5m: { available_at: availableAt },
    observed_at: observedAt,
    execution_profile: { recommendation: "CAUTION", context_grade: "B" },
    position_recommendation: { status: "CONDITIONAL", reason_codes: [] },
    higher_timeframe_risk: {
      market_gate: "UNRESOLVED",
      sector_gate: "UNRESOLVED",
      symbol_gate: "UNRESOLVED",
    },
    warmup: { converged: true },
    selection_sources: ["QMT_SECTOR_ELIGIBLE_SCOPE"],
  });
  const lunchFresh = sell(
    "2026-08-20T11:25:00+08:00",
    "2026-08-20T13:05:00+08:00",
  );
  const elevenMinutesOld = sell(
    "2026-08-20T10:00:00+08:00",
    "2026-08-20T10:11:00+08:00",
  );
  const overnight = sell(
    "2026-08-20T14:55:00+08:00",
    "2026-08-21T09:31:00+08:00",
  );

  assert.ok(Ui.reviewPriorityForSignal(lunchFresh) >= 80);
  assert.ok(Ui.reviewPriorityForSignal(elevenMinutesOld) <= 69);
  assert.ok(Ui.reviewPriorityForSignal(overnight) <= 69);
});

test("signal discovery delay excludes only the same A-share lunch closure", () => {
  const Ui = loadUi();
  const base = {
    code: "SZ.000001",
    market: "a",
    setup_5m: { available_at: "2026-08-20T11:25:00+08:00" },
    observed_at: "2026-08-20T13:05:00+08:00",
  };

  assert.equal(Ui.signalAgeSecondsForReview(base), 600);
  assert.equal(
    Ui.signalAgeSecondsForReview({ ...base, market: "us" }),
    6000,
  );
  assert.equal(
    Ui.signalAgeSecondsForReview({
      ...base,
      observed_at: "2026-08-21T13:05:00+08:00",
    }),
    92400,
  );

  const currentBuy = {
    signal_id: "current-buy",
    code: "SZ.000001",
    side: "buy",
    point_type: "2buy",
    lifecycle_stage: "triggered",
    setup_5m: { available_at: "2026-08-19T14:55:00+08:00" },
    observed_at: "2026-08-20T15:00:00+08:00",
    execution_profile: { recommendation: "READY", context_grade: "A" },
    position_recommendation: {
      status: "RECOMMENDED",
      side: "buy",
      recommended_percent: "6.15",
      reason_codes: ["CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED"],
    },
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "GREEN",
      symbol_gate: "GREEN",
    },
    warmup: { converged: true },
  };
  assert.ok(Ui.reviewPriorityForSignal(currentBuy) >= 70);
  assert.equal(
    Ui.positionRecommendationLabel(currentBuy),
    "结构风险参考比例：6.15% 以内（按当前价至5分钟防守位测算；仅作结构模型比较）",
  );
});

test("realtime notification priority does not expire a current structure by age", () => {
  const Ui = loadUi();
  const notification = {
    signal_id: "notification-current-age",
    code: "MSFT.US",
    market: "us",
    side: "buy",
    point_type: "2buy",
    lifecycle_stage: "triggered",
    realtime_notification: true,
    notification_delivery_status: "delivered",
    notification_signal_available_at: "2026-08-20T10:00:00-04:00",
    notification_detected_at: "2026-08-20T10:00:30-04:00",
    execution_profile: { recommendation: "READY", context_grade: "A" },
    position_recommendation: {
      status: "RECOMMENDED",
      side: "buy",
      basis: "STRUCTURAL_RISK_MODEL_UPPER_BOUND",
      recommended_percent: "6.15",
      reason_codes: ["CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED"],
    },
  };
  const earlierReviewAt = new Date("2026-08-20T10:05:00-04:00");
  const laterReviewAt = new Date("2026-08-20T10:11:00-04:00");

  assert.equal(Ui.signalAgeSecondsForReview(notification), 30);
  assert.equal(Ui.reviewPriorityForSignal(notification, earlierReviewAt), 100);
  assert.equal(
    Ui.positionRecommendationLabel(notification, "结构风险参考待人工核对", earlierReviewAt),
    "结构风险参考比例：6.15% 以内（按当前价至5分钟防守位测算；仅作结构模型比较）",
  );

  assert.equal(Ui.reviewPriorityForSignal(notification, laterReviewAt), 100);
  assert.match(Ui.decisionSummaryForSignal(notification, laterReviewAt).title, /5分钟.*买/);
  assert.equal(
    Ui.positionRecommendationLabel(notification, "结构风险参考待人工核对", laterReviewAt),
    "结构风险参考比例：6.15% 以内（按当前价至5分钟防守位测算；仅作结构模型比较）",
  );

  const failed = { ...notification, notification_delivery_status: "failed" };
  assert.equal(Ui.reviewPriorityForSignal(failed, earlierReviewAt), 110);
  assert.equal(Ui.reviewPriorityForSignal(failed, laterReviewAt), 110);

  const lunchNotification = {
    ...notification,
    code: "SZ.000001",
    market: "a",
    notification_signal_available_at: "2026-08-20T11:25:00+08:00",
    notification_detected_at: "2026-08-20T11:25:30+08:00",
  };
  assert.equal(
    Ui.reviewPriorityForSignal(
      lunchNotification,
      new Date("2026-08-20T13:05:00+08:00"),
    ),
    100,
  );
});

test("human review filters can preserve their rows and then sort priority high to low", () => {
  const HumanUi = loadHumanReviewUi();
  const rows = [
    { candidate_id: "low", review_priority: 20 },
    { candidate_id: "high", review_priority: 90 },
    { candidate_id: "middle", review_priority: "55" },
  ];

  assert.deepEqual(
    HumanUi.sortCandidatesByReviewPriority(rows).map((row) => row.candidate_id),
    ["high", "middle", "low"],
  );
  assert.deepEqual(rows.map((row) => row.candidate_id), ["low", "high", "middle"]);
});

test("chart selection defaults to a queue signal and clears an empty filter", () => {
  const Ui = loadUi();
  const [first, second] = snapshot.signals;

  assert.equal(Ui.resolveSelectedSignalId(null, snapshot.signals, snapshot.signals), "signal-1");
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [], snapshot.signals), null);
  assert.equal(Ui.resolveSelectedSignalId("removed", [], snapshot.signals), null);
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [second], snapshot.signals), "signal-2");
  assert.equal(Ui.resolveSelectedSignalId("signal-1", [], []), null);
  assert.equal(Ui.resolveSelectedSignalId(null, [first], snapshot.signals), "signal-1");
});

test("signal queue and chart publish one shared selected identity", () => {
  const Ui = loadUi();
  const signalList = fakeChartRoot();
  const chart = fakeChartRoot();
  const selected = snapshot.signals[1];

  const selectedCard = Ui.renderSignalWorkspace(
    signalList.root,
    snapshot.signals,
    selected.signal_id,
  );
  Ui.renderChartWorkspace(chart.root, selected, { frequency: "5m" });

  assert.equal(signalList.root.dataset.selectedSignalId, selected.signal_id);
  assert.equal(selectedCard.dataset.signalId, selected.signal_id);
  assert.equal(selectedCard.dataset.code, selected.code);
  assert.equal(selectedCard.getAttribute("aria-current"), "true");
  assert.equal(selectedCard.getAttribute("aria-controls"), "es-chart-workspace");
  const situation = selectedCard.children.find(
    (child) => child.className === "es-signal-card__evidence",
  );
  const tags = selectedCard.children.find(
    (child) => child.className === "es-signal-card__tags",
  );
  assert.match(situation.textContent, /日线 .* · 30m .* · 5m .* · 1m /);
  assert.ok(tags.children.some(
    (child) => child.textContent === "1m 区间套定位 · 买入位置有效",
  ));
  assert.equal(chart.root.dataset.signalId, selected.signal_id);
  assert.equal(chart.root.dataset.selectedCode, selected.code);
});

test("same symbol confirmed and newer forming structures keep both facts explicit", () => {
  const Ui = loadUi();
  const confirmedSell = {
    ...snapshot.signals[0],
    signal_id: "confirmed-sell",
    code: "SH.600215",
    name: "派斯林",
    point_type: "1sell",
    side: "sell",
    lifecycle_stage: "triggered",
    observed_at: "2026-08-20T15:00:00+08:00",
    setup_5m: {
      point_type: "1sell",
      status: "confirmed",
      formation_state: "confirmed",
      terminal_segment_role: "latest_completed",
      terminal_segment_end_at: "2026-08-13T14:00:00+08:00",
    },
    presentation_sibling_structure_context: {
      relation: "untrusted_snapshot_value",
      summary: "不应保留",
    },
  };
  const formingBuy = {
    ...snapshot.signals[0],
    signal_id: "forming-buy",
    code: "SH.600215",
    name: "派斯林",
    point_type: "3buy",
    side: "buy",
    lifecycle_stage: "approaching",
    observed_at: "2026-08-20T15:00:00+08:00",
    setup_5m: {
      point_type: "3buy",
      status: "provisional",
      formation_state: "forming",
      terminal_segment_role: "latest_unfinished",
      terminal_segment_end_at: "2026-08-19T14:45:00+08:00",
    },
  };

  const [confirmed, forming] = Ui.annotateSiblingStructureContexts([
    confirmedSell,
    formingBuy,
  ]);
  assert.equal(
    confirmed.presentation_sibling_structure_context.relation,
    "opposite_forming_candidate",
  );
  assert.equal(
    confirmed.presentation_sibling_structure_context.summary,
    "较新的反向三买候选正在形成（未确认）；当前一卖操作确认仍保留",
  );
  assert.equal(
    forming.presentation_sibling_structure_context.relation,
    "opposite_confirmed_setup",
  );
  assert.equal(
    forming.presentation_sibling_structure_context.summary,
    "当前为较新的反向三买候选（未确认）；同标的另有一卖操作确认",
  );
  assert.equal(
    confirmedSell.presentation_sibling_structure_context.summary,
    "不应保留",
  );

  const signalList = fakeChartRoot();
  const card = Ui.renderSignalWorkspace(
    signalList.root,
    [confirmed, forming],
    confirmed.signal_id,
  );
  const contextLine = card.children.find(
    (child) => child.className === "es-signal-card__structure-context",
  );
  const tags = card.children.find(
    (child) => child.className === "es-signal-card__tags",
  );
  assert.equal(contextLine.dataset.relation, "opposite_forming_candidate");
  assert.match(contextLine.textContent, /当前一卖操作确认仍保留/);
  assert.ok(tags.children.some(
    (child) => child.textContent === "反向候选形成中",
  ));
  assert.match(
    Ui.decisionSummaryForSignal(
      confirmed,
      new Date("2026-08-20T15:05:00+08:00"),
    ).detail,
    /较新的反向三买候选正在形成（未确认）/,
  );
});

test("same point new candidate is annotated but an older unfinished row is not", () => {
  const Ui = loadUi();
  const confirmed = {
    signal_id: "confirmed-3sell",
    code: "SH.688408",
    point_type: "3sell",
    side: "sell",
    lifecycle_stage: "triggered",
    observed_at: "2026-08-20T15:00:00+08:00",
    setup_5m: {
      status: "confirmed",
      formation_state: "confirmed",
      terminal_segment_role: "latest_completed",
      terminal_segment_end_at: "2026-08-17T14:25:00+08:00",
    },
  };
  const newer = {
    signal_id: "forming-3sell",
    code: "SH.688408",
    point_type: "3sell",
    side: "sell",
    lifecycle_stage: "approaching",
    observed_at: "2026-08-20T15:00:00+08:00",
    setup_5m: {
      status: "provisional",
      formation_state: "forming",
      terminal_segment_role: "latest_unfinished",
      terminal_segment_end_at: "2026-08-20T14:25:00+08:00",
    },
  };
  const rows = Ui.annotateSiblingStructureContexts([confirmed, newer]);
  assert.equal(
    rows[0].presentation_sibling_structure_context.relation,
    "same_point_forming_candidate",
  );
  assert.match(
    rows[1].presentation_sibling_structure_context.summary,
    /另有三卖操作确认/,
  );

  const older = {
    ...newer,
    signal_id: "older-unfinished",
    setup_5m: {
      ...newer.setup_5m,
      terminal_segment_end_at: "2026-08-16T14:25:00+08:00",
    },
  };
  const inconsistent = Ui.annotateSiblingStructureContexts([confirmed, older]);
  assert.equal(inconsistent[0].presentation_sibling_structure_context, undefined);
  assert.equal(inconsistent[1].presentation_sibling_structure_context, undefined);
});

test("signals group by native sector without price-change logic", () => {
  const Ui = loadUi();
  const grouped = Ui.groupSignalsBySector(snapshot.signals);

  assert.deepEqual(Object.keys(grouped), [
    "qmt-gics3:bank",
    "qmt-gics3:real-estate",
  ]);
  assert.equal(grouped["qmt-gics3:bank"][0].signal_id, "signal-1");
});

test("chart URLs use the supplied current four-period contract", () => {
  const Ui = loadUi();

  assert.deepEqual(Ui.chartUrlsForSignal(snapshot.signals[0]), {
    "d": "/?market=a&code=SZ.000001&layout=single&intervals=D&chart_sidebar=collapsed&default_study=MACD_HTF",
    "30m": "/?market=a&code=SZ.000001&layout=single&intervals=30&chart_sidebar=collapsed&default_study=MACD_HTF",
    "5m": "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
    "1m": "/?market=a&code=SZ.000001&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF",
  });
  assert.deepEqual(Ui.chartUrlsForSignal(snapshot.signals[1]), {
    "d": "/?market=a&code=SZ.000002&layout=single&intervals=D&chart_sidebar=collapsed&default_study=MACD_HTF",
    "30m": "/?market=a&code=SZ.000002&layout=single&intervals=30&chart_sidebar=collapsed&default_study=MACD_HTF",
    "5m": "/?market=a&code=SZ.000002&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
    "1m": "/?market=a&code=SZ.000002&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF",
  });
});

test("chart URLs keep fragments and normalize study and embedded mode exactly once", () => {
  const Ui = loadUi();
  const signal = {
    code: "SH.600000",
    chart_urls: {
      "d": "/?market=a&code=SH.600000&layout=single&intervals=D#daily",
      "30m": "/?market=a&code=SH.600000&layout=single&intervals=30&default_study=MACD_HTF#main",
      "5m": "/?market=a&code=SH.600000&layout=single&intervals=5&chart_sidebar=expanded#setup",
      "1m": "/?market=a&code=SH.600000&layout=single&intervals=1&chart_embed=legacy#trigger",
    },
  };
  const urls = Ui.chartUrlsForSignal(signal, { embedded: true });
  const workbenchUrls = Ui.chartUrlsForSignal(signal);

  assert.equal(
    urls["30m"],
    "/?market=a&code=SH.600000&layout=single&intervals=30&default_study=MACD_HTF&chart_sidebar=collapsed&chart_embed=decision-support#main",
  );
  assert.equal(
    urls["5m"],
    "/?market=a&code=SH.600000&layout=single&intervals=5&chart_sidebar=expanded&default_study=MACD_HTF&chart_embed=decision-support#setup",
  );
  assert.equal(
    urls["1m"],
    "/?market=a&code=SH.600000&layout=single&intervals=1&chart_embed=decision-support&chart_sidebar=collapsed&default_study=MACD_HTF#trigger",
  );
  for (const url of Object.values(urls)) {
    assert.equal((url.match(/default_study=MACD_HTF/g) || []).length, 1);
    assert.equal((url.match(/chart_embed=decision-support/g) || []).length, 1);
  }
  assert.equal(
    workbenchUrls["1m"],
    "/?market=a&code=SH.600000&layout=single&intervals=1&chart_sidebar=collapsed&default_study=MACD_HTF#trigger",
  );
  for (const url of Object.values(workbenchUrls)) assert.doesNotMatch(url, /chart_embed=/);
});

test("analysis layout switch accepts only current layout values", () => {
  const Ui = loadUi();
  const root = { dataset: { currentLayout: "focus" } };

  assert.equal(Ui.setChartLayout(root, "dual"), "dual");
  assert.equal(root.dataset.layout, "dual");
  assert.equal(root.dataset.currentLayout, "dual");
  assert.equal(Ui.setChartLayout(root, "triple"), "triple");
  assert.equal(root.dataset.layout, "triple");
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
  assert.match(dashboardCss, /\.es-analysis-grid\s*\{[^}]*overflow:\s*clip/s);
  assert.match(dashboardCss, /\.es-us-monitor-compact__summary\s*\{[^}]*display:\s*grid/s);
  assert.match(dashboardCss, /\.es-us-monitor-compact__metrics\s*\{[^}]*repeat\(4,/s);
  assert.doesNotMatch(dashboardCss, /\.es-us-monitor-compact\s*\{[^}]*grid-template-columns:/s);
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

test("product page never silently truncates decision and audit content", () => {
  for (const selector of [
    "\\.es-live-state small",
    "\\.hr-summary dd",
    "\\.es-sector-row__heading strong",
    "\\.es-sector-row small",
    "\\.es-global-filter-drawer > summary span",
    "\\.es-signal-card__identity strong",
    "\\.es-decision-identity h3",
  ]) {
    assert.match(
      dashboardCss,
      new RegExp(`${selector}\\s*\\{[^}]*white-space:\\s*normal`, "s"),
      `${selector} must wrap complete business text`,
    );
    assert.doesNotMatch(
      dashboardCss,
      new RegExp(`${selector}\\s*\\{[^}]*(?:text-overflow:\\s*ellipsis|overflow:\\s*hidden)`, "s"),
      `${selector} must not silently crop business text`,
    );
  }
  assert.match(
    dashboardCss,
    /\.hr-evidence-columns ul\s*\{[^}]*(?:overflow-wrap:\s*anywhere)[^}]*\}/s,
  );
  assert.doesNotMatch(
    dashboardCss,
    /\.hr-evidence-columns ul\s*\{[^}]*max-height:/s,
  );
  assert.doesNotMatch(humanReviewSource, /candidate\.candidate_id\.slice\(/);
  assert.match(humanReviewSource, /候选报告与参数身份已验证：\$\{text\(candidate\.candidate_id\)\}/);
});

test("narrow product layout keeps all four period cards and complete evidence accessible", () => {
  assert.match(
    dashboardCss,
    /@media \(max-width:\s*700px\)[\s\S]*\.es-period-path\s*\{[^}]*grid-template-columns:\s*minmax\(200px,\s*78vw\)\s+24px\s+minmax\(200px,\s*78vw\)\s+24px\s+minmax\(200px,\s*78vw\)\s+24px\s+minmax\(200px,\s*78vw\)/,
  );
  assert.match(
    dashboardCss,
    /@media \(max-width:\s*700px\)[\s\S]*\.es-status-facts--diagnostics\s*\{[^}]*position:\s*static[^}]*grid-template-columns:\s*1fr/s,
  );
  assert.match(
    dashboardCss,
    /@media \(max-width:\s*700px\)[\s\S]*\.es-evidence-panel\s*\{[^}]*position:\s*fixed[^}]*inset:\s*8px[^}]*width:\s*auto/s,
  );
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
    /\.es-chart-workspace\s*\{[^}]*grid-area:\s*charts[^}]*position:\s*sticky/s,
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

  assert.deepEqual(Ui.LIFECYCLE_LABELS, {
    observed: "结构观察",
    monitoring: "实时监听",
    approaching: "即将确认",
    formed: "几何候选待确认",
    armed: "旧版等待态",
    triggered: "5分钟操作确认",
    executable: "强提示待人工复核",
    active: "结构持续跟踪",
    invalidated: "结构已失效",
    closed: "跟踪已结束",
  });
  assert.equal(Ui.lifecycleLabel("triggered"), "5分钟操作确认");
  assert.equal(Ui.lifecycleLabel("unexpected-stage"), "未知状态");
  assert.match(template, /data-lifecycle="approaching"/);
  assert.doesNotMatch(template, /data-lifecycle="formed"/);
  assert.doesNotMatch(template, /data-lifecycle="armed"/);
  assert.doesNotMatch(template, /旧档案：/);
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
      discovered_symbol_count: 891,
      coverage_cycle_completed_symbol_count: 871,
      coverage_cycle_excluded_symbol_count: 20,
      coverage_cycle_failed_symbol_count: 0,
      pending_symbol_count: 0,
      coverage_cycle_complete: true,
    }),
    "全周期已分析 871/891 · 历史不足排除 20 · 范围已处置",
  );
  const coverageSnapshot = {
    coverage_manifest: {
      completed_codes: ["SH.600000", "SZ.000001", "SZ.000002"],
    },
    signals: [
      { code: "SZ.000001", signal_id: "signal-a" },
      { code: "SZ.000001", signal_id: "signal-b" },
    ],
  };
  assert.equal(Ui.completedWithoutSignalCount(coverageSnapshot), 2);
  assert.equal(
    Ui.scanCoverageText({
      discovered_symbol_count: 4,
      coverage_cycle_completed_symbol_count: 3,
      coverage_cycle_excluded_symbol_count: 0,
      coverage_cycle_failed_symbol_count: 0,
      pending_symbol_count: 1,
      coverage_cycle_complete: false,
    }, coverageSnapshot),
    "全周期已分析 3/4 · 已分析无当前结构信号 2 · 待分析 1",
  );
  const lookupSnapshot = {
    coverage_manifest: {
      discovered_codes: [
        "SH.600000",
        "SZ.000001",
        "SZ.000002",
        "SZ.000003",
        "SZ.000004",
      ],
      completed_codes: ["SH.600000", "SZ.000001"],
      excluded_codes: ["SZ.000002"],
      failed_codes: ["SZ.000003"],
      exclusions: [
        { code: "SZ.000002", reason_code: "KLINE_MINIMUM_HISTORY_NOT_MET" },
      ],
    },
    monitor_instrument_exclusions: [
      {
        code: "SH.000001",
        reason_code: "QMT_NATIVE_STOCK_OR_ETF_REQUIRED",
        qmt_instrument_type: "index_cn",
        diagnostic_only: true,
      },
      {
        code: "SH.600001",
        reason_code: "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED",
        qmt_instrument_type: "unresolved_cn",
        diagnostic_only: true,
      },
    ],
    signals: [{ code: "SH.600000", signal_id: "signal-1" }],
  };
  assert.equal(
    Ui.emptySignalDetail(lookupSnapshot, "600000"),
    "SH.600000 有 1 条当前结构信号，但被当前点类型、生命周期或板块筛选隐藏。",
  );
  assert.equal(
    Ui.emptySignalDetail(lookupSnapshot, "000001"),
    "SZ.000001 已完成分析，当前没有结构信号；这不是漏扫，也不代表未来不会出现。",
  );
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "SZ.000002"), /资格排除/);
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "000003"), /本轮分析失败/);
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "000004"), /尚在待分析队列/);
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "SH.600005"), /不在当前QMT板块触发/);
  assert.match(
    Ui.emptySignalDetail(lookupSnapshot, "SH.000001"),
    /QMT原生品种类型为 index_cn/,
  );
  assert.match(
    Ui.emptySignalDetail(lookupSnapshot, "SH.600001"),
    /QMT本轮未能解析其原生品种类型/,
  );
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "000005"), /当前快照没有匹配项/);
  assert.match(Ui.emptySignalDetail(lookupSnapshot, "平安银行"), /当前快照没有匹配项/);
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
      sector_resolution_ratio: null,
    }),
    "发现 10 · 完成 7 · 失败 3 · 解析完成率 70%",
  );
  assert.equal(
    Ui.sectorCoverageText({
      sector_discovered_count: 66,
      sector_completed_count: 56,
      sector_excluded_count: 10,
      sector_failed_count: 0,
      sector_completion_ratio: "0.8484848484848484848484848485",
      sector_resolution_ratio: "1",
    }),
    "发现 66 · 完成 56 · 资格排除 10 · 失败 0 · 解析完成率 100%",
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
    Ui.memberHistoryDiagnosticsText({
      sector_member_history_diagnostics: {
        schema: "chanlun-sector-member-history-diagnostics",
        unique_symbol_count: 5132,
        unique_symbol_status_counts: {
          COMPLETE: 5126,
          NEW_LISTING: 1,
          SUSPENDED: 4,
          UNEXPLAINED_GAP: 1,
        },
      },
    }),
    "完整 5126/5132 · 新股 1 · 停牌 4 · 无法解释 1（失败关闭）",
  );
  assert.equal(
    Ui.memberHistoryDiagnosticsText({}),
    "尚无认证成员状态",
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
    Ui.scanTimingText({
      batch_duration_ms: 22400,
      coverage_cycle_elapsed_ms: 65700,
      coverage_cycle_batch_count: 3,
      coverage_cycle_complete: false,
      coverage_cycle_throughput_symbols_per_minute: 25.678,
      coverage_cycle_estimated_remaining_seconds: 180.4,
    }),
    "本批 22.4秒 · 全周期已运行 65.7秒 / 3批 · 吞吐 25.7只/分钟 · 预计剩余 180.4秒",
  );
  assert.equal(
    Ui.sectorEvidenceText(snapshot.sectors[0]),
    "30m 向上/支撑/一买 · 5m 震荡/中性/无主导点",
  );
  assert.match(controllerSource, /Ui\.sectorCoverageText\(audit\)/);
  assert.match(
    controllerSource,
    /Ui\.scanCoverageText\(audit, snapshot, runtimeHealth\)/,
  );
  assert.match(controllerSource, /Ui\.emptySignalDetail\(state\.snapshot, state\.query,\s*\{/);
  assert.match(controllerSource, /Ui\.scanQualityText\(snapshot, runtimeHealth\)/);
  assert.match(controllerSource, /Ui\.memberHistoryDiagnosticsText\(snapshot\)/);
  assert.match(controllerSource, /Ui\.scanTimingText\(audit\)/);
  assert.match(controllerSource, /后台正连续分析剩余/);
  assert.match(controllerSource, /只因历史不足未参与/);
  assert.match(controllerSource, /小板块资格排除/);
  assert.match(controllerSource, /真实失败/);
  assert.doesNotMatch(controllerSource, /只标的需复核/);
  assert.match(controllerSource, /本轮板块结构质量不足，保留上一快照/);
});

test("the current lifecycle field is authoritative", () => {
  const Ui = loadUi();
  const formed = {
    point_type: "3buy",
    lifecycle_stage: "approaching",
    setup_5m: {
      point_type: "3buy",
      status: "provisional",
      evidence_codes: [
        "physical_timeframe_recursive_base_level",
        "provisional_center_completion",
        "core_boundary_held",
      ],
    },
    chart_urls: snapshot.signals[0].chart_urls,
  };
  const pending = {
    point_type: "3buy",
    lifecycle_stage: "approaching",
    setup_5m: {
      point_type: "3buy",
      status: "provisional",
      evidence_codes: ["live_first_return"],
    },
  };
  const nonThird = {
    ...formed,
    point_type: "2buy",
    setup_5m: { ...formed.setup_5m, point_type: "2buy" },
  };

  const explicitFormed = {
    ...formed,
    lifecycle_stage: "formed",
    setup_5m: {
      ...formed.setup_5m,
      state_contract: "chanlun-five-minute-setup-state-v3-geometric-candidate",
      formation_state: "geometry_ready",
      lock_state: "pending",
      contains_forming_segment: false,
      contains_unlocked_segment: true,
      contains_unfinished_segment: true,
      actionable: false,
    },
  };
  assert.equal(Ui.lifecycleStageForSignal(formed), "approaching");
  assert.equal(Ui.lifecycleStageForSignal(explicitFormed), "formed");
  assert.equal(Ui.lifecycleStageForSignal(pending), "approaching");
  assert.equal(Ui.lifecycleStageForSignal(nonThird), "approaching");
  assert.equal(Ui.decisionSummaryForSignal(explicitFormed).tone, "waiting");
  assert.deepEqual(
    Ui.filterSignals([formed, explicitFormed], { lifecycle: "formed" }),
    [],
  );
  assert.equal(Ui.lifecycleLabel("formed"), "几何候选待确认");
  assert.equal(Ui.setupFormationStateForSignal(explicitFormed), "geometry_ready");
  assert.equal(Ui.setupLockStateForSignal(explicitFormed), "pending");
  assert.equal(Ui.pointLabelForSignal(explicitFormed), "三买候选待锁定");
  const confirmedPending = {
    ...explicitFormed,
    lifecycle_stage: "triggered",
    setup_5m: {
      ...explicitFormed.setup_5m,
      status: "confirmed",
      formation_state: "confirmed",
      lock_state: "pending",
      contains_unlocked_segment: true,
      actionable: true,
    },
  };
  const confirmedLocked = {
    ...confirmedPending,
    setup_5m: {
      ...confirmedPending.setup_5m,
      lock_state: "locked",
      contains_unlocked_segment: false,
    },
  };
  assert.equal(Ui.pointLabelForSignal(confirmedPending), "三买操作确认");
  assert.equal(Ui.pointLabelForSignal(confirmedLocked), "三买操作确认");
  assert.equal(Ui.periodPathForSignal(confirmedPending)[2].state, "5分钟操作确认");
  assert.equal(
    Ui.periodPathForSignal(confirmedLocked)[2].state,
    "5分钟操作确认·末端已封存",
  );
  assert.match(
    Ui.periodPathForSignal(confirmedPending)[2].summary,
    /末端结构仍会随新K更新（不影响当前复核）/,
  );
  assert.doesNotMatch(Ui.pointLabelForSignal(confirmedPending), /复核中|待确认/);
  assert.equal(
    Ui.decisionSummaryForSignal(explicitFormed).title,
    "5分钟三买几何候选，尚未达到操作确认",
  );
  assert.equal(Ui.periodPathForSignal(explicitFormed)[2].state, "候选待锁定");
  assert.match(Ui.periodPathForSignal(explicitFormed)[2].summary, /^三买候选待锁定 ·/);

  const terminalFormed = {
    ...explicitFormed,
    setup_5m: {
      ...explicitFormed.setup_5m,
      terminal_segment_role: "latest_completed",
      terminal_segment_level: 0,
      terminal_segment_id: "segment-115",
      terminal_segment_source_kind: "segment",
      terminal_segment_direction: "down",
      terminal_segment_state: "formed",
      terminal_segment_start_at: "2026-08-13T09:35:00+08:00",
      terminal_segment_end_at: "2026-08-14T13:25:00+08:00",
      terminal_segment_available_at: "2026-08-18T15:00:00+08:00",
    },
  };
  assert.equal(
    Ui.terminalSegmentSummary(terminalFormed.setup_5m),
    "最新几何成形线段 · 向下 · 几何已成形、证据待固化",
  );
  assert.match(
    Ui.periodPathForSignal(terminalFormed)[2].summary,
    /^最新几何成形线段 · 向下 · 几何已成形、证据待固化 · 三买候选待锁定 ·/,
  );
  assert.match(
    Ui.periodPathForSignal(terminalFormed)[2].boundary,
    /线段 2026-08-13 09:35:00 → 2026-08-14 13:25:00/,
  );

  const hardBlockedFormedSell = {
    ...terminalFormed,
    point_type: "3sell",
    side: "sell",
    setup_5m: {
      ...terminalFormed.setup_5m,
      point_type: "3sell",
      side: "sell",
    },
    decision_reasons: [
      "sell_not_confirmed",
      "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
    ],
    execution_profile: {
      recommendation: "BLOCKED",
      recommendation_label: "结构或数据硬条件未通过",
      hard_blocked: true,
      hard_block_reason_codes: ["HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED"],
      advisory_reason_codes: [],
    },
  };
  assert.equal(
    Ui.decisionSummaryForSignal(hardBlockedFormedSell).title,
    "5分钟三卖几何候选，尚未达到操作确认",
  );
  assert.equal(Ui.decisionSummaryForSignal(hardBlockedFormedSell).tone, "blocked");
  const formedSellGroups = Ui.evidenceGroupsForSignal(hardBlockedFormedSell);
  assert.ok(formedSellGroups.established.some((line) => line.includes("三卖离开/回抽几何已出现")));
  assert.ok(formedSellGroups.blocking.some((line) => line.includes("高周期同源数据完整性")));
  assert.ok(formedSellGroups.missing.some((line) => line.includes("卖点尚未达到操作确认")));

  const invalidatedFormed = { ...explicitFormed, lifecycle_stage: "invalidated" };
  assert.equal(Ui.pointLabelForSignal(invalidatedFormed), "三买候选已失效");
  assert.equal(Ui.periodPathForSignal(invalidatedFormed)[2].state, "候选已失效");

  const explicitlyForming = {
    ...formed,
    setup_5m: {
      ...formed.setup_5m,
      formation_state: "forming",
      lock_state: "pending",
      contains_forming_segment: true,
      contains_unlocked_segment: true,
    },
  };
  assert.equal(Ui.pointLabelForSignal(explicitlyForming), "三买候选");
  assert.equal(Ui.periodPathForSignal(explicitlyForming)[2].state, "形成中");

  const invalidatedConfirmed = {
    ...explicitFormed,
    lifecycle_stage: "invalidated",
    setup_5m: {
      ...explicitFormed.setup_5m,
      status: "confirmed",
      formation_state: "confirmed",
      lock_state: "locked",
      contains_unlocked_segment: false,
      actionable: true,
    },
  };
  assert.equal(Ui.pointLabelForSignal(invalidatedConfirmed), "三买已确认后失效");
  assert.equal(Ui.periodPathForSignal(invalidatedConfirmed)[2].state, "已确认后失效");
  assert.equal(Ui.decisionSummaryForSignal(invalidatedConfirmed).title, "结构已失效");

  const normalized = Ui.normalizeSnapshot({
    ...snapshot,
    signals: [],
    manual_attention_signals: [explicitFormed],
  });
  assert.deepEqual(normalized.manual_attention_signals, []);
});

test("operator status copy explains degraded state without exposing internal codes", () => {
  const Ui = loadUi();
  const blockedHealth = {
    daily_preselection_ready: false,
    daily_preselection_status: "review_blocked",
    daily_preselection_reason_code: "HUMAN_REVIEW_MATERIALIZATION_FAILED",
    daily_preselection_candidate_count: 1664,
    daily_preselection_buy_candidate_count: 612,
    daily_preselection_sell_candidate_count: 1052,
    daily_preselection_target_session: "2026-08-04",
    daily_preselection_market_data_as_of: "2026-08-04T09:07:23+08:00",
    full_coverage_next_active_at: "NEXT_SCAN",
  };
  const summary = Ui.dailyPreselectionText(blockedHealth);
  const diagnostic = Ui.dailyPreselectionDiagnosticsText(blockedHealth);

  assert.equal(
    summary,
    "复核材料待重建 · 结构雷达可看 · 下一轮全量扫描 NEXT_SCAN",
  );
  assert.doesNotMatch(summary, /review_blocked|HUMAN_REVIEW_MATERIALIZATION_FAILED/);
  assert.match(diagnostic, /内部状态 复核受阻/);
  assert.match(diagnostic, /原因 人工复核材料生成失败/);
  assert.match(diagnostic, /结构线索 1664（买点 612 \/ 卖点 1052）/);
  assert.doesNotMatch(diagnostic, /重放板块证据/);

  assert.equal(
    Ui.dailyPreselectionText({
      daily_preselection_ready: true,
      daily_preselection_status: "ready",
      daily_preselection_target_session: "2026-08-05",
      daily_preselection_candidate_count: 28,
      daily_preselection_buy_candidate_count: 17,
      daily_preselection_sell_candidate_count: 11,
    }),
    "已就绪 · 适用 2026-08-05 · 买点 17 / 卖点 11 / 全部 28",
  );
  const intradayValidationHealth = {
    screening_scope_mode: "VALIDATION_COHORT",
    validation_cohort_size: 12,
    effective_monitor_universe_limit: 12,
    daily_preselection_ready: false,
    daily_preselection_status: "target_session_stale",
    daily_preselection_reason_code: "PRESELECTION_CLOSE_CUTOFF_INCOMPLETE",
    daily_preselection_market_data_as_of: "2026-08-26T11:30:00+08:00",
    snapshot_available: true,
    validation_snapshot_priority_only: true,
  };
  assert.equal(
    Ui.dailyPreselectionText(intradayValidationHealth),
    "12只小样本验证 · 盘中快照可用，15:05 后更新收盘候选",
  );
  assert.match(
    Ui.dailyPreselectionDiagnosticsText(intradayValidationHealth),
    /原因 盘中尚未形成完整收盘候选快照.*盘中调度 仅运行5分钟候选与按需1分钟定位，归档扫描等待15:05/,
  );

  const monitorHealth = {
    priority_monitor_status: "verified",
    priority_monitor_last_code_count: 13,
    priority_monitor_reason_codes: ["READY"],
    candidate_monitor_status: "verified",
    candidate_monitor_reason_codes: [],
    candidate_monitor_five_minute: {
      universe_count: 42,
      current_count: 42,
      missing_count: 0,
      overdue_count: 0,
      target_seconds: 300,
    },
    priority_monitor_sector_source_mode: "CURRENT_NATIVE",
    priority_monitor_immediate_universe_count: 3,
    realtime_alert_status: "ready",
    realtime_alert_reason_code: "READY",
    notification_dispatcher_configured: true,
    notification_operationally_verified: true,
    notification_delivery: {
      status: "verified",
      reason_code: "DELIVERY_SUCCESS_PROVEN",
    },
    priority_monitor_last_at: "LAST_RUN",
  };
  const monitorSummary = Ui.priorityMonitorText(monitorHealth, { signal_count: 0 });
  assert.equal(
    monitorSummary,
    "正常 · 范围：人工关注/自选/已有信号/支持板块 · 5分钟候选 42/42 只 · 1分钟通道暂无结构跟踪 · 通知送达已验证",
  );
  assert.doesNotMatch(monitorSummary, /verified|READY/);
  assert.match(
    Ui.priorityMonitorText(monitorHealth, { signal_count: 2 }),
    /1分钟通道跟踪 2 条5m结构/,
  );
  assert.match(
    Ui.priorityMonitorDiagnosticsText(monitorHealth, { signal_count: 0 }),
    /A股实时预警 已就绪（已就绪）.*即时复查 已验证（已就绪）· 最近 13 只.*5分钟候选轮换 已验证（节奏覆盖已验证）· 当前 42\/42 只.*1分钟精确定位队列 待定位的当前5分钟候选 3 只 · 持续轮转直至结构被替换.*全市场覆盖用于选股归档，不承诺每只股票5分钟实时预警.*1分钟通道当前结构 0 条（非新增通知计数）.*A股通知送达 已验证（已有成功送达证明）/,
  );
  const idleMonitor = {
    ...monitorHealth,
    candidate_monitor_status: "idle_no_candidates",
    candidate_monitor_reason_codes: [
      "CANDIDATE_MONITOR_NO_ELIGIBLE_UNIVERSE",
    ],
    candidate_monitor_five_minute: {
      universe_count: 0,
      current_count: 0,
      missing_count: 0,
      overdue_count: 0,
      target_seconds: 300,
    },
    priority_monitor_immediate_universe_count: 0,
    realtime_alert_status: "ready_idle",
    realtime_alert_reason_code: "CANDIDATE_MONITOR_NO_ELIGIBLE_UNIVERSE",
  };
  assert.equal(
    Ui.priorityMonitorText(idleMonitor, { signal_count: 0 }),
    "就绪但当前空闲 · 当前没有符合板块门控、已有5分钟信号或人工关注范围的监听对象 · 5分钟候选 0/0 只 · 1分钟定位不会提前启动 · 通知送达已验证",
  );
  assert.match(
    Ui.priorityMonitorDiagnosticsText(idleMonitor, { signal_count: 0 }),
    /A股实时预警 就绪但当前空闲（当前没有通过板块门控、已有信号或人工关注范围进入实时监听的标的）.*5分钟候选轮换 暂无合格监听对象（当前没有通过板块门控、已有信号或人工关注范围进入实时监听的标的）· 当前 0\/0 只/,
  );
  const preparingSectorScope = {
    ...monitorHealth,
    priority_monitor_sector_source_mode: "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED",
    candidate_monitor_five_minute: {
      ...monitorHealth.candidate_monitor_five_minute,
      universe_count: 1,
      current_count: 1,
    },
  };
  assert.equal(
    Ui.priorityMonitorText(preparingSectorScope, { signal_count: 0 }),
    "优先通道正常 · 支持板块范围准备中 · 当前已核验：人工关注/自选/已有信号 · 5分钟候选 1/1 只 · 1分钟通道暂无结构跟踪 · 通知送达已验证",
  );
  const continuityScope = {
    ...monitorHealth,
    priority_monitor_sector_source_mode: "PRESELECTION_CONTINUITY",
    preselection_continuity_active: true,
  };
  const continuitySummary = Ui.priorityMonitorText(continuityScope, {
    signal_count: 0,
  });
  assert.match(continuitySummary, /上一交易日已认证预选范围过渡/);
  assert.match(continuitySummary, /全部按当前规则实时重算/);
  assert.doesNotMatch(continuitySummary, /支持板块范围准备中/);
  const warmingSectorScope = {
    ...preparingSectorScope,
    candidate_monitor_status: "warming",
    candidate_monitor_reason_codes: ["CANDIDATE_MONITOR_WARMING_UP"],
    realtime_alert_status: "candidate_monitor_degraded",
    realtime_alert_reason_code: "CANDIDATE_MONITOR_WARMING_UP",
    candidate_monitor_five_minute: {
      ...preparingSectorScope.candidate_monitor_five_minute,
      universe_count: 7,
      current_count: 7,
    },
  };
  assert.equal(
    Ui.priorityMonitorText(warmingSectorScope, { signal_count: 0 }),
    "优先通道正常 · 5分钟候选覆盖暖机中 · 当前已核验：人工关注/自选/新鲜已有信号 · 支持板块范围准备中 · 5分钟候选 7/7 只 · 1分钟通道暂无结构跟踪 · 通知送达已验证",
  );
  assert.equal(
    Ui.segmentScopeText({
      priority_monitor_last_code_count: 2,
      priority_monitor_immediate_universe_count: 1,
    }, 0),
    "当前没有5分钟操作候选完成1分钟区间套精确定位 · 另有 1 只当前5分钟候选正在等待1分钟定位 · 5分钟决定交易级别，1分钟只确定精确买卖位置",
  );
  assert.equal(
    Ui.segmentScopeText({
    }, 3),
    "当前 3 个5分钟操作候选已完成1分钟区间套段差见证 · 5分钟决定交易级别，1分钟只确定精确买卖位置",
  );
  assert.equal(
    Ui.priorityMonitorText({ priority_monitor_status: "not_due" }, {}),
    "非交易时段 · 开盘后盯盘范围：人工关注/自选/已有信号/支持板块候选 · 仅页面提醒",
  );
  const afterHoursDiagnostics = Ui.priorityMonitorDiagnosticsText({
    priority_monitor_status: "not_due",
    priority_monitor_reason_codes: [],
    priority_monitor_last_code_count: 2,
    candidate_monitor_status: "not_due",
    candidate_monitor_reason_codes: [],
    candidate_monitor_five_minute: {
      universe_count: 12,
      current_count: 3,
      missing_count: 9,
      overdue_count: 9,
      target_seconds: 300,
    },
    realtime_alert_status: "not_due",
    realtime_alert_reason_code: "NON_TRADING_SESSION_NOT_DUE",
    notification_dispatcher_configured: true,
    notification_operationally_verified: true,
    notification_delivery: {
      status: "verified",
      reason_code: "DELIVERY_SUCCESS_PROVEN",
    },
  }, {});
  assert.match(afterHoursDiagnostics, /即时复查 未到运行时段（当前不在A股分钟监听时段）/);
  assert.match(afterHoursDiagnostics, /5分钟候选轮换 未到运行时段（当前不在A股分钟监听时段）/);
  assert.match(afterHoursDiagnostics, /非交易时段不计算当前缺失与逾期/);
  assert.doesNotMatch(afterHoursDiagnostics, /当前 3\/12 只|缺失 9|逾期 9/);
  assert.doesNotMatch(afterHoursDiagnostics, /（）|状态原因未提供/);

  const capacityBlocked = {
    ...monitorHealth,
    candidate_monitor_status: "capacity_insufficient",
    candidate_monitor_reason_codes: [
      "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT",
    ],
    candidate_monitor_five_minute: {
      universe_count: 2500,
      current_count: 1200,
      missing_count: 1300,
      overdue_count: 0,
      target_seconds: 300,
    },
    realtime_alert_status: "candidate_monitor_degraded",
    realtime_alert_reason_code: (
      "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
    ),
  };
  assert.equal(
    Ui.priorityMonitorText(capacityBlocked, {}),
    "优先通道正常 · 5分钟候选监听容量不足 · 当前优先复查：人工关注/自选/新鲜已有信号 · 5分钟候选 1200/2500 只 · 1分钟通道暂无结构跟踪 · 通知送达已验证",
  );

  const deliveryUnverified = {
    ...monitorHealth,
    realtime_alert_status: "notification_unverified",
    realtime_alert_reason_code: "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED",
    notification_operationally_verified: false,
    notification_delivery: {
      status: "awaiting_first_delivery",
      reason_code: "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED",
    },
  };
  assert.match(
    Ui.priorityMonitorText(deliveryUnverified, {}),
    /时效保障未就绪 · 尚无到期通知事件或成功送达记录.*通知已配置，送达尚未验证/,
  );
  const verifiedAuxiliary = {
    available: true,
    notification_configured: true,
    notification_delivery: {
      operationally_verified: true,
      status: "verified",
      reason_code: "DELIVERY_SUCCESS_PROVEN",
      delivered_event_count: 1,
    },
  };
  assert.match(
    Ui.priorityMonitorText(deliveryUnverified, {}, verifiedAuxiliary),
    /通知已配置，送达尚未验证 · 跨市场通知送达已验证（1 条）/,
  );
  assert.match(
    Ui.priorityMonitorDiagnosticsText(
      deliveryUnverified,
      {},
      verifiedAuxiliary,
    ),
    /A股通知已配置但送达未验证.*跨市场通知送达 已验证（1 条；已有成功送达证明）/,
  );
});

test("validation scope ignores stale full-coverage progress in operator copy", () => {
  const Ui = loadUi();
  const runtimeHealth = {
    screening_scope_mode: "VALIDATION_COHORT",
    validation_cohort_size: 12,
    effective_monitor_universe_limit: 12,
    full_coverage_refresh_enabled: false,
    daily_preselection_ready: false,
    daily_preselection_status: "coverage_in_progress",
    daily_preselection_candidate_count: 1664,
    full_coverage_next_active_at: "NEXT_SCAN",
  };
  const staleSnapshot = {
    screening_scope: {
      mode: "VALIDATION_COHORT",
      validation_cohort_size: 12,
      effective_monitor_universe_limit: 12,
    },
    scan_audit: {
      discovered_symbol_count: 5103,
      coverage_cycle_completed_symbol_count: 5086,
      coverage_cycle_excluded_symbol_count: 0,
      pending_symbol_count: 17,
      coverage_cycle_complete: false,
    },
  };

  assert.equal(
    Ui.scanCoverageText(staleSnapshot.scan_audit, staleSnapshot, runtimeHealth),
    "12只小样本验证 · 当前验证范围固定",
  );
  const warmupAuditSnapshot = {
    ...staleSnapshot,
    scan_audit: {
      ...staleSnapshot.scan_audit,
      warmup_sensitive_symbol_count: 2,
      warmup_context_only_sensitive_symbol_count: 1,
      trade_level_warmup_unconverged_symbol_count: 1,
      trade_level_warmup_fail_closed_symbol_count: 1,
      stock_decision_outcome_counts: {
        CURRENT_5M_STRUCTURAL_SIGNAL_EMITTED: 1,
        NO_CURRENT_5M_STRUCTURAL_POINT: 11,
      },
    },
  };
  assert.equal(
    Ui.scanCoverageText(
      warmupAuditSnapshot.scan_audit,
      warmupAuditSnapshot,
      runtimeHealth,
    ),
    "12只小样本验证 · 当前验证范围固定 · 当前5m严格信号 1只 · 无当前5m严格点 11只 · 历史边界敏感 2只 · 5m未收敛 1只，已失败关闭 · 上下文/1m差异 1只",
  );
  assert.equal(
    Ui.scanQualityText({
      ...staleSnapshot,
      available: true,
      data_quality: { complete: true, stale: false },
    }, runtimeHealth),
    "小样本结果完整",
  );
  assert.equal(
    Ui.dailyPreselectionText(runtimeHealth),
    "12只小样本验证 · 正在准备当前小样本",
  );
  for (const output of [
    Ui.scanCoverageText(staleSnapshot.scan_audit, staleSnapshot, runtimeHealth),
    Ui.dailyPreselectionText(runtimeHealth),
    Ui.priorityMonitorDiagnosticsText(runtimeHealth, {}),
  ]) {
    assert.doesNotMatch(output, /5086|5103|1664|全市场|全量扫描|自动继续/);
  }
  assert.equal(Ui.statusLabel("coverage_in_progress"), "当前范围扫描中");
  assert.match(
    controllerSource,
    /const cycleInProgress = !scopeFacts\.validation/,
  );
  assert.match(controllerSource, /if \(scopeFacts\.validation\)/);
  assert.match(controllerSource, /不会用 0\/0 冒充已覆盖/);
});

test("signal cards keep current 5m structures current regardless of age", () => {
  const Ui = loadUi();
  const staleBuy = {
    lifecycle_stage: "executable",
    side: "buy",
    setup_5m: { available_at: "2026-08-14T09:40:00+08:00" },
    observed_at: "2026-08-21T10:38:00+08:00",
  };
  const staleSell = { ...staleBuy, side: "sell" };

  assert.equal(
    Ui.signalCardLifecycleLabel(staleBuy, new Date("2026-08-21T10:38:00+08:00")),
    "强提示待人工复核",
  );
  assert.equal(
    Ui.signalCardLifecycleLabel(staleSell, new Date("2026-08-21T10:38:00+08:00")),
    "强提示待人工复核",
  );
  assert.equal(
    Ui.signalCardLifecycleLabel({
      lifecycle_stage: "executable",
      side: "buy",
      setup_5m: { terminal_segment_end_at: "2026-08-14T09:40:00+08:00" },
      observed_at: "2026-08-21T10:38:00+08:00",
    }, new Date("2026-08-21T10:38:00+08:00")),
    "强提示待人工复核",
  );
  assert.equal(
    Ui.signalCardLifecycleLabel({ ...staleBuy, setup_5m: { available_at: "2026-08-21T10:35:00+08:00" } }, new Date("2026-08-21T10:38:00+08:00")),
    "强提示待人工复核",
  );
  assert.equal(
    Ui.signalCardTimeText({ setup_5m: { available_at: "SIGNAL_AT" }, observed_at: "LATEST" }),
    "5m信号 SIGNAL_AT",
  );
  assert.equal(
    Ui.signalCardTimeText({
      setup_5m: {
        status: "confirmed",
        available_at: "SIGNAL_AT",
      },
      monitor_observed_at: "LATEST",
    }),
    "5m信号 SIGNAL_AT · 复查 LATEST",
  );
  assert.equal(
    Ui.signalCardTimeText({
      setup_5m: {
        status: "provisional",
        formation_state: "forming",
        anchor_at: "ANCHOR_AT",
        available_at: "DATA_AT",
      },
      observed_at: "FIRST_SEEN_AT",
      monitor_observed_at: "LATEST",
    }),
    "5m候选锚点 ANCHOR_AT · 数据截止 DATA_AT · 复查 LATEST",
  );
  assert.equal(
    Ui.signalCardTimeText({
      setup_5m: {
        status: "provisional",
        formation_state: "geometry_ready",
        terminal_segment_end_at: "STRUCTURE_AT",
        terminal_segment_available_at: "GEOMETRY_AT",
      },
    }),
    "5m候选结构 STRUCTURE_AT · 几何可用 GEOMETRY_AT",
  );
  assert.equal(
    Ui.signalCardTimeText({ setup_5m: { terminal_segment_end_at: "STRUCTURE_AT" }, observed_at: "LATEST" }),
    "5m结构 STRUCTURE_AT",
  );
  assert.equal(
    Ui.signalCardTimeText({ monitor_observed_at: "LATEST" }),
    "最近复查 LATEST",
  );
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

test("sector radar uses a stable business order for unranked rows", () => {
  const Ui = loadUi();
  const sectors = [
    { sector_id: "blocked", sector_name: "阻断", rank: null, hard_block: true, horizontal_strength: 99 },
    { sector_id: "quiet", sector_name: "无信号", rank: null, hard_block: false, horizontal_strength: 8 },
    { sector_id: "weak", sector_name: "有信号弱", rank: null, hard_block: false, horizontal_strength: 2 },
    { sector_id: "strong", sector_name: "有信号强", rank: null, hard_block: false, horizontal_strength: 7 },
    { sector_id: "ranked", sector_name: "正式排名", rank: 3, hard_block: false, horizontal_strength: 1 },
  ];
  const signals = [
    { signal_id: "a", sector: { sector_id: "weak" } },
    { signal_id: "b", sector: { sector_id: "strong" } },
  ];

  assert.deepEqual(
    Ui.sortSectorRowsForRadar(sectors, signals).map((row) => row.sector_id),
    ["ranked", "strong", "weak", "quiet", "blocked"],
  );
  assert.match(template, /有效结构名次优先；未排名板块再按非阻断、有当前线索、横向强度和名称排序/);
  assert.match(template, /人工复核优先级、生命周期、板块结构名次、买卖点类型和代码稳定排序/);
});

test("sector workspace defaults to top ten while retaining an off-list selection", () => {
  const Ui = loadUi();
  const workspace = fakeChartRoot();
  const sectors = Array.from({ length: 12 }, (_, index) => ({
    sector_id: `sector-${index + 1}`,
    sector_name: `板块 ${index + 1}`,
    rank: index + 1,
  }));
  const value = { scan_audit: { selected_sector_count: 12 }, signals: [], sectors };

  Ui.renderSectorWorkspace(workspace.root, value, "sector-12", undefined, {
    expanded: false,
    limit: 10,
  });
  assert.equal(workspace.root.dataset.expanded, "false");
  assert.equal(workspace.root.children.length, 11);
  assert.equal(workspace.root.children.at(-1).dataset.sectorId, "sector-12");

  Ui.renderSectorWorkspace(workspace.root, value, "sector-12", undefined, {
    expanded: true,
    limit: 10,
  });
  assert.equal(workspace.root.dataset.expanded, "true");
  assert.equal(workspace.root.children.length, 13);
});

test("signal lifecycle selects the analysis-first default chart and honest decision", () => {
  const Ui = loadUi();

  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "observed" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "approaching" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "armed" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "triggered" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "executable" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "active" }), "5m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "unknown" }), "5m");

  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[0]).title, "5分钟一买结构仍在形成");
  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[1]).title, "强提示待人工复核");
  assert.deepEqual(
    Ui.decisionSummaryForSignal({ lifecycle_stage: "armed", setup_5m: {} }),
    {
      tone: "waiting",
      title: "旧版等待态，下一次计算将迁移",
      detail: "等待剩余结构条件",
      invalidation: "未提供",
      structuralStop: "未提供",
      riskMultiplier: "未提供",
      positionRecommendation: "结构风险参考待人工核对",
    },
  );
});

test("period path and evidence groups separate established missing blocking and risk facts", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[0],
    higher_timeframe_risk: {},
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
    segment_difference_1m: null,
    decision_reasons: ["one_minute_not_confirmed", "lower_or_unrelated_structure_risk"],
  };

  assert.equal(Ui.reasonLabel("confirmed_buy_structure"), "买入方向结构已确认");
  assert.equal(
    Ui.reasonLabel("QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"),
    "高级别历史研究窗口不足 480 根已完成日线，仅作审计提示",
  );
  assert.equal(
    Ui.reasonLabel("QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"),
    "高级别历史研究完整前缀与 320 根后缀结论不一致，仅作审计提示",
  );
  assert.equal(Ui.reasonLabel("unmapped_code"), "诊断代码：unmapped_code");
  assert.equal(
    Ui.reasonLabel("MARKET_GATE_UNRESOLVED"),
    "市场高级别研究状态尚未解决，仅作环境提示",
  );
  assert.equal(
    Ui.reasonLabel("projected_geometric_structure"),
    "使用未锁定末端线段投影结构，仅作候选观察",
  );
  assert.equal(Ui.statusLabel("AMBER"), "琥珀色（需复核）");
  assert.deepEqual(
    Ui.periodPathForSignal(signal).map(({ frequency, state, tone, summary, boundary }) => ({
      frequency, state, tone, summary, boundary,
    })),
    [
      {
        frequency: "d",
        state: "未知",
        tone: "unknown",
        summary: "日线结构证据未提供",
        boundary: "环境证据未提供",
      },
      {
        frequency: "30m",
        state: "支持",
        tone: "supportive",
        summary: "方向 向上 · 主导 一买 · 本周期线段中枢",
        boundary: "没有关键限制",
      },
      {
        frequency: "5m",
        state: "形成中",
        tone: "waiting",
      summary: "二买候选 · 严格笔→递归中枢 · 第 0 层 · 第 1 中枢",
        boundary: "失效价未提供",
      },
      {
        frequency: "1m",
        state: "等待1分钟区间套",
        tone: "neutral",
        summary: "尚未取得1分钟区间套（5分钟信号保留，精确执行未解锁）",
        boundary: "5分钟信号已可复核；未完成1分钟区间套前不生成执行比例",
      },
    ],
  );

  const withSegmentDifference = {
    ...signal,
    segment_difference_1m: {
      point_type: "1buy",
      recursive_level: 0,
      evidence_codes: [],
    },
    position_recommendation: { segment_difference_max_percent: "25" },
  };
  const segmentNode = Ui.periodPathForSignal(withSegmentDifference).find(
    ({ frequency }) => frequency === "1m",
  );
  assert.equal(
    segmentNode.boundary,
    "区间套证据已保留；定位边界需人工核对",
  );

  const groups = Ui.evidenceGroupsForSignal(signal);
  assert.deepEqual(groups.established, [
    "30分钟：买入方向结构已确认",
    "5分钟：底分型确认",
  ]);
  assert.deepEqual(groups.missing, [
    "5分钟：末端结构确认",
    "1分钟：区间套尚未出现（5分钟信号保留，精确执行未解锁）",
    "5分钟买点已确认，等待1分钟区间套精确定位",
  ]);
  assert.deepEqual(groups.blocking, [
    "较低或无关结构存在风险",
    "板块高级别来源字段不完整，不能据此解除风险门",
  ]);
  assert.deepEqual(groups.next, ["等待 5分钟设置闭合并确认"]);
  assert.deepEqual(groups.risk, [
    "5分钟失效价：未提供",
    "结构防守价：9.80",
    "买入风险缩放系数：×0.50（仅供结构模型比较）",
    "结构风险参考待人工核对",
    "市场1分钟会话证据：当前契约字段缺失 · 高周期环境不可判定，不关闭5分钟主信号",
    "板块1分钟会话证据：当前契约字段缺失 · 高周期环境不可判定，不关闭5分钟主信号",
    "个股1分钟会话证据：当前契约字段缺失 · 高周期环境不可判定，不关闭5分钟主信号",
  ]);
  assert.deepEqual(groups.raw, [
    "confirmed_buy_structure",
    "bottom_fractal_confirmed",
    "terminal_line_confirmed",
    "one_minute_not_confirmed",
    "lower_or_unrelated_structure_risk",
  ]);
});

test("evidence uses the five minute invalidation as the structural stop fallback", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[0],
    structural_stop: null,
    setup_5m: {
      ...snapshot.signals[0].setup_5m,
      invalidation_price: "9.87",
    },
  };

  const groups = Ui.evidenceGroupsForSignal(signal);

  assert.ok(groups.risk.includes("结构防守价：9.87"));
  assert.ok(!groups.risk.includes("结构防守价：未提供"));
});

test("currently emitted screening diagnostics all have human-readable labels", () => {
  const Ui = loadUi();
  const emittedCodes = [
    "QMT_SECTOR_ELIGIBLE_SCOPE",
    "QMT_SECTOR_TRIGGER",
    "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH",
    "MARKET_GATE_UNRESOLVED",
    "SECTOR_GATE_UNRESOLVED",
    "projected_geometric_structure",
    "geometry_confirmed_before_audit_lock",
    "formal_center",
    "formal_center_confirmation",
    "complete_leave",
    "complete_first_return",
    "lifecycle_not_actionable",
    "consolidation_divergence",
    "complete_adjacent_rebound",
    "confirmed_first_class_parent",
    "complete_first_pullback",
    "width_matched_entry_departure_legs",
    "confirmed_same_level_boundary",
    "macd_any_indicator_decay",
    "strength_source_macd",
    "formal_consolidation_movement",
    "single_center_consolidation",
    "prior_extreme_held",
    "macd_dif_extreme_decay",
    "comparison_leg_width_1",
    "macd_histogram_area_decay",
    "macd_histogram_peak_decay",
    "comparison_leg_width_3",
    "formal_trend",
    "two_separated_centers",
    "trend_divergence",
    "confirmed_lower_level_first_class_parent",
    "small_to_large_reversal",
    "live_first_pullback",
    "prior_extreme_currently_held",
    "live_first_return",
    "core_boundary_currently_held",
    "terminal_unit_locked",
    `sha256:${"a".repeat(64)}`,
  ];

  for (const code of emittedCodes) {
    assert.doesNotMatch(Ui.reasonLabel(code), /^诊断代码：/);
    assert.doesNotMatch(Ui.reasonLabel(code), /未收录的系统诊断项/);
  }
  assert.equal(
    Ui.reasonLabel("formal_center_confirmation"),
    "中枢证据继续固化",
  );
});

test("position diagnostics use account-free human-readable labels", () => {
  const Ui = loadUi();
  const labels = [
    "HARD_BLOCKED_NO_TRADE",
    "POSITION_RATIO_INPUT_UNRESOLVED",
    "STRUCTURAL_RISK_BUDGET_SIZED",
    "CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED",
    "STRUCTURAL_MODEL_CAP_REQUIRES_MANUAL_REVIEW",
    "SAME_OR_HIGHER_STRUCTURE_FULL_EXIT",
    "LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION",
    "SELL_STRUCTURE_RELATION_REQUIRED",
    "LEGACY_STRUCTURAL_RISK_MODEL_RATIO",
    "LEGACY_BUY_RESTRICTION_REQUIRES_REVIEW",
    "LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED",
  ].map(Ui.reasonLabel);

  for (const label of labels) {
    assert.doesNotMatch(label, /^诊断代码：/);
    assert.doesNotMatch(label, /账户|现金|持仓|仓位|组合热度/);
  }
});

test("new execution profile keeps adverse context advisory and visible", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[1],
    context_d: {
      direction: "down",
      disposition: "hostile",
      hard_block: true,
      dominant_point_type: "1sell",
      reason_codes: ["confirmed_sell_with_down_structure"],
      same_period_technical_evidence: {
        ma5: 9,
        ma10: 10,
        ma5_vs_ma10: "ma5_below_ma10",
        fractal_type: "top",
        fractal_state: "confirmed",
      },
    },
    setup_5m: { ...snapshot.signals[1].setup_5m, status: "confirmed" },
    segment_difference_1m: {
      ...snapshot.signals[1].segment_difference_1m,
      status: "confirmed",
    },
    decision_reasons: ["daily_structure_hostile", "SAME_PERIOD_CONTEXT_GRADE_C"],
    execution_profile: {
      structure_signal_confirmed: true,
      recommendation: "CAUTION",
      recommendation_label: "结构已触发，环境逆风或证据需人工复核",
      hard_blocked: false,
      hard_block_reason_codes: [],
      advisory_reason_codes: ["daily_structure_hostile", "SAME_PERIOD_CONTEXT_GRADE_C"],
      context_grade: "C",
      context_grade_label: "C级（逆风观察）",
    },
  };

  assert.equal(
    Ui.decisionSummaryForSignal(signal).title,
    "5分钟二买操作确认，末端结构已封存，谨慎人工复核",
  );
  const daily = Ui.periodPathForSignal(signal)[0];
  assert.equal(daily.state, "逆风提示");
  assert.equal(daily.tone, "waiting");
  assert.equal(daily.boundary, "仅降低环境等级，不否定5分钟操作确认");
  assert.match(daily.summary, /MA5 9 \/ MA10 10/);
  assert.match(daily.summary, /顶分型已确认/);
  const groups = Ui.evidenceGroupsForSignal(signal);
  assert.deepEqual(groups.blocking, []);
  assert.ok(groups.risk.includes("环境提示：日线结构逆风，仅降低等级"));
  assert.ok(groups.risk.includes("环境提示：日线与30分钟环境逆风，谨慎观察"));
});

test("higher-timeframe research diagnostics collapse to one non-blocking explanation", () => {
  const Ui = loadUi();
  const signal = {
    ...snapshot.signals[1],
    execution_profile: {
      recommendation: "CAUTION",
      context_grade: "B",
      hard_blocked: false,
      hard_block_reason_codes: [],
      advisory_reason_codes: [
        "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN",
        "MARKET_GATE_UNRESOLVED",
        "SECTOR_GATE_UNRESOLVED",
        "SYMBOL_GATE_UNRESOLVED",
        "M_COMPLETED_MA5_UNAVAILABLE",
        "W_CENTER_MAPPING_UNRESOLVED",
        "D_CENTER_MAPPING_UNRESOLVED",
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        "SAME_PERIOD_CONTEXT_GRADE_B",
      ],
    },
  };
  const groups = Ui.evidenceGroupsForSignal(signal);
  const summaries = groups.risk.filter((line) => line.startsWith("月/周/日高级别研究："));

  assert.equal(summaries.length, 1);
  assert.match(summaries[0], /不阻断5分钟买卖点/);
  assert.ok(groups.risk.includes("环境提示：日线与30分钟环境混合或中性"));
  assert.equal(groups.blocking.some((line) => /高级别研究/.test(line)), false);
});

test("a hard data-integrity cause is not repeated as an advisory hint", () => {
  const Ui = loadUi();
  const reason = "QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH";
  const signal = {
    ...snapshot.signals[1],
    lifecycle_stage: "triggered",
    decision_reasons: ["HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED", reason],
    execution_profile: {
      recommendation: "BLOCKED",
      hard_blocked: true,
      hard_block_reason_codes: [
        "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
        reason,
      ],
      advisory_reason_codes: [reason, "SAME_PERIOD_CONTEXT_GRADE_B"],
    },
  };
  const groups = Ui.evidenceGroupsForSignal(signal);

  assert.ok(groups.blocking.includes(
    "QMT 原生日线与1分钟派生日线的开高低收量不一致",
  ));
  assert.equal(groups.risk.includes(
    "环境提示：QMT 原生日线与1分钟派生日线的开高低收量不一致",
  ), false);
  assert.ok(groups.risk.includes("环境提示：日线与30分钟环境混合或中性"));
  assert.ok(groups.raw.includes(reason));
});

test("established facts and hard blockers stay in one visible evidence group", () => {
  const Ui = loadUi();
  const warmupReason = "5M:WARMUP_TAIL_DIVERGED";
  const exactCalendar = {
    status: "EXACT",
    native_daily_bar_count: 70,
    expected_calendar_session_count: 70,
    native_first_session: "2026-05-14",
    native_last_session: "2026-08-20",
  };
  const signal = {
    ...snapshot.signals[1],
    side: "buy",
    lifecycle_stage: "triggered",
    decision_reasons: [warmupReason],
    execution_profile: {
      recommendation: "BLOCKED",
      hard_blocked: true,
      hard_block_reason_codes: [warmupReason],
      advisory_reason_codes: [],
    },
    higher_timeframe_risk: {
      ...(snapshot.signals[1].higher_timeframe_risk || {}),
      market_native_daily_calendar_coverage_evidence: exactCalendar,
      symbol_native_daily_calendar_coverage_evidence: exactCalendar,
    },
    warmup: {
      converged: false,
      reason_codes: [warmupReason],
      by_frequency: [],
      difference_codes_by_frequency: [],
    },
    position_recommendation: {
      side: "buy",
      status: "BLOCKED",
      basis: "STALE_BUY_SIGNAL_NO_CHASE",
      recommended_percent: "0",
      recommended_ratio: "0",
      label: "本条买入不纳入操作计划：等待新的5分钟结构",
      reason_codes: ["BUY_SIGNAL_STALE_NO_CHASE"],
    },
  };
  const groups = Ui.evidenceGroupsForSignal(signal);
  const exactMarketLine = groups.established.find((line) => (
    line.startsWith("市场原生日线交易日覆盖：精确")
  ));
  const positionLine = Ui.positionRecommendationLabel(signal);

  assert.ok(exactMarketLine);
  assert.equal(groups.risk.includes(exactMarketLine), false);
  assert.ok(groups.blocking.includes("5分钟暖机双窗口尾部不一致"));
  assert.equal(groups.missing.includes("5分钟暖机双窗口尾部不一致"), false);
  assert.ok(groups.blocking.includes(positionLine));
  assert.equal(groups.risk.includes(positionLine), false);
});

test("hard blockers are displayed with their concrete cause", () => {
  const Ui = loadUi();
  const base = {
    ...snapshot.signals[1],
    lifecycle_stage: "triggered",
    execution_profile: {
      recommendation: "BLOCKED",
      recommendation_label: "结构或数据硬条件未通过",
      hard_blocked: true,
      hard_block_reason_codes: [],
      advisory_reason_codes: [],
    },
  };
  const withReasons = (hardBlockReasonCodes) => ({
    ...base,
    execution_profile: {
      ...base.execution_profile,
      hard_block_reason_codes: hardBlockReasonCodes,
    },
  });

  const dataBlocked = withReasons([
    "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
    "QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",
  ]);
  assert.equal(Ui.hardBlockSummaryForSignal(dataBlocked), "行情数据完整性未通过");
  assert.equal(Ui.decisionSummaryForSignal(dataBlocked).title, "行情数据完整性未通过");
  assert.match(Ui.decisionSummaryForSignal(dataBlocked).detail, /时间穿越/);

  const conflictBlocked = withReasons([
    "structure_conflict",
    "same_or_higher_structure_conflict",
  ]);
  assert.equal(
    Ui.decisionSummaryForSignal(conflictBlocked).title,
    "同级或更高级反向结构冲突",
  );

  const warmupBlocked = withReasons([
    "WARMUP_CONVERGENCE_GATE_FAILED",
    "5M:WARMUP_TAIL_DIVERGED",
  ]);
  assert.equal(
    Ui.decisionSummaryForSignal(warmupBlocked).title,
    "5分钟结构暖机尚未收敛",
  );
  assert.match(Ui.decisionSummaryForSignal(warmupBlocked).detail, /双窗口尾部不一致/);

  const clearanceBlocked = withReasons(["three_buy_lacks_tick_clearance"]);
  assert.equal(
    Ui.decisionSummaryForSignal(clearanceBlocked).title,
    "三买离开中枢的价格空间不足",
  );
});

test("position copy removes legacy account wording from every displayed state", () => {
  const Ui = loadUi();
  const genericBlocked = {
    side: "buy",
    lifecycle_stage: "triggered",
    position_recommendation: {
      side: "buy",
      status: "BLOCKED",
      basis: "NO_TRADE",
      recommended_percent: "0",
      label: "建议买入比例：0%（存在结构或数据硬阻断）",
      reason_codes: ["HARD_BLOCKED_NO_TRADE"],
    },
    execution_profile: {
      hard_blocked: true,
      hard_block_reason_codes: [
        "WARMUP_CONVERGENCE_GATE_FAILED",
        "5M:WARMUP_TAIL_DIVERGED",
      ],
    },
  };
  assert.equal(
    Ui.positionRecommendationLabel(genericBlocked),
    "本条买入不纳入操作计划：5分钟完整历史与对照窗口的活动买卖点不一致，等待重新收敛",
  );
  assert.doesNotMatch(
    Ui.positionRecommendationLabel(genericBlocked),
    /结构或数据硬阻断/,
  );

  const calendarBlocked = {
    ...genericBlocked,
    execution_profile: {
      hard_blocked: true,
      hard_block_reason_codes: [
        "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
        "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
      ],
    },
  };
  assert.equal(
    Ui.positionRecommendationLabel(calendarBlocked),
    "本条买入不纳入操作计划：原生日线与交易日历覆盖不一致，等待数据校验通过",
  );

  const invalidatedSell = {
    side: "sell",
    lifecycle_stage: "invalidated",
    position_recommendation: {
      side: "sell",
      status: "BLOCKED",
      basis: "NO_TRADE",
      recommended_percent: "0",
      label: "建议卖出比例：0%（存在结构或数据硬阻断）",
      reason_codes: ["HARD_BLOCKED_NO_TRADE"],
    },
    execution_profile: {
      hard_blocked: true,
      hard_block_reason_codes: ["structure_invalidated"],
    },
  };
  assert.equal(
    Ui.positionRecommendationLabel(invalidatedSell),
    "本条卖点结构已失效：不再计算卖出比例，结束本结构跟踪",
  );

  const relationUnknownSell = {
    side: "sell",
    lifecycle_stage: "triggered",
    position_recommendation: {
      side: "sell",
      status: "CONDITIONAL",
      basis: "STRUCTURAL_EXIT_LEVEL_REQUIRED",
      recommended_percent: null,
      segment_difference_max_percent: "25",
      label: "结构退出参考上限 25%",
      reason_codes: ["SELL_STRUCTURE_RELATION_REQUIRED"],
    },
  };
  const relationUnknownLabel = Ui.positionRecommendationLabel(relationUnknownSell);
  assert.equal(
    relationUnknownLabel,
    "结构退出参考：卖点与目标结构的级别关系待人工核对；同级或更高级别卖点按完整退出规则复核，低级别或不同结构仅作段差处理；关系未确认前不生成退出比例",
  );
  assert.doesNotMatch(relationUnknownLabel, /25%|参考上限/);

  const recommended = {
    side: "buy",
    position_recommendation: {
      side: "buy",
      status: "RECOMMENDED",
      basis: "ACCOUNT_EQUITY_UPPER_BOUND",
      recommended_percent: "8.54",
      label: "建议买入比例：账户权益的 8.54% 以内（现金、行业暴露和组合热度只可下调）",
      reason_codes: [
        "CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED",
        "PORTFOLIO_CAPS_REQUIRE_MANUAL_REVIEW",
      ],
    },
  };
  const recommendedLabel = Ui.positionRecommendationLabel(recommended);
  assert.equal(
    recommendedLabel,
    "结构风险参考比例：8.54% 以内（按当前价至5分钟防守位测算；仅作结构模型比较）",
  );
  assert.doesNotMatch(recommendedLabel, /账户|现金|持仓|仓位|虚拟|组合热度/);

  const HumanUi = loadHumanReviewUi();
  assert.equal(
    HumanUi.positionRecommendationLabel(recommended),
    recommendedLabel,
  );
  assert.equal(
    HumanUi.positionRecommendationLabel(genericBlocked),
    Ui.positionRecommendationLabel(genericBlocked),
  );

  const priceProtected = {
    ...snapshot.signals[1],
    signal_id: "price-protected",
    warmup: { converged: true, reason_codes: [] },
    execution_profile: {
      recommendation: "CAUTION",
      recommendation_label: "5分钟操作确认已出现，谨慎人工复核",
      context_grade: "B",
      hard_blocked: false,
      hard_block_reason_codes: [],
      advisory_reason_codes: [],
    },
    position_recommendation: {
      side: "buy",
      status: "BLOCKED",
      basis: "NO_TRADE",
      recommended_percent: "0",
      label: "建议买入比例：0%（存在结构或数据硬阻断）",
      reason_codes: ["BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR"],
    },
  };
  const summary = Ui.decisionSummaryForSignal(priceProtected);
  assert.equal(summary.tone, "blocked");
  assert.equal(summary.title, "当前价格触发追价保护");
  assert.equal(
    summary.detail,
    "本条买入不纳入操作计划：当前价已超过结构锚点的5%追价保护线，等待新的5分钟结构",
  );

  const groups = Ui.evidenceGroupsForSignal(priceProtected);
  assert.ok(groups.blocking.includes(summary.detail));
  assert.deepEqual(groups.next, ["不追价、不执行本条买入，等待新的5分钟结构"]);

  const reviewable = {
    ...priceProtected,
    signal_id: "reviewable",
    position_recommendation: {
      side: "buy",
      status: "RECOMMENDED",
      basis: "STRUCTURAL_RISK_MODEL_UPPER_BOUND",
      recommended_percent: "8.54",
      reason_codes: ["CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED"],
    },
  };
  assert.ok(
    Ui.reviewPriorityForSignal(priceProtected)
      < Ui.reviewPriorityForSignal(reviewable),
  );

  const signalList = fakeChartRoot();
  const card = Ui.renderSignalWorkspace(
    signalList.root,
    [priceProtected],
    priceProtected.signal_id,
  );
  const cardRisk = card.children.find(
    (child) => child.className === "es-signal-card__risk",
  );
  assert.equal(card.classList.contains("is-position-blocked"), true);
  assert.match(cardRisk.textContent, /当前价已超过结构锚点的5%追价保护线/);
  assert.doesNotMatch(cardRisk.textContent, /谨慎人工复核/);
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

test("market sector and symbol daily diagnostics remain distinct in the evidence panel", () => {
  const Ui = loadUi();
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "AMBER",
      sector_gate: "UNRESOLVED",
      symbol_gate: "UNRESOLVED",
      market_reason_codes: ["D_CENTER_MAPPING_UNRESOLVED"],
      sector_reason_codes: ["QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE"],
      symbol_reason_codes: ["QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"],
      reason_codes: [
        "D_CENTER_MAPPING_UNRESOLVED",
        "QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE",
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
      ],
      market_period_diagnostics: [
        {
          period: "D",
          state: "FORMED_UNRESOLVED",
          completed_bar_count: 12,
          evidence_bar_end: "2026-02-27T15:00:00+08:00",
          active_top_interval: [
            "2025-11-28T15:00:00+08:00",
            "2026-02-27T15:00:00+08:00",
          ],
          mapping_unique: false,
          mapped_center_id: null,
          mapping_candidate_ids: [],
          blocker_codes: [
            "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",
          ],
          warning_codes: [],
          mapping_supply: {
            classification: "ONLY_THIRD_CLASS_POINTS",
            lower_structure_available: true,
            point_evidence_count: 5,
            point_type_counts: {
              "1sell": 0, "2sell": 0, "3sell": 2, "3buy": 3,
            },
            completed_sell12_count: 0,
            in_top_interval_sell12_count: 0,
            completed_in_top_interval_sell12_count: 0,
            incomplete_in_top_interval_sell12_count: 0,
            outside_top_interval_sell12_count: 0,
            highest_candidate_center_count: 0,
          },
        },
      ],
      sector_period_diagnostics: [],
      symbol_period_diagnostics: [],
      session_evidence_contract_id: "chanlun-higher-timeframe-session-evidence",
      market_session_evidence: {
        contract_id: "chanlun-higher-timeframe-session-evidence",
        status: "EXACT",
        issue_count: 0,
        issues: [],
        entry_disposition: "NO_SESSION_BLOCKER",
      },
      sector_session_evidence: {
        contract_id: "chanlun-higher-timeframe-session-evidence",
        status: "UNAVAILABLE",
        issue_count: 0,
        issues: [],
        entry_disposition: "FAIL_CLOSED",
      },
      symbol_session_evidence: {
        contract_id: "chanlun-higher-timeframe-session-evidence",
        status: "EXACT",
        issue_count: 1,
        issues: [
          {
            session: "2026-07-23",
            code: "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            observed_rows: 0,
            classification: "UNCLASSIFIED_EXPECTED_SESSION_ABSENCE",
            detail: "trading-calendar session is absent from the QMT 1m prefix",
            historical_trade_status_proven: false,
            entry_disposition: "FAIL_CLOSED",
          },
        ],
        entry_disposition: "FAIL_CLOSED",
      },
      warmup_evidence_contract_id: "chanlun-qmt-mwd-warmup-evidence",
      market_warmup_evidence: {
        contract_id: "chanlun-qmt-mwd-warmup-evidence",
        required_daily_bar_count: 480,
        full_daily_bar_count: 480,
        suffix_daily_bar_count: 320,
        converged: true,
        reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
        full_signature: `sha256:${"1".repeat(64)}`,
        suffix_signature: `sha256:${"1".repeat(64)}`,
        entry_disposition: "NO_WARMUP_BLOCKER",
      },
      sector_warmup_evidence: null,
      symbol_warmup_evidence: null,
      native_daily_reconciliation_contract_id:
        "chanlun-qmt-native-daily-reconciled-with-one-minute",
      market_native_daily_reconciliation_evidence: {
        contract_id: "chanlun-qmt-native-daily-reconciled-with-one-minute",
        symbol: "SH.000300",
        observed_at: "2026-07-23T14:30:00+08:00",
        native_daily_bar_count: 600,
        one_minute_daily_bar_count: 240,
        overlap_session_count: 240,
        first_overlap_session: "2025-08-01",
        last_overlap_session: "2026-07-23",
        price_tolerance_quanta: 1,
        max_observed_price_difference_quanta: 0,
        all_overlap_ohlcv_within_declared_tolerance: true,
        live_status: "LIVE_DISABLED",
      },
      sector_native_daily_reconciliation_evidence: null,
      symbol_native_daily_reconciliation_evidence: null,
      native_daily_calendar_coverage_contract_id:
        "chanlun-qmt-native-daily-calendar-coverage",
      market_native_daily_calendar_coverage_evidence: {
        status: "EXACT",
        native_daily_bar_count: 600,
        expected_calendar_session_count: 600,
        native_first_session: "2024-03-01",
        native_last_session: "2026-07-23",
        native_only_sessions: [],
        unexplained_calendar_only_sessions: [],
      },
      sector_native_daily_calendar_coverage_evidence: null,
      symbol_native_daily_calendar_coverage_evidence: {
        status: "UNEXPLAINED_CALENDAR_SESSION_MISSING",
        native_daily_bar_count: 599,
        expected_calendar_session_count: 600,
        native_first_session: "2024-03-01",
        native_last_session: "2026-07-23",
        native_only_sessions: [],
        unexplained_calendar_only_sessions: ["2026-07-23"],
      },
    },
  });

  assert.equal(
    groups.blocking.some((value) => value.includes("板块风险门 尚未解决")),
    true,
  );
  assert.equal(
    groups.blocking.includes(
      "市场风险门 琥珀色（需复核）：日线顶分型到30分钟中枢的映射未解决",
    ),
    true,
  );
  assert.equal(
    groups.blocking.includes(
      "个股风险门 尚未解决：QMT 1分钟同源序列缺少预期交易日",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场日线：尚未解决 · 完成K线 12 · 活动顶分型 2025/11/28 15:00 至 2026/02/27 15:00 · 证据截止 2026/02/27 15:00 · 映射未解决（候选 0） · 映射供给 只有三类点，缺少形成分型的一/二类卖点 · 低级别点 5（一卖 0 / 二卖 0 / 三卖 2 / 三买 3）· 分型内一二卖 0 / 已完成中枢 0 · 高级别顶分型区间内没有含一卖或二卖的已完成次级别中枢",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "个股1分钟缺失交易日 2026-07-23：观测 0 根 · 历史停牌状态未获认证 · 不自动填补 · 高周期环境失败关闭，不关闭5分钟主信号",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场高级别历史暖机（仅审计）：一致 · 完整 480 根日线 / 对照后缀 320 根 · 要求 480 根 · 高级别历史研究双窗口复算一致 · 不参与买入放行",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场原生日线左历史复核：通过 · 原生日线 600 根 / 1分钟派生日线 240 根 · 重叠 240 个交易日（2025-08-01 至 2026-07-23）· 容许价差 1 个量化单位 / 实测最大 0 · 原生日线只补左历史，30分钟仍由1分钟派生 · 不自动下单",
    ),
    true,
  );
  assert.equal(
    groups.established.includes(
      "市场原生日线交易日覆盖：精确 · 原生日线 600 根 / 日历应有 600 个交易日 · 2024-03-01 至 2026-07-23 · 前缀内无交易日缺口",
    ),
    true,
  );
  const hasSymbolCalendarGap = (value) => (
    value.includes(
      "个股原生日线交易日覆盖：尚未解决",
    )
    && value.includes("日历有而日线缺 1 日（2026-07-23）")
    && value.includes("缺失未证明为停牌、不自动填补 · 失败关闭")
  );
  assert.equal(
    groups.blocking.some(hasSymbolCalendarGap),
    true,
    JSON.stringify(groups.blocking, null, 2),
  );
  assert.equal(groups.risk.some(hasSymbolCalendarGap), false);
  assert.equal(groups.raw.includes("D_CENTER_MAPPING_UNRESOLVED"), true);
  assert.equal(
    groups.raw.includes(
      "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",
    ),
    true,
  );
});

test("sector native-daily research bridge is disclosed and can never look green", () => {
  const Ui = loadUi();
  const higherTimeframeRisk = {
    market_gate: "GREEN",
    sector_gate: "AMBER",
    symbol_gate: "GREEN",
    market_reason_codes: [],
    sector_reason_codes: [
      "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE",
    ],
    symbol_reason_codes: [],
    reason_codes: [
      "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE",
    ],
    sector_higher_timeframe_source_mode:
      "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH",
    sector_strict_same_5m_warmup_evidence: {
      required_daily_bar_count: 480,
      full_daily_bar_count: 240,
      suffix_daily_bar_count: 160,
      converged: false,
      reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
    },
    sector_strict_same_5m_source_coverage_evidence: sectorSameBaseCoverage(),
    sector_research_bridge_parameter_set_id: `sha256:${"7".repeat(64)}`,
  };
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: higherTimeframeRisk,
  });

  assert.equal(
    groups.risk.includes(
      "板块研究数据来源：QMT 原生日线用于长期历史审计；30分钟仍由同一5分钟基底派生，当前执行只使用日线与30分钟",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "研究限制：原生日线与5m/30m非线性聚合尚未调和 · 仅供研究 · 绿色结论最多降为琥珀色 · 不自动下单",
    ),
    true,
  );
  assert.equal(
    groups.risk.some((value) => value.startsWith("研究桥参数：sha256:")),
    true,
  );
  assert.equal(
    groups.risk.some((value) => value.includes("尚缺 240 个完整交易日")),
    true,
  );
  assert.equal(
    groups.blocking.includes("板块研究桥不能产生绿色结论；当前绿色字段矛盾，继续失败关闭"),
    false,
  );

  const forgedGreen = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: { ...higherTimeframeRisk, sector_gate: "GREEN" },
  });
  assert.equal(
    forgedGreen.blocking.includes(
      "板块研究桥不能产生绿色结论；当前绿色字段矛盾，继续失败关闭",
    ),
    true,
  );
});

test("strict same-base sector source passes and incomplete source fails closed", () => {
  const Ui = loadUi();
  const strict = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "GREEN",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: [],
      symbol_reason_codes: [],
      reason_codes: [],
      sector_higher_timeframe_source_mode: "PAGE_PARITY_SAME_5M_BASE",
      sector_strict_same_5m_warmup_evidence: {
        required_daily_bar_count: 480,
        full_daily_bar_count: 480,
        suffix_daily_bar_count: 320,
        converged: true,
        reason_code: "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
      },
      sector_strict_same_5m_source_coverage_evidence: sectorSameBaseCoverage({
        converged: true,
        fullCount: 480,
      }),
      sector_research_bridge_parameter_set_id: null,
    },
  });
  assert.equal(
    strict.risk.includes(
      "板块研究数据来源：严格同一5m基底；日线与30分钟均由该基底因果派生，当前执行只使用日线与30分钟",
    ),
    true,
  );
  assert.equal(strict.risk.some((value) => value.includes("研究限制")), false);
  assert.equal(
    strict.risk.some((value) => value.includes("尚缺 0 个完整交易日")),
    true,
  );

  const incomplete = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "GREEN",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: [],
      symbol_reason_codes: [],
      reason_codes: [],
    },
  });
  assert.equal(
    incomplete.risk.some((value) => value.includes("板块高级别来源")),
    false,
  );
  assert.equal(
    incomplete.blocking.some((value) => value.includes("板块高级别来源字段")),
    true,
  );

  const partial = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "GREEN",
      symbol_gate: "GREEN",
      market_reason_codes: [],
      sector_reason_codes: [],
      symbol_reason_codes: [],
      reason_codes: [],
      sector_research_bridge_parameter_set_id: `sha256:${"8".repeat(64)}`,
    },
  });
  assert.equal(
    partial.blocking.includes("板块高级别来源字段不完整，不能据此解除风险门"),
    true,
  );
});

test("missing current session evidence is explicit and not reviewable", () => {
  const Ui = loadUi();
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "GREEN",
      sector_gate: "GREEN",
      symbol_gate: "UNRESOLVED",
      market_reason_codes: [],
      sector_reason_codes: [],
      symbol_reason_codes: ["QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"],
      reason_codes: ["QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"],
      market_period_diagnostics: [],
      sector_period_diagnostics: [],
      symbol_period_diagnostics: [],
    },
  });

  assert.equal(
    groups.risk.includes(
      "个股1分钟会话证据：当前契约字段缺失 · 高周期环境不可判定，不关闭5分钟主信号",
    ),
    true,
  );
});

test("warmup convergence exposes every physical timeframe and translated causes", () => {
  const Ui = loadUi();
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    warmup: {
      converged: false,
      by_frequency: [
        { frequency: "d", converged: true, full_bar_count: 726, suffix_bar_count: 484 },
        { frequency: "30m", converged: false, full_bar_count: 1600, suffix_bar_count: 1067 },
        { frequency: "5m", converged: true, full_bar_count: 3200, suffix_bar_count: 2134 },
        { frequency: "1m", converged: false, full_bar_count: 4800, suffix_bar_count: 3200 },
      ],
      reason_codes: [
        "D:WARMUP_TAIL_STABLE",
        "30M:WARMUP_TAIL_DIVERGED",
        "5M:WARMUP_TAIL_STABLE",
        "1M:WARMUP_TAIL_DIVERGED",
      ],
      difference_codes_by_frequency: [
        { frequency: "d", difference_codes: [] },
        { frequency: "30m", difference_codes: ["WARMUP_DIRECTION_CHANGED"] },
        { frequency: "5m", difference_codes: [] },
        { frequency: "1m", difference_codes: ["WARMUP_ACTIVE_POINT_LANES_CHANGED"] },
      ],
    },
  });

  assert.equal(
    Ui.reasonLabel("30M:WARMUP_TAIL_DIVERGED"),
    "30分钟暖机双窗口尾部不一致",
  );
  assert.equal(
    groups.missing.includes("30分钟暖机双窗口尾部不一致"),
    true,
  );
  assert.equal(
    groups.missing.includes("1分钟暖机双窗口尾部不一致"),
    true,
  );
  assert.equal(
    groups.missing.includes("30分钟暖机差异：完整前缀与短前缀的当前走势方向不同"),
    true,
  );
  assert.equal(
    groups.missing.includes("1分钟暖机差异：完整前缀与短前缀的活动买卖点通道不同"),
    true,
  );
  assert.equal(
    groups.missing.includes("日线暖机双窗口尾部一致（非多前缀稳定证明）"),
    false,
  );
  assert.equal(
    groups.risk.includes("暖机30m：双窗口不一致 · 完整 1,600 根 / 对照后缀 1,067 根"),
    true,
  );
  assert.equal(
    groups.risk.includes("暖机口径：完整历史与去掉左侧三分之一后的后缀比较；当前门不证明多前缀稳定"),
    true,
  );
  assert.equal(groups.raw.includes("D:WARMUP_TAIL_STABLE"), true);
  assert.equal(groups.raw.includes("1M:WARMUP_TAIL_DIVERGED"), true);
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
    frequency: "5m",
    overrideSignalId: null,
  });

  const automaticallyTracked = Ui.resolveFocusState(initial, {
    ...snapshot.signals[0],
    lifecycle_stage: "triggered",
  });
  assert.equal(automaticallyTracked.frequency, "5m");
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
  assert.equal(view.node("[data-selected-stage]").textContent, "即将确认");
  assert.equal(view.node("[data-decision-title]").textContent, "5分钟二买结构仍在形成");
  assert.equal(view.node("[data-decision-invalidation]").textContent, "9.90");
  assert.equal(view.node('[data-period-state="30m"]').textContent, "支持");
  assert.equal(view.node('[data-period-state="5m"]').textContent, "形成中");
  assert.equal(view.node('[data-period-state="1m"]').textContent, "等待1分钟区间套");
  assert.deepEqual(
    view.node('[data-evidence-group="missing"]').children.map((node) => node.textContent),
    [
      "5分钟：末端结构确认",
      "1分钟：区间套尚未出现（5分钟信号保留，精确执行未解锁）",
      "5分钟买点已确认，等待1分钟区间套精确定位",
    ],
  );
  assert.equal(
    view.node("[data-chart-workbench]").getAttribute("href"),
    "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
  );
  assert.equal(
    view.node('[data-chart-link="5m"]').getAttribute("href"),
    "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF",
  );
  assert.equal(
    view.node('[data-chart-frame="5m"]').getAttribute("src"),
    "/?market=a&code=SZ.000001&layout=single&intervals=5&chart_sidebar=collapsed&default_study=MACD_HTF&chart_embed=decision-support",
  );
  assert.deepEqual(
    view.list("[data-focus-frequency]").map((node) => node.getAttribute("aria-pressed")),
    ["false", "false", "true", "false"],
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
  for (const frequency of ["d", "30m", "5m", "1m"]) {
    view.node(`[data-chart-frame="${frequency}"]`).setAttribute("src", `/stale-${frequency}`);
  }

  Ui.renderChartWorkspace(view.root, null);

  assert.equal(view.node("[data-chart-content]").hidden, false);
  assert.equal(view.node("[data-decision-title]").textContent, "数据未知");
  assert.equal(view.root.dataset.signalSide, "neutral");
  assert.deepEqual(
    ["d", "30m", "5m", "1m"].map((frequency) => (
      view.node(`[data-chart-frame="${frequency}"]`).getAttribute("src")
    )),
    ["about:blank", "about:blank", "about:blank", "about:blank"],
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
