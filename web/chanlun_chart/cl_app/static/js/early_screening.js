"use strict";

(function startTradingScreeningController() {
  const POLL_INTERVAL_MS = 60_000;
  const STORAGE_KEY = "chanlun:trading-screening:view-v1";

  function boot() {
    const Ui = globalThis.TradingScreeningUi;
    const root = document.getElementById("es-dashboard");
    if (!Ui || !root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const byId = (id) => document.getElementById(id);
    const sectorList = byId("es-sector-list");
    const signalList = byId("es-signal-list");
    const chartWorkspace = byId("es-chart-workspace");
    const saved = readView();
    const state = {
      snapshot: null,
      selectedSignalId: null,
      pointType: saved.pointType || "all",
      lifecycle: saved.lifecycle || "all",
      sectorId: "all",
      query: "",
      layout: Ui.setChartLayout(chartWorkspace, saved.layout || "single"),
      loading: false,
      pollTimer: null,
    };

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
          layout: state.layout,
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
      const completion = Ui.scanCoverageText(audit);
      const sectorCompletion = Ui.sectorCoverageText(audit);
      const pending = Math.max(0, Number(audit.pending_symbol_count) || 0);
      const errorCount = Array.isArray(snapshot.errors) ? snapshot.errors.length : 0;
      const failureCodes = Array.isArray(quality.failure_codes) ? quality.failure_codes : [];

      if (snapshot.scan_state === "complete" && !quality.stale) {
        setStatus(
          quality.complete ? "ready" : "warning",
          quality.complete ? "最新结构快照可用" : "快照可用，部分标的缺少数据",
          pending > 0
            ? `本批结果已发布；后台按结构队列继续覆盖，剩余 ${pending} 只${errorCount ? `；${errorCount} 只标的需复核` : ""}`
            : `当前结构队列已覆盖${errorCount ? `；${errorCount} 只标的需复核` : ""}`,
        );
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
      setText("es-quality", quality.stale ? "已过期" : quality.complete ? "完整" : "部分完整");
      setText("es-sector-count", Ui.selectedSectorCount(snapshot));
      setText("es-signal-count", snapshot.signals.length);
      setText("es-approaching-count", countStage("approaching"));
      setText("es-armed-count", countStage("armed"));
      setText("es-triggered-count", countStage("triggered"));
      setText("es-executable-count", countStage("executable"));
      setText("es-closed-count", countStage("invalidated") + countStage("closed"));
      document.title = snapshot.signals.length
        ? `(${snapshot.signals.length}) 缠论提前选股 · 实时盯盘与个股分析`
        : "缠论提前选股 · 实时盯盘与个股分析";
    }

    function currentSignals() {
      if (!state.snapshot) return [];
      return Ui.filterSignals(state.snapshot.signals, {
        pointType: state.pointType,
        lifecycle: state.lifecycle,
        sectorId: state.sectorId,
        query: state.query,
      });
    }

    function syncButtons(selector, dataKey, selected) {
      document.querySelectorAll(selector).forEach((button) => {
        const active = button.dataset[dataKey] === selected;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function syncFilterCounts() {
      if (!state.snapshot) return;
      document.querySelectorAll("[data-point-type]").forEach((button) => {
        const point = button.dataset.pointType;
        const count = point === "all"
          ? state.snapshot.signals.length
          : Number(state.snapshot.counts_by_point_type[point]) || 0;
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
      document.querySelectorAll("[data-lifecycle]").forEach((button) => {
        const stage = button.dataset.lifecycle;
        const count = stage === "all" ? state.snapshot.signals.length : countStage(stage);
        button.dataset.count = String(count);
        button.setAttribute("aria-label", `${button.textContent.trim()}，${count} 条`);
      });
    }

    function selectSignal(signal) {
      state.selectedSignalId = signal ? Ui.text(signal.signal_id, "") : null;
      renderWorkspaces();
    }

    function renderWorkspaces() {
      if (!state.snapshot) return;
      const filtered = currentSignals();
      if (!filtered.some((row) => Ui.text(row.signal_id, "") === state.selectedSignalId)) {
        state.selectedSignalId = filtered.length ? Ui.text(filtered[0].signal_id, "") : null;
      }
      const selected = state.snapshot.signals.find(
        (row) => Ui.text(row.signal_id, "") === state.selectedSignalId,
      ) || null;

      Ui.renderSectorWorkspace(
        sectorList,
        state.snapshot,
        state.sectorId,
        (sectorId) => {
          state.sectorId = sectorId;
          renderWorkspaces();
        },
      );
      Ui.renderSignalWorkspace(signalList, filtered, state.selectedSignalId, selectSignal);
      Ui.renderChartWorkspace(chartWorkspace, selected);

      setText("es-visible-count", `${filtered.length} / ${state.snapshot.signals.length}`);
      const empty = byId("es-empty");
      if (empty) empty.hidden = filtered.length !== 0;
      syncButtons("[data-point-type]", "pointType", state.pointType);
      syncButtons("[data-lifecycle]", "lifecycle", state.lifecycle);
      syncButtons("[data-layout]", "layout", state.layout);
      syncFilterCounts();
    }

    function render() {
      snapshotHeader();
      renderWorkspaces();
    }

    function schedulePoll() {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = window.setTimeout(() => {
        if (document.visibilityState === "visible") void requestSnapshot();
        else schedulePoll();
      }, POLL_INTERVAL_MS);
    }

    async function requestSnapshot() {
      if (state.loading) return;
      state.loading = true;
      root.dataset.loading = "true";
      const refreshButton = byId("es-refresh-now");
      if (refreshButton) refreshButton.disabled = true;
      if (!state.snapshot) setStatus("loading", "正在读取最新快照", "正在核对新交易系统的数据边界");
      try {
        const response = await fetch(root.dataset.endpoint, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const payload = await response.json();
        if (!response.ok || !payload || payload.ok !== true) throw new Error("snapshot_request_failed");
        state.snapshot = Ui.normalizeSnapshot(payload.data);
        render();
      } catch (error) {
        setStatus(
          "error",
          "实时快照暂不可用",
          state.snapshot ? "保留当前页面数据，下一轮继续重试" : "未展示未经验证或边界不完整的数据",
        );
        console.error("trading_screening_snapshot_failed", error && error.name ? error.name : "Error");
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
    document.querySelectorAll("[data-layout]").forEach((button) => {
      button.addEventListener("click", () => {
        state.layout = Ui.setChartLayout(chartWorkspace, button.dataset.layout);
        saveView();
        syncButtons("[data-layout]", "layout", state.layout);
      });
    });
    const search = byId("es-signal-search");
    if (search) search.addEventListener("input", () => {
      state.query = search.value;
      renderWorkspaces();
    });
    const refreshButton = byId("es-refresh-now");
    if (refreshButton) refreshButton.addEventListener("click", () => void requestSnapshot());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void requestSnapshot();
    });

    void requestSnapshot();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
})();
