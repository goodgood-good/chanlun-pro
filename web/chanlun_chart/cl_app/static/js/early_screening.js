"use strict";

(function startTradingScreeningController() {
  // The monitor publishes on completed one-minute bars. Conditional requests
  // keep unchanged polls cheap while limiting visible shortlist lag to 15s.
  const POLL_INTERVAL_MS = 15_000;
  const SNAPSHOT_REQUEST_TIMEOUT_MS = 20_000;
  const MANUAL_QUOTE_TIMEOUT_MS = 8_000;
  const SIGNAL_QUOTE_TIMEOUT_MS = 8_000;
  const SIGNAL_QUOTE_BATCH_SIZE = 500;
  const SIGNAL_QUOTE_REFRESH_GUARD_MS = 30_000;
  const SIGNAL_QUOTE_SCHEDULE_DELAY_MS = 120;
  const CHART_SWITCH_TIMEOUT_MS = 30_000;
  const CHART_SWITCH_MAX_RELOADS = 1;
  const CHART_BRIDGE_VERSION = 1;
  const CHART_SWITCH_MESSAGE = "chanlun:chart-switch";
  const CHART_ACTIVITY_MESSAGE = "chanlun:chart-activity";
  const CHART_REALTIME_RESUME_MESSAGE = "chanlun:chart-realtime-resume";
  const CHART_BRIDGE_READY_MESSAGE = "chanlun:chart-bridge-ready";
  const CHART_DATA_READY_MESSAGE = "chanlun:chart-data-ready";
  const CHART_SWITCH_ERROR_MESSAGE = "chanlun:chart-switch-error";
  const SNAPSHOT_RECOVERY_RETRY_MS = 750;
  const STORAGE_KEY = "chanlun:trading-screening:view";
  const CHART_STREAM_SESSION_KEY = "chanlun:trading-screening:stream-page";
  const ACCOUNT_PREFERENCE_KEY = "trading_screening_view";
  // V8 invalidates views where the primary “全部” button could retain a narrow
  // point/stage/segment filter and therefore present a partial queue as all.
  const VIEW_CONTRACT = "CANONICAL_SIX_POINT_CHANNELS_V8_EXPLICIT_ALL_SIGNALS";

  function boot() {
    const Ui = globalThis.TradingScreeningUi;
    const Resize = globalThis.TradingScreeningChartResize;
    const root = document.getElementById("es-dashboard");
    if (!Ui || !Resize || !root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const byId = (id) => document.getElementById(id);
    const normalizeAttentionCode = (value) => Ui.text(value, "").trim().toUpperCase();
    const manualAttentionIdentityKey = (market, code) => (
      `${Ui.text(market, "").trim().toLowerCase()}|${normalizeAttentionCode(code)}`
    );
    const manualQuoteCache = new Map();
    const manualQuoteStatus = new Map();
    let manualQuoteGeneration = 0;
    const signalQuoteCache = new Map();
    const signalQuoteStatus = new Map();
    let signalQuoteGeneration = 0;
    let signalQuoteTimer = null;
    let signalQuoteRequestKey = "";
    let signalQuoteRequestedAt = 0;
    let snapshotRequestController = null;
    let chartRequestSequence = 0;
    const chartFrameTimeouts = new WeakMap();
    const chartStreamPageId = (() => {
      try {
        const saved = window.sessionStorage.getItem(CHART_STREAM_SESSION_KEY);
        if (/^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(saved || "")) return saved;
        const generated = typeof window.crypto?.randomUUID === "function"
          ? window.crypto.randomUUID()
          : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        window.sessionStorage.setItem(CHART_STREAM_SESSION_KEY, generated);
        return generated;
      } catch (_error) {
        return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      }
    })();
    const sectorList = byId("es-sector-list");
    const sectorExpand = byId("es-sector-expand");
    const liveWorkspaces = byId("es-live-workspaces");
    const signalWorkspace = byId("es-signal-workspace");
    const signalList = byId("es-signal-list");
    const chartWorkspace = byId("es-chart-workspace");
    const theaterSignalList = chartWorkspace && chartWorkspace.querySelector("[data-theater-signal-list]");
    const theaterSignalSearch = chartWorkspace && chartWorkspace.querySelector("[data-theater-signal-search]");
    const theaterSignalEmpty = chartWorkspace && chartWorkspace.querySelector("[data-theater-signal-empty]");
    const saved = readView();
    const pointFilters = [
      "all", "buy", "sell", "1buy", "2buy", "3buy", "1sell", "2sell", "3sell",
    ];
    const lifecycleFilters = [
      "all", "observed", "monitoring", "approaching", "triggered",
      "executable", "active",
    ];
    const savedSelectionScope = saved.selectionScope === "sector-trigger"
      ? "sector-trigger"
      : "all-qualified";
    const state = {
      snapshot: null,
      selectedSignalId: null,
      // 首次打开必须同时展示六类买卖点；用户主动选择的筛选条件仍会持久化。
      pointType: pointFilters.includes(saved.pointType) ? saved.pointType : "all",
      lifecycle: lifecycleFilters.includes(saved.lifecycle) ? saved.lifecycle : "all",
      market: ["a", "us"].includes(saved.market) ? saved.market : "all",
      signalSource: saved.signalSource === "holding"
        ? "attention"
        : ["screening", "notification", "attention", "watchlist"].includes(saved.signalSource)
          ? saved.signalSource
          : "all",
      reviewStage: ["forming", "notified", "tracking"].includes(saved.reviewStage)
        ? saved.reviewStage
        : "all",
      hideResearchOnly: saved.hideResearchOnly === true,
      segmentState: ["present", "current", "historical", "absent"].includes(saved.segmentState)
        ? saved.segmentState
        : "all",
      selectionScope: savedSelectionScope,
      sectorId: "all",
      sectorExpanded: false,
      query: "",
      layout: Ui.setChartLayout(chartWorkspace, saved.layout || "focus"),
      chartSizing: Resize.normalizeSizing(saved.chartSizing),
      focusState: Ui.resolveFocusState(null, null),
      evidenceOpen: false,
      theaterMode: false,
      signalListOpen: saved.signalListOpen !== false,
      theaterPointType: "all",
      theaterQuery: "",
      theaterSignalRenderLimit: 200,
      mode: root.dataset.defaultMode || "human-review",
      loading: false,
      pollTimer: null,
      chartSwitchInFlight: false,
      signalRenderLimit: 200,
      signalFilterKey: "",
      revealCurrentSegmentsAfterRender: false,
    };

    const resizeController = Resize.createController(chartWorkspace, state.chartSizing, {
      onChange(nextSizing) {
        state.chartSizing = nextSizing;
        saveView();
      },
    });

    const evidenceToggle = chartWorkspace && chartWorkspace.querySelector("[data-evidence-toggle]");
    const evidenceClose = chartWorkspace && chartWorkspace.querySelector("[data-evidence-close]");
    const theaterToggle = chartWorkspace && chartWorkspace.querySelector("[data-theater-toggle]");
    const theaterPickerToggle = chartWorkspace && chartWorkspace.querySelector("[data-theater-picker-toggle]");
    const signalListToggles = [...document.querySelectorAll("[data-signal-list-toggle]")];
    const theaterToolbarExit = chartWorkspace && chartWorkspace.querySelector("[data-theater-toolbar-exit]");
    state.signalListOpen = syncSignalListVisibility(state.signalListOpen);

    async function requestJson(endpoint, options) {
      const controller = new AbortController();
      snapshotRequestController = controller;
      const timeout = window.setTimeout(
        () => controller.abort(),
        SNAPSHOT_REQUEST_TIMEOUT_MS,
      );
      try {
        const response = await fetch(endpoint, { ...options, signal: controller.signal });
        return {
          response,
          payload: response.status === 304 ? null : await response.json(),
        };
      } catch (error) {
        if (error && error.name === "AbortError") {
          if (state.chartSwitchInFlight) {
            throw new Error("snapshot_request_deferred_for_chart");
          }
          throw new Error("snapshot_request_timeout");
        }
        throw error;
      } finally {
        window.clearTimeout(timeout);
        if (snapshotRequestController === controller) {
          snapshotRequestController = null;
        }
      }
    }

    async function waitForSnapshotRetry(response) {
      const retryAfter = Number(
        response && response.headers
          ? response.headers.get("Retry-After")
          : Number.NaN,
      );
      const delay = Number.isFinite(retryAfter) && retryAfter >= 0
        ? Math.min(2_000, Math.max(250, retryAfter * 1_000))
        : SNAPSHOT_RECOVERY_RETRY_MS;
      await new Promise((resolve) => window.setTimeout(resolve, delay));
    }

    function readView() {
      try {
        const accountPreferences = globalThis.AccountPreferences;
        const accountBound = Boolean(accountPreferences && accountPreferences.enabled);
        const raw = accountBound
          ? accountPreferences.getItem(ACCOUNT_PREFERENCE_KEY)
          : localStorage.getItem(STORAGE_KEY);
        if (!raw) return {};
        const value = JSON.parse(raw);
        if (!value || typeof value !== "object" || value.contract !== VIEW_CONTRACT) {
          if (accountBound) accountPreferences.removeItem(ACCOUNT_PREFERENCE_KEY);
          else localStorage.removeItem(STORAGE_KEY);
          return {};
        }
        return value;
      } catch (_error) {
        try {
          const accountPreferences = globalThis.AccountPreferences;
          if (accountPreferences && accountPreferences.enabled) {
            accountPreferences.removeItem(ACCOUNT_PREFERENCE_KEY);
          } else {
            localStorage.removeItem(STORAGE_KEY);
          }
        } catch (_storageError) {
          // 本地存储不可用时仍以实时快照为唯一事实来源。
        }
        return {};
      }
    }

    function saveView() {
      try {
        const serialized = JSON.stringify({
          contract: VIEW_CONTRACT,
          pointType: state.pointType,
          lifecycle: state.lifecycle,
          market: state.market,
          signalSource: state.signalSource,
          reviewStage: state.reviewStage,
          hideResearchOnly: state.hideResearchOnly,
          segmentState: state.segmentState,
          selectionScope: state.selectionScope,
          layout: state.layout,
          chartSizing: state.chartSizing,
          signalListOpen: state.signalListOpen,
        });
        const accountPreferences = globalThis.AccountPreferences;
        if (accountPreferences && accountPreferences.enabled) {
          accountPreferences.setItem(ACCOUNT_PREFERENCE_KEY, serialized);
        } else {
          localStorage.setItem(STORAGE_KEY, serialized);
        }
      } catch (_error) {
        // 本地存储不可用时仍以实时快照为唯一事实来源。
      }
    }

    function setText(id, value) {
      const node = byId(id);
      if (node) node.textContent = Ui.text(value);
    }

    function setStatus(kind, title, detail) {
      const dot = byId("es-status-dot");
      if (dot) dot.className = `es-status-dot is-${kind}`;
      setText("es-status", title);
      setText("es-status-detail", detail);
    }

    function syncCurrentSegmentAction() {
      const action = byId("es-show-current-segments");
      if (!action) return;
      const count = Math.max(0, Number(byId("es-segment-count")?.textContent) || 0);
      const active = count > 0 && state.segmentState === "current";
      action.disabled = count === 0;
      action.classList.toggle("is-active", active);
      action.setAttribute("aria-pressed", active ? "true" : "false");
      action.textContent = count === 0
        ? "当前暂无精确定位"
        : active ? "正在查看当前定位" : "查看当前定位";
    }

    function resetSignalFilters(segmentState = "all") {
      const scopeChanged = state.selectionScope !== "all-qualified";
      state.pointType = "all";
      state.lifecycle = "all";
      state.market = "all";
      state.signalSource = "all";
      state.reviewStage = "all";
      state.hideResearchOnly = false;
      state.segmentState = segmentState;
      state.selectionScope = "all-qualified";
      state.sectorId = "all";
      state.query = "";
      state.revealCurrentSegmentsAfterRender = segmentState === "current";
      const search = byId("es-signal-search");
      if (search) search.value = "";
      saveView();
      return scopeChanged;
    }

    function syncShowAllSignalsAction() {
      const action = byId("es-show-all-signals");
      if (!action) return;
      const active = state.pointType === "all"
        && state.lifecycle === "all"
        && state.market === "all"
        && state.signalSource === "all"
        && state.reviewStage === "all"
        && state.hideResearchOnly === false
        && state.segmentState === "all"
        && state.selectionScope === "all-qualified"
        && state.sectorId === "all"
        && Ui.text(state.query, "").trim() === "";
      const facts = Ui.signalQueueFacts(allQualifiedSignals("all"));
      action.classList.toggle("is-active", active);
      action.setAttribute("aria-pressed", active ? "true" : "false");
      action.dataset.count = facts.monitor_position_count > 0
        ? `${facts.structure_clue_count}+${facts.monitor_position_count}监听`
        : String(facts.structure_clue_count);
      action.setAttribute(
        "aria-label",
        `全部线索，${facts.structure_clue_count} 条5分钟结构线索，${facts.monitor_position_count} 个独立监听；点击清除全部队列筛选`,
      );
    }

    function revealCurrentSegmentResults() {
      state.revealCurrentSegmentsAfterRender = false;
      const workspace = signalList && signalList.closest("[data-workspace=\"signals\"]");
      if (!workspace) return;
      window.requestAnimationFrame(() => {
        workspace.scrollIntoView({ behavior: "smooth", block: "start" });
        const focusTarget = signalList.querySelector("button") || byId("es-signal-search");
        if (focusTarget) focusTarget.focus({ preventScroll: true });
      });
    }

    function snapshotHeader() {
      const snapshot = state.snapshot;
      if (!snapshot) return;
      const audit = snapshot.scan_audit && typeof snapshot.scan_audit === "object"
        ? snapshot.scan_audit
        : {};
      const quality = snapshot.data_quality;
      const runtimeHealth = snapshot.runtime_health && typeof snapshot.runtime_health === "object"
        ? snapshot.runtime_health
        : {};
      const scopeFacts = Ui.screeningScopeFacts(runtimeHealth, snapshot);
      const scopeLabel = Ui.screeningScopeLabel(runtimeHealth, snapshot);
      const completion = Ui.scanCoverageText(audit, snapshot, runtimeHealth);
      const sectorCompletion = Ui.sectorCoverageText(audit);
      const pending = Math.max(0, Number(audit.pending_symbol_count) || 0);
      const excluded = Math.max(
        0,
        Number(audit.coverage_cycle_excluded_symbol_count) || 0,
      );
      const sectorExcluded = Math.max(
        0,
        Number(audit.sector_excluded_count) || 0,
      );
      const cycleInProgress = !scopeFacts.validation
        && (pending > 0 || audit.coverage_cycle_complete === false);
      const errorCount = Array.isArray(snapshot.errors) ? snapshot.errors.length : 0;
      const failureCodes = Array.isArray(quality.failure_codes) ? quality.failure_codes : [];
      const runtimeReasons = Array.isArray(runtimeHealth.reasons)
        ? runtimeHealth.reasons.map(Ui.reasonLabel).join(" · ")
        : "选股后台健康状态不可用";
      const priorityMonitorWarning = runtimeHealth.priority_monitoring_enabled === true
        && runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.priority_monitor_ready !== true;
      const candidateMonitorWarning = runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.candidate_monitor_ready !== true;
      const candidateMonitorIdle = runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.candidate_monitor_status === "idle_no_candidates";
      const realtimeAlertWarning = runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.realtime_alert_ready !== true;
      const priorityMonitorReasons = Array.isArray(runtimeHealth.priority_monitor_reason_codes)
        ? runtimeHealth.priority_monitor_reason_codes.map(Ui.reasonLabel).join(" · ")
        : Ui.reasonLabel("PRIORITY_MONITOR_UNAVAILABLE");
      const candidateMonitorReasons = Array.isArray(runtimeHealth.candidate_monitor_reason_codes)
        && runtimeHealth.candidate_monitor_reason_codes.length
        ? runtimeHealth.candidate_monitor_reason_codes.map(Ui.reasonLabel).join(" · ")
        : Ui.statusLabel(runtimeHealth.candidate_monitor_status, "候选轮换状态待核对");
      const fullCoveragePaused = runtimeHealth.full_coverage_refresh_paused === true;
      const fullCoverageNextActive = Ui.timeText(
        runtimeHealth.full_coverage_next_active_at,
      );

      if (scopeFacts.validation) {
        const runtimeBlocked = runtimeHealth.required === true
          && runtimeHealth.ready !== true;
        const validationPreparing = priorityMonitorWarning || candidateMonitorWarning;
        const detail = runtimeBlocked
          ? `当前仅保留固定小样本；后台状态：${runtimeReasons}`
          : priorityMonitorWarning
            ? `固定小样本的分钟级优先复查尚未就绪：${priorityMonitorReasons}`
            : candidateMonitorWarning
              ? `固定小样本的5分钟候选仍在准备：${candidateMonitorReasons}`
              : candidateMonitorIdle
                ? "固定小样本当前没有实际监听对象；不会用 0/0 冒充已覆盖，也不会提前启动1分钟定位"
              : realtimeAlertWarning
                ? `固定小样本可查看；通知状态：${Ui.reasonLabel(runtimeHealth.realtime_alert_reason_code)}`
                : "代码修改阶段只处理固定验证范围，不读取旧扫描剩余队列。";
        setStatus(
          runtimeBlocked || realtimeAlertWarning ? "warning" : validationPreparing ? "loading" : "ready",
          scopeLabel,
          detail,
        );
      } else if (runtimeHealth.required === true && runtimeHealth.ready !== true) {
        setStatus(
          "warning",
          "后台选股扫描健康门未通过",
          `当前仍显示最后一份只读快照；${runtimeReasons}`,
        );
      } else if (priorityMonitorWarning) {
        setStatus(
          "warning",
          "盘中实时预警通道尚未就绪",
          `提前选股快照仍可查看；优先复查人工关注、自选和强板块候选的通道状态：${priorityMonitorReasons}`,
        );
      } else if (candidateMonitorWarning) {
        setStatus(
          "loading",
          "优先预警正常，候选范围仍在准备",
          `人工关注、自选和新鲜已有信号继续按分钟复查；5分钟支持板块候选：${candidateMonitorReasons}`,
        );
      } else if (realtimeAlertWarning) {
        setStatus(
          "warning",
          "实时通知保障尚未就绪",
          Ui.reasonLabel(runtimeHealth.realtime_alert_reason_code),
        );
      } else if (candidateMonitorIdle) {
        setStatus(
          "ready",
          "实时监听就绪但当前空闲",
          "当前没有通过板块门控、已有5分钟信号或人工关注范围进入监听的标的；不会产生通知，1分钟定位也不会提前启动",
        );
      } else if (
        (snapshot.scan_state === "complete" || snapshot.scan_state === "in_progress") &&
        !quality.stale
      ) {
        if (cycleInProgress) {
          if (fullCoveragePaused) {
            const liveLane = runtimeHealth.priority_monitor_session_open === true
              ? "盘中算力正用于人工关注、自选与强板块候选的实时预警"
              : "当前不在全市场覆盖运行窗口";
            setStatus(
              "loading",
              "全市场覆盖等待下一运行窗口",
              `已保留剩余 ${pending} 只；${liveLane}；将于 ${fullCoverageNextActive} 自动继续，无需保持页面打开。排除与失败原因见“运行诊断”。`,
            );
          } else {
            setStatus(
              quality.complete ? "loading" : "warning",
              "全周期扫描进行中",
              `本批结果已发布；后台正连续分析剩余 ${pending} 只，无需保持页面打开。排除与失败原因见“运行诊断”。`,
            );
          }
        } else {
          const excludedDetail = excluded
            ? `；${excluded} 只因历史不足未参与`
            : "";
          setStatus(
            quality.complete ? "ready" : "warning",
            quality.complete
              ? "结构雷达可用"
              : errorCount
                ? `结构雷达可用，${errorCount} 项数据待修复`
                : "结构雷达可用，数据质量待复核",
            `候选与盘中预警可查看${excludedDetail}；详细原因见“运行诊断”。`,
          );
        }
      } else if (snapshot.scan_state === "incomplete_not_published") {
        if (failureCodes.includes("sector_scan_completion_below_threshold")) {
          setStatus(
            "warning",
            "本轮板块结构质量不足，保留上一快照",
            `${sectorCompletion}；未达到发布门槛，不发布本轮不完整结果`,
          );
        } else {
          setStatus("warning", "本轮扫描未达到发布门槛", "继续显示上一份可验证快照，不发布不完整结果");
        }
      } else if (!snapshot.available) {
        setStatus("loading", "等待首次有效扫描", "后台正在准备原生板块与多周期结构数据");
      } else {
        setStatus("warning", "快照状态需要复核", Ui.statusLabel(snapshot.scan_state));
      }

      setText(
        "es-generated",
        Ui.fullDateTimeText(
          snapshot.presentation_market_data_as_of
            || snapshot.market_data_as_of
            || snapshot.as_of
            || snapshot.generated_at,
        ),
      );
      setText(
        "es-market-data-label",
        scopeFacts.validation
          ? "验证基线截止"
          : snapshot.presentation_data_mode === "INTRADAY_INCREMENTAL"
            ? "盘中增量计算截止"
            : "行情结构截止",
      );
      setText(
        "es-sector-scope",
        scopeFacts.validation
          ? `板块仅用于固定 ${scopeFacts.cohort || scopeFacts.effectiveLimit || 12} 只样本复核，不代表全市场成员已扫描`
          : "合格板块成员进入股票扫描",
      );
      setText("es-sector-completion", sectorCompletion);
      setText("es-completion", completion);
      setText(
        "es-scan-timing",
        scopeFacts.validation
          ? "验证范围固定，代码修改后仅复查小样本"
          : Ui.scanTimingText(audit),
      );
      setText("es-quality", Ui.scanQualityText(snapshot, runtimeHealth));
      setText("es-member-history", Ui.memberHistoryDiagnosticsText(snapshot));
      const liveOverlay = snapshot.priority_live_overlay
        && typeof snapshot.priority_live_overlay === "object"
        ? snapshot.priority_live_overlay
        : {};
      setText("es-preselection-status", Ui.dailyPreselectionText(runtimeHealth));
      setText(
        "es-preselection-diagnostic",
        Ui.dailyPreselectionDiagnosticsText(runtimeHealth),
      );
      setText(
        "es-priority-monitor-status",
        Ui.priorityMonitorText(runtimeHealth, liveOverlay, snapshot.us_monitor),
      );
      setText(
        "es-priority-monitor-diagnostic",
        Ui.priorityMonitorDiagnosticsText(
          runtimeHealth,
          liveOverlay,
          snapshot.us_monitor,
        ),
      );
      setText(
        "es-snapshot-diagnostic",
        `小板块资格排除 ${sectorExcluded} · 历史不足排除 ${excluded} · 真实失败 ${errorCount}`,
      );
      setText("es-sector-count", Ui.selectedSectorCount(snapshot));
      const unifiedSignals = Array.isArray(snapshot.unified_signals)
        ? snapshot.unified_signals
        : snapshot.signals;
      const queueFacts = Ui.signalQueueFacts(unifiedSignals);
      const pointDistribution = snapshot.point_distribution
        && typeof snapshot.point_distribution === "object"
        ? snapshot.point_distribution
        : Ui.pointDistributionForSignals(snapshot.signals);
      const distributionTotal = (bucket) => (
        bucket && typeof bucket === "object" ? Number(bucket.total) || 0 : 0
      );
      setText("es-signal-count", queueFacts.structure_clue_count);
      setText(
        "es-sector-trigger-count",
        Number(snapshot.sector_trigger_signal_count) || 0,
      );
      setText(
        "es-total-qualified-count",
        distributionTotal(pointDistribution.all_signals),
      );
      const currentSignals = unifiedSignals.filter(Ui.isCurrentSelectionSignal);
      setText(
        "es-approaching-count",
        distributionTotal(pointDistribution.candidate),
      );
      const fiveMinuteConfirmedCount = distributionTotal(
        pointDistribution.operational_confirmed,
      );
      setText("es-triggered-count", fiveMinuteConfirmedCount);
      setText(
        "es-audit-locked-count",
        distributionTotal(pointDistribution.audit_locked),
      );
      setText(
        "es-confirmed-point-distribution",
        Ui.pointDistributionCountText(pointDistribution.operational_confirmed),
      );
      setText(
        "es-candidate-point-distribution",
        Ui.pointDistributionCountText(pointDistribution.candidate),
      );
      const segmentDifferenceCount = currentSignals.filter(
        (signal) => Ui.currentSegmentDifferenceReadyForSignal(signal),
      ).length;
      setText("es-segment-count", segmentDifferenceCount);
      const preciseExecutionReadyCount = currentSignals.filter(
        (signal) => Ui.currentPreciseExecutionReadyForSignal(signal),
      ).length;
      setText("es-precise-count", preciseExecutionReadyCount);
      const showCurrentSegments = byId("es-show-current-segments");
      if (showCurrentSegments) {
        syncCurrentSegmentAction();
      }
      if (
        segmentDifferenceCount === 0
        && state.segmentState === "current"
      ) {
        // 当前定位只属于仍有效的 5 分钟候选。定位清空后不能让旧的正向筛选
        // 继续隐藏全部 5 分钟交易级别信号。
        state.segmentState = "all";
        saveView();
      }
      setText(
        "es-segment-scope",
        Ui.segmentScopeText(runtimeHealth, segmentDifferenceCount),
      );
      setText(
        "es-executable-count",
        distributionTotal(pointDistribution.executable),
      );
      document.title = queueFacts.structure_clue_count
        ? `(${queueFacts.structure_clue_count}) 缠论提前选股 · 实时盯盘与个股分析`
        : "缠论提前选股 · 实时盯盘与个股分析";
    }

    async function refreshManualAttentionQuotes(symbols) {
      const generation = ++manualQuoteGeneration;
      const groups = new Map();
      const activeKeys = new Set();
      for (const row of Array.isArray(symbols) ? symbols : []) {
        const market = Ui.text(row && row.market, "").trim().toLowerCase();
        const code = normalizeAttentionCode(row && row.code);
        if (!market || market === "a" || !code) continue;
        const key = manualAttentionIdentityKey(market, code);
        activeKeys.add(key);
        if (!groups.has(market)) groups.set(market, new Set());
        groups.get(market).add(code);
      }
      for (const key of manualQuoteCache.keys()) {
        if (!activeKeys.has(key)) manualQuoteCache.delete(key);
      }
      for (const market of manualQuoteStatus.keys()) {
        if (!groups.has(market)) manualQuoteStatus.delete(market);
      }
      if (!groups.size) return;

      for (const market of groups.keys()) manualQuoteStatus.set(market, "loading");
      const requests = Array.from(groups.entries()).map(async ([market, codeSet]) => {
        const codes = Array.from(codeSet);
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), MANUAL_QUOTE_TIMEOUT_MS);
        try {
          const response = await fetch("/ticks", {
            method: "POST",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
            body: new URLSearchParams({
              market,
              codes: JSON.stringify(codes),
            }),
            signal: controller.signal,
          });
          const payload = await response.json();
          if (generation !== manualQuoteGeneration) return;
          if (!response.ok || !payload || payload.ok !== true) {
            manualQuoteStatus.set(market, "unavailable");
            return;
          }
          if (payload.quote_state === "deferred") {
            manualQuoteStatus.set(market, "deferred");
            return;
          }

          const returnedCodes = new Set();
          const ticks = Array.isArray(payload.ticks) ? payload.ticks : [];
          for (const tick of ticks) {
            const code = normalizeAttentionCode(tick && tick.code);
            const price = Number(tick && tick.price);
            const rate = Number(tick && tick.rate);
            if (!codeSet.has(code) || !Number.isFinite(price) || price <= 0 || !Number.isFinite(rate)) {
              continue;
            }
            returnedCodes.add(code);
            manualQuoteCache.set(manualAttentionIdentityKey(market, code), { price, rate });
          }
          if (payload.market_state !== "closed") {
            for (const code of codes) {
              if (!returnedCodes.has(code)) {
                manualQuoteCache.delete(manualAttentionIdentityKey(market, code));
              }
            }
          }
          manualQuoteStatus.set(
            market,
            returnedCodes.size > 0
              ? "ready"
              : payload.market_state === "closed" ? "closed" : "unavailable",
          );
        } catch (_error) {
          if (generation === manualQuoteGeneration) {
            manualQuoteStatus.set(market, "unavailable");
          }
        } finally {
          window.clearTimeout(timeout);
        }
      });
      await Promise.allSettled(requests);
      if (generation === manualQuoteGeneration) renderManualAttention(false);
    }

    function signalQuoteForIdentity(marketValue, codeValue) {
      const market = Ui.text(marketValue, "").trim().toLowerCase();
      const code = normalizeAttentionCode(codeValue);
      const cached = signalQuoteCache.get(manualAttentionIdentityKey(market, code));
      const marketStatus = signalQuoteStatus.get(market) || "loading";
      if (!cached) return { status: marketStatus };
      const status = marketStatus === "loading"
        ? "refreshing"
        : marketStatus === "closed"
          ? "closed"
          : ["deferred", "partial", "unavailable"].includes(marketStatus)
            ? "stale"
            : "ready";
      return { ...cached, status };
    }

    function signalQuoteForSignal(signal) {
      return signalQuoteForIdentity(
        Ui.inferSignalMarket(signal),
        signal && signal.code,
      );
    }

    function refreshRenderedSignalQuotes() {
      for (const container of [signalList, theaterSignalList]) {
        if (!container || typeof container.querySelectorAll !== "function") continue;
        container.querySelectorAll("[data-signal-quote]").forEach((node) => {
          Ui.updateSignalQuoteNode(
            node,
            signalQuoteForIdentity(node.dataset.market, node.dataset.code),
          );
        });
      }
    }

    function buildSignalQuoteGroups(signals) {
      const groups = new Map();
      for (const signal of Array.isArray(signals) ? signals : []) {
        const market = Ui.inferSignalMarket(signal);
        const code = normalizeAttentionCode(signal && signal.code);
        if (!market || !code) continue;
        if (!groups.has(market)) groups.set(market, new Set());
        groups.get(market).add(code);
      }
      return groups;
    }

    function signalQuoteGroupsKey(groups) {
      return Array.from(groups.entries())
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([market, codes]) => (
          `${market}:${Array.from(codes).sort().join(",")}`
        ))
        .join("|");
    }

    async function refreshSignalQuotes(groups) {
      const generation = ++signalQuoteGeneration;
      const requests = [];
      for (const [market, codeSet] of groups.entries()) {
        signalQuoteStatus.set(market, "loading");
        const allCodes = Array.from(codeSet);
        for (let offset = 0; offset < allCodes.length; offset += SIGNAL_QUOTE_BATCH_SIZE) {
          const codes = allCodes.slice(offset, offset + SIGNAL_QUOTE_BATCH_SIZE);
          const requestedCodes = new Set(codes);
          requests.push((async () => {
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), SIGNAL_QUOTE_TIMEOUT_MS);
            try {
              const response = await fetch("/ticks", {
                method: "POST",
                credentials: "same-origin",
                headers: { Accept: "application/json" },
                body: new URLSearchParams({
                  market,
                  codes: JSON.stringify(codes),
                }),
                signal: controller.signal,
              });
              const payload = await response.json();
              if (generation !== signalQuoteGeneration) {
                return { market, state: "obsolete" };
              }
              if (!response.ok || !payload || payload.ok !== true) {
                return { market, state: "unavailable" };
              }
              if (payload.quote_state === "deferred") {
                return { market, state: "deferred" };
              }

              const returnedCodes = new Set();
              const ticks = Array.isArray(payload.ticks) ? payload.ticks : [];
              for (const tick of ticks) {
                const code = normalizeAttentionCode(tick && tick.code);
                const price = Number(tick && tick.price);
                const rate = Number(tick && tick.rate);
                if (
                  !requestedCodes.has(code)
                  || !Number.isFinite(price)
                  || price <= 0
                  || !Number.isFinite(rate)
                ) continue;
                returnedCodes.add(code);
                signalQuoteCache.set(
                  manualAttentionIdentityKey(market, code),
                  { price, rate },
                );
              }
              if (payload.market_state !== "closed") {
                for (const code of codes) {
                  if (!returnedCodes.has(code)) {
                    signalQuoteCache.delete(manualAttentionIdentityKey(market, code));
                  }
                }
              }
              return {
                market,
                state: returnedCodes.size > 0
                  ? "ready"
                  : payload.market_state === "closed" ? "closed" : "unavailable",
              };
            } catch (_error) {
              return { market, state: "unavailable" };
            } finally {
              window.clearTimeout(timeout);
            }
          })());
        }
      }
      refreshRenderedSignalQuotes();
      const settled = await Promise.allSettled(requests);
      if (generation !== signalQuoteGeneration) return;
      const statesByMarket = new Map();
      for (const result of settled) {
        if (result.status !== "fulfilled" || result.value.state === "obsolete") continue;
        if (!statesByMarket.has(result.value.market)) {
          statesByMarket.set(result.value.market, []);
        }
        statesByMarket.get(result.value.market).push(result.value.state);
      }
      for (const market of groups.keys()) {
        const states = statesByMarket.get(market) || ["unavailable"];
        const status = states.every((value) => value === "ready")
          ? "ready"
          : states.some((value) => value === "ready")
            ? "partial"
            : states.every((value) => value === "closed")
              ? "closed"
              : states.every((value) => value === "deferred")
                ? "deferred"
                : "unavailable";
        signalQuoteStatus.set(market, status);
      }
      refreshRenderedSignalQuotes();
    }

    function scheduleSignalQuoteRefresh(signals) {
      const groups = buildSignalQuoteGroups(signals);
      const requestKey = signalQuoteGroupsKey(groups);
      if (!requestKey) {
        if (signalQuoteTimer !== null) window.clearTimeout(signalQuoteTimer);
        signalQuoteTimer = null;
        signalQuoteRequestKey = "";
        return;
      }
      const now = Date.now();
      if (
        requestKey === signalQuoteRequestKey
        && now - signalQuoteRequestedAt < SIGNAL_QUOTE_REFRESH_GUARD_MS
      ) return;
      if (signalQuoteTimer !== null) window.clearTimeout(signalQuoteTimer);
      signalQuoteRequestKey = requestKey;
      signalQuoteRequestedAt = now;
      signalQuoteTimer = window.setTimeout(() => {
        signalQuoteTimer = null;
        void refreshSignalQuotes(groups);
      }, SIGNAL_QUOTE_SCHEDULE_DELAY_MS);
    }

    function renderManualAttention(refreshQuotes = true) {
      const snapshot = state.snapshot || {};
      const attention = snapshot.manual_attention && typeof snapshot.manual_attention === "object"
        ? snapshot.manual_attention
        : {};
      const symbols = Array.isArray(attention.symbols)
        ? attention.symbols.filter((row) => row && typeof row === "object")
        : [];
      const list = byId("es-holdings-list");
      const empty = byId("es-holdings-empty");
      const inferSignalMarket = (signal) => {
        const explicit = Ui.text(signal && signal.market, "").trim().toLowerCase();
        if (explicit) return explicit;
        const code = normalizeAttentionCode(signal && signal.code);
        return code.startsWith("SH.") || code.startsWith("SZ.") || code.startsWith("BJ.")
          ? "a"
          : code.endsWith(".US")
            ? "us"
            : "";
      };
      const stagePriority = {
        executable: 0,
        triggered: 1,
        active: 2,
        approaching: 3,
        observed: 4,
      };
      const signalsByIdentity = new Map();
      const attentionSignals = [
        ...(Array.isArray(snapshot.signals) ? snapshot.signals : []),
        ...(Array.isArray(snapshot.manual_attention_signals)
          ? snapshot.manual_attention_signals
          : []),
      ];
      for (const signal of attentionSignals) {
        if (!signal || typeof signal !== "object") continue;
        const code = normalizeAttentionCode(signal.code);
        if (!code) continue;
        const key = manualAttentionIdentityKey(inferSignalMarket(signal), code);
        const current = signalsByIdentity.get(key);
        const nextRank = stagePriority[Ui.lifecycleStageForSignal(signal)] ?? 99;
        const currentRank = current
          ? stagePriority[Ui.lifecycleStageForSignal(current)] ?? 99
          : Number.POSITIVE_INFINITY;
        if (!current || nextRank < currentRank) signalsByIdentity.set(key, signal);
      }

      const monitored = symbols.filter((row) => row.realtime_status === "monitoring").length;
      const waiting = symbols.filter((row) => row.realtime_status !== "monitoring").length;
      setText("es-holdings-declared", symbols.length);
      setText("es-holdings-monitored", monitored);
      setText("es-holdings-unsupported", waiting);

      if (attention.available !== true) {
        setText("es-holdings-status", "本地人工关注分组暂不可用。");
      } else if (symbols.length) {
        setText(
          "es-holdings-status",
          "A股使用统一选股决策核心；其他市场使用独立辅助结构雷达。该分组只决定优先监听范围。",
        );
      } else {
        setText(
          "es-holdings-status",
          "人工关注组由用户在行情页维护，只代表跨市场优先监听范围。",
        );
      }

      if (empty) {
        empty.hidden = symbols.length !== 0;
        empty.textContent = attention.available === true
          ? "人工关注组尚无标的；可在行情页把需要优先监听的标的加入该分组。"
          : "暂时无法读取本地人工关注分组。";
      }
      if (refreshQuotes) void refreshManualAttentionQuotes(symbols);
      if (!list) return;
      const fragment = document.createDocumentFragment();
      const alertStages = new Set(["approaching", "triggered", "executable", "active"]);
      const marketLabels = {
        a: "A股", hk: "港股", us: "美股", fx: "外汇", futures: "期货",
        ny_futures: "纽约期货", currency: "数字货币", currency_spot: "数字货币现货",
      };
      for (const symbolRow of symbols) {
        const market = Ui.text(symbolRow.market, "").trim().toLowerCase();
        const code = Ui.text(symbolRow.code, "").trim();
        const name = Ui.text(symbolRow.name, code);
        const signal = signalsByIdentity.get(manualAttentionIdentityKey(market, code)) || null;
        const stage = signal ? Ui.lifecycleStageForSignal(signal) : "";
        const item = document.createElement("li");
        const card = document.createElement("a");
        card.className = "es-holding-card";
        card.setAttribute(
          "href",
          `/?market=${encodeURIComponent(market)}&code=${encodeURIComponent(code)}`,
        );
        if (signal && alertStages.has(stage)) card.classList.add("is-alert");

        const heading = document.createElement("span");
        heading.className = "es-holding-card__identity";
        const strong = document.createElement("strong");
        strong.textContent = name;
        const symbol = document.createElement("code");
        symbol.textContent = `${marketLabels[market] || market || "未知市场"} · ${code}`;
        heading.append(strong, symbol);

        const status = document.createElement("span");
        status.className = "es-holding-card__status";
        const quote = document.createElement("span");
        quote.className = "es-holding-card__quote";
        const cachedQuote = manualQuoteCache.get(manualAttentionIdentityKey(market, code));
        const quoteAvailable = market === "a"
          ? symbolRow.quote_available === true
          : Boolean(cachedQuote);
        const price = Number(market === "a" ? symbolRow.current_price : cachedQuote && cachedQuote.price);
        const change = Number(market === "a" ? symbolRow.change_percent : cachedQuote && cachedQuote.rate);
        if (quoteAvailable && Number.isFinite(price) && price > 0 && Number.isFinite(change)) {
          const priceDigits = price < 10 ? 3 : 2;
          quote.textContent = `${price.toFixed(priceDigits)}  ${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
          quote.dataset.direction = change > 0 ? "up" : change < 0 ? "down" : "flat";
        } else {
          const quoteState = manualQuoteStatus.get(market);
          quote.textContent = quoteState === "loading"
            ? "行情读取中"
            : quoteState === "deferred"
              ? "行情请求合并中"
              : quoteState === "closed" ? "休市暂无报价" : "行情暂不可用";
        }
        const realtimeStatus = Ui.text(symbolRow.realtime_status, "awaiting_first_run");
        if (realtimeStatus === "error") {
          status.textContent = "实时监听异常 · 系统将重试";
          card.classList.add("is-alert");
        } else if (signal) {
          const point = Ui.pointLabelForSignal(signal);
          status.textContent = `${Ui.lifecycleLabel(stage)} · ${point}`;
        } else if (realtimeStatus === "market_closed") {
          status.textContent = "当前休市 · 开市后自动恢复实时监听";
        } else if (realtimeStatus === "warming_up") {
          status.textContent = "多周期历史暖机中 · 暂不产生提醒";
        } else if (realtimeStatus === "monitoring") {
          status.textContent = symbolRow.monitoring_scope === "A_SHARE_STRICT_DECISION_CORE"
            ? "统一决策核心实时监听中 · 暂无新增预警"
            : "非A股辅助结构雷达监听中 · 暂无新增线索";
        } else {
          status.textContent = "等待首次实时检查";
        }
        card.append(heading, quote, status);
        item.append(card);
        fragment.append(item);
      }
      list.replaceChildren(fragment);
    }

    function renderUsMonitorStatus() {
      const snapshot = state.snapshot || {};
      const monitor = snapshot.us_monitor && typeof snapshot.us_monitor === "object"
        ? snapshot.us_monitor
        : {};
      const symbols = Array.isArray(monitor.symbols)
        ? monitor.symbols.filter((row) => row && typeof row === "object")
        : [];
      const notificationEvents = snapshot.realtime_notifications
        && Array.isArray(snapshot.realtime_notifications.events)
        ? snapshot.realtime_notifications.events.filter((row) => row && row.market === "us")
        : [];
      const delivery = monitor.notification_delivery
        && typeof monitor.notification_delivery === "object"
        ? monitor.notification_delivery
        : {};
      const active = symbols.filter((row) => row.status === "monitoring").length;
      const other = symbols.length - active;
      setText("es-us-monitor-count", symbols.length);
      setText("es-us-monitor-active", active);
      setText("es-us-monitor-other", other);
      setText("es-us-monitor-notifications", notificationEvents.length);
      setText(
        "es-us-monitor-updated",
        monitor.last_completed_at
          ? Ui.fullDateTimeText(monitor.last_completed_at)
          : "尚无完成记录",
      );

      const healthPanel = byId("es-us-monitor-health-panel");
      const setMonitorHealth = (health, title, detail) => {
        if (healthPanel) healthPanel.dataset.health = health;
        setText("es-us-monitor-health", title);
        setText("es-us-monitor-status", detail);
      };
      if (monitor.available !== true) {
        setMonitorHealth(
          "unavailable",
          "状态暂不可用",
          "辅助监听会独立重试，A股提前选股快照不受影响。",
        );
      } else if (monitor.stale === true || ["stale", "degraded"].includes(monitor.status)) {
        setMonitorHealth(
          "attention",
          "监听需要关注",
          `${Ui.reasonLabel(monitor.reason_code || "US_MONITOR_UNAVAILABLE")} · 系统将自动重试`,
        );
      } else if (monitor.ready === true) {
        const delivered = Math.max(
          0,
          Number(delivery.delivered_event_count) || 0,
        );
        setMonitorHealth(
          "ready",
          delivery.operationally_verified === true
            ? "运行正常 · 通知已验证"
            : "运行正常 · 等待首个通知事件",
          delivery.operationally_verified === true
            ? `最近一轮已完成；已成功送达 ${delivered} 条，结构通知同时保留在人工复核队列。`
            : "最近一轮已完成；尚无到期通知事件，结构事件会先保留在人工复核队列。",
        );
      } else if (monitor.status === "warming_up") {
        setMonitorHealth(
          "warming",
          "历史暖机中",
          "多周期历史准备完成前不产生结构提醒。",
        );
      } else {
        setMonitorHealth(
          "loading",
          "正在准备",
          Ui.reasonLabel(monitor.reason_code || "US_MONITOR_AWAITING_FIRST_RUN"),
        );
      }

    }

    function renderSectorCatalogStatus() {
      const node = byId("es-sector-catalog-status");
      if (!node || !state.snapshot) return;
      const snapshot = state.snapshot;
      const overlay = snapshot.sector_catalog_overlay
        && typeof snapshot.sector_catalog_overlay === "object"
        ? snapshot.sector_catalog_overlay
        : {};
      const sectorCount = Array.isArray(snapshot.sectors) ? snapshot.sectors.length : 0;
      const runtimeHealth = snapshot.runtime_health
        && typeof snapshot.runtime_health === "object"
        ? snapshot.runtime_health
        : {};
      const scopeFacts = Ui.screeningScopeFacts(runtimeHealth, snapshot);
      if (scopeFacts.validation && sectorCount > 0) {
        node.dataset.state = "published";
        node.textContent = `本次验证基线载入 ${sectorCount} 个板块；当前只复查固定 ${scopeFacts.cohort || scopeFacts.effectiveLimit || 12} 只标的，不代表全市场覆盖。`;
      } else if (overlay.provisional === true && overlay.source === "CURRENT_COVERAGE_CYCLE") {
        node.dataset.state = "preview";
        node.textContent = `本轮板块目录已载入 ${sectorCount} 个；个股全覆盖仍在进行，当前仅用于浏览和筛选。`;
      } else if (overlay.provisional === true && overlay.source === "CACHED_SECTOR_SNAPSHOT") {
        node.dataset.state = "preview";
        node.textContent = `已恢复最近一次校验通过的 ${sectorCount} 个板块目录；新快照仍在重建，当前仅用于浏览。`;
      } else if (sectorCount > 0 && overlay.source === "LAST_INVALIDATED_SNAPSHOT") {
        node.dataset.state = "preview";
        node.textContent = `新快照正在重建；暂显示上次的 ${sectorCount} 个板块目录，仅用于浏览。`;
      } else if (sectorCount > 0 && overlay.source === "PUBLISHED_SNAPSHOT") {
        node.dataset.state = "published";
        node.textContent = `当前正式快照已载入 ${sectorCount} 个板块。`;
      } else if (sectorCount > 0) {
        node.dataset.state = "preview";
        node.textContent = `已载入 ${sectorCount} 个板块目录；正式快照状态仍在核对。`;
      } else if (snapshot.available !== true) {
        node.dataset.state = "loading";
        node.textContent = "板块目录正在构建；完成前不会把 0 个板块当成有效结果。";
      } else {
        node.dataset.state = "empty";
        node.textContent = "当前正式快照没有可展示的板块，请查看运行诊断中的板块扫描原因。";
      }
    }

    function allQualifiedSignals(source = state.signalSource) {
      if (!state.snapshot) return [];
      return Ui.signalRowsForSource(state.snapshot, source);
    }

    function selectionScopedSignals(source = state.signalSource) {
      const rows = allQualifiedSignals(source);
      return state.selectionScope === "sector-trigger"
        ? rows.filter((signal) => (
          Ui.inferSignalMarket(signal) === "us"
          || (
            Array.isArray(signal.selection_sources)
            && signal.selection_sources.includes("QMT_SECTOR_TRIGGER")
          )
        ))
        : rows;
    }

    function currentSignals() {
      if (!state.snapshot) return [];
      const source = selectionScopedSignals();
      const filtered = Ui.filterSignals(source, {
        pointType: state.pointType,
        lifecycle: state.lifecycle,
        sectorId: state.sectorId,
        market: state.market,
        source: state.signalSource,
        reviewStage: state.reviewStage,
        hideResearchOnly: state.hideResearchOnly,
        segmentState: state.segmentState,
        query: state.query,
      });
      return Ui.sortSignalsForReview(filtered, state.snapshot.sectors);
    }

    function syncButtons(selector, dataKey, selected) {
      document.querySelectorAll(selector).forEach((button) => {
        const active = button.dataset[dataKey] === selected;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        if (dataKey === "layout") {
          button.setAttribute("aria-checked", active ? "true" : "false");
          button.tabIndex = active ? 0 : -1;
        }
      });
    }

    function syncFilterCounts() {
      if (!state.snapshot) return;
      // Facet counts keep every other active condition and replace only the
      // dimension being counted.  This avoids showing an attractive count
      // that would immediately collapse to zero after the user clicks it.
      const countWith = (overrides) => {
        const requestedSource = Object.prototype.hasOwnProperty.call(overrides, "source")
          ? overrides.source
          : state.signalSource;
        return Ui.filterSignals(selectionScopedSignals(requestedSource), {
          pointType: state.pointType,
          lifecycle: state.lifecycle,
          sectorId: state.sectorId,
          market: state.market,
          source: requestedSource,
          reviewStage: state.reviewStage,
          hideResearchOnly: state.hideResearchOnly,
          segmentState: state.segmentState,
          query: state.query,
          ...overrides,
        }).length;
      };
      document.querySelectorAll("[data-point-type]").forEach((button) => {
        const point = button.dataset.pointType;
        const count = countWith({ pointType: point || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-lifecycle]").forEach((button) => {
        const stage = button.dataset.lifecycle;
        const count = countWith({ lifecycle: stage || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-market]").forEach((button) => {
        const count = countWith({ market: button.dataset.market || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-signal-source]").forEach((button) => {
        const count = countWith({ source: button.dataset.signalSource || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-review-stage]").forEach((button) => {
        const count = countWith({ reviewStage: button.dataset.reviewStage || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-segment-state]").forEach((button) => {
        const count = countWith({ segmentState: button.dataset.segmentState || "all" });
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      const researchOnlyToggle = byId("es-hide-research-only");
      if (researchOnlyToggle) {
        const included = countWith({ hideResearchOnly: false });
        const excluded = countWith({ hideResearchOnly: true });
        const researchOnlyCount = Math.max(0, included - excluded);
        researchOnlyToggle.dataset.count = String(researchOnlyCount);
        researchOnlyToggle.setAttribute(
          "aria-label",
          state.hideResearchOnly
            ? `恢复显示 ${researchOnlyCount} 条后续中枢仅研究线索`
            : `隐藏 ${researchOnlyCount} 条后续中枢仅研究线索`,
        );
      }
    }

    function syncFilterSummary() {
      const lifecycle = Array.from(document.querySelectorAll("[data-lifecycle]"))
        .find((button) => button.dataset.lifecycle === state.lifecycle);
      const pointType = Array.from(document.querySelectorAll("[data-point-type]"))
        .find((button) => button.dataset.pointType === state.pointType);
      const market = Array.from(document.querySelectorAll("[data-market]"))
        .find((button) => button.dataset.market === state.market);
      const source = Array.from(document.querySelectorAll("[data-signal-source]"))
        .find((button) => button.dataset.signalSource === state.signalSource);
      const reviewStage = Array.from(document.querySelectorAll("[data-review-stage]"))
        .find((button) => button.dataset.reviewStage === state.reviewStage);
      const segmentState = Array.from(document.querySelectorAll("[data-segment-state]"))
        .find((button) => button.dataset.segmentState === state.segmentState);
      setText(
        "es-filter-summary",
        `${market ? market.textContent.trim() : "全部市场"} · ${source ? source.textContent.trim() : "全部来源"} · ${reviewStage ? reviewStage.textContent.trim() : "全部任务"} · ${state.hideResearchOnly ? "已隐藏仅研究" : "包含仅研究"} · ${segmentState ? segmentState.textContent.trim() : "全部定位状态"} · ${lifecycle ? lifecycle.textContent.trim() : "全部状态"} · ${pointType ? pointType.textContent.trim() : "全部买卖点"}`,
      );
    }

    function syncSectorExpandButton() {
      if (!sectorExpand || !state.snapshot) return;
      const total = Array.isArray(state.snapshot.sectors) ? state.snapshot.sectors.length : 0;
      sectorExpand.hidden = total <= 10;
      sectorExpand.textContent = state.sectorExpanded ? "仅显示前 10 个" : `显示全部 ${total} 个`;
      sectorExpand.setAttribute("aria-expanded", state.sectorExpanded ? "true" : "false");
    }

    function selectedSignalById(signalId = state.selectedSignalId) {
      if (!state.snapshot || !signalId) return null;
      const unifiedSignals = Array.isArray(state.snapshot.unified_signals)
        ? state.snapshot.unified_signals
        : state.snapshot.signals;
      return unifiedSignals.find(
        (row) => Ui.text(row && row.signal_id, "") === signalId,
      ) || null;
    }

    function chartFrameForSource(source) {
      if (!chartWorkspace || !source) return null;
      return Array.from(chartWorkspace.querySelectorAll("[data-chart-frame]"))
        .find((frame) => frame.contentWindow === source) || null;
    }

    function clearChartFrameTimeout(frame) {
      const timeout = chartFrameTimeouts.get(frame);
      if (timeout !== undefined) window.clearTimeout(timeout);
      chartFrameTimeouts.delete(frame);
    }

    function chartFrameIsVisible(frame) {
      if (!frame || typeof frame.getClientRects !== "function") return true;
      return frame.getClientRects().length > 0;
    }

    function postChartFrameActivity(frame, active = chartFrameIsVisible(frame)) {
      if (!frame) return false;
      const value = active === true ? "true" : "false";
      frame.dataset.chartActive = value;
      if (
        frame.dataset.chartBridgeReady !== "true"
        || !frame.contentWindow
      ) return false;
      if (frame.dataset.chartActivitySent === value) return true;
      frame.contentWindow.postMessage({
        type: CHART_ACTIVITY_MESSAGE,
        version: CHART_BRIDGE_VERSION,
        active: active === true,
      }, window.location.origin);
      frame.dataset.chartActivitySent = value;
      return true;
    }

    function postChartFrameRealtimeResume(frame) {
      if (
        !frame
        || frame.dataset.chartBridgeReady !== "true"
        || !frame.contentWindow
      ) return false;
      const requestId = frame.dataset.chartRequestId || "";
      if (!requestId || frame.dataset.chartRealtimeResumeRequestId === requestId) {
        return Boolean(requestId);
      }
      frame.contentWindow.postMessage({
        type: CHART_REALTIME_RESUME_MESSAGE,
        version: CHART_BRIDGE_VERSION,
        requestId,
      }, window.location.origin);
      frame.dataset.chartRealtimeResumeRequestId = requestId;
      return true;
    }

    function syncChartFrameActivity() {
      if (!chartWorkspace) return;
      chartWorkspace.querySelectorAll("[data-chart-frame]").forEach((frame) => {
        const active = chartFrameIsVisible(frame);
        if (!active) clearChartFrameTimeout(frame);
        postChartFrameActivity(frame, active);
      });
    }

    function armChartFrameTimeout(frame) {
      if (!frame) return;
      const requestId = frame.dataset.chartRequestId;
      clearChartFrameTimeout(frame);
      // Focus mode intentionally keeps three lazy iframes at display:none.
      // They already carry the newest target URL, but cannot emit bridge-ready
      // until selected. Do not spend reload budget or show a false timeout for
      // work the browser has correctly deferred.
      if (!chartFrameIsVisible(frame)) return;
      chartFrameTimeouts.set(frame, window.setTimeout(() => {
        chartFrameTimeouts.delete(frame);
        if (
          frame.dataset.chartRequestId !== requestId
          || !chartFrameIsVisible(frame)
        ) return;
        reloadChartFrame(frame);
      }, CHART_SWITCH_TIMEOUT_MS));
    }

    function setChartFrameLoading(frame, loading) {
      if (!frame) return;
      frame.setAttribute("aria-busy", loading ? "true" : "false");
      const card = typeof frame.closest === "function"
        ? frame.closest(".es-chart-card")
        : null;
      if (!card) return;
      card.dataset.loading = loading ? "true" : "false";
      const status = card.querySelector("[data-chart-loading]");
      if (!status) return;
      status.hidden = !loading;
      if (loading) {
        status.dataset.state = "loading";
        status.setAttribute("role", "status");
        status.removeAttribute("tabindex");
        const name = frame.dataset.chartTargetName || frame.dataset.chartTargetCode || "标的";
        status.textContent = `正在切换至 ${name} · ${frame.dataset.chartTargetFrequency || "图表"}，K线与缠论结构完成后显示`;
      }
    }

    function beginChartFrameBatch() {
      state.chartSwitchInFlight = true;
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
      if (snapshotRequestController) snapshotRequestController.abort();
      if (signalQuoteTimer !== null) window.clearTimeout(signalQuoteTimer);
      signalQuoteTimer = null;
      signalQuoteRequestKey = "";
      signalQuoteRequestedAt = 0;
    }

    function finishChartFrameBatch() {
      if (!state.chartSwitchInFlight) return;
      state.chartSwitchInFlight = false;
      schedulePoll();
      scheduleSignalQuoteRefresh(currentSignals().slice(0, state.signalRenderLimit));
    }

    function revealSettledChartFrameBatch() {
      if (!chartWorkspace) return false;
      const visibleFrames = [...chartWorkspace.querySelectorAll("[data-chart-frame]")]
        .filter(chartFrameIsVisible);
      if (!visibleFrames.length) return false;
      const settled = visibleFrames.every((frame) => (
        frame.dataset.chartReadyRequestId === frame.dataset.chartRequestId
        || frame.dataset.chartFailedRequestId === frame.dataset.chartRequestId
      ));
      if (!settled) return false;
      visibleFrames.forEach((frame) => {
        if (frame.dataset.chartReadyRequestId === frame.dataset.chartRequestId) {
          setChartFrameLoading(frame, false);
        }
        // All visible history transfers have completed, so their realtime
        // streams can reconnect without competing for the HTTP/1.1 connection
        // slots that make a four-period switch slow on the public tunnel.
        postChartFrameRealtimeResume(frame);
      });
      finishChartFrameBatch();
      return true;
    }

    function setChartFrameFailure(frame) {
      if (!frame) return;
      frame.dataset.chartFailedRequestId = frame.dataset.chartRequestId || "failed";
      frame.setAttribute("aria-busy", "false");
      const card = typeof frame.closest === "function"
        ? frame.closest(".es-chart-card")
        : null;
      const status = card && card.querySelector("[data-chart-loading]");
      if (!card || !status) return;
      // Never expose the incomplete iframe after a timeout. The user sees one
      // honest, actionable state and may retry without selecting the symbol
      // again; partial K-lines or a strict-structure error remain behind it.
      card.dataset.loading = "true";
      status.hidden = false;
      status.dataset.state = "error";
      status.setAttribute("role", "button");
      status.setAttribute("tabindex", "0");
      const name = frame.dataset.chartTargetName || frame.dataset.chartTargetCode || "标的";
      status.textContent = `${name} · ${frame.dataset.chartTargetFrequency || "图表"} 未能完整加载，点击重试`;
      // A failed period keeps its actionable error veil, but it must not leave
      // every other completed period hidden forever.
      revealSettledChartFrameBatch();
      if (status.dataset.retryBound === "true") return;
      status.dataset.retryBound = "true";
      const retry = () => {
        beginChartFrameBatch();
        frame.dataset.chartReloads = "0";
        frame.dataset.chartFailedRequestId = "";
        frame.dataset.chartReadyRequestId = "";
        frame.dataset.chartSwitchPending = "true";
        setChartFrameLoading(frame, true);
        reloadChartFrame(frame);
      };
      status.addEventListener("click", retry);
      status.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        retry();
      });
    }

    function chartFrameTargetMatches(frame, message) {
      if (!frame || !message) return false;
      const messageFrequency = Ui.text(message.frequency, "").trim().toLowerCase();
      return Ui.text(message.market, "").trim().toLowerCase()
          === Ui.text(frame.dataset.chartTargetMarket, "").trim().toLowerCase()
        && normalizeAttentionCode(message.code)
          === normalizeAttentionCode(frame.dataset.chartTargetCode)
        && messageFrequency
          === Ui.text(frame.dataset.chartTargetFrequency, "").trim().toLowerCase();
    }

    function reloadChartFrame(frame) {
      if (!frame) return;
      clearChartFrameTimeout(frame);
      const reloads = Number.parseInt(frame.dataset.chartReloads || "0", 10) || 0;
      if (reloads >= CHART_SWITCH_MAX_RELOADS) {
        frame.dataset.chartSwitchPending = "false";
        frame.dataset.chartBridgeReady = "false";
        setChartFrameFailure(frame);
        return;
      }
      frame.dataset.chartReloads = String(reloads + 1);
      frame.dataset.chartBridgeReady = "false";
      frame.dataset.chartActivitySent = "";
      frame.dataset.chartRealtimeResumeRequestId = "";
      const targetUrl = frame.dataset.chartTargetUrl;
      if (targetUrl && frame.getAttribute("src") !== targetUrl) {
        frame.setAttribute("src", targetUrl);
      } else if (targetUrl) {
        const separator = targetUrl.includes("?") ? "&" : "?";
        frame.setAttribute("src", `${targetUrl}${separator}chart_bridge_retry=${Date.now()}`);
      }
      armChartFrameTimeout(frame);
    }

    function postChartFrameTarget(frame) {
      if (
        !frame
        || !chartFrameIsVisible(frame)
        || frame.dataset.chartBridgeReady !== "true"
        || !frame.contentWindow
      ) return false;
      const payload = {
        type: CHART_SWITCH_MESSAGE,
        version: CHART_BRIDGE_VERSION,
        requestId: frame.dataset.chartRequestId,
        market: frame.dataset.chartTargetMarket,
        code: frame.dataset.chartTargetCode,
        frequency: frame.dataset.chartTargetFrequency,
      };
      frame.contentWindow.postMessage(payload, window.location.origin);
      armChartFrameTimeout(frame);
      return true;
    }

    function flushChartFrameTarget(frame) {
      if (
        !frame
        || frame.dataset.chartSwitchPending !== "true"
        || !chartFrameIsVisible(frame)
      ) return;
      setChartFrameLoading(frame, true);
      postChartFrameActivity(frame, true);
      if (frame.dataset.chartBridgeReady === "true") {
        if (postChartFrameTarget(frame)) {
          frame.dataset.chartSwitchPending = "false";
        }
        return;
      }
      const targetUrl = frame.dataset.chartTargetUrl;
      if (targetUrl && frame.getAttribute("src") !== targetUrl) {
        frame.dataset.chartBridgeReady = "false";
        frame.dataset.chartActivitySent = "";
        frame.setAttribute("src", targetUrl);
      }
      armChartFrameTimeout(frame);
    }

    function flushChartFrameTargets() {
      if (!chartWorkspace) return;
      chartWorkspace.querySelectorAll("[data-chart-frame]").forEach((frame) => {
        flushChartFrameTarget(frame);
      });
    }

    function navigateChartFrame(frame, target) {
      if (!frame || !target || !target.url) return;
      const targetKey = [
        Ui.text(target.market, "").trim().toLowerCase(),
        normalizeAttentionCode(target.code),
        Ui.text(target.frequency, "").trim().toLowerCase(),
      ].join("|");
      let targetUrl = target.url;
      try {
        const parsed = new URL(target.url, window.location.origin);
        parsed.searchParams.set(
          "stream_channel",
          `screening.${chartStreamPageId}.${Ui.text(target.frequency, "chart")}`,
        );
        targetUrl = `${parsed.pathname}${parsed.search}${parsed.hash}`;
      } catch (_error) {
        // A malformed URL is handled by the existing bounded iframe retry path.
      }
      frame.dataset.chartTargetUrl = targetUrl;
      frame.dataset.chartTargetMarket = Ui.text(target.market, "").trim().toLowerCase();
      frame.dataset.chartTargetCode = normalizeAttentionCode(target.code);
      frame.dataset.chartTargetFrequency = Ui.text(target.frequency, "").trim().toLowerCase();
      frame.dataset.chartTargetName = Ui.text(target.name, target.code);
      if (frame.dataset.chartTargetKey === targetKey) {
        // A lazy frame can retain the correct target without ever having been
        // loaded. Once its period becomes visible, start a fresh bounded
        // request and restore an honest loading state.
        if (!chartFrameIsVisible(frame)) return;
        postChartFrameActivity(frame, true);
        const currentSrc = frame.getAttribute("src") || "about:blank";
        if (
          frame.dataset.chartSwitchPending !== "true"
          && frame.dataset.chartBridgeReady !== "true"
          && currentSrc === "about:blank"
        ) {
          beginChartFrameBatch();
          frame.dataset.chartRequestId = `chart-${++chartRequestSequence}`;
          frame.dataset.chartReloads = "0";
          frame.dataset.chartReadyRequestId = "";
          frame.dataset.chartFailedRequestId = "";
          frame.dataset.chartRealtimeResumeRequestId = "";
          frame.dataset.chartSwitchPending = "true";
        }
        flushChartFrameTarget(frame);
        return;
      }
      if (chartFrameIsVisible(frame)) beginChartFrameBatch();
      frame.dataset.chartTargetKey = targetKey;
      frame.dataset.chartRequestId = `chart-${++chartRequestSequence}`;
      frame.dataset.chartReloads = "0";
      frame.dataset.chartReadyRequestId = "";
      frame.dataset.chartFailedRequestId = "";
      frame.dataset.chartRealtimeResumeRequestId = "";
      frame.dataset.chartSwitchPending = "true";
      if (!chartFrameIsVisible(frame)) {
        clearChartFrameTimeout(frame);
        setChartFrameLoading(frame, false);
        return;
      }
      flushChartFrameTarget(frame);
    }

    function renderSelectedChart(signal) {
      if (state.mode === "human-review") return;
      Ui.renderChartWorkspace(chartWorkspace, signal, {
        frequency: state.focusState.frequency,
        navigateChartFrame,
      });
      syncChartFrameActivity();
      flushChartFrameTargets();
    }

    function renderTheaterSignalPicker(filteredSignals = []) {
      if (!theaterSignalList) return;
      if (!state.theaterMode) {
        theaterSignalList.replaceChildren();
        theaterSignalList.dataset.selectedSignalId = "";
        return;
      }
      if (!state.signalListOpen) return;
      const allPointSignals = Ui.theaterPointSignals(filteredSignals, "all", "");
      const visibleSignals = Ui.theaterPointSignals(
        allPointSignals,
        state.theaterPointType,
        state.theaterQuery,
      );
      const countNode = chartWorkspace.querySelector("[data-theater-signal-count]");
      if (countNode) countNode.textContent = `${visibleSignals.length} / ${allPointSignals.length}`;
      if (theaterSignalEmpty) theaterSignalEmpty.hidden = visibleSignals.length !== 0;
      if (theaterSignalSearch && theaterSignalSearch.value !== state.theaterQuery) {
        theaterSignalSearch.value = state.theaterQuery;
      }
      chartWorkspace.querySelectorAll("[data-theater-point-type]").forEach((button) => {
        const pointType = button.dataset.theaterPointType;
        const active = pointType === state.theaterPointType;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        button.dataset.count = String(
          pointType === "all"
            ? allPointSignals.length
            : Ui.theaterPointSignals(allPointSignals, pointType, "").length,
        );
      });
      Ui.renderSignalWorkspace(
        theaterSignalList,
        visibleSignals,
        state.selectedSignalId,
        selectSignal,
        {
          limit: state.theaterSignalRenderLimit,
          quoteForSignal: signalQuoteForSignal,
          scrollSelectedIntoView: true,
          onLoadMore: () => {
            state.theaterSignalRenderLimit += 200;
            renderTheaterSignalPicker(currentSignals());
          },
        },
      );
    }

    function selectSignal(signal) {
      const nextSignalId = signal ? Ui.text(signal.signal_id, "") : null;
      if (!nextSignalId || nextSignalId === state.selectedSignalId) return;
      state.selectedSignalId = nextSignalId;
      state.focusState = Ui.resolveFocusState(state.focusState, signal);
      Ui.updateSignalWorkspaceSelection(signalList, nextSignalId);
      Ui.updateSignalWorkspaceSelection(theaterSignalList, nextSignalId, {
        scrollSelectedIntoView: state.theaterMode,
      });
      renderSelectedChart(signal);
    }

    function showEvidence(open, restoreFocus = false) {
      const requested = Boolean(open && evidenceToggle && !evidenceToggle.disabled);
      state.evidenceOpen = Ui.setEvidencePanelOpen(chartWorkspace, requested);
      if (state.evidenceOpen && evidenceClose) evidenceClose.focus();
      else if (restoreFocus && evidenceToggle) evidenceToggle.focus();
    }

    function restoreFocusBeforeClosingTheater() {
      if (!chartWorkspace) return;
      const picker = chartWorkspace.querySelector("[data-theater-signal-picker]");
      const activeElement = document.activeElement;
      if (
        (!picker || !picker.contains(activeElement))
        && activeElement !== theaterPickerToggle
        && activeElement !== theaterToolbarExit
      ) return;
      const toggleVisible = theaterToggle && (
        typeof theaterToggle.getClientRects !== "function"
        || theaterToggle.getClientRects().length > 0
      );
      const target = toggleVisible
        ? theaterToggle
        : chartWorkspace.querySelector(".es-chart-display-settings > summary");
      if (!target || typeof target.focus !== "function") return;
      try {
        target.focus({ preventScroll: true });
      } catch (_error) {
        target.focus();
      }
    }

    function syncSignalListVisibility(open) {
      const requested = Ui.setTheaterPickerOpen(chartWorkspace, open);
      if (liveWorkspaces) liveWorkspaces.dataset.signalListOpen = String(requested);
      if (signalWorkspace) signalWorkspace.setAttribute("aria-hidden", String(!requested));
      signalListToggles.forEach((button) => {
        button.setAttribute("aria-expanded", String(requested));
        const actionLabel = requested ? "收起买卖点股票列表" : "展开买卖点股票列表";
        button.setAttribute("aria-label", actionLabel);
        button.setAttribute("title", actionLabel);
        const label = button.querySelector("[data-signal-list-label]");
        if (label) label.textContent = requested ? "收起列表" : "展开列表";
      });
      return requested;
    }

    function showSignalList(open, moveFocus = false) {
      state.signalListOpen = syncSignalListVisibility(open);
      saveView();
      if (state.signalListOpen && state.theaterMode) {
        renderTheaterSignalPicker(state.snapshot ? currentSignals() : []);
      }
      window.requestAnimationFrame(() => {
        window.dispatchEvent(new Event("resize"));
        if (!moveFocus) return;
        const target = theaterPickerToggle;
        if (!target || typeof target.focus !== "function") return;
        try {
          target.focus({ preventScroll: true });
        } catch (_error) {
          target.focus();
        }
      });
    }

    function showTheater(active) {
      const requested = Boolean(active && theaterToggle && !theaterToggle.disabled);
      // ``aria-hidden`` must never be applied while focus is still inside the
      // picker. The theater toggle lives in a collapsible <details>, so fall
      // back to its always-visible summary when that control is currently hidden.
      if (!requested && state.theaterMode) restoreFocusBeforeClosingTheater();
      if (requested && state.evidenceOpen) showEvidence(false);
      state.theaterMode = Ui.setTheaterMode(chartWorkspace, document.body, requested);
      renderTheaterSignalPicker(state.snapshot ? currentSignals() : []);
      if (requested) {
        // The toggle is inside this popover. Close it before the fixed theater
        // workspace is painted so it cannot cover the upper-right chart.
        const settings = chartWorkspace.querySelector(".es-chart-display-settings");
        if (settings) settings.removeAttribute("open");
        const entryFocus = theaterPickerToggle;
        if (entryFocus && typeof entryFocus.focus === "function") {
          try {
            entryFocus.focus({ preventScroll: true });
          } catch (_error) {
            entryFocus.focus();
          }
        }
      }
    }

    function renderWorkspaces() {
      if (!state.snapshot) return;
      renderSectorCatalogStatus();
      const filtered = currentSignals();
      const signalFilterKey = JSON.stringify([
        state.pointType,
        state.lifecycle,
        state.market,
        state.signalSource,
        state.reviewStage,
        state.hideResearchOnly,
        state.segmentState,
        state.selectionScope,
        state.sectorId,
        state.query,
      ]);
      if (state.signalFilterKey !== signalFilterKey) {
        state.signalFilterKey = signalFilterKey;
        state.signalRenderLimit = 200;
      }
      state.selectedSignalId = Ui.resolveSelectedSignalId(
        state.selectedSignalId,
        filtered,
        state.snapshot.signals,
      );
      const unifiedSignals = Array.isArray(state.snapshot.unified_signals)
        ? state.snapshot.unified_signals
        : state.snapshot.signals;
      const selected = unifiedSignals.find(
        (row) => Ui.text(row.signal_id, "") === state.selectedSignalId,
      ) || null;
      state.focusState = Ui.resolveFocusState(state.focusState, selected);

      Ui.renderSectorWorkspace(
        sectorList,
        state.snapshot,
        state.sectorId,
        (sectorId) => {
          state.sectorId = sectorId;
          renderWorkspaces();
        },
        { expanded: state.sectorExpanded, limit: 10 },
      );
      Ui.renderSignalWorkspace(
        signalList,
        filtered,
        state.selectedSignalId,
        selectSignal,
        {
          limit: state.signalRenderLimit,
          quoteForSignal: signalQuoteForSignal,
          scrollSelectedIntoView: false,
          onLoadMore: () => {
            state.signalRenderLimit += 200;
            renderWorkspaces();
          },
        },
      );
      renderTheaterSignalPicker(filtered);
      scheduleSignalQuoteRefresh(filtered.slice(0, state.signalRenderLimit));
      if (state.mode !== "human-review") {
        Ui.renderChartWorkspace(chartWorkspace, selected, {
          frequency: state.focusState.frequency,
          navigateChartFrame,
        });
        syncChartFrameActivity();
        flushChartFrameTargets();
      }
      if (!selected) {
        if (state.evidenceOpen) showEvidence(false);
        if (state.theaterMode) showTheater(false);
      }

      setText(
        "es-visible-count",
        Ui.signalQueueCountText(filtered, selectionScopedSignals()),
      );
      const empty = byId("es-empty");
      if (empty) empty.hidden = filtered.length !== 0;
      setText(
        "es-empty-detail",
        Ui.emptySignalDetail(state.snapshot, state.query, {
          pointType: state.pointType,
          lifecycle: state.lifecycle,
          market: state.market,
          source: state.signalSource,
          reviewStage: state.reviewStage,
          hideResearchOnly: state.hideResearchOnly,
          segmentState: state.segmentState,
          selectionScope: state.selectionScope,
          sectorId: state.sectorId,
        }),
      );
      syncButtons("[data-point-type]", "pointType", state.pointType);
      syncButtons("[data-lifecycle]", "lifecycle", state.lifecycle);
      syncButtons("[data-selection-scope]", "selectionScope", state.selectionScope);
      syncButtons("[data-market]", "market", state.market);
      syncButtons("[data-signal-source]", "signalSource", state.signalSource);
      syncButtons("[data-review-stage]", "reviewStage", state.reviewStage);
      syncButtons("[data-segment-state]", "segmentState", state.segmentState);
      const researchOnlyToggle = byId("es-hide-research-only");
      if (researchOnlyToggle) {
        researchOnlyToggle.classList.toggle("is-active", state.hideResearchOnly);
        researchOnlyToggle.setAttribute(
          "aria-pressed",
          state.hideResearchOnly ? "true" : "false",
        );
        researchOnlyToggle.firstChild.textContent = state.hideResearchOnly
          ? "显示仅研究"
          : "隐藏仅研究";
      }
      syncButtons(".es-layout-switch [data-layout]", "layout", state.layout);
      syncFilterCounts();
      syncFilterSummary();
      syncSectorExpandButton();
      syncCurrentSegmentAction();
      syncShowAllSignalsAction();
      if (state.revealCurrentSegmentsAfterRender) revealCurrentSegmentResults();
    }

    function render() {
      snapshotHeader();
      renderManualAttention();
      renderUsMonitorStatus();
      renderWorkspaces();
    }

    function schedulePoll() {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = window.setTimeout(() => {
        state.pollTimer = null;
        if (
          document.visibilityState === "visible"
          && state.mode === "live"
          && !state.chartSwitchInFlight
        ) {
          void requestSnapshot();
        }
        else schedulePoll();
      }, POLL_INTERVAL_MS);
    }

    async function requestSnapshot() {
      if (state.loading || state.chartSwitchInFlight) return false;
      state.loading = true;
      root.dataset.loading = "true";
      const requestedScope = state.selectionScope;
      const refreshButton = byId("es-refresh-now");
      if (refreshButton) refreshButton.disabled = true;
      if (!state.snapshot) setStatus("loading", "正在读取最新快照", "正在核对新交易系统的数据边界");
      try {
        const endpoint = new URL(root.dataset.endpoint, window.location.href);
        endpoint.searchParams.set("scope", requestedScope);
        endpoint.searchParams.set("transport", "signal-catalog-v1");
        let response = null;
        let payload = null;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            ({ response, payload } = await requestJson(endpoint.toString(), {
              cache: "no-cache",
              credentials: "same-origin",
              headers: { Accept: "application/json" },
            }));
          } catch (error) {
            if (error && error.message === "snapshot_request_deferred_for_chart") {
              return false;
            }
            if (attempt === 0) {
              await waitForSnapshotRetry(null);
              continue;
            }
            throw error;
          }
          if (response.status === 401) {
            throw new Error("snapshot_authentication_required");
          }
          if (response.status === 304) {
            if (
              state.snapshot
              && state.snapshot.presentation_scope === requestedScope
            ) return true;
            throw new Error("snapshot_not_modified_without_state");
          }
          if (response.ok && payload && payload.ok === true) break;
          const recoverable = response.status === 408
            || response.status === 429
            || response.status >= 500;
          if (attempt === 0 && recoverable) {
            await waitForSnapshotRetry(response);
            continue;
          }
          throw new Error("snapshot_request_failed");
        }
        if (!response || !response.ok || !payload || payload.ok !== true) {
          throw new Error("snapshot_request_failed");
        }
        if (state.chartSwitchInFlight) return false;
        const nextSnapshot = Ui.normalizeSnapshot(payload.data);
        if (nextSnapshot.presentation_scope !== requestedScope) {
          throw new Error("snapshot_scope_mismatch");
        }
        state.snapshot = nextSnapshot;
        render();
        return true;
      } catch (error) {
        if (error && error.message === "snapshot_authentication_required") {
          setStatus(
            "warning",
            "登录状态已失效",
            "请重新登录后返回本页面；当前快照不会被未认证响应覆盖",
          );
        } else {
          setStatus(
            "error",
            "实时快照暂不可用",
            state.snapshot ? "已自动重试并保留当前页面数据，下一轮继续恢复" : "已自动重试；未展示未经验证或边界不完整的数据",
          );
        }
        console.error(
          "trading_screening_snapshot_failed",
          error && error.name ? error.name : "Error",
          error && error.message ? error.message : "unknown_error",
        );
        return false;
      } finally {
        state.loading = false;
        root.dataset.loading = "false";
        if (refreshButton) refreshButton.disabled = false;
        schedulePoll();
      }
    }

    document.querySelectorAll("[data-point-type]").forEach((button) => {
      button.addEventListener("click", () => {
        state.pointType = button.dataset.pointType || "all";
        saveView();
        renderWorkspaces();
      });
    });
    document.querySelectorAll("[data-lifecycle]").forEach((button) => {
      button.addEventListener("click", () => {
        state.lifecycle = button.dataset.lifecycle || "all";
        saveView();
        renderWorkspaces();
      });
    });
    document.querySelectorAll("[data-market]").forEach((button) => {
      button.addEventListener("click", () => {
        state.market = button.dataset.market || "all";
        saveView();
        renderWorkspaces();
      });
    });
    document.querySelectorAll("[data-signal-source]").forEach((button) => {
      button.addEventListener("click", () => {
        state.signalSource = button.dataset.signalSource || "all";
        saveView();
        renderWorkspaces();
      });
    });
    document.querySelectorAll("[data-review-stage]").forEach((button) => {
      button.addEventListener("click", () => {
        state.reviewStage = button.dataset.reviewStage || "all";
        saveView();
        renderWorkspaces();
      });
    });
    const researchOnlyToggle = byId("es-hide-research-only");
    if (researchOnlyToggle) researchOnlyToggle.addEventListener("click", () => {
      state.hideResearchOnly = !state.hideResearchOnly;
      saveView();
      renderWorkspaces();
    });
    document.querySelectorAll("[data-segment-state]").forEach((button) => {
      button.addEventListener("click", () => {
        state.segmentState = button.dataset.segmentState || "all";
        saveView();
        renderWorkspaces();
      });
    });
    document.querySelectorAll("[data-selection-scope]").forEach((button) => {
      button.addEventListener("click", async () => {
        const previous = state.selectionScope;
        const requested = button.dataset.selectionScope === "all-qualified"
          ? "all-qualified"
          : "sector-trigger";
        if (requested === previous) return;
        state.selectionScope = requested;
        saveView();
        syncButtons("[data-selection-scope]", "selectionScope", requested);
        setStatus("loading", "正在切换候选范围", "读取对应范围的轻量结构快照");
        if (!await requestSnapshot()) {
          state.selectionScope = previous;
          saveView();
          syncButtons("[data-selection-scope]", "selectionScope", previous);
        }
      });
    });
    const layoutButtons = [...document.querySelectorAll(".es-layout-switch [data-layout]")];
    const selectLayout = (button, moveFocus = false) => {
      if (!button) return;
      state.layout = Ui.setChartLayout(chartWorkspace, button.dataset.layout);
      resizeController.setLayout(state.layout);
      saveView();
      syncButtons(".es-layout-switch [data-layout]", "layout", state.layout);
      syncChartFrameActivity();
      flushChartFrameTargets();
      revealSettledChartFrameBatch();
      if (moveFocus) button.focus();
    };
    layoutButtons.forEach((button) => {
      button.addEventListener("click", () => {
        selectLayout(button);
      });
      button.addEventListener("keydown", (event) => {
        const key = event.key;
        if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(key)) return;
        event.preventDefault();
        const current = layoutButtons.indexOf(button);
        let next = current;
        if (key === "Home") next = 0;
        else if (key === "End") next = layoutButtons.length - 1;
        else if (key === "ArrowRight" || key === "ArrowDown") next = (current + 1) % layoutButtons.length;
        else next = (current - 1 + layoutButtons.length) % layoutButtons.length;
        selectLayout(layoutButtons[next], true);
      });
    });
    document.querySelectorAll("[data-focus-frequency], [data-period-node]").forEach((button) => {
      button.addEventListener("click", () => {
        const frequency = button.dataset.focusFrequency || button.dataset.periodNode;
        state.focusState = Ui.manualFocusState(
          state.focusState,
          state.selectedSignalId,
          frequency,
        );
        renderSelectedChart(selectedSignalById());
      });
    });

    window.addEventListener("message", (event) => {
      if (event.origin !== window.location.origin) return;
      const frame = chartFrameForSource(event.source);
      const message = event.data && typeof event.data === "object" ? event.data : null;
      if (!frame || !message || message.version !== CHART_BRIDGE_VERSION) return;
      if (message.type === CHART_BRIDGE_READY_MESSAGE) {
        frame.dataset.chartBridgeReady = "true";
        frame.dataset.chartActivitySent = "";
        postChartFrameActivity(frame, chartFrameIsVisible(frame));
        if (
          chartFrameIsVisible(frame)
          && frame.dataset.chartTargetUrl
          && postChartFrameTarget(frame)
        ) {
          frame.dataset.chartSwitchPending = "false";
        }
        return;
      }
      if (message.type === CHART_DATA_READY_MESSAGE) {
        if (
          message.requestId
          && message.requestId !== frame.dataset.chartRequestId
        ) return;
        if (!chartFrameTargetMatches(frame, message)) return;
        clearChartFrameTimeout(frame);
        frame.dataset.chartReloads = "0";
        frame.dataset.chartSwitchPending = "false";
        frame.dataset.chartFailedRequestId = "";
        frame.dataset.chartReadyRequestId = frame.dataset.chartRequestId || "ready";
        revealSettledChartFrameBatch();
        return;
      }
      if (
        message.type === CHART_SWITCH_ERROR_MESSAGE
        && (!message.requestId || message.requestId === frame.dataset.chartRequestId)
      ) {
        reloadChartFrame(frame);
      }
    });
    if (evidenceToggle) evidenceToggle.addEventListener("click", () => {
      showEvidence(!state.evidenceOpen);
    });
    if (evidenceClose) evidenceClose.addEventListener("click", () => {
      showEvidence(false, true);
    });
    if (theaterToggle) theaterToggle.addEventListener("click", () => {
      showTheater(!state.theaterMode);
    });
    signalListToggles.forEach((button) => {
      button.addEventListener("click", () => {
        showSignalList(!state.signalListOpen, true);
      });
    });
    if (theaterToolbarExit) theaterToolbarExit.addEventListener("click", () => {
      showTheater(false);
    });
    if (theaterSignalSearch) theaterSignalSearch.addEventListener("input", () => {
      state.theaterQuery = theaterSignalSearch.value;
      state.theaterSignalRenderLimit = 200;
      renderTheaterSignalPicker(currentSignals());
    });
    if (chartWorkspace) chartWorkspace.querySelectorAll("[data-theater-point-type]").forEach((button) => {
      button.addEventListener("click", () => {
        state.theaterPointType = button.dataset.theaterPointType;
        state.theaterSignalRenderLimit = 200;
        renderTheaterSignalPicker(currentSignals());
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (state.evidenceOpen) showEvidence(false, true);
      else if (state.theaterMode) showTheater(false);
    });
    const search = byId("es-signal-search");
    if (search) search.addEventListener("input", () => {
      state.query = search.value;
      renderWorkspaces();
    });
    const refreshButton = byId("es-refresh-now");
    if (refreshButton) refreshButton.addEventListener("click", () => void requestSnapshot());
    const filterReset = byId("es-filter-reset");
    if (filterReset) filterReset.addEventListener("click", () => {
      const scopeChanged = resetSignalFilters();
      if (scopeChanged) void requestSnapshot();
      else renderWorkspaces();
    });
    const showAllSignals = byId("es-show-all-signals");
    if (showAllSignals) showAllSignals.addEventListener("click", () => {
      const scopeChanged = resetSignalFilters();
      if (scopeChanged || !state.snapshot) void requestSnapshot();
      else renderWorkspaces();
    });
    const showCurrentSegments = byId("es-show-current-segments");
    if (showCurrentSegments) showCurrentSegments.addEventListener("click", () => {
      const scopeChanged = resetSignalFilters("current");
      const liveModeButton = document.querySelector('[role="tab"][data-screening-mode="live"]');
      if (liveModeButton && !liveModeButton.classList.contains("is-active")) {
        liveModeButton.click();
        return;
      }
      if (scopeChanged || !state.snapshot) void requestSnapshot();
      else renderWorkspaces();
    });
    if (sectorExpand) sectorExpand.addEventListener("click", () => {
      state.sectorExpanded = !state.sectorExpanded;
      renderWorkspaces();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && state.mode === "live") {
        void requestSnapshot();
      }
    });
    window.addEventListener("chanlun-screening-mode-change", (event) => {
      const requested = event && event.detail && event.detail.mode;
      state.mode = requested === "live" ? "live" : "human-review";
      if (state.mode === "live") {
        if (state.snapshot) renderWorkspaces();
        else void requestSnapshot();
      }
    });

    if (state.mode === "live") void requestSnapshot();
    else schedulePoll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
})();
