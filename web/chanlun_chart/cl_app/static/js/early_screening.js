"use strict";

(function startTradingScreeningController() {
  const POLL_INTERVAL_MS = 60_000;
  const SNAPSHOT_REQUEST_TIMEOUT_MS = 20_000;
  const SNAPSHOT_RECOVERY_RETRY_MS = 750;
  const STORAGE_KEY = "chanlun:trading-screening:view";
  // The current-only contract invalidates every persisted pre-migration view.
  // Earlier versions could retain a narrow point/stage/scope filter and make
  // current first/second-class rows appear to be missing.
  const VIEW_CONTRACT = "CANONICAL_SIX_POINT_CHANNELS_V7_5M_TRADE_1M_PRECISION";

  function boot() {
    const Ui = globalThis.TradingScreeningUi;
    const Resize = globalThis.TradingScreeningChartResize;
    const root = document.getElementById("es-dashboard");
    if (!Ui || !Resize || !root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const byId = (id) => document.getElementById(id);
    const sectorList = byId("es-sector-list");
    const sectorExpand = byId("es-sector-expand");
    const signalList = byId("es-signal-list");
    const chartWorkspace = byId("es-chart-workspace");
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
      mode: root.dataset.defaultMode || "human-review",
      loading: false,
      pollTimer: null,
      signalRenderLimit: 200,
      signalFilterKey: "",
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

    async function requestJson(endpoint, options) {
      const controller = new AbortController();
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
          throw new Error("snapshot_request_timeout");
        }
        throw error;
      } finally {
        window.clearTimeout(timeout);
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
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return {};
        const value = JSON.parse(raw);
        if (!value || typeof value !== "object" || value.contract !== VIEW_CONTRACT) {
          localStorage.removeItem(STORAGE_KEY);
          return {};
        }
        return value;
      } catch (_error) {
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch (_storageError) {
          // 本地存储不可用时仍以实时快照为唯一事实来源。
        }
        return {};
      }
    }

    function saveView() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
          contract: VIEW_CONTRACT,
          pointType: state.pointType,
          lifecycle: state.lifecycle,
          market: state.market,
          signalSource: state.signalSource,
          reviewStage: state.reviewStage,
          segmentState: state.segmentState,
          selectionScope: state.selectionScope,
          layout: state.layout,
          chartSizing: state.chartSizing,
        }));
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

    function countStage(stage) {
      const counts = state.snapshot && state.snapshot.counts_by_stage;
      return Number(counts && counts[stage]) || 0;
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
          snapshot.market_data_as_of || snapshot.as_of || snapshot.generated_at,
        ),
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
      setText("es-signal-count", unifiedSignals.length);
      setText(
        "es-sector-trigger-count",
        Number(snapshot.sector_trigger_signal_count) || 0,
      );
      setText(
        "es-total-qualified-count",
        Number(snapshot.total_qualified_signal_count) || snapshot.signals.length,
      );
      const currentSignals = unifiedSignals.filter(Ui.isCurrentSelectionSignal);
      setText("es-approaching-count", countStage("approaching"));
      const fiveMinuteConfirmedCount = currentSignals.filter(
        (signal) => Ui.fiveMinuteTradeSignalConfirmedForSignal(signal),
      ).length;
      setText("es-triggered-count", fiveMinuteConfirmedCount);
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
        showCurrentSegments.disabled = segmentDifferenceCount === 0;
        showCurrentSegments.textContent = segmentDifferenceCount === 0
          ? "当前暂无精确定位"
          : "查看当前定位";
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
      setText("es-executable-count", countStage("executable"));
      document.title = unifiedSignals.length
        ? `(${unifiedSignals.length}) 缠论提前选股 · 实时盯盘与个股分析`
        : "缠论提前选股 · 实时盯盘与个股分析";
    }

    function renderManualAttention() {
      const snapshot = state.snapshot || {};
      const attention = snapshot.manual_attention && typeof snapshot.manual_attention === "object"
        ? snapshot.manual_attention
        : {};
      const symbols = Array.isArray(attention.symbols)
        ? attention.symbols.filter((row) => row && typeof row === "object")
        : [];
      const list = byId("es-holdings-list");
      const empty = byId("es-holdings-empty");
      const normalizeCode = (value) => Ui.text(value, "").trim().toUpperCase();
      const identityKey = (market, code) => `${Ui.text(market, "").trim().toLowerCase()}|${normalizeCode(code)}`;
      const inferSignalMarket = (signal) => {
        const explicit = Ui.text(signal && signal.market, "").trim().toLowerCase();
        if (explicit) return explicit;
        const code = normalizeCode(signal && signal.code);
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
        const code = normalizeCode(signal.code);
        if (!code) continue;
        const key = identityKey(inferSignalMarket(signal), code);
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
      if (!list) return;
      const fragment = document.createDocumentFragment();
      const alertStages = new Set(["approaching", "triggered", "executable", "active"]);
      const marketLabels = {
        a: "A股", hk: "港股", us: "美股", fx: "外汇", futures: "期货",
        ny_futures: "纽约期货", currency: "数字货币", currency_spot: "数字货币现货",
      };
      for (const symbolRow of symbols) {
        const market = Ui.text(symbolRow.market, "").trim();
        const code = Ui.text(symbolRow.code, "").trim();
        const name = Ui.text(symbolRow.name, code);
        const signal = signalsByIdentity.get(identityKey(market, code)) || null;
        const stage = signal ? Ui.lifecycleStageForSignal(signal) : "";
        const card = document.createElement("a");
        card.className = "es-holding-card";
        card.setAttribute("role", "listitem");
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
        if (market === "a" && symbolRow.quote_available === true) {
          const price = Number(symbolRow.current_price);
          const change = Number(symbolRow.change_percent);
          if (Number.isFinite(price) && price > 0 && Number.isFinite(change)) {
            const priceDigits = price < 10 ? 3 : 2;
            quote.textContent = `${price.toFixed(priceDigits)}  ${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
            quote.dataset.direction = change > 0 ? "up" : change < 0 ? "down" : "flat";
          } else {
            quote.textContent = "行情暂不可用";
          }
        } else if (market === "a") {
          quote.textContent = "行情暂不可用";
        } else {
          quote.hidden = true;
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
        fragment.append(card);
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
          Ui.text(monitor.reason_code, "等待首次运行"),
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
      if (overlay.provisional === true && overlay.source === "CURRENT_COVERAGE_CYCLE") {
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

    function selectionScopedSignals() {
      if (!state.snapshot) return [];
      const rows = Array.isArray(state.snapshot.unified_signals)
        ? state.snapshot.unified_signals
        : state.snapshot.signals;
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
        if (dataKey === "layout") button.setAttribute("aria-checked", active ? "true" : "false");
      });
    }

    function syncFilterCounts() {
      if (!state.snapshot) return;
      const scopedSignals = selectionScopedSignals();
      // Facet counts keep every other active condition and replace only the
      // dimension being counted.  This avoids showing an attractive count
      // that would immediately collapse to zero after the user clicks it.
      const countWith = (overrides) => Ui.filterSignals(scopedSignals, {
        pointType: state.pointType,
        lifecycle: state.lifecycle,
        sectorId: state.sectorId,
        market: state.market,
        source: state.signalSource,
        reviewStage: state.reviewStage,
        segmentState: state.segmentState,
        query: state.query,
        ...overrides,
      }).length;
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
        `${market ? market.textContent.trim() : "全部市场"} · ${source ? source.textContent.trim() : "全部来源"} · ${reviewStage ? reviewStage.textContent.trim() : "全部任务"} · ${segmentState ? segmentState.textContent.trim() : "全部定位状态"} · ${lifecycle ? lifecycle.textContent.trim() : "全部状态"} · ${pointType ? pointType.textContent.trim() : "全部买卖点"}`,
      );
    }

    function syncSectorExpandButton() {
      if (!sectorExpand || !state.snapshot) return;
      const total = Array.isArray(state.snapshot.sectors) ? state.snapshot.sectors.length : 0;
      sectorExpand.hidden = total <= 10;
      sectorExpand.textContent = state.sectorExpanded ? "仅显示前 10 个" : `显示全部 ${total} 个`;
      sectorExpand.setAttribute("aria-expanded", state.sectorExpanded ? "true" : "false");
    }

    function selectSignal(signal) {
      state.selectedSignalId = signal ? Ui.text(signal.signal_id, "") : null;
      renderWorkspaces();
    }

    function showEvidence(open, restoreFocus = false) {
      const requested = Boolean(open && evidenceToggle && !evidenceToggle.disabled);
      state.evidenceOpen = Ui.setEvidencePanelOpen(chartWorkspace, requested);
      if (state.evidenceOpen && evidenceClose) evidenceClose.focus();
      else if (restoreFocus && evidenceToggle) evidenceToggle.focus();
    }

    function showTheater(active) {
      const requested = Boolean(active && theaterToggle && !theaterToggle.disabled);
      state.theaterMode = Ui.setTheaterMode(chartWorkspace, document.body, requested);
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
          onLoadMore: () => {
            state.signalRenderLimit += 200;
            renderWorkspaces();
          },
        },
      );
      if (state.mode !== "human-review") {
        Ui.renderChartWorkspace(chartWorkspace, selected, {
          frequency: state.focusState.frequency,
        });
      }
      if (!selected) {
        if (state.evidenceOpen) showEvidence(false);
        if (state.theaterMode) showTheater(false);
      }

      setText("es-visible-count", `${filtered.length} / ${selectionScopedSignals().length}`);
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
      syncButtons(".es-layout-switch [data-layout]", "layout", state.layout);
      syncFilterCounts();
      syncFilterSummary();
      syncSectorExpandButton();
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
        if (document.visibilityState === "visible" && state.mode === "live") {
          void requestSnapshot();
        }
        else schedulePoll();
      }, POLL_INTERVAL_MS);
    }

    async function requestSnapshot() {
      if (state.loading) return false;
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
    document.querySelectorAll(".es-layout-switch [data-layout]").forEach((button) => {
      button.addEventListener("click", () => {
        state.layout = Ui.setChartLayout(chartWorkspace, button.dataset.layout);
        resizeController.setLayout(state.layout);
        saveView();
        syncButtons(".es-layout-switch [data-layout]", "layout", state.layout);
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
        renderWorkspaces();
      });
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
      const scopeChanged = state.selectionScope !== "all-qualified";
      state.pointType = "all";
      state.lifecycle = "all";
      state.market = "all";
      state.signalSource = "all";
      state.reviewStage = "all";
      state.segmentState = "all";
      state.selectionScope = "all-qualified";
      state.sectorId = "all";
      state.query = "";
      if (search) search.value = "";
      saveView();
      if (scopeChanged) void requestSnapshot();
      else renderWorkspaces();
    });
    const showCurrentSegments = byId("es-show-current-segments");
    if (showCurrentSegments) showCurrentSegments.addEventListener("click", () => {
      const scopeChanged = state.selectionScope !== "all-qualified";
      state.pointType = "all";
      state.lifecycle = "all";
      state.market = "all";
      state.signalSource = "all";
      state.reviewStage = "all";
      state.segmentState = "current";
      state.selectionScope = "all-qualified";
      state.sectorId = "all";
      state.query = "";
      if (search) search.value = "";
      saveView();
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
