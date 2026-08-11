"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const path = require("node:path");

const uiPath = path.resolve(__dirname, "../early_screening_ui.js");
const uiSource = fs.readFileSync(uiPath, "utf8");
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
  structure_contract_id: "physical-timeframe-l0",
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
      lifecycle_stage: "armed",
      observed_at: "2026-07-20T14:55:00+08:00",
      sector: { sector_id: "qmt-gics3:bank", sector_name: "银行" },
      context_30m: { direction: "up", disposition: "supportive" },
      setup_5m: { point_type: "1buy", center_ordinal: null },
      trigger_1m: null,
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
      trigger_1m: { point_type: "1buy" },
      structural_stop: "7.50",
      risk_multiplier: "1.00",
      entry_allowed: true,
      exit_allowed: false,
      decision_reasons: [],
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
  assert.match(template, /data-selection-scope="sector-trigger"/);
  assert.match(template, /data-selection-scope="all-qualified"/);
  assert.match(controllerSource, /function renderManualHoldings\(\)/);
  assert.match(controllerSource, /A_SHARE_STRICT_DECISION_CORE/);
  assert.match(controllerSource, /非A股辅助结构雷达监听中/);
  assert.match(controllerSource, /selectionScope: savedSelectionScope/);
  assert.match(controllerSource, /searchParams\.set\("scope", requestedScope\)/);
  assert.match(controllerSource, /QMT_SECTOR_TRIGGER/);
  assert.match(controllerSource, /snapshot\.manual_holding_signals/);
  assert.match(controllerSource, /当前休市 · 开市后自动恢复实时监听/);
  assert.match(dashboardCss, /\.es-holding-card\.is-alert/);
  assert.match(humanReviewSource, /sector_capture_receipts/);
  assert.match(humanReviewSource, /forward_operations/);
  assert.match(humanReviewSource, /forwardOperations\.qmt_runtime/);
  assert.match(humanReviewSource, /SCHEDULED_TASK_PRINCIPAL_MISMATCH/);
  assert.match(humanReviewSource, /FORWARD_SCHEDULER_NOT_READY_FOR_PAPER/);
  assert.match(humanReviewSource, /FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER/);
  assert.match(humanReviewSource, /SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER/);
  assert.match(humanReviewSource, /尚未获得同日 QMT Capture 回执/);
  assert.doesNotMatch(humanReviewSource, /09:10 后缺少同日 QMT Capture 回执/);
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
  assert.match(template, /id="hr-execution-evidence-status"/);
  assert.match(template, /id="hr-entry-selection-evidence-status"/);
  assert.match(template, /id="hr-portfolio-rejection-evidence-status"/);
  assert.match(template, /id="hr-portfolio-decision-audit-status"/);
  assert.match(template, /id="hr-portfolio-fill-decision-audit-status"/);
  assert.match(template, /id="hr-tactical-execution-status"/);
  assert.match(template, /固定 100 股（一手）仅观察；不覆盖多手部分成交/);
  assert.match(template, /id="hr-virtual-reserved-sell-quantity"/);
  assert.match(template, /id="hr-virtual-cancelled-count"/);
  assert.match(template, /id="hr-virtual-operations-cancelled-count"/);
  assert.match(template, /id="hr-paper-path-status"/);
  assert.match(template, /id="hr-paper-path-reasons"/);
  assert.match(humanReviewSource, /paperPathDecision/);
  assert.match(humanReviewSource, /HISTORICAL_SOURCE_REVIEW_ONLY/);
  assert.match(humanReviewSource, /SOURCE_SUPERSEDED_FOR_PAPER/);
  assert.match(humanReviewSource, /SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER/);
  assert.match(humanReviewSource, /CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER/);
  assert.match(humanReviewSource, /来源行情会话/);
  assert.match(humanReviewSource, /归档可做因果复核，但不能创建新的虚拟意图/);
  assert.match(humanReviewSource, /HIGHER_TIMEFRAME_GATE_NOT_GREEN/);
  assert.match(humanReviewSource, /NO_CAUSAL_1M_EXECUTION_BAR_REMAINS_BEFORE_TTL/);
  assert.match(humanReviewSource, /INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES/);
  assert.match(humanReviewSource, /虚拟意图已建立，等待后续合法 1m K 线/);
  assert.match(humanReviewSource, /在账本状态可验证前保持失败关闭/);
  assert.match(template, /候选报告自身零订单、零成交且哈希验证通过/);
  assert.match(template, /虚拟账本意图与成交另行展示/);
  assert.match(humanReviewSource, /候选报告自身零订单\/零成交/);
  assert.match(humanReviewSource, /虚拟账本累计/);
  assert.doesNotMatch(humanReviewSource, /QMT 板块先行 · 零订单\/零成交/);
  assert.match(template, /历史或已被新快照替代的报告只记录识别结果/);
  assert.match(template, /id="hr-pending-continuity-status"/);
  assert.match(template, /id="hr-execution-chronology-status"/);
  assert.match(template, /执行门处置/);
  assert.match(humanReviewSource, /执行门失败撤买，持久退出继续/);
  assert.match(
    humanReviewSource,
    /optional_buy_data_fault_cancelled/,
  );
  assert.match(
    humanReviewSource,
    /optional_buy_security_gate_cancelled/,
  );
  assert.match(
    humanReviewSource,
    /execution_fact_incomplete_optional_buy_cancelled/,
  );
  assert.match(
    humanReviewSource,
    /persistent_exit_security_blocked_remains_pending/,
  );
  assert.match(
    humanReviewSource,
    /persistent_exit_fact_incomplete_remains_pending/,
  );
  assert.match(humanReviewSource, /persistent_exit_independent_symbol_continues/);
  assert.match(template, /id="hr-paper-accounting-status"/);
  assert.match(template, /id="hr-paper-cash-balance"/);
  assert.match(template, /id="hr-paper-total-fees"/);
  assert.match(template, /id="hr-paper-valuation-status"/);
  assert.match(template, /id="hr-paper-market-value"/);
  assert.match(template, /id="hr-paper-equity"/);
  assert.match(humanReviewSource, /paper_execution_evidence/);
  assert.match(humanReviewSource, /paper_entry_selection_attestation/);
  assert.match(humanReviewSource, /paper_entry_selection_source_audit/);
  assert.match(humanReviewSource, /精确 QMT 目录/);
  assert.match(humanReviewSource, /paper_operations_cancellation_evidence/);
  assert.match(humanReviewSource, /paper_portfolio_rejection_evidence/);
  assert.match(humanReviewSource, /paper_portfolio_decision_audit/);
  assert.match(humanReviewSource, /paper_portfolio_fill_decision_audit/);
  assert.match(humanReviewSource, /terminal_signal_lifecycle_one_shot_enforced/);
  assert.match(humanReviewSource, /fixed_one_lot_tactical_review_only/);
  assert.match(humanReviewSource, /本页面不验证多手订单的部分成交行为/);
  assert.match(humanReviewSource, /sectorSourceDisclosure/);
  assert.match(humanReviewSource, /sector_higher_timeframe_evidence/);
  assert.match(humanReviewSource, /原生日线研究桥（AMBER上限）/);
  assert.match(humanReviewSource, /严格5m暖机/);
  assert.match(humanReviewSource, /marketSymbolHigherTimeframeDisclosure/);
  assert.match(humanReviewSource, /market_symbol_higher_timeframe_evidence/);
  assert.match(humanReviewSource, /M\/W\/D结构与来源可复核/);
  assert.match(humanReviewSource, /M\/W\/D结构可复核·来源未附/);
  assert.match(humanReviewSource, /M\/W\/D结构可复核·来源部分/);
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
  assert.match(humanReviewSource, /paper_pending_continuity/);
  assert.match(humanReviewSource, /CAUSAL_GAPS/);
  assert.match(humanReviewSource, /paper_accounting/);
  assert.match(humanReviewSource, /paper_valuation/);
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
  assert.match(humanReviewSource, /日终净值来源未验证/);
  assert.match(humanReviewSource, /日终净值连续性未验证/);
  assert.match(humanReviewSource, /日终净值序列缺失/);
  assert.match(humanReviewSource, /估值来源已接通 · 尚无日终净值点/);
  assert.match(humanReviewSource, /valuationStatus === "COMPLETE"/);
  assert.match(humanReviewSource, /日终净值点 · 截至/);
  assert.match(humanReviewSource, /latestValuation\.session/);
  assert.match(humanReviewSource, /OPEN_POSITIONS_UNMARKED/);
  assert.match(humanReviewSource, /CLOSED_BOOK_NO_DAILY_EQUITY/);
  assert.match(humanReviewSource, /virtual_reserved_sell_quantity/);
  assert.match(humanReviewSource, /virtual_cancelled_intent_count/);
  assert.match(humanReviewSource, /superseded_paper_intents/);
  assert.match(humanReviewSource, /已追加撤销/);
  assert.match(humanReviewSource, /paper_observation_eligible/);
  assert.match(humanReviewSource, /paper_reconciliation_pending/);
  assert.match(humanReviewSource, /paper_reconciliation_eligible/);
  assert.match(humanReviewSource, /feedbackMatchesLatest/);
  assert.match(humanReviewSource, /retryRequestId/);
  assert.match(humanReviewSource, /paperOption\.disabled = false/);
  assert.doesNotMatch(humanReviewSource, /paperOption\.disabled = !paperEligible/);
  assert.match(humanReviewSource, /同一请求 ID 幂等补建虚拟意图/);
  assert.match(humanReviewSource, /REVIEW_ONLY/);
  assert.match(humanReviewSource, /成交证据对象缺失/);
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
  assert.match(controllerSource, /full_coverage_refresh_paused/);
  assert.match(controllerSource, /full_coverage_next_active_at/);
  assert.match(controllerSource, /全市场覆盖等待下一运行窗口/);
  assert.match(controllerSource, /盘中算力正用于持仓、自选与强板块候选的实时预警/);
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
  assert.match(controllerSource, /Ui\.priorityMonitorText\(runtimeHealth, liveOverlay\)/);
  assert.match(template, /4 个自然日/);
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

