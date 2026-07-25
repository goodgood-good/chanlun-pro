"use strict";

(function attachTradingScreeningUi(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TradingScreeningUi = api;
})(typeof globalThis === "object" ? globalThis : this, function createTradingScreeningUi() {
  const SCHEMA_VERSION = "chanlun-trading-screening/v3";
  const POINT_TYPES = ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"];
  const FREQUENCIES = new Set(["30m", "5m", "1m"]);
  const LAYOUTS = new Set(["focus", "dual", "triple"]);
  const LEGACY_LAYOUTS = { single: "focus", split: "dual", quad: "triple" };
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
  const TOWER_LABELS = { bi: "笔", xd: "线段" };
  const REASON_LABELS = {
    structural_ranking_only: "仅按缠论结构排序",
    core_confirmed_point: "核心买卖点已确认",
    one_minute_not_confirmed: "1分钟同向确认尚未完成",
    one_minute_sell_not_confirmed: "1分钟同向卖点确认尚未完成",
    confirmed_sell_with_down_structure: "下跌结构中的卖点已确认",
    confirmed_buy_structure: "买入方向结构已确认",
    terminal_line_confirmed: "末端结构确认",
    unfinished_core_mmd: "核心买卖点结构尚未闭合",
    no_active_directional_point: "暂无有效方向买卖点",
    same_or_higher_structure_conflict: "同级或更高结构存在反向冲突",
    structure_conflict: "结构方向存在冲突",
    thirty_minute_hostile: "30分钟环境构成阻断",
    lower_or_unrelated_structure_risk: "较低或无关结构存在风险",
    sell_not_confirmed: "卖点尚未确认",
    top_fractal_confirmed: "顶分型确认",
    bottom_fractal_confirmed: "底分型确认",
    five_minute_not_confirmed: "5分钟设置尚未确认",
    setup_not_confirmed: "5分钟设置尚未闭合",
    mixed_or_transition_structure: "结构处于混合或过渡状态",
    three_buy_not_first_center: "三买不属于当前走势第一中枢",
    no_active_position: "当前没有活动持仓",
    unfinished_trend_divergence: "趋势背驰结构尚未闭合",
    three_buy_lacks_tick_clearance: "三买回抽未留出最小价格间隔",
    sector_membership_missing: "未匹配 QMT GICS3 板块",
    higher_structure_sell_risk: "更高结构存在卖点风险",
    sector_hostile: "行业结构构成阻断",
  };
  const MISSING_REASON_CODES = new Set([
    "one_minute_not_confirmed",
    "one_minute_sell_not_confirmed",
    "five_minute_not_confirmed",
    "setup_not_confirmed",
    "sell_not_confirmed",
    "unfinished_core_mmd",
    "unfinished_trend_divergence",
  ]);

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
      ? `本批 ${completed}/${planned} · 待分析 ${pending}`
      : safeAudit.coverage_cycle_complete === false
        ? `本批 ${completed}/${planned} · 等待续扫`
        : `本批 ${completed}/${planned} · 全周期已覆盖`;
  }

  function scanQualityText(snapshot) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const audit = isRecord(safeSnapshot.scan_audit) ? safeSnapshot.scan_audit : {};
    const quality = isRecord(safeSnapshot.data_quality) ? safeSnapshot.data_quality : {};
    const pending = Math.max(0, Number(audit.pending_symbol_count) || 0);
    if (safeSnapshot.available !== true) return "等待首批";
    if (quality.stale === true) return "已过期";
    const cycleComplete = pending === 0 && audit.coverage_cycle_complete !== false;
    if (!cycleComplete) {
      return quality.complete === true
        ? "本批完整 · 全周期扫描中"
        : "本批部分完整 · 全周期扫描中";
    }
    return quality.complete === true ? "全周期完整" : "全周期部分完整";
  }

  function scanTimingText(audit) {
    const safeAudit = isRecord(audit) ? audit : {};
    const batchMs = Number(safeAudit.batch_duration_ms);
    const cycleMs = Number(safeAudit.coverage_cycle_elapsed_ms);
    const batches = Math.max(0, Number(safeAudit.coverage_cycle_batch_count) || 0);
    const duration = (milliseconds) => {
      if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
      if (milliseconds < 1000) return `${Math.round(milliseconds)}毫秒`;
      return `${(milliseconds / 1000).toFixed(1)}秒`;
    };
    const batchText = duration(batchMs);
    const cycleText = duration(cycleMs);
    if (!batchText && !cycleText) return "待统计";
    const values = [];
    if (batchText) values.push(`本批 ${batchText}`);
    if (cycleText) {
      const state = safeAudit.coverage_cycle_complete === true ? "全周期" : "全周期已运行";
      values.push(`${state} ${cycleText}${batches ? ` / ${batches}批` : ""}`);
    }
    return values.join(" · ");
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
      .filter((sector) => isRecord(sector) && sectorRank(sector.rank) !== null)
      .length;
  }

  function sectorRank(value) {
    if (value === null || value === undefined || value === "") return null;
    const rank = Number(value);
    return Number.isFinite(rank) && rank > 0 ? rank : null;
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

  function uniqueText(values) {
    const rows = Array.isArray(values) ? values : [];
    return Array.from(new Set(rows.map((value) => text(value, "").trim()).filter(Boolean)));
  }

  function reasonLabel(code) {
    const value = text(code, "").trim();
    if (!value) return "未提供";
    return REASON_LABELS[value] || `${value}（未翻译）`;
  }

  function prefixedLabels(prefix, values) {
    return uniqueText(values).map((value) => `${prefix}：${reasonLabel(value)}`);
  }

  function defaultFrequencyForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    return ["triggered", "executable", "active"].includes(safeSignal.lifecycle_stage)
      ? "1m"
      : "5m";
  }

  function resolveFocusState(previous, signal) {
    const safePrevious = isRecord(previous) ? previous : {};
    const safeSignal = isRecord(signal) ? signal : {};
    const signalId = text(safeSignal.signal_id, "");
    if (!signalId) {
      return { signalId: null, frequency: "5m", overrideSignalId: null };
    }
    const sameSignal = safePrevious.signalId === signalId;
    const hasManualOverride = sameSignal
      && safePrevious.overrideSignalId === signalId
      && FREQUENCIES.has(safePrevious.frequency);
    if (hasManualOverride) {
      return {
        signalId,
        frequency: safePrevious.frequency,
        overrideSignalId: signalId,
      };
    }
    return {
      signalId,
      frequency: defaultFrequencyForSignal(safeSignal),
      overrideSignalId: null,
    };
  }

  function manualFocusState(previous, signalId, requestedFrequency) {
    const safePrevious = isRecord(previous) ? previous : {};
    const normalizedSignalId = text(signalId, "");
    const frequency = FREQUENCIES.has(requestedFrequency)
      ? requestedFrequency
      : FREQUENCIES.has(safePrevious.frequency) ? safePrevious.frequency : "5m";
    return {
      signalId: normalizedSignalId || null,
      frequency,
      overrideSignalId: normalizedSignalId || null,
    };
  }

  function decisionSummaryForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const stage = text(safeSignal.lifecycle_stage, "");
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const allowed = safeSignal.entry_allowed === true || safeSignal.exit_allowed === true;
    let tone = "neutral";
    let title = "继续观察";
    if (!stage) {
      tone = "unknown";
      title = "数据未知";
    } else if (allowed || stage === "executable") {
      tone = "action";
      title = "可执行复核";
    } else if (stage === "triggered") {
      tone = "waiting";
      title = "已触发，等待执行复核";
    } else if (stage === "armed") {
      tone = "waiting";
      title = "等待 1分钟精确触发";
    } else if (stage === "approaching") {
      tone = "waiting";
      title = "5分钟结构正在形成";
    } else if (stage === "active") {
      tone = "action";
      title = "持有跟踪";
    } else if (stage === "invalidated") {
      tone = "blocked";
      title = "结构已失效";
    } else if (stage === "closed") {
      tone = "blocked";
      title = "跟踪已关闭";
    }
    const reasons = Array.isArray(safeSignal.decision_reasons)
      ? safeSignal.decision_reasons.filter(Boolean)
      : [];
    return {
      tone,
      title,
      detail: allowed ? "结构条件已进入可执行复核" : (reasons[0] ? reasonLabel(reasons[0]) : "等待剩余结构条件"),
      invalidation: text(setup.invalidation_price, "未提供"),
      structuralStop: text(safeSignal.structural_stop, "未提供"),
      riskMultiplier: text(safeSignal.risk_multiplier, "未提供"),
    };
  }

  function periodPathForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const trigger = isRecord(safeSignal.trigger_1m) ? safeSignal.trigger_1m : null;
    const contextKnown = Object.keys(context).length > 0;
    const setupKnown = Object.keys(setup).length > 0;
    const triggerKnown = trigger !== null && Object.keys(trigger).length > 0;

    let contextState = "未知";
    let contextTone = "unknown";
    if (contextKnown && (context.hard_block === true || context.disposition === "hostile")) {
      contextState = "阻断";
      contextTone = "blocked";
    } else if (contextKnown && context.disposition === "supportive") {
      contextState = "支持";
      contextTone = "supportive";
    } else if (contextKnown) {
      contextState = "中性";
      contextTone = "neutral";
    }

    let setupState = "未知";
    let setupTone = "unknown";
    if (setup.status === "confirmed") {
      setupState = "已确认";
      setupTone = "supportive";
    } else if (setup.status === "provisional") {
      setupState = "形成中";
      setupTone = "waiting";
    } else if (setup.status === "invalidated") {
      setupState = "已失效";
      setupTone = "blocked";
    } else if (!setupKnown) {
      setupState = "未知";
    }

    let triggerState = "等待";
    let triggerTone = "waiting";
    if (triggerKnown && trigger.status === "invalidated") {
      triggerState = "已失效";
      triggerTone = "blocked";
    } else if (triggerKnown) {
      triggerState = "已触发";
      triggerTone = "supportive";
    }

    const contextReasons = uniqueText(context.reason_codes);
    const setupEvidence = uniqueText(setup.evidence_codes);
    const triggerEvidence = triggerKnown ? uniqueText(trigger.evidence_codes) : [];
    const tower = TOWER_LABELS[setup.tower || safeSignal.tower]
      || text(setup.tower || safeSignal.tower, "未知塔层");
    const recursiveLevel = setup.recursive_level ?? safeSignal.recursive_level;
    const center = setup.center_ordinal === null || setup.center_ordinal === undefined
      ? "中枢序号不适用"
      : `第 ${text(setup.center_ordinal)} 中枢`;
    const triggerPoint = triggerKnown
      ? POINT_LABELS[trigger.point_type] || text(trigger.point_type, "精确触发")
      : null;

    return [
      {
        frequency: "30m",
        state: contextState,
        tone: contextTone,
        summary: contextKnown
          ? `方向 ${DIRECTION_LABELS[context.direction] || "待判定"} · 主导 ${POINT_LABELS[context.dominant_point_type] || "无主导点"}`
          : "大级别环境证据未提供",
        boundary: contextTone === "blocked"
          ? reasonLabel(contextReasons[0])
          : contextKnown ? "无硬阻断" : "环境证据未提供",
        evidence: contextReasons.map(reasonLabel),
      },
      {
        frequency: "5m",
        state: setupState,
        tone: setupTone,
        summary: setupKnown
          ? `${POINT_LABELS[setup.point_type || safeSignal.point_type] || text(setup.point_type || safeSignal.point_type)} · ${tower}中枢 · 递归 ${text(recursiveLevel, "未知")} · ${center}`
          : "操作级别设置未提供",
        boundary: setup.invalidation_price === null || setup.invalidation_price === undefined || setup.invalidation_price === ""
          ? "失效价未提供"
          : `失效价 ${text(setup.invalidation_price)}`,
        evidence: setupEvidence.map(reasonLabel),
      },
      {
        frequency: "1m",
        state: triggerState,
        tone: triggerTone,
        summary: triggerKnown ? `${triggerPoint}已触发` : "尚未取得同向精确触发",
        boundary: `结构止损 ${text(safeSignal.structural_stop, "未提供")}`,
        evidence: triggerEvidence.map(reasonLabel),
      },
    ];
  }

  function evidenceGroupsForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const trigger = isRecord(safeSignal.trigger_1m) && Object.keys(safeSignal.trigger_1m).length
      ? safeSignal.trigger_1m
      : null;
    const contextCodes = uniqueText(context.reason_codes);
    const setupEvidence = uniqueText(setup.evidence_codes);
    const setupPendingEvidence = setupEvidence.filter((code) => MISSING_REASON_CODES.has(code));
    const setupConfirmedEvidence = setupEvidence.filter((code) => !MISSING_REASON_CODES.has(code));
    const setupMissing = uniqueText(setup.missing_conditions);
    const triggerEvidence = trigger ? uniqueText(trigger.evidence_codes) : [];
    const triggerMissing = trigger ? uniqueText(trigger.missing_conditions) : [];
    const decisions = uniqueText(safeSignal.decision_reasons);
    const missingDecisions = decisions.filter((code) => MISSING_REASON_CODES.has(code));
    const blockingDecisions = decisions.filter((code) => !MISSING_REASON_CODES.has(code));
    const established = [
      ...(context.hard_block === true || context.disposition === "hostile"
        ? []
        : prefixedLabels("30分钟", contextCodes)),
      ...prefixedLabels("5分钟", setupConfirmedEvidence),
      ...prefixedLabels("1分钟", triggerEvidence),
    ];
    const missing = [
      ...prefixedLabels("5分钟", setupPendingEvidence),
      ...prefixedLabels("5分钟", setupMissing),
      ...prefixedLabels("1分钟", triggerMissing),
      ...(trigger ? [] : ["1分钟：尚未取得同向精确触发"]),
      ...missingDecisions.map(reasonLabel),
    ];
    const contextBlocking = context.hard_block === true || context.disposition === "hostile"
      ? contextCodes.map(reasonLabel)
      : [];
    const nextByStage = {
      observed: "等待 5分钟形成可审计买卖点设置",
      approaching: "等待 5分钟设置闭合并确认",
      armed: "等待 1分钟同向买卖点闭合",
      triggered: "等待下一根可交易 K 线执行条件复核",
      executable: "执行前复核停牌、涨跌停、成交量与滑点",
      active: "跟踪反向买卖点与结构止损",
      invalidated: "信号已失效，等待新的结构设置",
      closed: "本次跟踪已经结束",
    };
    const invalidation = text(setup.invalidation_price, "未提供");
    const structuralStop = text(safeSignal.structural_stop, "未提供");
    const riskMultiplier = text(safeSignal.risk_multiplier, "未提供");
    return {
      established: uniqueText(established),
      missing: uniqueText(missing),
      blocking: uniqueText([...contextBlocking, ...blockingDecisions.map(reasonLabel)]),
      next: [nextByStage[safeSignal.lifecycle_stage] || "等待新的可审计结构事实"],
      risk: [
        `5分钟失效价：${invalidation}`,
        `结构止损：${structuralStop}`,
        `风险乘数：${riskMultiplier}`,
      ],
      raw: uniqueText([
        ...contextCodes,
        ...setupEvidence,
        ...setupMissing,
        ...triggerEvidence,
        ...triggerMissing,
        ...decisions,
      ]),
    };
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

  function resolveSelectedSignalId(selectedSignalId, filteredSignals, allSignals) {
    const selectedId = text(selectedSignalId, "");
    const filteredIds = (Array.isArray(filteredSignals) ? filteredSignals : [])
      .map((signal) => text(isRecord(signal) ? signal.signal_id : "", ""))
      .filter(Boolean);
    const allIds = (Array.isArray(allSignals) ? allSignals : [])
      .map((signal) => text(isRecord(signal) ? signal.signal_id : "", ""))
      .filter(Boolean);
    if (selectedId && filteredIds.includes(selectedId)) return selectedId;
    if (filteredIds.length) return filteredIds[0];
    if (selectedId && allIds.includes(selectedId)) return selectedId;
    return allIds.length ? allIds[0] : null;
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
    const appendQueryValue = (url, key, value) => {
      const hashIndex = url.indexOf("#");
      const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
      const hash = hashIndex >= 0 ? url.slice(hashIndex) : "";
      const separator = base.includes("?") ? (/[?&]$/.test(base) ? "" : "&") : "?";
      return `${base}${separator}${encodeURIComponent(key)}=${encodeURIComponent(value)}${hash}`;
    };
    const withInitialSidebarState = (url) => {
      if (/[?&]chart_sidebar=/.test(url)) return url;
      return appendQueryValue(url, "chart_sidebar", "collapsed");
    };
    const withDefaultMacdStudy = (url) => {
      if (/[?&]default_study=MACD_HTF(?:&|#|$)/.test(url)) return url;
      return appendQueryValue(url, "default_study", "MACD_HTF");
    };
    const normalized = (frequency, interval) => {
      const value = text(supplied[frequency], "");
      const url = value && !/[?&]frequency=/.test(value) ? value : fallback(interval);
      return withDefaultMacdStudy(withInitialSidebarState(url));
    };
    return {
      "30m": normalized("30m", "30"),
      "5m": normalized("5m", "5"),
      "1m": normalized("1m", "1"),
    };
  }

  function setChartLayout(rootElement, requested) {
    const migrated = LEGACY_LAYOUTS[requested] || requested;
    const layout = LAYOUTS.has(migrated) ? migrated : "focus";
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
    const shortlistSize = selectedSectorCount(snapshot);
    const sectorRows = snapshot.sectors.slice().sort((left, right) => {
      const leftRank = sectorRank(left.rank) ?? Number.MAX_SAFE_INTEGER;
      const rightRank = sectorRank(right.rank) ?? Number.MAX_SAFE_INTEGER;
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
      const rank = sectorRank(sector.rank);
      const shortlisted = sectorId !== "all" && rank !== null && rank <= shortlistSize;
      button.dataset.shortlisted = shortlisted ? "true" : "false";
      button.classList.toggle("is-shortlisted", shortlisted);
      const heading = element(documentRef, "span", "es-sector-row__heading");
      heading.append(
        element(documentRef, "strong", "", sectorId === "all" ? "全部板块" : text(sector.sector_name, sectorId)),
        element(documentRef, "b", "", numberText(count)),
      );
      const reasonCodes = Array.isArray(sector.reason_codes) ? sector.reason_codes : [];
      const facts = sectorId === "all"
        ? `共 ${numberText(snapshot.sectors.length)} 个 QMT GICS3 板块`
        : `${shortlisted ? "符合要求并进入扫描" : "未通过结构门槛"} · ${rank === null ? "无有效排序" : `结构排序 #${numberText(rank)}`} · ${sectorEvidenceText(sector)}`;
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

  function setEvidencePanelOpen(rootElement, requested) {
    if (!rootElement || !rootElement.querySelector) return false;
    const open = Boolean(requested);
    rootElement.dataset.evidenceOpen = String(open);
    const toggle = rootElement.querySelector("[data-evidence-toggle]");
    const panel = rootElement.querySelector("[data-evidence-panel]");
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
    if (panel) panel.setAttribute("aria-hidden", String(!open));
    return open;
  }

  function setTheaterMode(rootElement, bodyElement, requested) {
    if (!rootElement || !rootElement.querySelector) return false;
    const active = Boolean(requested);
    rootElement.dataset.theaterMode = String(active);
    const toggle = rootElement.querySelector("[data-theater-toggle]");
    if (toggle) toggle.setAttribute("aria-pressed", String(active));
    setNodeText(rootElement, "[data-theater-label]", active ? "退出影院" : "影院模式");
    if (bodyElement && bodyElement.classList) {
      bodyElement.classList.toggle("es-theater-open", active);
    }
    return active;
  }

  function renderChartWorkspace(rootElement, signal, options = {}) {
    if (!rootElement || !rootElement.querySelector) return;
    const content = rootElement.querySelector("[data-chart-content]");
    if (content) content.hidden = false;
    if (!signal) {
      const frequency = FREQUENCIES.has(options.frequency) ? options.frequency : "5m";
      rootElement.dataset.focusedFrequency = frequency;
      rootElement.dataset.signalSide = "neutral";
      setNodeText(rootElement, "[data-selected-name]", "暂无可用信号");
      setNodeText(rootElement, "[data-selected-code]", "—");
      setNodeText(rootElement, "[data-selected-point]", "—");
      setNodeText(rootElement, "[data-selected-stage]", "—");
      setNodeText(rootElement, "[data-selected-tower]", "—");
      setNodeText(rootElement, "[data-selected-stop]", "未提供");
      setNodeText(rootElement, "[data-selected-risk]", "未提供");
      setNodeText(rootElement, "[data-decision-title]", "数据未知");
      setNodeText(rootElement, "[data-decision-detail]", "当前快照没有可用买卖点信号");
      setNodeText(rootElement, "[data-decision-invalidation]", "未提供");
      const decisionCard = rootElement.querySelector("[data-decision-card]");
      if (decisionCard && decisionCard.dataset) decisionCard.dataset.tone = "unknown";

      const emptyPeriods = [
        ["30m", "未知", "等待大级别环境证据", "环境边界未提供"],
        ["5m", "未知", "等待操作级别设置", "失效价未提供"],
        ["1m", "等待", "尚未取得同向精确触发", "结构止损未提供"],
      ];
      for (const [periodFrequency, state, summary, boundary] of emptyPeriods) {
        const periodNode = rootElement.querySelector(`[data-period-node="${periodFrequency}"]`);
        if (periodNode) {
          if (periodNode.dataset) periodNode.dataset.tone = "unknown";
          periodNode.setAttribute("aria-pressed", periodFrequency === frequency ? "true" : "false");
        }
        setNodeText(rootElement, `[data-period-state="${periodFrequency}"]`, state);
        setNodeText(rootElement, `[data-period-summary="${periodFrequency}"]`, summary);
        setNodeText(rootElement, `[data-period-boundary="${periodFrequency}"]`, boundary);
      }
      if (rootElement.querySelectorAll) {
        rootElement.querySelectorAll("[data-focus-frequency]").forEach((button) => {
          const active = button.dataset.focusFrequency === frequency;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-pressed", active ? "true" : "false");
        });
      }
      for (const periodFrequency of ["30m", "5m", "1m"]) {
        const frame = rootElement.querySelector(`[data-chart-frame="${periodFrequency}"]`);
        const link = rootElement.querySelector(`[data-chart-link="${periodFrequency}"]`);
        if (frame && frame.getAttribute("src") !== "about:blank") frame.setAttribute("src", "about:blank");
        if (link) link.setAttribute("href", "/");
      }
      const workbench = rootElement.querySelector("[data-chart-workbench]");
      if (workbench) workbench.setAttribute("href", "/");
      const emptyEvidence = {
        established: "尚未取得已确认结构证据",
        missing: "当前没有可分析信号",
        blocking: "当前没有硬阻断或结构冲突",
        next: "等待新的可审计结构事实",
        risk: "风险边界尚未提供",
      };
      for (const groupName of ["established", "missing", "blocking", "next", "risk"]) {
        replaceList(rootElement.querySelector(`[data-evidence-group="${groupName}"]`), [], emptyEvidence[groupName]);
      }
      replaceList(rootElement.querySelector("[data-raw-evidence]"), [], "当前没有原始证据代码");
      setNodeText(rootElement, "[data-evidence-count]", "0");
      const evidenceToggle = rootElement.querySelector("[data-evidence-toggle]");
      if (evidenceToggle) evidenceToggle.disabled = true;
      const theaterToggle = rootElement.querySelector("[data-theater-toggle]");
      if (theaterToggle) theaterToggle.disabled = true;
      return;
    }
    const frequency = FREQUENCIES.has(options.frequency)
      ? options.frequency
      : defaultFrequencyForSignal(signal);
    rootElement.dataset.focusedFrequency = frequency;
    rootElement.dataset.signalSide = signal.side === "sell" ? "sell" : signal.side === "buy" ? "buy" : "neutral";
    setNodeText(rootElement, "[data-selected-name]", text(signal.name, signal.code));
    setNodeText(rootElement, "[data-selected-code]", signal.code);
    setNodeText(rootElement, "[data-selected-point]", POINT_LABELS[signal.point_type] || signal.point_type);
    setNodeText(rootElement, "[data-selected-stage]", LIFECYCLE_LABELS[signal.lifecycle_stage] || signal.lifecycle_stage);
    setNodeText(rootElement, "[data-selected-tower]", `${TOWER_LABELS[signal.tower] || text(signal.tower, "未知塔层")}中枢 / 递归 ${numberText(signal.recursive_level)}`);
    setNodeText(rootElement, "[data-selected-stop]", text(signal.structural_stop, "未提供"));
    setNodeText(rootElement, "[data-selected-risk]", text(signal.risk_multiplier, "未提供"));

    const decision = decisionSummaryForSignal(signal);
    setNodeText(rootElement, "[data-decision-title]", decision.title);
    setNodeText(rootElement, "[data-decision-detail]", decision.detail);
    setNodeText(rootElement, "[data-decision-invalidation]", decision.invalidation);
    const decisionCard = rootElement.querySelector("[data-decision-card]");
    if (decisionCard && decisionCard.dataset) decisionCard.dataset.tone = decision.tone;

    for (const period of periodPathForSignal(signal)) {
      const periodNode = rootElement.querySelector(`[data-period-node="${period.frequency}"]`);
      if (periodNode) {
        if (periodNode.dataset) periodNode.dataset.tone = period.tone;
        periodNode.setAttribute("aria-pressed", period.frequency === frequency ? "true" : "false");
      }
      setNodeText(rootElement, `[data-period-state="${period.frequency}"]`, period.state);
      setNodeText(rootElement, `[data-period-summary="${period.frequency}"]`, period.summary);
      setNodeText(rootElement, `[data-period-boundary="${period.frequency}"]`, period.boundary);
    }

    if (rootElement.querySelectorAll) {
      rootElement.querySelectorAll("[data-focus-frequency]").forEach((button) => {
        const active = button.dataset.focusFrequency === frequency;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    const urls = chartUrlsForSignal(signal);
    for (const frequency of ["30m", "5m", "1m"]) {
      const frame = rootElement.querySelector(`[data-chart-frame="${frequency}"]`);
      const link = rootElement.querySelector(`[data-chart-link="${frequency}"]`);
      if (frame && frame.getAttribute("src") !== urls[frequency]) frame.setAttribute("src", urls[frequency]);
      if (link) link.setAttribute("href", urls[frequency]);
    }
    const workbench = rootElement.querySelector("[data-chart-workbench]");
    if (workbench) workbench.setAttribute("href", urls[frequency]);

    const groups = evidenceGroupsForSignal(signal);
    const evidenceCount = ["established", "missing", "blocking", "next", "risk"]
      .reduce((count, groupName) => count + groups[groupName].length, 0);
    setNodeText(rootElement, "[data-evidence-count]", String(evidenceCount));
    const evidenceToggle = rootElement.querySelector("[data-evidence-toggle]");
    if (evidenceToggle) evidenceToggle.disabled = false;
    const theaterToggle = rootElement.querySelector("[data-theater-toggle]");
    if (theaterToggle) theaterToggle.disabled = false;
    const emptyText = {
      established: "尚未取得已确认结构证据",
      missing: "没有额外缺失条件",
      blocking: "当前没有硬阻断或结构冲突",
      next: "等待新的可审计结构事实",
      risk: "风险边界尚未提供",
    };
    for (const groupName of ["established", "missing", "blocking", "next", "risk"]) {
      replaceList(
        rootElement.querySelector(`[data-evidence-group="${groupName}"]`),
        groups[groupName],
        emptyText[groupName],
      );
    }
    replaceList(
      rootElement.querySelector("[data-raw-evidence]"),
      groups.raw,
      "当前没有原始证据代码",
    );
  }

  return {
    LIFECYCLE_LABELS,
    POINT_LABELS,
    POINT_TYPES,
    SCHEMA_VERSION,
    chartUrlsForSignal,
    decisionSummaryForSignal,
    defaultFrequencyForSignal,
    evidenceGroupsForSignal,
    filterSignals,
    groupSignalsBySector,
    manualFocusState,
    normalizeSnapshot,
    renderChartWorkspace,
    renderSectorWorkspace,
    renderSignalWorkspace,
    resolveFocusState,
    resolveSelectedSignalId,
    periodPathForSignal,
    reasonLabel,
    scanCoverageText,
    scanQualityText,
    scanTimingText,
    sectorCoverageText,
    sectorEvidenceText,
    selectedSectorCount,
    setChartLayout,
    setEvidencePanelOpen,
    setTheaterMode,
    text,
    timeText,
  };
});
