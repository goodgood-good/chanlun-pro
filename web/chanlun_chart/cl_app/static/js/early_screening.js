"use strict";

(function startTradingScreeningController() {
  const POLL_INTERVAL_MS = 60_000;
  const STORAGE_KEY = "chanlun:trading-screening:view-v1";

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
    const savedSelectionScope = saved.selectionScope === "all-qualified"
      ? "all-qualified"
      : "sector-trigger";
    const state = {
      snapshot: null,
      selectedSignalId: null,
      // The page is primarily an early stock-selection workspace.  Preserve
      // an explicit prior choice, but do not let hundreds of no-position sell
      // observations bury the buy shortlist on a first visit.
      pointType: saved.pointType || "buy",
      lifecycle: saved.lifecycle || "all",
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

    function readView() {
      try {
        const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        return value && typeof value === "object" ? value : {};
      } catch (_error) {
        return {};
      }
    }

    function saveView() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
          pointType: state.pointType,
          lifecycle: state.lifecycle,
          selectionScope: state.selectionScope,
          layout: state.layout,
          chartSizing: state.chartSizing,
        }));
      } catch (_error) {
        // Storage is optional; the live snapshot remains the source of truth.
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
      const completion = Ui.scanCoverageText(audit, snapshot);
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
      const cycleInProgress = pending > 0 || audit.coverage_cycle_complete === false;
      const errorCount = Array.isArray(snapshot.errors) ? snapshot.errors.length : 0;
      const failureCodes = Array.isArray(quality.failure_codes) ? quality.failure_codes : [];
      const runtimeHealth = snapshot.runtime_health && typeof snapshot.runtime_health === "object"
        ? snapshot.runtime_health
        : {};
      const runtimeReasons = Array.isArray(runtimeHealth.reasons)
        ? runtimeHealth.reasons.join(" · ")
        : "screening_health_unavailable";
      const priorityMonitorWarning = runtimeHealth.priority_monitoring_enabled === true
        && runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.priority_monitor_ready !== true;
      const realtimeAlertWarning = runtimeHealth.priority_monitor_session_open === true
        && runtimeHealth.realtime_alert_ready !== true;
      const priorityMonitorReasons = Array.isArray(runtimeHealth.priority_monitor_reason_codes)
        ? runtimeHealth.priority_monitor_reason_codes.join(" · ")
        : "PRIORITY_MONITOR_UNAVAILABLE";
      const fullCoveragePaused = runtimeHealth.full_coverage_refresh_paused === true;
      const fullCoverageNextActive = Ui.timeText(
        runtimeHealth.full_coverage_next_active_at,
      );

      if (runtimeHealth.required === true && runtimeHealth.ready !== true) {
        setStatus(
          "warning",
          "后台选股扫描健康门未通过",
          `当前仍显示最后一份只读快照；${runtimeReasons}`,
        );
      } else if (priorityMonitorWarning || realtimeAlertWarning) {
        setStatus(
          "warning",
          "盘中实时预警通道尚未就绪",
          `提前选股快照仍可查看；优先复查持仓、自选和强板块候选的通道状态：${priorityMonitorReasons}`,
        );
      } else if (snapshot.scan_state === "complete" && !quality.stale) {
        if (cycleInProgress) {
          if (fullCoveragePaused) {
            const liveLane = runtimeHealth.priority_monitor_session_open === true
              ? "盘中算力正用于持仓、自选与强板块候选的实时预警"
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
        setStatus("warning", "快照状态需要复核", Ui.text(snapshot.scan_state, "未知状态"));
      }

      setText("es-generated", Ui.timeText(snapshot.generated_at));
      setText("es-sector-completion", sectorCompletion);
      setText("es-completion", completion);
      setText("es-scan-timing", Ui.scanTimingText(audit));
      setText("es-quality", Ui.scanQualityText(snapshot));
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
        Ui.priorityMonitorText(runtimeHealth, liveOverlay),
      );
      setText(
        "es-priority-monitor-diagnostic",
        Ui.priorityMonitorDiagnosticsText(runtimeHealth, liveOverlay),
      );
      setText(
        "es-snapshot-diagnostic",
        `小板块资格排除 ${sectorExcluded} · 历史不足排除 ${excluded} · 真实失败 ${errorCount}`,
      );
      setText("es-sector-count", Ui.selectedSectorCount(snapshot));
      setText("es-signal-count", snapshot.signals.length);
      setText(
        "es-sector-trigger-count",
        Number(snapshot.sector_trigger_signal_count) || 0,
      );
      setText(
        "es-total-qualified-count",
        Number(snapshot.total_qualified_signal_count) || snapshot.signals.length,
      );
      setText("es-approaching-count", countStage("approaching"));
      setText("es-armed-count", countStage("armed"));
      setText("es-triggered-count", countStage("triggered"));
      setText("es-executable-count", countStage("executable"));
      setText("es-closed-count", countStage("invalidated") + countStage("closed"));
      document.title = snapshot.signals.length
        ? `(${snapshot.signals.length}) 缠论提前选股 · 实时盯盘与个股分析`
        : "缠论提前选股 · 实时盯盘与个股分析";
    }

    function renderManualHoldings() {
      const snapshot = state.snapshot || {};
      const holdings = snapshot.manual_holdings && typeof snapshot.manual_holdings === "object"
        ? snapshot.manual_holdings
        : {};
      const positions = Array.isArray(holdings.positions)
        ? holdings.positions.filter((row) => row && typeof row === "object")
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
        armed: 3,
        approaching: 4,
        observed: 5,
        invalidated: 6,
        closed: 7,
      };
      const signalsByIdentity = new Map();
      const holdingSignals = [
        ...(Array.isArray(snapshot.signals) ? snapshot.signals : []),
        ...(Array.isArray(snapshot.manual_holding_signals)
          ? snapshot.manual_holding_signals
          : []),
      ];
      for (const signal of holdingSignals) {
        if (!signal || typeof signal !== "object") continue;
        const code = normalizeCode(signal.code);
        if (!code) continue;
        const key = identityKey(inferSignalMarket(signal), code);
        const current = signalsByIdentity.get(key);
        const nextRank = stagePriority[Ui.text(signal.lifecycle_stage, "")] ?? 99;
        const currentRank = current
          ? stagePriority[Ui.text(current.lifecycle_stage, "")] ?? 99
          : Number.POSITIVE_INFINITY;
        if (!current || nextRank < currentRank) signalsByIdentity.set(key, signal);
      }

      const monitored = positions.filter((row) => row.realtime_status === "monitoring").length;
      const waiting = positions.filter((row) => row.realtime_status !== "monitoring").length;
      setText("es-holdings-declared", positions.length);
      setText("es-holdings-monitored", monitored);
      setText("es-holdings-unsupported", waiting);

      if (holdings.available !== true) {
        setText("es-holdings-status", "本地持仓分组暂不可用；未访问任何交易账户。");
      } else if (positions.length) {
        setText(
          "es-holdings-status",
          "A股使用统一选股决策核心；其他市场使用独立辅助结构雷达。两者均不读取交易账户。",
        );
      } else {
        setText(
          "es-holdings-status",
          "“我的持仓”是人工声明的跨市场分组，不读取 QMT 或其他交易账户。",
        );
      }

      if (empty) {
        empty.hidden = positions.length !== 0;
        empty.textContent = holdings.available === true
          ? "“我的持仓”尚无标的；可在行情页把关注标的加入该分组。"
          : "暂时无法读取本地“我的持仓”分组，系统不会用账户数据补填。";
      }
      if (!list) return;
      const fragment = document.createDocumentFragment();
      const alertStages = new Set(["approaching", "armed", "triggered", "executable", "active"]);
      const marketLabels = {
        a: "A股", hk: "港股", us: "美股", fx: "外汇", futures: "期货",
        ny_futures: "纽约期货", currency: "数字货币", currency_spot: "数字货币现货",
      };
      for (const position of positions) {
        const market = Ui.text(position.market, "").trim();
        const code = Ui.text(position.code, "").trim();
        const name = Ui.text(position.name, code);
        const signal = signalsByIdentity.get(identityKey(market, code)) || null;
        const stage = signal ? Ui.text(signal.lifecycle_stage, "") : "";
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
        const realtimeStatus = Ui.text(position.realtime_status, "awaiting_first_run");
        if (realtimeStatus === "error") {
          status.textContent = "实时监听异常 · 系统将重试";
          card.classList.add("is-alert");
        } else if (signal) {
          const point = Ui.POINT_LABELS[signal.point_type] || Ui.text(signal.point_type, "结构提示");
          status.textContent = `${Ui.lifecycleLabel(stage)} · ${point}`;
        } else if (realtimeStatus === "market_closed") {
          status.textContent = "当前休市 · 开市后自动恢复实时监听";
        } else if (realtimeStatus === "warming_up") {
          status.textContent = "多周期历史暖机中 · 暂不产生提醒";
        } else if (realtimeStatus === "monitoring") {
          status.textContent = position.monitoring_scope === "A_SHARE_STRICT_DECISION_CORE"
            ? "统一决策核心实时监听中 · 暂无新增预警"
            : "非A股辅助结构雷达监听中 · 暂无新增线索";
        } else {
          status.textContent = "等待首次实时检查";
        }
        card.append(heading, status);
        fragment.append(card);
      }
      list.replaceChildren(fragment);
    }

    function selectionScopedSignals() {
      if (!state.snapshot) return [];
      return state.selectionScope === "sector-trigger"
        ? state.snapshot.signals.filter((signal) => (
          Array.isArray(signal.selection_sources)
          && signal.selection_sources.includes("QMT_SECTOR_TRIGGER")
        ))
        : state.snapshot.signals;
    }

    function currentSignals() {
      if (!state.snapshot) return [];
      const source = selectionScopedSignals();
      const filtered = Ui.filterSignals(source, {
        pointType: state.pointType,
        lifecycle: state.lifecycle,
        sectorId: state.sectorId,
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
      document.querySelectorAll("[data-point-type]").forEach((button) => {
        const point = button.dataset.pointType;
        const count = point === "all"
          ? scopedSignals.length
          : point === "buy"
            ? scopedSignals.filter((signal) => /buy$/.test(Ui.text(signal.point_type, ""))).length
            : point === "sell"
              ? scopedSignals.filter((signal) => /sell$/.test(Ui.text(signal.point_type, ""))).length
              : scopedSignals.filter((signal) => Ui.text(signal.point_type, "") === point).length;
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-lifecycle]").forEach((button) => {
        const stage = button.dataset.lifecycle;
        const count = stage === "all"
          ? scopedSignals.length
          : scopedSignals.filter(
            (signal) => Ui.text(signal.lifecycle_stage, "") === stage,
          ).length;
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
    }

    function syncFilterSummary() {
      const lifecycle = Array.from(document.querySelectorAll("[data-lifecycle]"))
        .find((button) => button.dataset.lifecycle === state.lifecycle);
      const pointType = Array.from(document.querySelectorAll("[data-point-type]"))
        .find((button) => button.dataset.pointType === state.pointType);
      setText(
        "es-filter-summary",
        `${state.selectionScope === "sector-trigger" ? "板块已触发" : "全部资格观察"} · ${lifecycle ? lifecycle.textContent.trim() : "全部"} · ${pointType ? pointType.textContent.trim() : "全部"}`,
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
      const filtered = currentSignals();
      state.selectedSignalId = Ui.resolveSelectedSignalId(
        state.selectedSignalId,
        filtered,
        state.snapshot.signals,
      );
      const selected = state.snapshot.signals.find(
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
      Ui.renderSignalWorkspace(signalList, filtered, state.selectedSignalId, selectSignal);
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
        Ui.emptySignalDetail(state.snapshot, state.query),
      );
      syncButtons("[data-point-type]", "pointType", state.pointType);
      syncButtons("[data-lifecycle]", "lifecycle", state.lifecycle);
      syncButtons("[data-selection-scope]", "selectionScope", state.selectionScope);
      syncButtons(".es-layout-switch [data-layout]", "layout", state.layout);
      syncFilterCounts();
      syncFilterSummary();
      syncSectorExpandButton();
    }

    function render() {
      snapshotHeader();
      renderManualHoldings();
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
        const response = await fetch(endpoint.toString(), {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const payload = await response.json();
        if (!response.ok || !payload || payload.ok !== true) throw new Error("snapshot_request_failed");
        const nextSnapshot = Ui.normalizeSnapshot(payload.data);
        if (nextSnapshot.presentation_scope !== requestedScope) {
          throw new Error("snapshot_scope_mismatch");
        }
        state.snapshot = nextSnapshot;
        render();
        return true;
      } catch (error) {
        setStatus(
          "error",
          "实时快照暂不可用",
          state.snapshot ? "保留当前页面数据，下一轮继续重试" : "未展示未经验证或边界不完整的数据",
        );
        console.error("trading_screening_snapshot_failed", error && error.name ? error.name : "Error");
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