test("human review carries sector M/W/D diagnostics and labels buy points diagnostic-only", () => {
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
    period: "M",
    state: "FORMED",
    completed_bar_count: 24,
    evidence_bar_end: "2026-07-31T15:00:00+08:00",
    mapping_unique: true,
    mapped_center_id: "M-center-1",
    mapping_candidate_ids: ["M-center-1"],
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
    states: { M: "FORMED", W: "NONE", D: "NONE" },
    reason_codes: ["SECTOR_MONTHLY_TOP_FORMED"],
    period_diagnostics: [active, inactive("W"), inactive("D")],
    evidence_id: `sha256:${"d".repeat(64)}`,
  };
  const sector = Ui.sectorSourceDisclosure({
    sector_higher_timeframe_evidence: sectorEvidence,
  });
  assert.match(sector.lines.join("\n"), /板块 M\/W\/D：M 顶部结构已形成/);
  assert.match(sector.lines.join("\n"), /板块M方向诊断：一买 2 \/ 二买 1/);
  assert.match(sector.lines.join("\n"), /稳定身份 3 个/);
  assert.match(sector.lines.join("\n"), /仅供人工识别，不参与卖点映射、风险门或订单/);
  assert.match(sector.lines.join("\n"), /板块W映射供给：无活动顶部结构/);
  assert.match(sector.lines.join("\n"), /板块当前来源多前缀暖机诊断：非单调/);
  assert.match(sector.lines.join("\n"), /严格5m同源多前缀暖机诊断/);
  assert.match(sector.lines.join("\n"), /合格前缀 4 个（480–960 根日线）/);
  assert.match(sector.lines.join("\n"), /仅诊断，不改变现有双窗口交易门/);
  assert.deepEqual(sector.factIds, [sectorEvidence.evidence_id]);

  const marketSymbol = Ui.marketSymbolHigherTimeframeDisclosure({
    market_symbol_higher_timeframe_evidence: {
      market: {
        gate: "AMBER",
        states: { M: "FORMED", W: "NONE", D: "NONE" },
        reason_codes: [],
        period_diagnostics: [active, inactive("W"), inactive("D")],
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
  assert.match(marketSymbol.lines.join("\n"), /市场M方向诊断：一买 2 \/ 二买 1/);
  assert.match(marketSymbol.lines.join("\n"), /市场M\/W\/D多前缀暖机诊断：非单调/);
  assert.match(marketSymbol.lines.join("\n"), /仅诊断，不改变现有双窗口交易门/);

  const invalid = Ui.sectorSourceDisclosure({});
  assert.match(invalid.lines.join("\n"), /当前契约要求的板块高级别证据/);
});

test("candidate QMT catalog gate is visible before virtual entry feedback", () => {
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
  assert.match(blocked.headline, /不建立虚拟意图/);
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
  }), "板块触发 + 持仓监控");
  assert.equal(ui.selectionLabelForSignal({
    sector_triggered: false,
    selection_sources: ["HOLDING_MONITOR"],
  }), "持仓监控");
  assert.deepEqual(
    ui.evidenceGroupsForSignal({
      selection_sources: ["ACTIVE_WATCHLIST_MONITOR"],
      sector_triggered: false,
    }).established,
    ["候选来源：自选监控"],
  );
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

test("stock selection and human review open on focused workloads without hiding channels", () => {
  assert.match(template, /data-point-type="buy"[^>]*aria-pressed="true"/);
  assert.match(template, /data-point-type="sell"[^>]*aria-pressed="false"/);
  assert.match(template, /data-point-type="all"[^>]*aria-pressed="false"/);
  assert.match(controllerSource, /pointType:\s*saved\.pointType\s*\|\|\s*"buy"/);
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
    /value="POSSIBLE_SELL_REVIEW">卖点待人工判断（30m退出或5m短差）/,
  );
  assert.match(
    humanReviewSource,
    /POSSIBLE_SELL_REVIEW:\s*"卖点待人工判断（30m退出或5m短差）"/,
  );
  assert.match(
    humanReviewSource,
    /HUMAN_TREND_TYPE_CONFIRMATION_INCOMPLETE:\s*"人工尚未确认走势类型"/,
  );
  assert.match(
    humanReviewSource,
    /WARMUP_CONVERGENCE_REQUIRED_FOR_VIRTUAL_ENTRY:\s*"暖机双窗口尚未一致，不能建立新的虚拟买入"/,
  );
  assert.match(
    humanReviewSource,
    /EXPECTED_REVIEW_LEVEL_30M_OR_5M:\s*"该卖点线索需人工确认是 30m 退出还是 5m 短差"/,
  );
});

test("normalizeSnapshot accepts only the new read-only schema", () => {
  const Ui = loadUi();
  const normalized = Ui.normalizeSnapshot(snapshot);

  assert.equal(normalized.schema, "chanlun-trading-screening");
  assert.equal(normalized.signals.length, 2);
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, schema: "chanlun-early-screening" }),
    /snapshot_schema_invalid/,
  );
  assert.throws(
    () => Ui.normalizeSnapshot({ ...snapshot, no_order_execution: false }),
    /snapshot_boundary_invalid/,
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
});

