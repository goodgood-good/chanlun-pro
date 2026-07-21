"use strict";

(function attachTradingScreeningUi(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TradingScreeningUi = api;
})(typeof globalThis === "object" ? globalThis : this, function createTradingScreeningUi() {
  const SCHEMA_VERSION = "chanlun-trading-screening/v2";
  const POINT_TYPES = ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"];
  const LAYOUTS = new Set(["single", "split", "quad"]);
  const POINT_LABELS = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
  };
  const LIFECYCLE_LABELS = {
    observed: "结构观察",
    approaching: "即将确认",
    armed: "已入观察池",
    triggered: "1m 已触发",
    executable: "可执行复核",
    active: "持有跟踪",
    invalidated: "结构已失效",
    closed: "跟踪已结束",
  };
  const DIRECTION_LABELS = { up: "向上", down: "向下", neutral: "震荡" };
  const DISPOSITION_LABELS = { supportive: "支撑", neutral: "中性", hostile: "风险" };

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function numberText(value) {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "0";
  }

  function scanCoverageText(audit) {
    const safeAudit = isRecord(audit) ? audit : {};
    const planned = Math.max(0, Number(safeAudit.planned_symbol_count) || 0);
    const completed = Math.max(0, Number(safeAudit.completed_symbol_count) || 0);
    const pending = Math.max(0, Number(safeAudit.pending_symbol_count) || 0);
    if (
      safeAudit.background_full_refresh_required === true
      && planned === 0
      && completed === 0
      && pending === 0
    ) return "等待首批扫描";
    return pending > 0
      ? `本批 ${completed}/${planned} · 待扫 ${pending}`
      : `本批 ${completed}/${planned} · 队列已覆盖`;
  }

  function sectorCoverageText(audit) {
    const safeAudit = isRecord(audit) ? audit : {};
    const discovered = Math.max(0, Number(safeAudit.sector_discovered_count) || 0);
    const completed = Math.max(0, Number(safeAudit.sector_completed_count) || 0);
    const failed = Math.max(
      0,
      Number(safeAudit.sector_failed_count) || Math.max(0, discovered - completed),
    );
    const providedRatio = Number(safeAudit.sector_completion_ratio);
    const ratio = Number.isFinite(providedRatio)
      ? Math.min(1, Math.max(0, providedRatio))
      : discovered > 0 ? Math.min(1, completed / discovered) : 0;
    const percentage = new Intl.NumberFormat("zh-CN", {
      style: "percent",
      maximumFractionDigits: 1,
    }).format(ratio);
    return `发现 ${discovered} · 完成 ${completed} · 失败 ${failed} · 成功率 ${percentage}`;
  }

  function selectedSectorCount(snapshot) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const audit = isRecord(safeSnapshot.scan_audit) ? safeSnapshot.scan_audit : {};
    const explicit = Number(audit.selected_sector_count);
    if (Number.isFinite(explicit) && explicit >= 0) return Math.floor(explicit);
    return (Array.isArray(safeSnapshot.sectors) ? safeSnapshot.sectors : [])
      .filter((sector) => isRecord(sector) && Number.isFinite(Number(sector.rank)))
      .length;
  }

  function sectorEvidenceText(sector) {
    const safeSector = isRecord(sector) ? sector : {};
    return ["30m", "5m"].map((frequency) => {
      const context = isRecord(safeSector[`context_${frequency}`])
        ? safeSector[`context_${frequency}`]
        : {};
      const direction = DIRECTION_LABELS[context.direction] || "待判定";
      const disposition = DISPOSITION_LABELS[context.disposition] || "待判定";
      const point = POINT_LABELS[context.dominant_point_type] || "无主导点";
      return `${frequency} ${direction}/${disposition}/${point}`;
    }).join(" · ");
  }

  function timeText(value) {
    if (!value) return "尚未发布";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  function normalizeSnapshot(value) {
    if (!isRecord(value) || value.schema_version !== SCHEMA_VERSION) {
      throw new Error("snapshot_schema_invalid");
    }
    if (
      value.sector_first !== true
      || value.read_only !== true
      || value.research_only !== true
      || value.no_order_execution !== true
    ) {
      throw new Error("snapshot_boundary_invalid");
    }
    if (!Array.isArray(value.sectors) || !Array.isArray(value.signals) || !isRecord(value.data_quality)) {
      throw new Error("snapshot_shape_invalid");
    }
    return {
      ...value,
      counts_by_stage: isRecord(value.counts_by_stage) ? { ...value.counts_by_stage } : {},
      counts_by_point_type: isRecord(value.counts_by_point_type)
        ? { ...value.counts_by_point_type }
        : Object.fromEntries(POINT_TYPES.map((point) => [point, 0])),
      sectors: value.sectors.filter(isRecord).map((row) => ({ ...row })),
      signals: value.signals.filter(isRecord).map((row) => ({ ...row })),
      data_quality: { ...value.data_quality },
      errors: Array.isArray(value.errors) ? value.errors.slice() : [],
    };
  }

  function filterSignals(signals, filters = {}) {
    const pointType = text(filters.pointType, "all");
    const lifecycle = text(filters.lifecycle, "all");
    const sectorId = text(filters.sectorId, "all");
    const query = text(filters.query, "").trim().toLocaleLowerCase("zh-CN");
    return (Array.isArray(signals) ? signals : []).filter((signal) => {
      if (!isRecord(signal)) return false;
      if (pointType !== "all" && signal.point_type !== pointType) return false;
      if (lifecycle !== "all" && signal.lifecycle_stage !== lifecycle) return false;
      const sector = isRecord(signal.sector) ? signal.sector : {};
      if (sectorId !== "all" && text(sector.sector_id, "unclassified") !== sectorId) return false;
      if (!query) return true;
      return [signal.code, signal.name, sector.sector_name, POINT_LABELS[signal.point_type]]
        .map((part) => text(part, "").toLocaleLowerCase("zh-CN"))
        .some((part) => part.includes(query));
    });
  }

  function groupSignalsBySector(signals) {
    const rows = (Array.isArray(signals) ? signals : []).filter(isRecord);
    const sectorIds = Array.from(new Set(rows.map((signal) => {
      const sector = isRecord(signal.sector) ? signal.sector : {};
      return text(sector.sector_id, "unclassified");
    }))).sort((left, right) => left.localeCompare(right, "zh-CN"));
    return Object.fromEntries(sectorIds.map((sectorId) => [
      sectorId,
      rows.filter((signal) => {
        const sector = isRecord(signal.sector) ? signal.sector : {};
        return text(sector.sector_id, "unclassified") === sectorId;
      }),
    ]));
  }

  function chartUrlsForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const supplied = isRecord(safeSignal.chart_urls) ? safeSignal.chart_urls : {};
    const code = encodeURIComponent(text(safeSignal.code, ""));
    const fallback = (interval) => `/?market=a&code=${code}&layout=single&intervals=${interval}`;
    const normalized = (frequency, interval) => {
      const value = text(supplied[frequency], "");
      return value && !/[?&]frequency=/.test(value) ? value : fallback(interval);
    };
    return {
      "30m": normalized("30m", "30"),
      "5m": normalized("5m", "5"),
      "1m": normalized("1m", "1"),
    };
  }

  function setChartLayout(rootElement, requested) {
    const layout = LAYOUTS.has(requested) ? requested : "single";
    if (rootElement && rootElement.dataset) {
      rootElement.dataset.layout = layout;
      rootElement.dataset.currentLayout = layout;
    }
    return layout;
  }

  function element(documentRef, tag, className, content) {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function replaceList(container, values, emptyText) {
    if (!container || !container.ownerDocument) return;
    const documentRef = container.ownerDocument;
    const items = Array.isArray(values) && values.length ? values : [emptyText];
    const fragment = documentRef.createDocumentFragment();
    for (const value of items) fragment.append(element(documentRef, "li", "", text(value)));
    container.replaceChildren(fragment);
  }

  function renderSectorWorkspace(container, snapshot, selectedSectorId = "all", onSelect) {
    if (!container || !container.ownerDocument) return;
    const documentRef = container.ownerDocument;
    const grouped = groupSignalsBySector(snapshot.signals);
    const sectorRows = snapshot.sectors.slice().sort((left, right) => {
      const leftRank = Number.isFinite(Number(left.rank)) ? Number(left.rank) : Number.MAX_SAFE_INTEGER;
      const rightRank = Number.isFinite(Number(right.rank)) ? Number(right.rank) : Number.MAX_SAFE_INTEGER;
      return leftRank - rightRank || text(left.sector_id).localeCompare(text(right.sector_id), "zh-CN");
    });
    const rows = [{ sector_id: "all", sector_name: "全部板块", rank: null }, ...sectorRows];
    const fragment = documentRef.createDocumentFragment();
    for (const sector of rows) {
      const sectorId = text(sector.sector_id, "unclassified");
      const count = sectorId === "all" ? snapshot.signals.length : (grouped[sectorId] || []).length;
      const button = element(documentRef, "button", "es-sector-row");
      button.type = "button";
      button.dataset.sectorId = sectorId;
      button.classList.toggle("is-active", sectorId === selectedSectorId);
      button.setAttribute("aria-pressed", sectorId === selectedSectorId ? "true" : "false");
      const heading = element(documentRef, "span", "es-sector-row__heading");
      heading.append(
        element(documentRef, "strong", "", sectorId === "all" ? "全部板块" : text(sector.sector_name, sectorId)),
        element(documentRef, "b", "", numberText(count)),
      );
      const reasonCodes = Array.isArray(sector.reason_codes) ? sector.reason_codes : [];
      const rank = sector.rank === null || sector.rank === undefined
        ? "未入选"
        : `#${numberText(sector.rank)}`;
      const facts = sectorId === "all"
        ? `共 ${numberText(snapshot.sectors.length)} 个原生行业板块`
        : `结构排序 ${rank} · ${sectorEvidenceText(sector)}`;
      const gate = sectorId === "all"
        ? "仅按结构筛选，不使用板块涨跌幅"
        : sector.hard_block === true
          ? `硬阻断：${text(reasonCodes[0], "原因未提供")}`
          : `结构依据：${text(reasonCodes[0], "待补充")}`;
      button.classList.toggle("is-blocked", sector.hard_block === true);
      button.append(
        heading,
        element(documentRef, "small", "", facts),
        element(documentRef, "small", "es-sector-row__reason", gate),
      );
      if (typeof onSelect === "function") button.addEventListener("click", () => onSelect(sectorId));
      fragment.append(button);
    }
    container.replaceChildren(fragment);
  }

  function signalCard(documentRef, signal, selected, onSelect) {
    const card = element(documentRef, "button", `es-signal-card is-${text(signal.side, "neutral")}`);
    card.type = "button";
    card.dataset.signalId = text(signal.signal_id, "");
    card.classList.toggle("is-selected", selected);
    card.setAttribute("aria-pressed", selected ? "true" : "false");

    const identity = element(documentRef, "span", "es-signal-card__identity");
    identity.append(
      element(documentRef, "strong", "", text(signal.name, signal.code)),
      element(documentRef, "code", "", text(signal.code)),
    );
    const tags = element(documentRef, "span", "es-signal-card__tags");
    tags.append(
      element(documentRef, "b", "", POINT_LABELS[signal.point_type] || text(signal.point_type)),
      element(documentRef, "em", "", LIFECYCLE_LABELS[signal.lifecycle_stage] || text(signal.lifecycle_stage)),
    );
    const sector = isRecord(signal.sector) ? signal.sector : {};
    const evidence = element(documentRef, "span", "es-signal-card__evidence");
    evidence.textContent = `${text(sector.sector_name, "未分类")} · 30m ${text(signal.context_30m && signal.context_30m.disposition, "待判定")} · 5m ${POINT_LABELS[signal.point_type] || text(signal.point_type)} · 1m ${signal.trigger_1m ? "已确认" : "等待"}`;
    const meta = element(documentRef, "span", "es-signal-card__meta");
    meta.append(
      element(documentRef, "span", "", `${text(signal.tower, "bi")} 中枢 · 递归 ${numberText(signal.recursive_level)}`),
      element(documentRef, "time", "", timeText(signal.observed_at)),
    );
    const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    const invalidation = setup.invalidation_price ?? signal.structural_stop;
    const risk = element(documentRef, "span", "es-signal-card__risk");
    risk.textContent = `失效 ${text(invalidation, "未提供")} · 结构止损 ${text(signal.structural_stop, "未提供")} · 风险乘数 ${text(signal.risk_multiplier, "0")}`;
    card.append(identity, tags, evidence, meta, risk);
    if (typeof onSelect === "function") card.addEventListener("click", () => onSelect(signal));
    return card;
  }

  function renderSignalWorkspace(container, signals, selectedSignalId, onSelect) {
    if (!container || !container.ownerDocument) return;
    const documentRef = container.ownerDocument;
    const fragment = documentRef.createDocumentFragment();
    for (const signal of signals) {
      fragment.append(signalCard(
        documentRef,
        signal,
        text(signal.signal_id, "") === selectedSignalId,
        onSelect,
      ));
    }
    container.replaceChildren(fragment);
  }

  function setNodeText(rootElement, selector, value) {
    const node = rootElement && rootElement.querySelector ? rootElement.querySelector(selector) : null;
    if (node) node.textContent = text(value);
  }

  function renderChartWorkspace(rootElement, signal) {
    if (!rootElement || !rootElement.querySelector) return;
    const placeholder = rootElement.querySelector("[data-chart-placeholder]");
    const content = rootElement.querySelector("[data-chart-content]");
    if (!signal) {
      if (placeholder) placeholder.hidden = false;
      if (content) content.hidden = true;
      return;
    }
    if (placeholder) placeholder.hidden = true;
    if (content) content.hidden = false;
    setNodeText(rootElement, "[data-selected-name]", text(signal.name, signal.code));
    setNodeText(rootElement, "[data-selected-code]", signal.code);
    setNodeText(rootElement, "[data-selected-point]", POINT_LABELS[signal.point_type] || signal.point_type);
    setNodeText(rootElement, "[data-selected-stage]", LIFECYCLE_LABELS[signal.lifecycle_stage] || signal.lifecycle_stage);
    setNodeText(rootElement, "[data-selected-tower]", `${text(signal.tower, "bi")} 中枢 / 递归 ${numberText(signal.recursive_level)}`);
    setNodeText(rootElement, "[data-selected-stop]", text(signal.structural_stop, "未提供"));
    setNodeText(rootElement, "[data-selected-risk]", text(signal.risk_multiplier, "0"));
    const urls = chartUrlsForSignal(signal);
    for (const frequency of ["30m", "5m", "1m"]) {
      const frame = rootElement.querySelector(`[data-chart-frame="${frequency}"]`);
      const link = rootElement.querySelector(`[data-chart-link="${frequency}"]`);
      if (frame && frame.getAttribute("src") !== urls[frequency]) frame.setAttribute("src", urls[frequency]);
      if (link) link.setAttribute("href", urls[frequency]);
    }
    const workbench = rootElement.querySelector("[data-chart-workbench]");
    if (workbench) workbench.setAttribute("href", urls["1m"]);
    const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    const context = isRecord(signal.context_30m) ? signal.context_30m : {};
    const trigger = isRecord(signal.trigger_1m) ? signal.trigger_1m : null;
    replaceList(rootElement.querySelector("[data-evidence-30m]"), context.reason_codes, `方向 ${text(context.direction, "待判定")}；关系 ${text(context.disposition, "待判定")}`);
    replaceList(rootElement.querySelector("[data-evidence-5m]"), setup.evidence_codes, `${POINT_LABELS[setup.point_type || signal.point_type] || text(setup.point_type || signal.point_type)}；中枢序号 ${text(setup.center_ordinal, "不适用")}`);
    replaceList(rootElement.querySelector("[data-evidence-1m]"), trigger && trigger.evidence_codes, trigger ? `${POINT_LABELS[trigger.point_type] || text(trigger.point_type)} 已触发` : "尚未取得 1m 同向触发");
    replaceList(rootElement.querySelector("[data-decision-reasons]"), signal.decision_reasons, signal.entry_allowed || signal.exit_allowed ? "结构条件已进入可执行复核" : "等待剩余结构条件");
  }

  return {
    LIFECYCLE_LABELS,
    POINT_LABELS,
    POINT_TYPES,
    SCHEMA_VERSION,
    chartUrlsForSignal,
    filterSignals,
    groupSignalsBySector,
    normalizeSnapshot,
    renderChartWorkspace,
    renderSectorWorkspace,
    renderSignalWorkspace,
    scanCoverageText,
    sectorCoverageText,
    sectorEvidenceText,
    selectedSectorCount,
    setChartLayout,
    text,
    timeText,
  };
});
