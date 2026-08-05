"use strict";

(function attachTradingScreeningUi(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TradingScreeningUi = api;
})(typeof globalThis === "object" ? globalThis : this, function createTradingScreeningUi() {
  const SCHEMA_VERSION = "chanlun-trading-screening/v3";
  const SECTOR_SAME_BASE_SOURCE_MODE = "PAGE_PARITY_SAME_5M_BASE";
  const SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE =
    "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH";
  const SECTOR_SAME_BASE_COVERAGE_CONTRACT_ID =
    "chanlun-qmt-sector-same-5m-source-coverage/v3";
  const POINT_TYPES = ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"];
  const REVIEW_STAGE_ORDER = {
    executable: 0,
    triggered: 1,
    armed: 2,
    approaching: 3,
    observed: 4,
    active: 5,
    invalidated: 6,
    closed: 7,
  };
  const FREQUENCIES = new Set(["d", "30m", "5m", "1m"]);
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
    triggered: "1分钟已触发",
    executable: "强提示待人工复核",
    active: "持有跟踪",
    invalidated: "结构已失效",
    closed: "跟踪已结束",
  };

  function lifecycleLabel(value) {
    const stage = text(value, "");
    return LIFECYCLE_LABELS[stage] || "未知状态";
  }
  const DIRECTION_LABELS = { up: "向上", down: "向下", neutral: "震荡" };
  const DISPOSITION_LABELS = { supportive: "支撑", neutral: "中性", hostile: "风险" };
  const TOWER_LABELS = { bi: "笔", xd: "线段" };
  const MAPPING_SUPPLY_LABELS = {
    LOWER_STRUCTURE_UNAVAILABLE: "低级别结构不可用",
    NO_LOWER_POINT_EVIDENCE: "没有低级别买卖点证据",
    ONLY_THIRD_CLASS_POINTS: "只有三类点，缺少形成分型的一/二类卖点",
    SELL12_OUTSIDE_TOP_FRACTAL: "一/二类卖点均在本次顶分型区间外",
    SELL12_CENTER_INCOMPLETE: "分型内一/二类卖点对应中枢尚未完成",
    HIGHEST_MAPPING_NOT_UNIQUE: "最高层级中枢映射不唯一",
    UNIQUE_MAPPING: "已唯一映射",
  };
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
    daily_structure_hostile: "日线结构构成阻断",
    unfinished_segment_lock: "末端线段尚未锁定",
    formal_center_confirmation: "线段中枢尚未正式确认",
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
    HIGHER_TIMEFRAME_GATE_NOT_ATTACHED: "月/周/日风险门证据未接入",
    HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED: "板块月/周/日风险门证据未接入",
    HIGHER_TIMEFRAME_GATE_NOT_GREEN: "月/周/日市场、板块与个股风险门尚未同时为绿色",
    QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE: "QMT 板块月/周/日同源风险证据尚未接入",
    QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE: "板块名称或点时成员清单未接入高级别风险门",
    QMT_SECTOR_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE: "QMT 板块高级别行情服务不可用",
    QMT_SECTOR_FIVE_MINUTE_SAME_BASE_STREAM_UNRESOLVED: "QMT 板块5分钟同源合成序列未解决",
    QMT_SECTOR_FIVE_MINUTE_SESSION_GRID_INVALID: "QMT 板块5分钟交易时段网格不完整",
    QMT_SECTOR_FIVE_MINUTE_EXPECTED_SESSION_MISSING: "QMT 板块5分钟同源序列缺少预期交易日",
    QMT_SECTOR_FIVE_MINUTE_NO_ACCEPTED_COMPLETED_BARS: "QMT 板块没有可接受的已完成5分钟K线",
    QMT_SECTOR_FIVE_MINUTE_PRICE_BASIS_UNRESOLVED: "QMT 板块5分钟价格基准未解决",
    QMT_SECTOR_MEMBERSHIP_PROVENANCE_MISMATCH: "QMT 板块代理与本轮点时成员身份不一致",
    QMT_SECTOR_COMPOSITE_MEMBER_COVERAGE_MISMATCH: "QMT 板块代理每根 5 分钟K线的代表成员覆盖不足",
    QMT_SECTOR_COMPOSITE_MEMBER_PATH_PROVENANCE_MISMATCH: "QMT 板块代理逐根贡献成员路径不一致",
    QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE: "板块原生日线与 5m/30m 非线性聚合尚未调和，只能用于研究，绿色结论最多降为琥珀",
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_UNAVAILABLE: "QMT 板块原生日线研究源不可用，保留严格同源路径并失败关闭",
    NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL: "高级别顶分型区间内没有含一卖或二卖的已完成次级别中枢",
    HIGHEST_LOWER_CENTER_MAPPING_NOT_UNIQUE: "最高次级别中枢映射不唯一",
    M_CENTER_MAPPING_UNRESOLVED: "月线顶分型到周线中枢的映射未解决",
    W_CENTER_MAPPING_UNRESOLVED: "周线顶分型到日线中枢的映射未解决",
    D_CENTER_MAPPING_UNRESOLVED: "日线顶分型到30分钟中枢的映射未解决",
    QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING: "QMT 1分钟同源序列缺少预期交易日",
    QMT_ONE_MINUTE_SESSION_GRID_INVALID: "QMT 1分钟交易时段网格不完整",
    QMT_BENCHMARK_ONE_MINUTE_PREFIX_STALE: "QMT 市场基准1分钟前缀已过期",
    KLINE_MINIMUM_HISTORY_NOT_MET: "当前一年窗口的最小K线历史不足",
    THIRD_SELL_IGNORED_AFTER_CENTER_EXTENSION: "中枢扩展后的三卖不用于确认该风险事件",
  };
  Object.assign(REASON_LABELS, {
    QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT: "月/周/日风险门历史不足 480 根已完成日线，已失败关闭",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED: "月/周/日风险门完整前缀与 320 根后缀结论不一致，已失败关闭",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE: "月/周/日风险门双窗口复算一致",
    WARMUP_DIRECTION_CHANGED: "完整前缀与短前缀的当前走势方向不同",
    WARMUP_ACTIVE_POINT_LANES_CHANGED: "完整前缀与短前缀的活动买卖点通道不同",
    WARMUP_POINT_STATUS_CHANGED: "同一活动买卖点的确认状态不同",
    WARMUP_POINT_TIMING_CHANGED: "同一活动买卖点的时点不同",
    WARMUP_PRICE_OR_BOUNDARY_CHANGED: "同一活动结构的价格锚点或失效边界不同",
    WARMUP_STRUCTURE_IDENTITY_CHANGED: "同一活动买卖点的结构归属不同",
    WARMUP_POINT_EVIDENCE_CHANGED: "同一活动买卖点的证据条件不同",
    WARMUP_OTHER_SEMANTIC_CHANGED: "活动结构存在其他决策语义差异",
  });
  const MISSING_REASON_CODES = new Set([
    "one_minute_not_confirmed",
    "one_minute_sell_not_confirmed",
    "five_minute_not_confirmed",
    "setup_not_confirmed",
    "sell_not_confirmed",
    "unfinished_core_mmd",
    "unfinished_trend_divergence",
  ]);
  const SESSION_ISSUE_REASON_CODES = new Set([
    "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
    "QMT_ONE_MINUTE_SESSION_GRID_INVALID",
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

  function completedWithoutSignalCount(snapshot) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const manifest = isRecord(safeSnapshot.coverage_manifest)
      ? safeSnapshot.coverage_manifest
      : {};
    if (!Array.isArray(manifest.completed_codes) || !Array.isArray(safeSnapshot.signals)) {
      return null;
    }
    const completed = new Set(
      manifest.completed_codes.map((value) => text(value, "").trim()).filter(Boolean),
    );
    const signaled = new Set(
      safeSnapshot.signals
        .filter(isRecord)
        .map((value) => text(value.code, "").trim())
        .filter(Boolean),
    );
    return Array.from(completed).filter((code) => !signaled.has(code)).length;
  }

  function exactCoverageCodeForQuery(snapshot, query) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const manifest = isRecord(safeSnapshot.coverage_manifest)
      ? safeSnapshot.coverage_manifest
      : {};
    const normalized = text(query, "").trim().toUpperCase();
    if (!/^(?:(?:SH|SZ|BJ)\.)?\d{6}$/.test(normalized)) return null;
    const discovered = Array.isArray(manifest.discovered_codes)
      ? manifest.discovered_codes.map((value) => text(value, "").trim()).filter(Boolean)
      : [];
    const matches = discovered.filter((code) => (
      code.toUpperCase() === normalized
      || code.split(".").at(-1) === normalized
    ));
    if (matches.length === 1) return matches[0];
    return /^(?:SH|SZ|BJ)\.\d{6}$/.test(normalized) ? normalized : null;
  }

  function emptySignalDetail(snapshot, query) {
    const generic = "这表示当前快照没有匹配项，不等于扫描成功率或未来收益判断。";
    const code = exactCoverageCodeForQuery(snapshot, query);
    if (code === null) return generic;
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const manifest = isRecord(safeSnapshot.coverage_manifest)
      ? safeSnapshot.coverage_manifest
      : {};
    const signals = Array.isArray(safeSnapshot.signals)
      ? safeSnapshot.signals.filter((row) => isRecord(row) && row.code === code)
      : [];
    if (signals.length) {
      return `${code} 有 ${signals.length} 条当前结构信号，但被当前点类型、生命周期或板块筛选隐藏。`;
    }
    const monitorExclusions = Array.isArray(safeSnapshot.monitor_instrument_exclusions)
      ? safeSnapshot.monitor_instrument_exclusions.filter(isRecord)
      : [];
    const monitorExclusion = monitorExclusions.find((row) => row.code === code);
    if (
      monitorExclusion
      && monitorExclusion.reason_code === "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED"
    ) {
      return `${code} 来自自选、虚拟持仓或旧信号监控，但QMT本轮未能解析其原生品种类型；系统已失败关闭，不会把未知品种加入交易结构线索队列。`;
    }
    if (
      monitorExclusion
      && monitorExclusion.reason_code === "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
    ) {
      const instrumentType = text(monitorExclusion.qmt_instrument_type, "非股票/ETF");
      return `${code} 来自自选、虚拟持仓或旧信号监控，但QMT原生品种类型为 ${instrumentType}，不是可交易A股股票或场内ETF；它不会进入交易结构线索队列。`;
    }
    const includes = (field) => (
      Array.isArray(manifest[field]) && manifest[field].includes(code)
    );
    if (includes("completed_codes")) {
      return `${code} 已完成分析，当前没有结构信号；这不是漏扫，也不代表未来不会出现。`;
    }
    if (includes("excluded_codes")) {
      const exclusions = Array.isArray(manifest.exclusions)
        ? manifest.exclusions.filter(isRecord)
        : [];
      const exclusion = exclusions.find((row) => row.code === code);
      const reason = exclusion ? reasonLabel(exclusion.reason_code) : "资格条件未满足";
      return `${code} 已按当前数据纪元资格排除：${reason}；未计为分析成功或运行失败。`;
    }
    if (includes("failed_codes")) {
      return `${code} 本轮分析失败，失败事实已写入覆盖清单；可在数据质量记录中查看原因。`;
    }
    if (includes("discovered_codes")) {
      return `${code} 已进入当前板块触发范围，尚在待分析队列。`;
    }
    return `${code} 不在当前QMT板块触发与监控补充范围内。`;
  }

  function scanCoverageText(audit, snapshot = null) {
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
    if (Object.prototype.hasOwnProperty.call(
      safeAudit,
      "coverage_cycle_excluded_symbol_count",
    )) {
      const discovered = Math.max(0, Number(safeAudit.discovered_symbol_count) || 0);
      const analyzed = Math.max(
        0,
        Number(safeAudit.coverage_cycle_completed_symbol_count) || 0,
      );
      const excluded = Math.max(
        0,
        Number(safeAudit.coverage_cycle_excluded_symbol_count) || 0,
      );
      const failed = Math.max(
        0,
        Number(safeAudit.coverage_cycle_failed_symbol_count) || 0,
      );
      const parts = [`全周期已分析 ${analyzed}/${discovered}`];
      const withoutSignal = completedWithoutSignalCount(snapshot);
      if (withoutSignal !== null && withoutSignal > 0) {
        parts.push(`已分析无当前结构信号 ${withoutSignal}`);
      }
      if (excluded) parts.push(`历史不足排除 ${excluded}`);
      if (failed) parts.push(`失败 ${failed}`);
      if (pending) parts.push(`待分析 ${pending}`);
      else if (safeAudit.coverage_cycle_complete === false) parts.push("等待续扫");
      else parts.push("范围已处置");
      return parts.join(" · ");
    }
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

  function memberHistoryDiagnosticsText(snapshot) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const diagnostics = isRecord(safeSnapshot.sector_member_history_diagnostics)
      ? safeSnapshot.sector_member_history_diagnostics
      : {};
    if (diagnostics.schema !== "chanlun-sector-member-history-diagnostics/v1") {
      return "尚无认证成员状态";
    }
    const counts = isRecord(diagnostics.unique_symbol_status_counts)
      ? diagnostics.unique_symbol_status_counts
      : {};
    const total = Math.max(0, Number(diagnostics.unique_symbol_count) || 0);
    const complete = Math.max(0, Number(counts.COMPLETE) || 0);
    const newListing = Math.max(0, Number(counts.NEW_LISTING) || 0);
    const suspended = Math.max(0, Number(counts.SUSPENDED) || 0);
    const unexplained = Math.max(0, Number(counts.UNEXPLAINED_GAP) || 0);
    const parts = [`完整 ${complete}/${total}`];
    if (newListing) parts.push(`新股 ${newListing}`);
    if (suspended) parts.push(`停牌 ${suspended}`);
    parts.push(
      unexplained
        ? `无法解释 ${unexplained}（失败关闭）`
        : "无法解释 0",
    );
    return parts.join(" · ");
  }

  function scanTimingText(audit) {
    const safeAudit = isRecord(audit) ? audit : {};
    const batchMs = Number(safeAudit.batch_duration_ms);
    const cycleMs = Number(safeAudit.coverage_cycle_elapsed_ms);
    const batches = Math.max(0, Number(safeAudit.coverage_cycle_batch_count) || 0);
    const throughput = Number(
      safeAudit.coverage_cycle_throughput_symbols_per_minute,
    );
    const etaSeconds = Number(
      safeAudit.coverage_cycle_estimated_remaining_seconds,
    );
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
    if (Number.isFinite(throughput) && throughput > 0) {
      values.push(`吞吐 ${throughput.toFixed(1)}只/分钟`);
    }
    if (
      safeAudit.coverage_cycle_complete !== true
      && Number.isFinite(etaSeconds)
      && etaSeconds >= 0
    ) {
      const etaText = duration(etaSeconds * 1000);
      if (etaText) values.push(`预计剩余 ${etaText}`);
    }
    return values.join(" · ");
  }

  function sectorCoverageText(audit) {
    const safeAudit = isRecord(audit) ? audit : {};
    const discovered = Math.max(0, Number(safeAudit.sector_discovered_count) || 0);
    const completed = Math.max(0, Number(safeAudit.sector_completed_count) || 0);
    const hasExcluded = Object.prototype.hasOwnProperty.call(
      safeAudit,
      "sector_excluded_count",
    );
    const excluded = hasExcluded
      ? Math.max(0, Number(safeAudit.sector_excluded_count) || 0)
      : 0;
    const hasFailed = Object.prototype.hasOwnProperty.call(
      safeAudit,
      "sector_failed_count",
    );
    const failed = hasFailed
      ? Math.max(0, Number(safeAudit.sector_failed_count) || 0)
      : Math.max(0, discovered - completed - excluded);
    const providedRatio = Number(safeAudit.sector_completion_ratio);
    const ratio = Number.isFinite(providedRatio)
      ? Math.min(1, Math.max(0, providedRatio))
      : discovered > 0 ? Math.min(1, completed / discovered) : 0;
    const percentage = new Intl.NumberFormat("zh-CN", {
      style: "percent",
      maximumFractionDigits: 1,
    }).format(ratio);
    const exclusionText = hasExcluded ? ` · 资格排除 ${excluded}` : "";
    return `发现 ${discovered} · 完成 ${completed}${exclusionText} · 失败 ${failed} · 成功率 ${percentage}`;
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

  function dailyPreselectionText(runtimeHealth) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const status = text(health.daily_preselection_status, "unavailable");
    const reason = text(
      health.daily_preselection_reason_code,
      "PRESELECTION_UNAVAILABLE",
    );
    const target = text(health.daily_preselection_target_session, "交易日待定");
    const expected = text(health.daily_preselection_expected_session, "交易日待定");
    const candidates = Math.max(
      0,
      Number(health.daily_preselection_candidate_count) || 0,
    );
    const buys = Math.max(
      0,
      Number(health.daily_preselection_buy_candidate_count) || 0,
    );
    if (health.daily_preselection_ready === true) {
      return `已就绪 · 适用 ${target} · 买入线索 ${buys} / 全部 ${candidates}`;
    }
    if (status === "target_session_stale") {
      return `待更新 · 当前名单适用 ${target}，正在准备 ${expected}`;
    }
    if (status === "coverage_in_progress") {
      return "正在生成 · 全市场结构扫描尚未完成";
    }
    if (status === "awaiting_first_snapshot") {
      return "首次生成中 · 等待结构扫描完成";
    }
    if (status === "review_blocked") {
      const nextActive = health.full_coverage_next_active_at
        ? ` · 下一轮全量扫描 ${timeText(health.full_coverage_next_active_at)}`
        : "";
      if (reason === "HUMAN_REVIEW_MATERIALIZATION_FAILED") {
        return `复核材料待重建 · 结构雷达可看${nextActive}`;
      }
      if (reason === "REVIEW_BOUNDARY_INVALID") {
        return "发布校验未通过 · 结构雷达可看，名单暂不可确认";
      }
      return "待人工复核 · 结构雷达可看，名单暂不可确认";
    }
    return "尚未就绪 · 详情见运行诊断";
  }

  function dailyPreselectionDiagnosticsText(runtimeHealth) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const status = text(health.daily_preselection_status, "unavailable");
    const reason = text(
      health.daily_preselection_reason_code,
      "PRESELECTION_UNAVAILABLE",
    );
    const candidates = Math.max(
      0,
      Number(health.daily_preselection_candidate_count) || 0,
    );
    const buys = Math.max(
      0,
      Number(health.daily_preselection_buy_candidate_count) || 0,
    );
    const replay = Math.max(
      0,
      Number(health.sector_evidence_replay_symbol_count) || 0,
    );
    const parts = [
      `内部状态 ${status}`,
      `原因 ${reason}`,
      `结构线索 ${candidates}（买入 ${buys}）`,
    ];
    if (health.daily_preselection_target_session) {
      parts.push(`适用 ${text(health.daily_preselection_target_session)}`);
    }
    if (health.daily_preselection_market_data_as_of) {
      parts.push(`数据截止 ${timeText(health.daily_preselection_market_data_as_of)}`);
    }
    if (replay) parts.push(`待重放板块证据 ${replay} 只`);
    return parts.join(" · ");
  }

  function priorityMonitorText(runtimeHealth, liveOverlay) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const overlay = isRecord(liveOverlay) ? liveOverlay : {};
    const status = text(health.priority_monitor_status, "unavailable");
    const priorityCount = Math.max(
      0,
      Number(health.priority_monitor_last_code_count) || 0,
    );
    const liveSignalCount = Math.max(0, Number(overlay.signal_count) || 0);
    const signalText = liveSignalCount
      ? `${liveSignalCount} 条新结构变化`
      : "暂无新结构变化";
    if (status === "verified") {
      return health.notification_dispatcher_configured === true
        ? `正常 · 复查 ${priorityCount} 只 · ${signalText} · 通知已接通`
        : `识别正常 · 复查 ${priorityCount} 只 · ${signalText} · 仅页面提醒`;
    }
    if (status === "not_due") return "非交易时段 · 开盘后自动盯盘";
    if (status === "disabled") return "未启用";
    return "暂不可用 · 详情见运行诊断";
  }

  function priorityMonitorDiagnosticsText(runtimeHealth, liveOverlay) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const overlay = isRecord(liveOverlay) ? liveOverlay : {};
    const status = text(health.priority_monitor_status, "unavailable");
    const reasons = Array.isArray(health.priority_monitor_reason_codes)
      ? health.priority_monitor_reason_codes.map((value) => text(value)).join(" / ")
      : "PRIORITY_MONITOR_UNAVAILABLE";
    const priorityCount = Math.max(
      0,
      Number(health.priority_monitor_last_code_count) || 0,
    );
    const liveSignalCount = Math.max(0, Number(overlay.signal_count) || 0);
    const parts = [
      `内部状态 ${status}`,
      `原因 ${reasons}`,
      `复查 ${priorityCount} 只`,
      `结构变化 ${liveSignalCount} 条`,
      health.notification_dispatcher_configured === true ? "主动通知已接通" : "仅页面提醒",
    ];
    if (health.priority_monitor_last_at) {
      parts.push(`最近运行 ${timeText(health.priority_monitor_last_at)}`);
    }
    return parts.join(" · ");
  }

  function evidenceTimeText(value) {
    if (!value) return "未提供";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
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
    const warmup = value.match(/^(D|30M|5M|1M):WARMUP_(TAIL_STABLE|TAIL_DIVERGED|HISTORY_INSUFFICIENT)$/);
    if (warmup) {
      const frequency = { D: "日线", "30M": "30分钟", "5M": "5分钟", "1M": "1分钟" }[warmup[1]];
      const state = {
        TAIL_STABLE: "暖机双窗口尾部一致（非多前缀稳定证明）",
        TAIL_DIVERGED: "暖机双窗口尾部不一致",
        HISTORY_INSUFFICIENT: "暖机历史长度不足",
      }[warmup[2]];
      return `${frequency}${state}`;
    }
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

  function selectionLabelForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const sources = uniqueText(safeSignal.selection_sources);
    const labels = [];
    if (safeSignal.sector_triggered === true || sources.includes("QMT_SECTOR_TRIGGER")) {
      labels.push("板块触发");
    }
    if (sources.includes("QMT_SECTOR_ELIGIBLE_SCOPE")) labels.push("板块范围");
    if (sources.includes("ACTIVE_WATCHLIST_MONITOR")) labels.push("自选监控");
    if (
      sources.includes("HOLDING_MONITOR")
      || sources.includes("VIRTUAL_HOLDING_MONITOR")
    ) labels.push("持仓监控");
    if (sources.includes("PREVIOUS_SIGNAL_MONITOR")) labels.push("旧信号跟踪");
    if (sources.includes("INCREMENTAL_SCAN_SCOPE")) labels.push("增量监控");
    return labels.length ? labels.join(" + ") : "来源待确认";
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
      title = "强提示待人工复核";
    } else if (stage === "triggered") {
      tone = "waiting";
      title = "已触发，等待人工识别";
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
      detail: allowed ? "程序条件已形成强提示，仍须人工识别" : (reasons[0] ? reasonLabel(reasons[0]) : "等待剩余结构条件"),
      invalidation: text(setup.invalidation_price, "未提供"),
      structuralStop: text(setup.invalidation_price ?? safeSignal.structural_stop, "未提供"),
      riskMultiplier: text(safeSignal.risk_multiplier, "未提供"),
    };
  }

  function periodPathForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const daily = isRecord(safeSignal.context_d) ? safeSignal.context_d : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const trigger = isRecord(safeSignal.trigger_1m) ? safeSignal.trigger_1m : null;
    const setupKnown = Object.keys(setup).length > 0;
    const triggerKnown = trigger !== null && Object.keys(trigger).length > 0;

    const contextPeriod = (frequency, value, label) => {
      const known = Object.keys(value).length > 0;
      const blocked = known && (value.hard_block === true || value.disposition === "hostile");
      const supportive = known && value.disposition === "supportive";
      const tone = blocked ? "blocked" : supportive ? "supportive" : known ? "neutral" : "unknown";
      const state = blocked ? "阻断" : supportive ? "支持" : known ? "中性" : "未知";
      const reasons = uniqueText(value.reason_codes);
      return {
        frequency,
        state,
        tone,
        summary: known
          ? `方向 ${DIRECTION_LABELS[value.direction] || "待判定"} · 主导 ${POINT_LABELS[value.dominant_point_type] || "无主导点"} · 本周期线段中枢`
          : `${label}结构证据未提供`,
        boundary: blocked
          ? reasonLabel(reasons[0])
          : known ? "无硬阻断" : "环境证据未提供",
        evidence: reasons.map(reasonLabel),
      };
    };

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

    const setupEvidence = uniqueText(setup.evidence_codes);
    const triggerEvidence = triggerKnown ? uniqueText(trigger.evidence_codes) : [];
    const center = setup.center_ordinal === null || setup.center_ordinal === undefined
      ? "中枢序号不适用"
      : `第 ${text(setup.center_ordinal)} 中枢`;
    const unfinished = setup.contains_unfinished_segment === true
      ? " · 含未完成线段（待确认）"
      : "";
    const triggerPoint = triggerKnown
      ? POINT_LABELS[trigger.point_type] || text(trigger.point_type, "精确触发")
      : null;

    return [
      contextPeriod("d", daily, "日线"),
      contextPeriod("30m", context, "30分钟"),
      {
        frequency: "5m",
        state: setupState,
        tone: setupTone,
        summary: setupKnown
          ? `${POINT_LABELS[setup.point_type || safeSignal.point_type] || text(setup.point_type || safeSignal.point_type)} · 老笔→线段中枢 · 本周期0级（非递归） · ${center}${unfinished}`
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
        boundary: `结构防守价 ${text(setup.invalidation_price ?? safeSignal.structural_stop, "未提供")}`,
        evidence: triggerEvidence.map(reasonLabel),
      },
    ];
  }

  function sectorHigherTimeframeSourceEvidence(higherRisk) {
    const safeRisk = isRecord(higherRisk) ? higherRisk : {};
    const mode = text(safeRisk.sector_higher_timeframe_source_mode, "");
    const strictWarmup = isRecord(safeRisk.sector_strict_same_5m_warmup_evidence)
      ? safeRisk.sector_strict_same_5m_warmup_evidence
      : null;
    const strictCoverage = isRecord(
      safeRisk.sector_strict_same_5m_source_coverage_evidence,
    )
      ? safeRisk.sector_strict_same_5m_source_coverage_evidence
      : null;
    const bridgeId = typeof safeRisk.sector_research_bridge_parameter_set_id === "string"
      ? safeRisk.sector_research_bridge_parameter_set_id
      : "";
    const extensionFieldsPresent = [
      "sector_higher_timeframe_source_mode",
      "sector_strict_same_5m_warmup_evidence",
      "sector_strict_same_5m_source_coverage_evidence",
      "sector_research_bridge_parameter_set_id",
    ].some((field) => Object.prototype.hasOwnProperty.call(safeRisk, field));
    if (!mode) {
      return extensionFieldsPresent
        ? {
          cardLabel: "来源证据不完整",
          risk: [],
          blocking: ["板块高级别来源字段不完整，不能据此解除风险门"],
          raw: [],
        }
        : { cardLabel: "", risk: [], blocking: [], raw: [] };
    }

    const strictLine = strictWarmup
      ? `严格同一5m基底暖机：${strictWarmup.converged === true ? "一致" : "未通过"} · 完整 ${numberText(strictWarmup.full_daily_bar_count)} 根日线 / 要求 ${numberText(strictWarmup.required_daily_bar_count)} 根 · ${reasonLabel(strictWarmup.reason_code)}`
      : "严格同一5m基底暖机证据缺失";
    const coverageLine = strictCoverage
      ? `严格5m历史边界：${text(strictCoverage.first_completed_session, "尚无完整交易日")} 至 ${text(strictCoverage.last_completed_session, "尚无完整交易日")} · 尚缺 ${numberText(strictCoverage.remaining_daily_bar_count)} 个完整交易日 · 左边界前缺 ${numberText(strictCoverage.missing_leading_calendar_session_count)} 个市场交易日`
      : "严格5m历史边界证据缺失";
    const coverageMalformed = strictCoverage === null
      || strictCoverage.contract_id !== SECTOR_SAME_BASE_COVERAGE_CONTRACT_ID
      || strictCoverage.base_frequency !== "5m"
      || strictCoverage.prefix_only !== true
      || strictCoverage.live_status !== "LIVE_DISABLED"
      || strictWarmup === null
      || Number(strictCoverage.completed_daily_bar_count) < Number(strictWarmup.full_daily_bar_count)
      || Number(strictCoverage.required_daily_bar_count) !== Number(strictWarmup.required_daily_bar_count)
      || strictCoverage.warmup_reason_code !== strictWarmup.reason_code;
    const raw = uniqueText([
      mode,
      strictWarmup && strictWarmup.reason_code,
      strictCoverage && strictCoverage.boundary_status,
      bridgeId,
    ]);
    if (mode === SECTOR_SAME_BASE_SOURCE_MODE) {
      const inconsistent = strictWarmup === null
        || bridgeId !== ""
        || (safeRisk.sector_gate === "GREEN" && strictWarmup.converged !== true);
      return {
        cardLabel: "同一5m基底",
        risk: [
          "板块高级别来源：严格同一5m基底；M/W/D 与 30m 均由该基底因果派生",
          strictLine,
          coverageLine,
        ],
        blocking: [
          ...(inconsistent
            ? ["板块严格同源模式与暖机/研究桥证据矛盾，继续失败关闭"]
            : []),
          ...(coverageMalformed
            ? ["板块严格5m历史边界证据缺失或与暖机证据矛盾，继续失败关闭"]
            : []),
        ],
        raw,
      };
    }
    if (mode === SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE) {
      const bridgeMalformed = !bridgeId.startsWith("sha256:");
      const strictCauseInvalid = strictWarmup === null
        || strictWarmup.reason_code !== "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT";
      const greenConflict = safeRisk.sector_gate === "GREEN";
      return {
        cardLabel: "原生日线研究桥（AMBER上限）",
        risk: [
          "板块高级别来源：QMT 原生日线构造 M/W/D；30m 仍由同一5m基底派生",
          "研究限制：原生日线与5m/30m非线性聚合尚未调和 · 仅 RESEARCH_ONLY · GREEN 最多降为 AMBER · LIVE_DISABLED",
          strictLine,
          coverageLine,
          `研究桥参数：${bridgeId || "缺失"}`,
        ],
        blocking: [
          ...(greenConflict
            ? ["板块研究桥不能产生 GREEN；当前绿色字段矛盾，继续失败关闭"]
            : []),
          ...(bridgeMalformed || strictCauseInvalid
            ? ["板块研究桥身份或启用原因不完整，不能据此解除风险门"]
            : []),
          ...(coverageMalformed
            ? ["板块严格5m历史边界证据缺失或与暖机证据矛盾，不能据此解除风险门"]
            : []),
        ],
        raw,
      };
    }
    return {
      cardLabel: "来源模式未认证",
      risk: [`板块高级别来源模式未认证：${mode}`],
      blocking: ["板块高级别来源模式未认证，继续失败关闭"],
      raw,
    };
  }

  function evidenceGroupsForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const daily = isRecord(safeSignal.context_d) ? safeSignal.context_d : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const trigger = isRecord(safeSignal.trigger_1m) && Object.keys(safeSignal.trigger_1m).length
      ? safeSignal.trigger_1m
      : null;
    const dailyCodes = uniqueText(daily.reason_codes);
    const contextCodes = uniqueText(context.reason_codes);
    const setupEvidence = uniqueText(setup.evidence_codes);
    const setupPendingEvidence = setupEvidence.filter((code) => MISSING_REASON_CODES.has(code));
    const setupConfirmedEvidence = setupEvidence.filter((code) => !MISSING_REASON_CODES.has(code));
    const setupMissing = uniqueText(setup.missing_conditions);
    const triggerEvidence = trigger ? uniqueText(trigger.evidence_codes) : [];
    const triggerMissing = trigger ? uniqueText(trigger.missing_conditions) : [];
    const decisions = uniqueText(safeSignal.decision_reasons);
    const selectionSources = uniqueText(safeSignal.selection_sources);
    const higherRisk = isRecord(safeSignal.higher_timeframe_risk)
      ? safeSignal.higher_timeframe_risk
      : {};
    const marketRiskReasons = uniqueText(higherRisk.market_reason_codes);
    const sectorRiskReasons = uniqueText(higherRisk.sector_reason_codes);
    const symbolRiskReasons = uniqueText(higherRisk.symbol_reason_codes);
    const mergedRiskReasons = uniqueText(higherRisk.reason_codes);
    const warmup = isRecord(safeSignal.warmup) ? safeSignal.warmup : {};
    const warmupReasons = uniqueText(warmup.reason_codes);
    const warmupRows = Array.isArray(warmup.by_frequency)
      ? warmup.by_frequency.filter(isRecord)
      : [];
    const warmupDifferenceRows = Array.isArray(warmup.difference_codes_by_frequency)
      ? warmup.difference_codes_by_frequency.filter(isRecord)
      : [];
    const warmupDifferenceLines = warmupDifferenceRows.flatMap((row) => {
      const frequency = text(row.frequency, "?");
      const label = frequency === "d"
        ? "日线"
        : frequency === "30m"
          ? "30分钟"
          : frequency === "5m"
            ? "5分钟"
            : frequency === "1m" ? "1分钟" : frequency;
      return uniqueText(row.difference_codes).map(
        (code) => `${label}暖机差异：${reasonLabel(code)}`,
      );
    });
    const marketDiagnostics = Array.isArray(higherRisk.market_period_diagnostics)
      ? higherRisk.market_period_diagnostics.filter(isRecord)
      : [];
    const sectorDiagnostics = Array.isArray(higherRisk.sector_period_diagnostics)
      ? higherRisk.sector_period_diagnostics.filter(isRecord)
      : [];
    const symbolDiagnostics = Array.isArray(higherRisk.symbol_period_diagnostics)
      ? higherRisk.symbol_period_diagnostics.filter(isRecord)
      : [];
    const marketSessionEvidence = isRecord(higherRisk.market_session_evidence)
      ? higherRisk.market_session_evidence
      : null;
    const sectorSessionEvidence = isRecord(higherRisk.sector_session_evidence)
      ? higherRisk.sector_session_evidence
      : null;
    const symbolSessionEvidence = isRecord(higherRisk.symbol_session_evidence)
      ? higherRisk.symbol_session_evidence
      : null;
    const marketMwdWarmup = isRecord(higherRisk.market_warmup_evidence)
      ? higherRisk.market_warmup_evidence
      : null;
    const sectorMwdWarmup = isRecord(higherRisk.sector_warmup_evidence)
      ? higherRisk.sector_warmup_evidence
      : null;
    const symbolMwdWarmup = isRecord(higherRisk.symbol_warmup_evidence)
      ? higherRisk.symbol_warmup_evidence
      : null;
    const marketNativeDaily = isRecord(
      higherRisk.market_native_daily_reconciliation_evidence,
    )
      ? higherRisk.market_native_daily_reconciliation_evidence
      : null;
    const symbolNativeDaily = isRecord(
      higherRisk.symbol_native_daily_reconciliation_evidence,
    )
      ? higherRisk.symbol_native_daily_reconciliation_evidence
      : null;
    const marketNativeDailyCalendar = isRecord(
      higherRisk.market_native_daily_calendar_coverage_evidence,
    )
      ? higherRisk.market_native_daily_calendar_coverage_evidence
      : null;
    const symbolNativeDailyCalendar = isRecord(
      higherRisk.symbol_native_daily_calendar_coverage_evidence,
    )
      ? higherRisk.symbol_native_daily_calendar_coverage_evidence
      : null;
    const sectorSourceEvidence = sectorHigherTimeframeSourceEvidence(higherRisk);
    const riskGateLine = (subject, gate, reasons) => {
      const labels = reasons.map(reasonLabel);
      return `${subject}风险门 ${text(gate, "UNRESOLVED")}：${labels.length ? labels.join("；") : "无附加拒绝原因"}`;
    };
    const periodDiagnosticLines = (subject, rows) => rows.map((row) => {
      const candidateIds = uniqueText(row.mapping_candidate_ids);
      const mapping = row.mapped_center_id
        ? `映射中枢 ${text(row.mapped_center_id)}`
        : row.mapping_unique === false
          ? `映射未解决（候选 ${candidateIds.length}）`
          : "当前无需活动中枢映射";
      const blockerCodes = uniqueText(row.blocker_codes);
      const warnings = uniqueText(row.warning_codes);
      const suffix = [...blockerCodes, ...warnings].map(reasonLabel);
      const interval = Array.isArray(row.active_top_interval)
        && row.active_top_interval.length === 2
        ? `活动顶分型 ${evidenceTimeText(row.active_top_interval[0])} 至 ${evidenceTimeText(row.active_top_interval[1])}`
        : "活动顶分型：无";
      const evidenceEnd = `证据截止 ${evidenceTimeText(row.evidence_bar_end)}`;
      const supply = isRecord(row.mapping_supply) ? row.mapping_supply : null;
      const pointCounts = supply && isRecord(supply.point_type_counts)
        ? supply.point_type_counts
        : {};
      const supplyText = supply
        ? `映射供给 ${MAPPING_SUPPLY_LABELS[supply.classification] || text(supply.classification, "未分类")} · 低级别点 ${numberText(supply.point_evidence_count)}（一卖 ${numberText(pointCounts["1sell"])} / 二卖 ${numberText(pointCounts["2sell"])} / 三卖 ${numberText(pointCounts["3sell"])} / 三买 ${numberText(pointCounts["3buy"])}）· 分型内一二卖 ${numberText(supply.in_top_interval_sell12_count)} / 已完成中枢 ${numberText(supply.completed_in_top_interval_sell12_count)}`
        : "映射供给：旧证据未保存明细";
      return `${subject}${text(row.period, "?")}：${text(row.state, "UNRESOLVED")} · 完成K线 ${numberText(row.completed_bar_count)} · ${interval} · ${evidenceEnd} · ${mapping} · ${supplyText}${suffix.length ? ` · ${suffix.join("；")}` : ""}`;
    });
    const sessionEvidenceLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      if (evidence.status === "UNAVAILABLE") {
        return [`${subject}1分钟会话证据：不可用 · 继续失败关闭`];
      }
      const issues = Array.isArray(evidence.issues)
        ? evidence.issues.filter(isRecord)
        : [];
      return issues.map((issue) => {
        if (issue.code === "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING") {
          return `${subject}1分钟缺失交易日 ${text(issue.session, "未知")}：观测 ${numberText(issue.observed_rows)} 根 · 历史停牌状态未获认证 · 不自动填补 · 失败关闭`;
        }
        if (issue.code === "QMT_ONE_MINUTE_SESSION_GRID_INVALID") {
          return `${subject}1分钟会话网格异常 ${text(issue.session, "未知")}：观测 ${numberText(issue.observed_rows)} 根 · 失败关闭`;
        }
        return `${subject}1分钟会话异常 ${text(issue.session, "未知")}：${reasonLabel(issue.code)} · 失败关闭`;
      });
    };
    const legacySessionEvidenceLines = (subject, reasons, evidence) => (
      evidence === null && reasons.some((code) => SESSION_ISSUE_REASON_CODES.has(code))
        ? [`${subject}1分钟会话证据：旧信号缺少精确日期 · 不可用于最终复核 · 失败关闭`]
        : []
    );
    const mwdWarmupLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      const verdict = evidence.converged === true ? "一致" : "失败关闭";
      return [
        `${subject}月/周/日暖机：${verdict} · 完整 ${numberText(evidence.full_daily_bar_count)} 根日线 / 对照后缀 ${numberText(evidence.suffix_daily_bar_count)} 根 · 要求 ${numberText(evidence.required_daily_bar_count)} 根 · ${reasonLabel(evidence.reason_code)}`,
      ];
    };
    const nativeDailyLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      const passed = evidence.all_overlap_ohlcv_within_declared_tolerance === true;
      return [
        `${subject}原生日线左历史复核：${passed ? "通过" : "失败关闭"} · 原生日线 ${numberText(evidence.native_daily_bar_count)} 根 / 1分钟派生日线 ${numberText(evidence.one_minute_daily_bar_count)} 根 · 重叠 ${numberText(evidence.overlap_session_count)} 个交易日（${text(evidence.first_overlap_session, "未知")} 至 ${text(evidence.last_overlap_session, "未知")}）· 容许价差 ${numberText(evidence.price_tolerance_quanta)} 个量化单位 / 实测最大 ${numberText(evidence.max_observed_price_difference_quanta)} · 原生日线只补左历史，30分钟仍由1分钟派生 · ${text(evidence.live_status, "LIVE_DISABLED")}`,
      ];
    };
    const nativeDailyCalendarLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      const missingSessions = uniqueText(
        evidence.unexplained_calendar_only_sessions,
      );
      const nativeOnlySessions = uniqueText(evidence.native_only_sessions);
      if (evidence.status === "EXACT") {
        return [
          `${subject}原生日线交易日覆盖：精确 · 原生日线 ${numberText(evidence.native_daily_bar_count)} 根 / 日历应有 ${numberText(evidence.expected_calendar_session_count)} 个交易日 · ${text(evidence.native_first_session, "未知")} 至 ${text(evidence.native_last_session, "未知")} · 前缀内无交易日缺口`,
        ];
      }
      const missingText = missingSessions.length
        ? `${missingSessions.length} 日（${missingSessions.join("、")}）`
        : "0 日";
      const nativeOnlyText = nativeOnlySessions.length
        ? `${nativeOnlySessions.length} 日（${nativeOnlySessions.join("、")}）`
        : "0 日";
      return [
        `${subject}原生日线交易日覆盖：${text(evidence.status, "UNRESOLVED")} · 原生日线 ${numberText(evidence.native_daily_bar_count)} 根 / 日历应有 ${numberText(evidence.expected_calendar_session_count)} 个交易日 · 日历有而日线缺 ${missingText} · 日线有而日历缺 ${nativeOnlyText} · 缺失未证明为停牌、不自动填补 · 失败关闭`,
      ];
    };
    const missingDecisions = decisions.filter((code) => MISSING_REASON_CODES.has(code));
    const blockingDecisions = decisions.filter((code) => !MISSING_REASON_CODES.has(code));
    const established = [
      ...(selectionSources.length || typeof safeSignal.sector_triggered === "boolean"
        ? [`候选来源：${selectionLabelForSignal(safeSignal)}`]
        : []),
      ...(daily.hard_block === true || daily.disposition === "hostile"
        ? []
        : prefixedLabels("日线", dailyCodes)),
      ...(context.hard_block === true || context.disposition === "hostile"
        ? []
        : prefixedLabels("30分钟", contextCodes)),
      ...prefixedLabels("5分钟", setupConfirmedEvidence),
      ...prefixedLabels("1分钟", triggerEvidence),
      ...(higherRisk.market_gate === "GREEN"
        ? [riskGateLine("市场", higherRisk.market_gate, marketRiskReasons)]
        : []),
      ...(higherRisk.sector_gate === "GREEN"
        ? [riskGateLine("板块", higherRisk.sector_gate, sectorRiskReasons)]
        : []),
      ...(higherRisk.symbol_gate === "GREEN"
        ? [riskGateLine("个股", higherRisk.symbol_gate, symbolRiskReasons)]
        : []),
      ...(marketNativeDailyCalendar && marketNativeDailyCalendar.status === "EXACT"
        ? nativeDailyCalendarLines("市场", marketNativeDailyCalendar)
        : []),
      ...(symbolNativeDailyCalendar && symbolNativeDailyCalendar.status === "EXACT"
        ? nativeDailyCalendarLines("个股", symbolNativeDailyCalendar)
        : []),
      ...(warmup.converged === true
        ? ["暖机：四周期当前双窗口尾部一致（尚非多前缀稳定证明）"]
        : []),
    ];
    const missing = [
      ...prefixedLabels("5分钟", setupPendingEvidence),
      ...prefixedLabels("5分钟", setupMissing),
      ...prefixedLabels("1分钟", triggerMissing),
      ...(trigger ? [] : ["1分钟：尚未取得同向精确触发"]),
      ...missingDecisions.map(reasonLabel),
      ...(warmup.converged === false
        ? warmupReasons.filter((code) => !code.endsWith("TAIL_STABLE")).map(reasonLabel)
        : []),
      ...(warmup.converged === false ? warmupDifferenceLines : []),
    ];
    const dailyBlocking = daily.hard_block === true || daily.disposition === "hostile"
      ? dailyCodes.map(reasonLabel)
      : [];
    const contextBlocking = context.hard_block === true || context.disposition === "hostile"
      ? contextCodes.map(reasonLabel)
      : [];
    const nextByStage = {
      observed: "等待 5分钟形成可审计买卖点设置",
      approaching: "等待 5分钟设置闭合并确认",
      armed: "等待 1分钟同向买卖点闭合",
      triggered: "等待人工核对下一根已完成 K 线",
      executable: "人工复核中枢、走势类型、级别与买卖点",
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
      blocking: uniqueText([
      ...dailyBlocking,
      ...contextBlocking,
      ...blockingDecisions.map(reasonLabel),
      ...sectorSourceEvidence.blocking,
      ...(higherRisk.market_gate && higherRisk.market_gate !== "GREEN"
        ? [riskGateLine("市场", higherRisk.market_gate, marketRiskReasons)]
        : []),
      ...(higherRisk.sector_gate && higherRisk.sector_gate !== "GREEN"
        ? [riskGateLine("板块", higherRisk.sector_gate, sectorRiskReasons)]
        : []),
      ...(higherRisk.symbol_gate && higherRisk.symbol_gate !== "GREEN"
        ? [riskGateLine("个股", higherRisk.symbol_gate, symbolRiskReasons)]
        : []),
      ...(marketNativeDailyCalendar && marketNativeDailyCalendar.status
          && marketNativeDailyCalendar.status !== "EXACT"
        ? nativeDailyCalendarLines("市场", marketNativeDailyCalendar)
        : []),
      ...(symbolNativeDailyCalendar && symbolNativeDailyCalendar.status
          && symbolNativeDailyCalendar.status !== "EXACT"
        ? nativeDailyCalendarLines("个股", symbolNativeDailyCalendar)
        : []),
      ]),
      next: [nextByStage[safeSignal.lifecycle_stage] || "等待新的可审计结构事实"],
      risk: [
        `5分钟失效价：${invalidation}`,
        `结构防守价：${structuralStop}`,
        `风险乘数：${riskMultiplier}`,
        ...periodDiagnosticLines("市场", marketDiagnostics),
        ...periodDiagnosticLines("板块", sectorDiagnostics),
        ...periodDiagnosticLines("个股", symbolDiagnostics),
        ...sessionEvidenceLines("市场", marketSessionEvidence),
        ...sessionEvidenceLines("板块", sectorSessionEvidence),
        ...sessionEvidenceLines("个股", symbolSessionEvidence),
        ...legacySessionEvidenceLines(
          "市场",
          marketRiskReasons,
          marketSessionEvidence,
        ),
        ...legacySessionEvidenceLines(
          "板块",
          sectorRiskReasons,
          sectorSessionEvidence,
        ),
        ...legacySessionEvidenceLines(
          "个股",
          symbolRiskReasons,
          symbolSessionEvidence,
        ),
        ...mwdWarmupLines("市场", marketMwdWarmup),
        ...mwdWarmupLines("板块", sectorMwdWarmup),
        ...mwdWarmupLines("个股", symbolMwdWarmup),
        ...nativeDailyLines("市场", marketNativeDaily),
        ...nativeDailyLines("个股", symbolNativeDaily),
        ...nativeDailyCalendarLines("市场", marketNativeDailyCalendar),
        ...nativeDailyCalendarLines("个股", symbolNativeDailyCalendar),
        ...sectorSourceEvidence.risk,
        ...(warmupRows.length || warmupReasons.length
          ? ["暖机口径：完整历史与去掉左侧三分之一后的后缀比较；当前门不证明多前缀稳定"]
          : []),
        ...warmupRows.map((row) => (
          `暖机${text(row.frequency, "?")}：双窗口${row.converged === true ? "一致" : "不一致"} · 完整 ${numberText(row.full_bar_count)} 根 / 对照后缀 ${numberText(row.suffix_bar_count)} 根`
        )),
      ],
      raw: uniqueText([
        ...dailyCodes,
        ...contextCodes,
        ...setupEvidence,
        ...setupMissing,
        ...triggerEvidence,
        ...triggerMissing,
        ...decisions,
        ...selectionSources,
        ...marketRiskReasons,
        ...symbolRiskReasons,
        ...mergedRiskReasons,
        ...sectorSourceEvidence.raw,
        ...warmupReasons,
        ...marketDiagnostics.flatMap((row) => [
          ...uniqueText(row.blocker_codes),
          ...uniqueText(row.warning_codes),
        ]),
        ...symbolDiagnostics.flatMap((row) => [
          ...uniqueText(row.blocker_codes),
          ...uniqueText(row.warning_codes),
        ]),
        ...(marketSessionEvidence && Array.isArray(marketSessionEvidence.issues)
          ? marketSessionEvidence.issues.filter(isRecord).map((row) => row.code)
          : []),
        ...(symbolSessionEvidence && Array.isArray(symbolSessionEvidence.issues)
          ? symbolSessionEvidence.issues.filter(isRecord).map((row) => row.code)
          : []),
        ...[marketMwdWarmup, sectorMwdWarmup, symbolMwdWarmup]
          .filter(isRecord)
          .map((row) => row.reason_code),
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
    const signals = value.signals.filter(isRecord).map((row) => ({ ...row }));
    if (signals.some((signal) => {
      const risk = isRecord(signal.higher_timeframe_risk)
        ? signal.higher_timeframe_risk
        : {};
      return sectorHigherTimeframeSourceEvidence(risk).blocking.length > 0;
    })) {
      throw new Error("snapshot_sector_source_invalid");
    }
    return {
      ...value,
      counts_by_stage: isRecord(value.counts_by_stage) ? { ...value.counts_by_stage } : {},
      counts_by_point_type: isRecord(value.counts_by_point_type)
        ? { ...value.counts_by_point_type }
        : Object.fromEntries(POINT_TYPES.map((point) => [point, 0])),
      sectors: value.sectors.filter(isRecord).map((row) => ({ ...row })),
      signals,
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
      const signalPoint = text(signal.point_type, "");
      if (pointType === "buy" && !signalPoint.endsWith("buy")) return false;
      if (pointType === "sell" && !signalPoint.endsWith("sell")) return false;
      if (!["all", "buy", "sell"].includes(pointType) && signalPoint !== pointType) return false;
      if (lifecycle !== "all" && signal.lifecycle_stage !== lifecycle) return false;
      const sector = isRecord(signal.sector) ? signal.sector : {};
      if (sectorId !== "all" && text(sector.sector_id, "unclassified") !== sectorId) return false;
      if (!query) return true;
      return [signal.code, signal.name, sector.sector_name, POINT_LABELS[signal.point_type]]
        .map((part) => text(part, "").toLocaleLowerCase("zh-CN"))
        .some((part) => part.includes(query));
    });
  }

  function sortSignalsForReview(signals, sectors = []) {
    const ranks = new Map(
      (Array.isArray(sectors) ? sectors : [])
        .filter(isRecord)
        .map((sector) => [
          text(sector.sector_id, ""),
          sectorRank(sector.rank) ?? Number.MAX_SAFE_INTEGER,
        ]),
    );
    const pointOrder = new Map(POINT_TYPES.map((value, index) => [value, index]));
    return (Array.isArray(signals) ? signals : []).slice().sort((left, right) => {
      const leftStage = REVIEW_STAGE_ORDER[text(left && left.lifecycle_stage, "")]
        ?? Number.MAX_SAFE_INTEGER;
      const rightStage = REVIEW_STAGE_ORDER[text(right && right.lifecycle_stage, "")]
        ?? Number.MAX_SAFE_INTEGER;
      if (leftStage !== rightStage) return leftStage - rightStage;
      const leftSector = isRecord(left && left.sector) ? left.sector : {};
      const rightSector = isRecord(right && right.sector) ? right.sector : {};
      const leftRank = ranks.get(text(leftSector.sector_id, "")) ?? Number.MAX_SAFE_INTEGER;
      const rightRank = ranks.get(text(rightSector.sector_id, "")) ?? Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) return leftRank - rightRank;
      const leftPoint = pointOrder.get(text(left && left.point_type, ""))
        ?? Number.MAX_SAFE_INTEGER;
      const rightPoint = pointOrder.get(text(right && right.point_type, ""))
        ?? Number.MAX_SAFE_INTEGER;
      if (leftPoint !== rightPoint) return leftPoint - rightPoint;
      const codeOrder = text(left && left.code, "").localeCompare(
        text(right && right.code, ""),
        "zh-CN",
      );
      if (codeOrder !== 0) return codeOrder;
      return text(left && left.signal_id, "").localeCompare(
        text(right && right.signal_id, ""),
        "zh-CN",
      );
    });
  }

  function resolveSelectedSignalId(selectedSignalId, filteredSignals, _allSignals) {
    const selectedId = text(selectedSignalId, "");
    const filteredIds = (Array.isArray(filteredSignals) ? filteredSignals : [])
      .map((signal) => text(isRecord(signal) ? signal.signal_id : "", ""))
      .filter(Boolean);
    if (selectedId && filteredIds.includes(selectedId)) return selectedId;
    if (filteredIds.length) return filteredIds[0];
    // The chart is a projection of the visible queue, not an independent
    // watchlist.  Keeping an all-signal fallback here made an empty sector or
    // point filter show a stale symbol that no longer existed on the left.
    // Clear the chart instead so the list and every chart frame stay aligned.
    return null;
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
      "d": normalized("d", "D"),
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

  function renderSectorWorkspace(
    container,
    snapshot,
    selectedSectorId = "all",
    onSelect,
    options = {},
  ) {
    if (!container || !container.ownerDocument) return;
    const documentRef = container.ownerDocument;
    const grouped = groupSignalsBySector(snapshot.signals);
    const shortlistSize = selectedSectorCount(snapshot);
    const sectorRows = snapshot.sectors.slice().sort((left, right) => {
      const leftRank = sectorRank(left.rank) ?? Number.MAX_SAFE_INTEGER;
      const rightRank = sectorRank(right.rank) ?? Number.MAX_SAFE_INTEGER;
      return leftRank - rightRank || text(left.sector_id).localeCompare(text(right.sector_id), "zh-CN");
    });
    const requestedLimit = Number(options.limit);
    const limit = Number.isInteger(requestedLimit) && requestedLimit > 0
      ? requestedLimit
      : sectorRows.length;
    let visibleSectorRows = sectorRows;
    if (options.expanded !== true && sectorRows.length > limit) {
      visibleSectorRows = sectorRows.slice(0, limit);
      const selectedSector = sectorRows.find(
        (sector) => text(sector.sector_id, "unclassified") === selectedSectorId,
      );
      if (
        selectedSector
        && !visibleSectorRows.some(
          (sector) => text(sector.sector_id, "unclassified") === selectedSectorId,
        )
      ) {
        visibleSectorRows = [...visibleSectorRows.slice(0, Math.max(0, limit - 1)), selectedSector];
      }
    }
    const rows = [
      { sector_id: "all", sector_name: "全部板块", rank: null },
      ...visibleSectorRows,
    ];
    container.dataset.expanded = options.expanded === true ? "true" : "false";
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
      const hasStrength = sector.horizontal_strength !== null
        && sector.horizontal_strength !== undefined
        && String(sector.horizontal_strength).trim() !== "";
      const rawStrength = Number(sector.horizontal_strength);
      const horizontalStrength = hasStrength && Number.isFinite(rawStrength)
        ? rawStrength.toFixed(2)
        : text(sector.horizontal_strength, "未解析");
      const strengthAnchor = text(sector.strength_anchor_session, "无锚点");
      const fullEvidence = sectorId === "all" ? "" : sectorEvidenceText(sector);
      const facts = sectorId === "all"
        ? `共 ${numberText(snapshot.sectors.length)} 个 QMT GICS3 板块`
        : `${shortlisted ? "符合要求并进入扫描" : "未通过结构门槛"} · ${rank === null ? "无有效排序" : `#${numberText(rank)}`} · 强度 ${horizontalStrength}`;
      const gate = sectorId === "all"
        ? "仅按结构筛选，不使用板块涨跌幅"
        : sector.hard_block === true
          ? `硬阻断：${reasonLabel(reasonCodes[0] || "原因未提供")}`
          : `结构依据：${reasonLabel(reasonCodes[0] || "待补充")}`;
      button.classList.toggle("is-blocked", sector.hard_block === true);
      if (sectorId !== "all") {
        button.setAttribute(
          "title",
          `${facts} · 锚点 ${strengthAnchor} · ${fullEvidence} · ${gate}`,
        );
      }
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
    card.dataset.code = text(signal.code, "");
    card.classList.toggle("is-selected", selected);
    card.setAttribute("aria-pressed", selected ? "true" : "false");
    card.setAttribute("aria-current", selected ? "true" : "false");
    card.setAttribute("aria-controls", "es-chart-workspace");

    const identity = element(documentRef, "span", "es-signal-card__identity");
    identity.append(
      element(documentRef, "strong", "", text(signal.name, signal.code)),
      element(documentRef, "code", "", text(signal.code)),
    );
    const tags = element(documentRef, "span", "es-signal-card__tags");
    tags.append(
      element(documentRef, "b", "", POINT_LABELS[signal.point_type] || text(signal.point_type)),
      element(documentRef, "em", "", lifecycleLabel(signal.lifecycle_stage)),
      element(documentRef, "em", "", selectionLabelForSignal(signal)),
    );
    const sector = isRecord(signal.sector) ? signal.sector : {};
    const dispositionLabel = (context) => {
      const disposition = text(context && context.disposition, "待判定");
      return DISPOSITION_LABELS[disposition] || disposition;
    };
    const evidence = element(documentRef, "span", "es-signal-card__evidence");
    evidence.textContent = `${text(sector.sector_name, "未分类")} · 日线 ${dispositionLabel(signal.context_d)} · 30m ${dispositionLabel(signal.context_30m)} · 1m ${signal.trigger_1m ? "已确认" : "等待"}`;
    const meta = element(documentRef, "span", "es-signal-card__meta");
    meta.append(element(documentRef, "time", "", timeText(signal.observed_at)));
    const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    const warmup = isRecord(signal.warmup) ? signal.warmup : {};
    const invalidation = setup.invalidation_price ?? signal.structural_stop;
    const supplied = (value) => value !== null && value !== undefined && String(value).trim() !== "";
    const riskParts = [];
    if (warmup.converged !== true) riskParts.push("暖机未收敛");
    if (supplied(invalidation)) riskParts.push(`防守 ${text(invalidation)}`);
    if (supplied(signal.structural_stop) && String(signal.structural_stop) !== String(invalidation)) {
      riskParts.push(`止损 ${text(signal.structural_stop)}`);
    }
    const riskMultiplier = Number(signal.risk_multiplier);
    if (Number.isFinite(riskMultiplier) && riskMultiplier > 0 && riskMultiplier !== 1) {
      riskParts.push(`风险 ×${numberText(riskMultiplier)}`);
    }
    card.append(identity, tags, evidence, meta);
    if (riskParts.length) {
      const risk = element(documentRef, "span", "es-signal-card__risk");
      risk.textContent = riskParts.join(" · ");
      card.append(risk);
    }
    if (typeof onSelect === "function") card.addEventListener("click", () => onSelect(signal));
    return card;
  }

  function renderSignalWorkspace(container, signals, selectedSignalId, onSelect) {
    if (!container || !container.ownerDocument) return null;
    const documentRef = container.ownerDocument;
    const fragment = documentRef.createDocumentFragment();
    let selectedCard = null;
    for (const signal of signals) {
      const card = signalCard(
        documentRef,
        signal,
        text(signal.signal_id, "") === selectedSignalId,
        onSelect,
      );
      if (text(signal.signal_id, "") === selectedSignalId) selectedCard = card;
      fragment.append(card);
    }
    container.replaceChildren(fragment);
    container.dataset.selectedSignalId = selectedCard ? selectedSignalId : "";
    if (
      selectedCard
      && typeof selectedCard.getBoundingClientRect === "function"
      && typeof container.getBoundingClientRect === "function"
    ) {
      const cardRect = selectedCard.getBoundingClientRect();
      const containerRect = container.getBoundingClientRect();
      const outsideViewport = cardRect.top < containerRect.top || cardRect.bottom > containerRect.bottom;
      if (outsideViewport && typeof selectedCard.scrollIntoView === "function") {
        selectedCard.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    }
    return selectedCard;
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
      delete rootElement.dataset.signalId;
      delete rootElement.dataset.selectedCode;
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
        ["d", "未知", "等待日线线段结构证据", "日线环境边界未提供"],
        ["30m", "未知", "等待大级别环境证据", "环境边界未提供"],
        ["5m", "未知", "等待操作级别设置", "失效价未提供"],
        ["1m", "等待", "尚未取得同向精确触发", "结构防守价未提供"],
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
      for (const periodFrequency of ["d", "30m", "5m", "1m"]) {
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
    rootElement.dataset.signalId = text(signal.signal_id, "");
    rootElement.dataset.selectedCode = text(signal.code, "");
    rootElement.dataset.focusedFrequency = frequency;
    rootElement.dataset.signalSide = signal.side === "sell" ? "sell" : signal.side === "buy" ? "buy" : "neutral";
    setNodeText(rootElement, "[data-selected-name]", text(signal.name, signal.code));
    setNodeText(rootElement, "[data-selected-code]", signal.code);
    setNodeText(rootElement, "[data-selected-point]", POINT_LABELS[signal.point_type] || signal.point_type);
    setNodeText(rootElement, "[data-selected-stage]", lifecycleLabel(signal.lifecycle_stage));
    setNodeText(rootElement, "[data-selected-tower]", "老笔 → 线段中枢 / 本周期0级（非递归）");
    const selectedSetup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    setNodeText(
      rootElement,
      "[data-selected-stop]",
      text(selectedSetup.invalidation_price ?? signal.structural_stop, "未提供"),
    );
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
    for (const frequency of ["d", "30m", "5m", "1m"]) {
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
    completedWithoutSignalCount,
    dailyPreselectionDiagnosticsText,
    dailyPreselectionText,
    decisionSummaryForSignal,
    defaultFrequencyForSignal,
    evidenceGroupsForSignal,
    emptySignalDetail,
    filterSignals,
    groupSignalsBySector,
    lifecycleLabel,
    manualFocusState,
    memberHistoryDiagnosticsText,
    normalizeSnapshot,
    renderChartWorkspace,
    renderSectorWorkspace,
    renderSignalWorkspace,
    resolveFocusState,
    resolveSelectedSignalId,
    selectionLabelForSignal,
    periodPathForSignal,
    priorityMonitorDiagnosticsText,
    priorityMonitorText,
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
    sortSignalsForReview,
    text,
    timeText,
  };
});