test("review display sorting uses lifecycle then sector rank without mutating facts", () => {
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
  ];
  const originalIds = rows.map((row) => row.signal_id);
  const sorted = Ui.sortSignalsForReview(rows, [
    { sector_id: "sector-1", rank: 1 },
    { sector_id: "sector-2", rank: null },
  ]);

  assert.deepEqual(sorted.map((row) => row.signal_id), [
    "triggered-unranked",
    "armed-ranked",
    "armed-unranked",
    "approaching-ranked",
  ]);
  assert.deepEqual(rows.map((row) => row.signal_id), originalIds);
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
  assert.equal(chart.root.dataset.signalId, selected.signal_id);
  assert.equal(chart.root.dataset.selectedCode, selected.code);
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

test("chart URLs keep fragments and never duplicate the requested MACD_HTF study", () => {
  const Ui = loadUi();
  const urls = Ui.chartUrlsForSignal({
    code: "SH.600000",
    chart_urls: {
      "d": "/?market=a&code=SH.600000&layout=single&intervals=D#daily",
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
    approaching: "即将确认",
    formed: "已形成",
    armed: "已入观察池",
    triggered: "1分钟已触发",
    executable: "强提示待人工复核",
    active: "持有跟踪",
    invalidated: "结构已失效",
    closed: "跟踪已结束",
  });
  assert.equal(Ui.lifecycleLabel("triggered"), "1分钟已触发");
  assert.equal(Ui.lifecycleLabel("unexpected-stage"), "未知状态");
  assert.match(template, /data-lifecycle="approaching"/);
  assert.match(template, /data-lifecycle="formed"/);
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
    }),
    "发现 10 · 完成 7 · 失败 3 · 成功率 70%",
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
    "发现 66 · 完成 56 · 资格排除 10 · 失败 0 · 成功率 84.8%",
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
  assert.match(controllerSource, /Ui\.scanCoverageText\(audit, snapshot\)/);
  assert.match(controllerSource, /Ui\.emptySignalDetail\(state\.snapshot, state\.query\)/);
  assert.match(controllerSource, /Ui\.scanQualityText\(snapshot\)/);
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
        "physical_timeframe_level_zero",
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

  const explicitFormed = { ...formed, lifecycle_stage: "formed" };
  assert.equal(Ui.lifecycleStageForSignal(formed), "approaching");
  assert.equal(Ui.lifecycleStageForSignal(explicitFormed), "formed");
  assert.equal(Ui.lifecycleStageForSignal(pending), "approaching");
  assert.equal(Ui.lifecycleStageForSignal(nonThird), "approaching");
  assert.equal(Ui.decisionSummaryForSignal(explicitFormed).tone, "waiting");
  assert.deepEqual(
    Ui.filterSignals([formed, explicitFormed], { lifecycle: "formed" }),
    [explicitFormed],
  );
  assert.equal(Ui.lifecycleLabel("formed"), "已形成");

  const normalized = Ui.normalizeSnapshot({
    ...snapshot,
    signals: [],
    manual_holding_signals: [explicitFormed],
  });
  assert.equal(normalized.manual_holding_signals[0].lifecycle_stage, "formed");
});

test("operator status copy explains degraded state without exposing internal codes", () => {
  const Ui = loadUi();
  const blockedHealth = {
    daily_preselection_ready: false,
    daily_preselection_status: "review_blocked",
    daily_preselection_reason_code: "HUMAN_REVIEW_MATERIALIZATION_FAILED",
    daily_preselection_candidate_count: 1664,
    daily_preselection_buy_candidate_count: 612,
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
  assert.match(diagnostic, /内部状态 review_blocked/);
  assert.match(diagnostic, /原因 HUMAN_REVIEW_MATERIALIZATION_FAILED/);
  assert.match(diagnostic, /结构线索 1664（买入 612）/);
  assert.doesNotMatch(diagnostic, /重放板块证据/);

  assert.equal(
    Ui.dailyPreselectionText({
      daily_preselection_ready: true,
      daily_preselection_status: "ready",
      daily_preselection_target_session: "2026-08-05",
      daily_preselection_candidate_count: 28,
      daily_preselection_buy_candidate_count: 17,
    }),
    "已就绪 · 适用 2026-08-05 · 买入线索 17 / 全部 28",
  );

  const monitorHealth = {
    priority_monitor_status: "verified",
    priority_monitor_last_code_count: 13,
    priority_monitor_reason_codes: ["READY"],
    notification_dispatcher_configured: true,
    priority_monitor_last_at: "LAST_RUN",
  };
  const monitorSummary = Ui.priorityMonitorText(monitorHealth, { signal_count: 0 });
  assert.equal(
    monitorSummary,
    "正常 · 复查 13 只 · 暂无新结构变化 · 通知已接通",
  );
  assert.doesNotMatch(monitorSummary, /verified|READY/);
  assert.match(
    Ui.priorityMonitorDiagnosticsText(monitorHealth, { signal_count: 0 }),
    /内部状态 verified · 原因 READY · 复查 13 只 · 结构变化 0 条/,
  );
  assert.equal(
    Ui.priorityMonitorText({ priority_monitor_status: "not_due" }, {}),
    "非交易时段 · 开盘后自动盯盘",
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
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "triggered" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "executable" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "active" }), "1m");
  assert.equal(Ui.defaultFrequencyForSignal({ lifecycle_stage: "unknown" }), "5m");

  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[0]).title, "等待 1分钟精确触发");
  assert.equal(Ui.decisionSummaryForSignal(snapshot.signals[1]).title, "强提示待人工复核");
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
    trigger_1m: null,
    decision_reasons: ["one_minute_not_confirmed", "lower_or_unrelated_structure_risk"],
  };

  assert.equal(Ui.reasonLabel("confirmed_buy_structure"), "买入方向结构已确认");
  assert.equal(
    Ui.reasonLabel("QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"),
    "月/周/日风险门历史不足 480 根已完成日线，已失败关闭",
  );
  assert.equal(
    Ui.reasonLabel("QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"),
    "月/周/日风险门完整前缀与 320 根后缀结论不一致，已失败关闭",
  );
  assert.equal(Ui.reasonLabel("unmapped_code"), "unmapped_code（未翻译）");
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
        boundary: "无硬阻断",
      },
      {
        frequency: "5m",
        state: "形成中",
        tone: "waiting",
        summary: "二买 · 老笔→线段中枢 · 本周期0级（非递归） · 第 1 中枢",
        boundary: "失效价未提供",
      },
      {
        frequency: "1m",
        state: "等待",
        tone: "waiting",
        summary: "尚未取得同向精确触发",
        boundary: "结构防守价 9.80",
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
  assert.deepEqual(groups.blocking, [
    "较低或无关结构存在风险",
    "板块高级别来源字段不完整，不能据此解除风险门",
  ]);
  assert.deepEqual(groups.next, ["等待 1分钟同向买卖点闭合"]);
  assert.deepEqual(groups.risk, [
    "5分钟失效价：未提供",
    "结构防守价：9.80",
    "风险乘数：0.50",
    "市场1分钟会话证据：当前契约字段缺失 · 失败关闭",
    "板块1分钟会话证据：当前契约字段缺失 · 失败关闭",
    "个股1分钟会话证据：当前契约字段缺失 · 失败关闭",
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

test("market sector and symbol MWD diagnostics remain distinct in the evidence panel", () => {
  const Ui = loadUi();
  const groups = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: {
      market_gate: "AMBER",
      sector_gate: "UNRESOLVED",
      symbol_gate: "UNRESOLVED",
      market_reason_codes: ["M_CENTER_MAPPING_UNRESOLVED"],
      sector_reason_codes: ["QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE"],
      symbol_reason_codes: ["QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"],
      reason_codes: [
        "M_CENTER_MAPPING_UNRESOLVED",
        "QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE",
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
      ],
      market_period_diagnostics: [
        {
          period: "M",
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
    groups.blocking.some((value) => value.includes("板块风险门 UNRESOLVED")),
    true,
  );
  assert.equal(
    groups.blocking.includes(
      "市场风险门 AMBER：月线顶分型到周线中枢的映射未解决",
    ),
    true,
  );
  assert.equal(
    groups.blocking.includes(
      "个股风险门 UNRESOLVED：QMT 1分钟同源序列缺少预期交易日",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场M：FORMED_UNRESOLVED · 完成K线 12 · 活动顶分型 2025/11/28 15:00 至 2026/02/27 15:00 · 证据截止 2026/02/27 15:00 · 映射未解决（候选 0） · 映射供给 只有三类点，缺少形成分型的一/二类卖点 · 低级别点 5（一卖 0 / 二卖 0 / 三卖 2 / 三买 3）· 分型内一二卖 0 / 已完成中枢 0 · 高级别顶分型区间内没有含一卖或二卖的已完成次级别中枢",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "个股1分钟缺失交易日 2026-07-23：观测 0 根 · 历史停牌状态未获认证 · 不自动填补 · 失败关闭",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场月/周/日暖机：一致 · 完整 480 根日线 / 对照后缀 320 根 · 要求 480 根 · 月/周/日风险门双窗口复算一致",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "市场原生日线左历史复核：通过 · 原生日线 600 根 / 1分钟派生日线 240 根 · 重叠 240 个交易日（2025-08-01 至 2026-07-23）· 容许价差 1 个量化单位 / 实测最大 0 · 原生日线只补左历史，30分钟仍由1分钟派生 · LIVE_DISABLED",
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
      "个股原生日线交易日覆盖：UNEXPLAINED_CALENDAR_SESSION_MISSING",
    )
    && value.includes("日历有而日线缺 1 日（2026-07-23）")
    && value.includes("缺失未证明为停牌、不自动填补 · 失败关闭")
  );
  assert.equal(
    groups.blocking.some(hasSymbolCalendarGap),
    true,
    JSON.stringify(groups.blocking, null, 2),
  );
  assert.equal(groups.risk.some(hasSymbolCalendarGap), true);
  assert.equal(groups.raw.includes("M_CENTER_MAPPING_UNRESOLVED"), true);
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
      "板块高级别来源：QMT 原生日线构造 M/W/D；30m 仍由同一5m基底派生",
    ),
    true,
  );
  assert.equal(
    groups.risk.includes(
      "研究限制：原生日线与5m/30m非线性聚合尚未调和 · 仅 RESEARCH_ONLY · GREEN 最多降为 AMBER · LIVE_DISABLED",
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
    groups.blocking.includes("板块研究桥不能产生 GREEN；当前绿色字段矛盾，继续失败关闭"),
    false,
  );

  const forgedGreen = Ui.evidenceGroupsForSignal({
    ...snapshot.signals[0],
    higher_timeframe_risk: { ...higherTimeframeRisk, sector_gate: "GREEN" },
  });
  assert.equal(
    forgedGreen.blocking.includes(
      "板块研究桥不能产生 GREEN；当前绿色字段矛盾，继续失败关闭",
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
      "板块高级别来源：严格同一5m基底；M/W/D 与 30m 均由该基底因果派生",
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
      "个股1分钟会话证据：当前契约字段缺失 · 失败关闭",
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
  assert.equal(view.node("[data-selected-stage]").textContent, "已入观察池");
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
