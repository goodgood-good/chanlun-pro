"use strict";

(function attachTradingScreeningUi(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TradingScreeningUi = api;
})(typeof globalThis === "object" ? globalThis : this, function createTradingScreeningUi() {
  const SCHEMA = "chanlun-trading-screening";
  const SIGNAL_CATALOG_TRANSPORT = "signal-catalog-v1";
  const SIGNAL_CATALOG_SCHEMA = "chanlun-early-signals-signal-catalog-v1";
  const SIGNAL_CATALOG_FIELDS = [
    "execution_profile",
    "higher_timeframe_risk",
    "position_recommendation",
    "sector",
    "context_30m",
    "context_d",
    "decision_reasons",
    "warmup",
  ];
  const SECTOR_SAME_BASE_SOURCE_MODE = "PAGE_PARITY_SAME_5M_BASE";
  const SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE =
    "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH";
  const SECTOR_SAME_BASE_COVERAGE_CONTRACT_ID =
    "chanlun-qmt-sector-same-5m-source-coverage";
  const SELL_ONLY_HIGHER_TIMEFRAME_EVIDENCE_POLICY =
    "SCHEMA_COMPLETE_UNRESOLVED_WITHOUT_PROVIDER_CALL";
  const SELL_ONLY_ENTRY_GATE_REASON =
    "HIGHER_TIMEFRAME_ENTRY_GATE_NOT_APPLICABLE_TO_SELL_ONLY";
  const SELL_ONLY_PRESENTATION_CURRENT = "CURRENT_EXPLICIT_REASON";
  const SELL_ONLY_PRESENTATION_LEGACY = "LEGACY_POLICY_COMPATIBILITY";
  const POINT_TYPES = ["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"];
  const POINT_REVIEW_ORDER = ["1buy", "1sell", "2buy", "2sell", "3buy", "3sell"];
  const REVIEW_STAGE_ORDER = {
    executable: 0,
    triggered: 1,
    armed: 2,
    formed: 3,
    approaching: 4,
    observed: 5,
    monitoring: 6,
    active: 6,
    invalidated: 7,
    closed: 8,
  };
  const REVIEW_PRIORITY_CONFIDENCE = {
    HIGH: 3,
    MEDIUM: 2,
    LOW: 1,
    UNRESOLVED: 0,
  };
  const REVIEW_PRIORITY_POSITION_BANDS = {
    BLOCKED: { base: 8, min: 0, max: 19 },
    NOT_ACTIONABLE: { base: 30, min: 20, max: 39 },
    UNRESOLVED: { base: 30, min: 20, max: 39 },
    CONDITIONAL: { base: 55, min: 40, max: 69 },
    RECOMMENDED: { base: 72, min: 70, max: 89 },
    STRUCTURAL_SELL_REVIEW: { base: 82, min: 80, max: 89 },
    MANUAL_ATTENTION_SELL_REVIEW: { base: 92, min: 90, max: 100 },
  };
  const REVIEW_PRIORITY_LIFECYCLE_STAGES = new Set([
    "approaching", "formed", "armed", "observed", "triggered", "executable",
  ]);
  const REVIEW_PRIORITY_RISK_GATES = new Set(["GREEN", "AMBER", "RED", "UNRESOLVED"]);
  const CURRENT_SELECTION_LIFECYCLE_STAGES = new Set([
    "observed", "monitoring", "approaching", "triggered", "executable", "active",
  ]);
  const FREQUENCIES = new Set(["d", "30m", "5m", "1m"]);
  const LAYOUTS = new Set(["focus", "dual", "triple"]);
  const REALTIME_REVIEW_SCHEMA = "chanlun-realtime-review-inbox";
  const POINT_LABELS = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
  };
  const DIVERGENCE_LABELS = {
    trend: "趋势背驰",
    consolidation: "盘整背驰",
  };
  const LIFECYCLE_LABELS = {
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
  };
  const STATUS_LABELS = {
    ready: "已就绪",
    verified: "已验证",
    running: "运行中",
    pending: "等待中",
    not_due: "未到运行时段",
    disabled: "未启用",
    unavailable: "不可用",
    review_blocked: "复核受阻",
    coverage_in_progress: "当前范围扫描中",
    awaiting_runtime_verification: "等待运行验证",
    awaiting_first_run: "等待首次运行",
    warming: "覆盖暖机中",
    catching_up: "午间补齐中",
    idle_no_candidates: "暂无合格监听对象",
    ready_idle: "就绪但当前空闲",
    capacity_insufficient: "监听容量不足",
    cadence_overdue: "监听节奏逾期",
    degraded: "运行降级",
    priority_monitor_degraded: "即时复查通道降级",
    candidate_monitor_degraded: "候选轮换通道降级",
    notification_unverified: "通知送达尚未验证",
    notification_degraded: "通知投递降级",
    notification_not_configured: "未配置主动通知",
    awaiting_first_snapshot: "等待首份快照",
    target_session_stale: "适用交易日已过期",
    complete: "已完成",
    in_progress: "进行中",
    incomplete_not_published: "未达到发布条件",
    GREEN: "绿色（通过）",
    AMBER: "琥珀色（需复核）",
    RED: "红色（阻断）",
    UNRESOLVED: "尚未解决",
    EXACT: "精确匹配",
    UNAVAILABLE: "不可用",
    STABLE_ALL_PREFIXES: "全部前缀稳定",
    CONVERGED_ONLY_WITH_LONGER_HISTORY: "增加历史后才稳定",
    INSUFFICIENT_PREFIXES: "可用前缀不足",
    NON_MONOTONIC: "不同历史长度下结论不单调",
    LIVE_DISABLED: "不自动下单",
    RESEARCH_ONLY: "仅供研究",
    REVIEW_REQUIRED: "必须人工复核",
    READY: "已就绪",
  };

  function statusLabel(value, fallback = "未知状态") {
    const status = text(value, "").trim();
    return status ? (STATUS_LABELS[status] || fallback) : fallback;
  }

  function periodLabel(value) {
    const period = text(value, "").trim().toUpperCase();
    return {
      M: "月线",
      W: "周线",
      D: "日线",
      "30M": "30分钟",
      "5M": "5分钟",
      "1M": "1分钟",
    }[period] || "未知周期";
  }

  function lifecycleLabel(value) {
    const stage = text(value, "");
    return LIFECYCLE_LABELS[stage] || "未知状态";
  }

  function lifecycleStageForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    return text(safeSignal.lifecycle_stage, "");
  }

  function isCurrentSelectionSignal(signal) {
    return CURRENT_SELECTION_LIFECYCLE_STAGES.has(
      lifecycleStageForSignal(signal),
    );
  }

  function signalQueueFacts(signals) {
    const current = Array.isArray(signals)
      ? signals.filter((signal) => isRecord(signal) && isCurrentSelectionSignal(signal))
      : [];
    const monitorPositionCount = current.filter((signal) => (
      signal.us_monitor_projection === true
      && !POINT_TYPES.includes(text(signal.point_type, ""))
    )).length;
    return {
      total_count: current.length,
      structure_clue_count: current.length - monitorPositionCount,
      monitor_position_count: monitorPositionCount,
    };
  }

  function signalQueueCountText(visibleSignals, allSignals = visibleSignals) {
    const visible = signalQueueFacts(visibleSignals);
    const total = signalQueueFacts(allSignals);
    const describe = (facts) => {
      const parts = [`${facts.structure_clue_count} 条5m结构线索`];
      if (facts.monitor_position_count > 0) {
        parts.push(`${facts.monitor_position_count} 个独立监听`);
      }
      return parts.join(" · ");
    };
    if (
      visible.structure_clue_count === total.structure_clue_count
      && visible.monitor_position_count === total.monitor_position_count
    ) return describe(visible);
    return `${describe(visible)} / 全部 ${describe(total)}`;
  }

  function setupFormationStateForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const declared = text(setup.formation_state, "");
    if (["forming", "geometry_ready", "confirmed"].includes(declared)) return declared;
    // v2 快照曾把非交易几何候选写成 formed。只读兼容时立即降回
    // geometry_ready，禁止旧字段继续生成“买卖点已形成”的文案。
    if (declared === "formed" && setup.status === "provisional") return "geometry_ready";
    if (setup.status === "confirmed") return "confirmed";
    if (lifecycleStageForSignal(safeSignal) === "formed") return "geometry_ready";
    if (setup.status === "provisional") return "forming";
    return "";
  }

  function setupLockStateForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const declared = text(setup.lock_state, "");
    if (["pending", "locked", "unknown"].includes(declared)) return declared;
    const formation = setupFormationStateForSignal(safeSignal);
    if (formation === "confirmed") return "locked";
    if (["forming", "geometry_ready"].includes(formation)) return "pending";
    return "";
  }

  function fiveMinuteTradeSignalConfirmedForSignal(signal) {
    if (!isCurrentSelectionSignal(signal)) return false;
    if (setupFormationStateForSignal(signal) === "confirmed") return true;
    return ["triggered", "executable", "active"].includes(
      lifecycleStageForSignal(signal),
    );
  }

  function pointLabelForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const pointType = text(safeSignal.point_type, "");
    const label = POINT_LABELS[pointType]
      || (pointType
        ? "未识别买卖点"
        : safeSignal.us_monitor_projection === true ? "等待买卖点" : "结构提示");
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const formationState = setupFormationStateForSignal(safeSignal);
    const lifecycleStage = lifecycleStageForSignal(safeSignal);
    if (lifecycleStage === "invalidated") {
      if (formationState === "confirmed") return `${label}已确认后失效`;
      return formationState === "geometry_ready" ? `${label}候选已失效` : `${label}已失效`;
    }
    if (formationState === "confirmed") {
      // 买卖点主标签只回答“5 分钟操作确认是否成立”。防重绘锁是随后独立
      // 跟踪的审计事实，继续在周期路径和证据面板中展示，不能让大量正常的
      // live-tail 操作确认看起来仍在等待触发。
      return `${label}操作确认`;
    }
    if (formationState === "geometry_ready") {
      return setup.contains_unlocked_segment === true
        ? `${label}候选待锁定`
        : `${label}候选待确认`;
    }
    if (formationState === "forming") return `${label}候选`;
    return label;
  }

  function terminalSegmentSummary(setup) {
    const safe = isRecord(setup) ? setup : {};
    const role = text(safe.terminal_segment_role, "");
    if (!["latest_unfinished", "latest_completed"].includes(role)) return "";
    const stateCode = text(safe.terminal_segment_state, "");
    const roleLabel = role === "latest_unfinished"
      ? "最新形成中线段"
      : stateCode === "locked" ? "最新已锁定线段" : "最新几何成形线段";
    const direction = DIRECTION_LABELS[text(safe.terminal_segment_direction, "")]
      || "方向待判定";
    const state = {
      forming: "形成中",
      formed: "几何已成形、证据待固化",
      locked: "已锁定",
    }[stateCode] || "状态待判定";
    return `${roleLabel} · ${direction} · ${state}`;
  }

  function terminalSegmentRange(setup) {
    const safe = isRecord(setup) ? setup : {};
    if (!terminalSegmentSummary(safe)) return "";
    const start = fullDateTimeText(safe.terminal_segment_start_at);
    const end = fullDateTimeText(safe.terminal_segment_end_at);
    return start === "暂不可用" || end === "暂不可用"
      ? "线段时间待核对"
      : `线段 ${start} → ${end}`;
  }

  function siblingStructureTimestamp(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    for (const value of [
      setup.terminal_segment_end_at,
      setup.available_at,
      setup.confirmed_at,
      safeSignal.observed_at,
    ]) {
      const parsed = Date.parse(text(value, ""));
      if (Number.isFinite(parsed)) return parsed;
    }
    return null;
  }

  function siblingStructureContextSummary(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const context = isRecord(safeSignal.presentation_sibling_structure_context)
      ? safeSignal.presentation_sibling_structure_context
      : {};
    return text(context.summary, "");
  }

  function siblingStructureContextBadge(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const context = isRecord(safeSignal.presentation_sibling_structure_context)
      ? safeSignal.presentation_sibling_structure_context
      : {};
    const relation = text(context.relation, "");
    if (relation === "opposite_forming_candidate") return "反向候选形成中";
    if (relation === "opposite_confirmed_setup") return "另有反向操作确认";
    if (relation === "same_point_forming_candidate") return "同类新候选形成中";
    if (relation === "same_point_confirmed_setup") return "另有同类操作确认";
    if (relation === "same_side_forming_candidate") return "同向候选形成中";
    if (relation === "same_side_confirmed_setup") return "另有同向操作确认";
    return "";
  }

  function annotateSiblingStructureContexts(values) {
    const rows = (Array.isArray(values) ? values : [])
      .filter(isRecord)
      .map((row) => {
        const signal = { ...row };
        // 该字段只能由本次页面投影计算，不能信任磁盘快照或通知记录里可能
        // 残留的旧展示结论。
        delete signal.presentation_sibling_structure_context;
        return signal;
      });
    const grouped = new Map();
    rows.forEach((signal) => {
      if (
        signal.synthetic_notification_projection === true
        || signal.us_monitor_projection === true
      ) return;
      const code = text(signal.code, "");
      if (!code) return;
      if (!grouped.has(code)) grouped.set(code, []);
      grouped.get(code).push(signal);
    });

    const newerThan = (forming, confirmed) => {
      const formingAt = siblingStructureTimestamp(forming);
      const confirmedAt = siblingStructureTimestamp(confirmed);
      return formingAt === null || confirmedAt === null || formingAt >= confirmedAt;
    };
    const newestFirst = (left, right) => (
      (siblingStructureTimestamp(right) ?? Number.NEGATIVE_INFINITY)
      - (siblingStructureTimestamp(left) ?? Number.NEGATIVE_INFINITY)
    );
    const activeConfirmedStages = new Set(["triggered", "executable", "active"]);
    const activeFormingStages = new Set(["observed", "approaching", "formed", "armed"]);

    grouped.forEach((group) => {
      if (group.length < 2) return;
      const confirmed = group.filter((signal) => {
        const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
        return setup.terminal_segment_role === "latest_completed"
          && setupFormationStateForSignal(signal) === "confirmed"
          && activeConfirmedStages.has(lifecycleStageForSignal(signal));
      }).sort(newestFirst);
      const forming = group.filter((signal) => {
        const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
        return setup.terminal_segment_role === "latest_unfinished"
          && ["forming", "geometry_ready"].includes(
            setupFormationStateForSignal(signal),
          )
          && activeFormingStages.has(lifecycleStageForSignal(signal));
      }).sort(newestFirst);
      if (!confirmed.length || !forming.length) return;

      confirmed.forEach((signal) => {
        const sibling = forming.find((candidate) => newerThan(candidate, signal));
        if (!sibling) return;
        const currentPoint = POINT_LABELS[text(signal.point_type, "")] || "买卖点";
        const siblingPoint = POINT_LABELS[text(sibling.point_type, "")] || "买卖点";
        const opposite = text(signal.side, "") !== text(sibling.side, "");
        const samePoint = text(signal.point_type, "") === text(sibling.point_type, "");
        const relation = opposite
          ? "opposite_forming_candidate"
          : samePoint
            ? "same_point_forming_candidate"
            : "same_side_forming_candidate";
        const relationLabel = opposite ? "反向" : samePoint ? "同类" : "同向";
        signal.presentation_sibling_structure_context = {
          relation,
          sibling_signal_id: text(sibling.signal_id, ""),
          sibling_point_type: text(sibling.point_type, ""),
          sibling_side: text(sibling.side, ""),
          sibling_lifecycle_stage: lifecycleStageForSignal(sibling),
          sibling_terminal_segment_end_at: isRecord(sibling.setup_5m)
            ? sibling.setup_5m.terminal_segment_end_at || null
            : null,
          summary: `较新的${relationLabel}${siblingPoint}候选正在形成（未确认）；当前${currentPoint}操作确认仍保留`,
        };
      });

      forming.forEach((signal) => {
        const sibling = confirmed.find((candidate) => newerThan(signal, candidate));
        if (!sibling) return;
        const currentPoint = POINT_LABELS[text(signal.point_type, "")] || "买卖点";
        const siblingPoint = POINT_LABELS[text(sibling.point_type, "")] || "买卖点";
        const opposite = text(signal.side, "") !== text(sibling.side, "");
        const samePoint = text(signal.point_type, "") === text(sibling.point_type, "");
        const relation = opposite
          ? "opposite_confirmed_setup"
          : samePoint
            ? "same_point_confirmed_setup"
            : "same_side_confirmed_setup";
        const relationLabel = opposite ? "反向" : samePoint ? "同类" : "同向";
        signal.presentation_sibling_structure_context = {
          relation,
          sibling_signal_id: text(sibling.signal_id, ""),
          sibling_point_type: text(sibling.point_type, ""),
          sibling_side: text(sibling.side, ""),
          sibling_lifecycle_stage: lifecycleStageForSignal(sibling),
          sibling_terminal_segment_end_at: isRecord(sibling.setup_5m)
            ? sibling.setup_5m.terminal_segment_end_at || null
            : null,
          summary: `当前为较新的${relationLabel}${currentPoint}候选（未确认）；同标的另有${siblingPoint}操作确认`,
        };
      });
    });
    return rows;
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
    HARD_BLOCKED_NO_TRADE: "当前结构条件不适合纳入操作计划",
    POSITION_RATIO_INPUT_UNRESOLVED: "结构价格或风险参数不足，风险参考待核对",
    STRUCTURAL_RISK_BUDGET_SIZED: "已按5分钟结构锚点至防守位测算风险参考",
    CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED: "已按当前价至5分钟防守位测算风险参考",
    STRUCTURAL_MODEL_CAP_REQUIRES_MANUAL_REVIEW: "比例只用于结构模型比较，仍须人工复核",
    SAME_OR_HIGHER_STRUCTURE_FULL_EXIT: "同级或更高级别卖点按完整退出规则处理",
    LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION: "低级别或不同结构卖点只按段差规则处理",
    SELL_STRUCTURE_RELATION_REQUIRED: "卖点与目标结构的级别关系需人工核对",
    ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_FOR_PRECISE_EXECUTION: "5分钟信号已确认，等待1分钟区间套精确定位",
    LEGACY_STRUCTURAL_RISK_MODEL_RATIO: "历史通知只保留结构模型比较值",
    LEGACY_BUY_RESTRICTION_REQUIRES_REVIEW: "历史买入限制原因需重新核对",
    LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED: "历史通知的结构价格或风险参数不完整",
    directional_points_expired: "方向性买卖点已超过当前有效窗口",
    stock_one_minute_segment_difference_only: "个股1分钟数据只用于区间套精确买卖位置确认",
    core_confirmed_point: "核心买卖点已达到操作确认",
    five_minute_geometric_point_formed: "旧版含义：5分钟离开/回抽几何已出现；仍未达到操作确认",
    five_minute_geometric_candidate_awaiting_confirmation: "5分钟仅出现非交易几何候选，尚未达到操作确认",
    one_minute_not_confirmed: "5分钟买点已确认，等待1分钟区间套精确定位",
    one_minute_sell_not_confirmed: "5分钟卖点已确认，等待1分钟区间套精确定位",
    confirmed_sell_with_down_structure: "下跌结构中的卖点已确认",
    confirmed_buy_structure: "买入方向结构已确认",
    terminal_line_confirmed: "末端结构确认",
    unfinished_core_mmd: "核心买卖点结构尚未闭合",
    no_active_directional_point: "暂无有效方向买卖点",
    same_or_higher_structure_conflict: "同级或更高结构存在反向冲突",
    structure_conflict: "结构方向存在冲突",
    thirty_minute_hostile: "30分钟环境逆风，仅降低等级",
    daily_structure_hostile: "日线结构逆风，仅降低等级",
    unified_strict_signal_engine: "来自统一严格买卖点引擎",
    unfinished_segment_participates: "操作确认已成立；末端结构仍会随新K更新（不影响当前复核）",
    provisional_center_completion: "中枢离开与首次回抽已经完成",
    core_boundary_held: "首次回抽保持在中枢核心之外",
    unfinished_segment_lock: "末端结构仍会随新K更新",
    formal_center_confirmation: "中枢证据继续固化",
    lower_or_unrelated_structure_risk: "较低或无关结构存在风险",
    sell_not_confirmed: "5分钟卖点尚未达到操作确认",
    top_fractal_confirmed: "顶分型确认",
    bottom_fractal_confirmed: "底分型确认",
    five_minute_not_confirmed: "5分钟买卖点尚未达到操作确认",
    setup_not_confirmed: "5分钟设置尚未达到操作确认",
    five_minute_setup_formed_awaiting_lock: "旧版几何候选，尚未达到操作确认",
    five_minute_geometry_candidate_awaiting_confirmation: "5分钟仅为几何候选，尚未达到操作确认",
    CANDIDATE_MONITOR_ERRORS: "候选轮换最近一轮存在计算错误",
    CANDIDATE_MONITOR_RUNTIME_UNVERIFIED: "候选轮换尚未完成本进程验证",
    PRESELECTION_CLOSE_CUTOFF_INCOMPLETE: "盘中尚未形成完整收盘候选快照",
    CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT: "配置容量不足以在目标周期覆盖全部实时候选",
    CANDIDATE_MONITOR_OBSERVED_CAPACITY_INSUFFICIENT: "最近一轮实际容量未覆盖全部实时候选",
    CANDIDATE_MONITOR_CADENCE_OVERDUE: "部分实时候选已超过目标复查周期",
    CANDIDATE_MONITOR_WARMING: "实时候选仍在完成首轮覆盖",
    CANDIDATE_MONITOR_NO_ELIGIBLE_UNIVERSE: "当前没有通过板块门控、已有信号或人工关注范围进入实时监听的标的",
    CANDIDATE_MONITOR_DEGRADED: "候选轮换通道未满足时效要求",
    PRIORITY_MONITOR_DEGRADED: "人工关注、自选和已有信号即时复查通道异常",
    PRIORITY_MONITOR_RUNTIME_UNVERIFIED: "即时复查通道尚未完成本进程验证",
    PRIORITY_MONITOR_UNAVAILABLE: "即时复查状态未提供",
    REALTIME_NOTIFICATION_NOT_CONFIGURED: "尚未配置主动通知投递",
    REALTIME_NOTIFICATION_DELIVERY_UNVERIFIED: "通知投递已配置，但尚无成功送达证明",
    NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED: "尚无到期通知事件或成功送达记录",
    DELIVERY_SUCCESS_PROVEN: "已有成功送达证明",
    NON_TRADING_SESSION_NOT_DUE: "当前不在A股分钟监听时段",
    mixed_or_transition_structure: "结构处于混合或过渡状态",
    three_buy_not_first_center: "三买不属于当前走势第一中枢",
    no_active_position: "旧版卖点缺少目标结构级别，需人工核对",
    external_position_unknown_manual_review: "旧版卖点缺少目标结构级别，需人工核对",
    sell_structure_relation_requires_manual_review: "需人工核对卖点与目标结构的级别关系",
    unfinished_trend_divergence: "趋势背驰结构尚未闭合",
    three_buy_lacks_tick_clearance: "三买回抽未留出最小价格间隔",
    structure_invalidated: "对应结构已经失效",
    sector_membership_missing: "未匹配 QMT GICS3/GICS4 板块",
    gics3_parent_gate_unavailable: "GICS3 父行业数据不可用，子行业关闭",
    gics3_parent_gate_blocked: "GICS3 父行业结构不利，子行业关闭",
    higher_structure_sell_risk: "更高结构存在卖点风险",
    sector_hostile: "行业结构逆风，仅降低等级",
    HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN: "旧高周期风险环境未全绿，仅作审计提示",
    HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED: "高周期同源数据完整性未通过，已关闭新买入",
    WARMUP_CONVERGENCE_GATE_FAILED: "5分钟结构暖机尚未收敛，已关闭新买入",
    SAME_PERIOD_CONTEXT_GRADE_A: "日线与30分钟环境均支持",
    SAME_PERIOD_CONTEXT_GRADE_B: "日线与30分钟环境混合或中性",
    SAME_PERIOD_CONTEXT_GRADE_C: "日线与30分钟环境逆风，谨慎观察",
    SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED: "日线或30分钟MA/分型证据不足",
    HIGHER_TIMEFRAME_GATE_NOT_ATTACHED: "日线高级别研究证据未接入",
    HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED: "板块日线高级别研究证据未接入",
    HIGHER_TIMEFRAME_ENTRY_GATE_NOT_APPLICABLE_TO_SELL_ONLY: "纯卖出结构不适用买入专用高级别风险门",
    HIGHER_TIMEFRAME_GATE_NOT_GREEN: "高级别历史研究状态未全部就绪（不参与当前执行放行）",
    ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED: "1分钟区间套历史证据仍保留，但当前精确定位边界已经过期",
    ONE_MINUTE_SEGMENT_BOUNDARY_MISSING: "1分钟区间套历史证据仍保留，但当前精确定位边界不可用",
    QMT_SECTOR_HIGHER_TIMEFRAME_RISK_UNAVAILABLE: "QMT 板块日线高级别同源研究证据尚未接入",
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
    M_CENTER_MAPPING_UNRESOLVED: "长期历史顶分型映射尚未解决（研究诊断）",
    W_CENTER_MAPPING_UNRESOLVED: "中期历史顶分型映射尚未解决（研究诊断）",
    D_CENTER_MAPPING_UNRESOLVED: "日线顶分型到30分钟中枢的映射未解决",
    QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING: "QMT 1分钟同源序列缺少预期交易日",
    QMT_ONE_MINUTE_SESSION_GRID_INVALID: "QMT 1分钟交易时段网格不完整",
    QMT_BENCHMARK_ONE_MINUTE_PREFIX_STALE: "QMT 市场基准1分钟前缀已过期",
    QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE: "QMT 原生日线超前于1分钟同源截止，存在时间穿越",
    KLINE_MINIMUM_HISTORY_NOT_MET: "当前一年窗口的最小K线历史不足",
    THIRD_SELL_IGNORED_AFTER_CENTER_EXTENSION: "中枢扩展后的三卖不用于确认该风险事件",
    MARKET_GATE_AMBER: "市场高级别风险门为琥珀色，需要人工复核",
    SECTOR_GATE_AMBER: "板块高级别风险门为琥珀色，需要人工复核",
    SYMBOL_GATE_AMBER: "个股高级别风险门为琥珀色，需要人工复核",
    MARKET_GATE_UNRESOLVED: "市场高级别研究状态尚未解决，仅作环境提示",
    SECTOR_GATE_UNRESOLVED: "板块高级别研究状态尚未解决，仅作环境提示",
    SYMBOL_GATE_UNRESOLVED: "个股高级别风险门尚未解决",
    M_COMPLETED_MA5_UNAVAILABLE: "长期历史样本不足，研究均线不可用",
    QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH: "QMT 原生日线与1分钟派生日线的开高低收量不一致",
    QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH: "QMT 原生日线与交易日历覆盖不一致",
    QMT_SECTOR_TRIGGER_REQUIRED: "A股买入线索需要当前 QMT 板块触发",
    SIGNED_SELECTION_RESEARCH_REQUIRED: "旧版正式研究账本条件未满足（当前生产选股不再依赖该账本）",
    ETF_PROXY_SECTOR_NOT_REQUIRED: "ETF 代理路径不要求行业板块门",
    sector_catalog_members_missing: "板块目录没有可用成分",
    sector_constituent_count_below_minimum: "板块有效成分数量低于最低要求",
    sector_member_coverage_insufficient: "板块成分行情覆盖不足",
    COMMON_BROAD_MARKET_DAILY_BOTTOM_FRACTAL_ANCHOR: "使用统一大盘日线底分型作为相对强弱锚点",
    CURRENT_QMT_MEMBERSHIP_AUTHORIZED: "板块成分来自当前 QMT 授权目录",
    EMPTY_POINT_IN_TIME_BASKET: "该历史时点没有可用板块成分篮子",
    EQUAL_WEIGHT_MEMBER_MA_CATEGORY_MEAN: "板块强弱采用成分股均线类别等权平均",
    ACTIVE_PAIRWISE_WARMUP_MAY_BE_FALSE_STABLE: "当前双窗口看似稳定，但多前缀检查仍可能不稳定",
    LONGER_HISTORY_REQUIRED_FOR_STABILITY_EVIDENCE: "需要更长历史才能证明结构稳定",
    WARMUP_ENVELOPE_INSUFFICIENT_PREFIXES: "暖机可比较的历史前缀数量不足",
    WARMUP_ENVELOPE_NON_MONOTONIC: "暖机结论随历史长度变化且不单调",
    WARMUP_ENVELOPE_PREFIX_SENSITIVE: "结构结论对暖机起点敏感",
    WARMUP_ENVELOPE_STABLE_ALL_PREFIXES: "全部受检暖机前缀的结构结论一致",
    VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP: "可见行情起点晚于要求的暖机起点",
    PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE: "QMT 物理行情源的左边界不可验证",
    PRESELECTION_UNAVAILABLE: "今日候选名单尚不可用",
    PRIORITY_MONITOR_UNAVAILABLE: "盘中优先监听尚不可用",
    HUMAN_REVIEW_MATERIALIZATION_FAILED: "人工复核材料生成失败",
    REVIEW_BOUNDARY_INVALID: "人工复核发布边界校验未通过",
    US_MONITOR_UNAVAILABLE: "美股辅助监听尚不可用",
    US_MONITOR_CONTRACT_INVALID: "美股辅助监听返回的数据契约无效",
    US_MONITOR_HEALTH_UNAVAILABLE: "美股辅助监听健康状态暂不可用",
    US_MONITOR_JOB_NOT_REGISTERED: "美股辅助监听任务尚未注册",
    US_NOTIFICATION_NOT_CONFIGURED: "美股辅助监听尚未配置主动通知",
    US_MONITOR_AWAITING_FIRST_RUN: "美股辅助监听等待首次运行",
    US_MONITOR_NOT_READY: "美股辅助监听尚未就绪",
    US_MONITOR_DEGRADED: "美股辅助监听运行降级，系统将重试",
    US_MONITOR_STALE: "美股辅助监听结果已过期，系统将重试",
    QMT_SECTOR_ELIGIBLE_SCOPE: "来自当前 QMT 合格板块候选范围",
    QMT_SECTOR_TRIGGER: "来自当前 QMT 支撑板块触发范围",
    NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH: "板块长期环境采用 QMT 原生日线研究桥",
    projected_geometric_structure: "使用未锁定末端线段投影结构，仅作候选观察",
    geometry_confirmed_before_audit_lock: "离开与回抽几何已满足，操作确认成立",
    formal_center: "已识别正式中枢",
    complete_leave: "离开段已经完成",
    complete_first_return: "首次回抽已经完成",
    lifecycle_not_actionable: "当前生命周期尚不可操作",
    consolidation_divergence: "盘整背驰证据成立",
    complete_adjacent_rebound: "相邻反弹段已经完成",
    confirmed_first_class_parent: "对应的一类买卖点母结构已确认",
    complete_first_pullback: "首次回踩已经完成",
    width_matched_entry_departure_legs: "进入段与离开段已按同宽口径比较",
    confirmed_same_level_boundary: "同级别结构边界已确认",
    macd_any_indicator_decay: "MACD 至少一项力度指标衰减",
    strength_source_macd: "力度比较采用 MACD",
    formal_consolidation_movement: "已识别正式盘整走势",
    single_center_consolidation: "盘整走势包含一个中枢",
    prior_extreme_held: "首次回抽未突破前高或前低",
    macd_dif_extreme_decay: "MACD DIF 极值衰减",
    comparison_leg_width_1: "力度比较采用相邻一段",
    macd_histogram_area_decay: "MACD 柱面积衰减",
    macd_histogram_peak_decay: "MACD 柱峰值衰减",
    comparison_leg_width_3: "力度比较采用跨三段口径",
    formal_trend: "已识别正式趋势",
    two_separated_centers: "趋势包含两个不重叠中枢",
    trend_divergence: "趋势背驰证据成立",
    confirmed_lower_level_first_class_parent: "次级别一类买卖点母结构已确认",
    small_to_large_reversal: "小转大结构反转证据成立",
    live_first_pullback: "首次回踩正在形成",
    prior_extreme_currently_held: "当前回踩仍未突破前高或前低",
    live_first_return: "首次回抽正在形成",
    core_boundary_currently_held: "当前回抽仍保持在中枢核心之外",
    terminal_unit_locked: "等待末端结构单元完成锁定",
  };
  Object.assign(REASON_LABELS, {
    QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT: "高级别历史研究窗口不足 480 根已完成日线，仅作审计提示",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED: "高级别历史研究完整前缀与 320 根后缀结论不一致，仅作审计提示",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE: "高级别历史研究双窗口复算一致",
    PREFIX_SIGNATURE_DIVERGED: "多前缀结构签名不一致",
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
    "five_minute_setup_formed_awaiting_lock",
    "five_minute_geometry_candidate_awaiting_confirmation",
  ]);
  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isStrictOneMinuteSegmentPoint(value) {
    if (!isRecord(value)) return false;
    const sourceFrequency = text(value.source_frequency, "1m").trim();
    const rawLevel = value.recursive_level;
    const recursiveLevel = rawLevel === null || rawLevel === undefined || rawLevel === ""
      ? 0
      : Number(rawLevel);
    return sourceFrequency === "1m"
      && Number.isInteger(recursiveLevel)
      && recursiveLevel === 0;
  }

  function segmentDifferenceForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const canonical = safeSignal.segment_difference_1m;
    return (
      isRecord(canonical)
      && Object.keys(canonical).length
      && isStrictOneMinuteSegmentPoint(canonical)
    )
      ? canonical
      : null;
  }

  function segmentPointEvidenceLabel(segment, fallback = "1分钟结构点") {
    const safeSegment = isRecord(segment) ? segment : {};
    const point = POINT_LABELS[safeSegment.point_type]
      || text(safeSegment.point_type, fallback);
    const divergence = DIVERGENCE_LABELS[text(safeSegment.divergence_kind, "")];
    return divergence ? `${point}（${divergence}）` : point;
  }

  function segmentDifferenceStatusForSignal(signal) {
    const boundaryStatus = segmentDifferenceBoundaryStatusForSignal(signal);
    // Compatibility alias for consumers that still expect the old combined
    // status.  Sell-side evidence historically used "current" even though a
    // A sell signal does not require a buy-side interval-nesting witness.
    return boundaryStatus === "not_applicable" ? "current" : boundaryStatus;
  }

  function segmentDifferenceEvidenceStatusForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const trigger = segmentDifferenceForSignal(safeSignal);
    if (safeSignal.synthetic_notification_projection === true) {
      const persisted = text(
        safeSignal.notification_segment_difference_evidence_status,
        "",
      );
      if (persisted === "present") return trigger ? "present" : "absent";
      if (["absent", "unknown"].includes(persisted)) return persisted;
    }
    return trigger ? "present" : "absent";
  }

  function segmentDifferenceBoundaryStatusForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    let persisted = "";
    if (safeSignal.synthetic_notification_projection === true) {
      persisted = text(
        safeSignal.notification_segment_difference_boundary_status,
        "",
      );
      if ([
        "absent", "expired", "unavailable", "unknown", "not_applicable",
      ].includes(persisted)) return persisted;
    }
    const trigger = segmentDifferenceForSignal(safeSignal);
    if (!trigger) return "absent";
    if (text(safeSignal.side || trigger.side, "") !== "buy") {
      return "not_applicable";
    }
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const reasons = new Set([
      ...uniqueText(safeSignal.decision_reasons),
      ...uniqueText(profile.advisory_reason_codes),
      ...uniqueText(profile.hard_block_reason_codes),
    ]);
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED")) return "expired";
    const boundary = isRecord(safeSignal.entry_execution_boundary)
      ? safeSignal.entry_execution_boundary
      : {};
    const boundaryValue = boundary.entry_valid_until
      || safeSignal.notification_segment_difference_valid_until;
    const validUntil = Date.parse(text(boundaryValue, ""));
    const evaluatedMillis = evaluatedAt instanceof Date
      ? evaluatedAt.getTime()
      : Date.parse(text(evaluatedAt, ""));
    if (
      Number.isFinite(validUntil)
      && Number.isFinite(evaluatedMillis)
      && validUntil <= evaluatedMillis
    ) {
      return "expired";
    }
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_MISSING")) return "unavailable";
    if (persisted === "current") return "current";
    return Object.keys(boundary).length || boundaryValue ? "current" : "unavailable";
  }

  function preciseExecutionReadyForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    return segmentDifferenceReadyForSignal(safeSignal, evaluatedAt)
      && profile.precise_execution_ready === true
      && (safeSignal.entry_allowed === true || safeSignal.exit_allowed === true);
  }

  function segmentDifferenceReadyForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const trigger = segmentDifferenceForSignal(safeSignal);
    const side = text(safeSignal.side || (trigger && trigger.side), "");
    const boundaryStatus = segmentDifferenceBoundaryStatusForSignal(
      safeSignal,
      evaluatedAt,
    );
    return ["buy", "sell"].includes(side)
      && segmentDifferenceEvidenceStatusForSignal(safeSignal) === "present"
      && (side === "buy" ? boundaryStatus === "current" : boundaryStatus === "not_applicable")
      && profile.segment_difference_ready === true;
  }

  function currentSegmentDifferenceReadyForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    if (!isCurrentSelectionSignal(safeSignal)) return false;
    return segmentDifferenceReadyForSignal(safeSignal, evaluatedAt)
      && segmentDifferenceEvidenceCurrentForReview(safeSignal, evaluatedAt);
  }

  function segmentDifferenceEvidenceCurrentForReview(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const trigger = segmentDifferenceForSignal(safeSignal);
    const side = text(safeSignal.side || (trigger && trigger.side), "");
    const boundaryStatus = segmentDifferenceBoundaryStatusForSignal(
      safeSignal,
      evaluatedAt,
    );
    if (segmentDifferenceEvidenceStatusForSignal(safeSignal) !== "present") {
      return false;
    }
    if (side === "buy") return boundaryStatus === "current";
    if (side !== "sell" || boundaryStatus !== "not_applicable") return false;
    return true;
  }

  function currentPreciseExecutionReadyForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    return currentSegmentDifferenceReadyForSignal(safeSignal, evaluatedAt)
      && profile.precise_execution_ready === true
      && (safeSignal.entry_allowed === true || safeSignal.exit_allowed === true);
  }

  function recommendationReasonCodes(signal, recommendation, profile) {
    const safeSignal = isRecord(signal) ? signal : {};
    return uniqueText([
      ...(isRecord(recommendation) ? recommendation.reason_codes || [] : []),
      ...(isRecord(profile) ? profile.hard_block_reason_codes || [] : []),
      ...(Array.isArray(safeSignal.warning_codes) ? safeSignal.warning_codes : []),
      ...(Array.isArray(safeSignal.decision_reasons) ? safeSignal.decision_reasons : []),
    ]);
  }

  function positionRecommendationForSignal(signal, evaluatedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const recommendation = isRecord(safeSignal.position_recommendation)
      ? safeSignal.position_recommendation
      : isRecord(profile.position_recommendation)
        ? profile.position_recommendation
        : {};
    const trigger = segmentDifferenceForSignal(safeSignal);
    const side = text(
      safeSignal.side || (trigger && trigger.side) || recommendation.side,
      "",
    );
    if (
      side !== "buy"
      || segmentDifferenceEvidenceStatusForSignal(safeSignal) !== "present"
      || segmentDifferenceBoundaryStatusForSignal(safeSignal, evaluatedAt) !== "expired"
    ) return recommendation;
    return {
      ...recommendation,
      side: "buy",
      status: "BLOCKED",
      basis: "NO_TRADE",
      recommended_ratio: "0",
      recommended_percent: "0",
      label: "结构风险参考：本条买入不纳入操作计划（1分钟区间套定位窗口已过）",
      reason_codes: uniqueText([
        ...(Array.isArray(recommendation.reason_codes) ? recommendation.reason_codes : []),
        "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
      ]),
      conditional_options: [],
    };
  }

  function blockedPositionReason(signal, recommendation, profile) {
    const safeSignal = isRecord(signal) ? signal : {};
    const reasons = new Set(
      recommendationReasonCodes(safeSignal, recommendation, profile),
    );
    const side = text(
      safeSignal.side || (isRecord(recommendation) && recommendation.side),
      "",
    );
    const invalidated = lifecycleStageForSignal(safeSignal) === "invalidated"
      || reasons.has("structure_invalidated")
      || reasons.has("STRUCTURE_INVALIDATED");
    if (invalidated) {
      return side === "sell"
        ? "本条卖点结构已失效：不再计算卖出比例，结束本结构跟踪"
        : "本条买点结构已失效：本条买入不纳入操作计划，等待新的5分钟结构";
    }
    if (reasons.has("BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR")) {
      return "本条买入不纳入操作计划：当前价已超过结构锚点的5%追价保护线，等待新的5分钟结构";
    }
    if (reasons.has("CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP")) {
      return "本条买入不纳入操作计划：当前价已触及或跌破5分钟结构防守位";
    }
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED")) {
      return "5分钟信号仍保留，但1分钟区间套定位窗口已过；等待新的1分钟区间套";
    }
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_MISSING")) {
      return "5分钟信号仍保留，但1分钟区间套精确执行边界不可用";
    }
    if (reasons.has("WARMUP_CONVERGENCE_GATE_FAILED")) {
      return "本条买入不纳入操作计划：5分钟完整历史与对照窗口的活动买卖点不一致，等待重新收敛";
    }
    if (reasons.has("QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH")) {
      return "本条买入不纳入操作计划：原生日线与交易日历覆盖不一致，等待数据校验通过";
    }
    if (reasons.has("QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH")) {
      return "本条买入不纳入操作计划：原生日线与1分钟派生日线的开高低收量不一致，等待数据校验通过";
    }
    if (reasons.has("HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED")) {
      return "本条买入不纳入操作计划：高周期同源行情完整性校验未通过";
    }
    if (
      reasons.has("same_or_higher_structure_conflict")
      || reasons.has("structure_conflict")
    ) {
      return "本条买入不纳入操作计划：同级或更高级别存在反向结构冲突";
    }
    if (reasons.has("three_buy_lacks_tick_clearance")) {
      return "本条买入不纳入操作计划：三买离开中枢的价格空间不足一个最小价位";
    }
    return side === "sell"
      ? "本条卖出信号当前不可执行：具体限制原因未完整保存，请查看诊断证据"
      : "本条买入不纳入操作计划：具体限制原因未完整保存，请查看诊断证据";
  }

  function blockedPositionTitle(signal, recommendation, profile) {
    const reasons = new Set(
      recommendationReasonCodes(signal, recommendation, profile),
    );
    if (
      lifecycleStageForSignal(signal) === "invalidated"
      || reasons.has("structure_invalidated")
      || reasons.has("STRUCTURE_INVALIDATED")
    ) return "结构已失效";
    if (reasons.has("BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR")) {
      return "当前价格触发追价保护";
    }
    if (reasons.has("CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP")) {
      return "当前价格已触及结构防守位";
    }
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED")) {
      return "1分钟定位窗口已过";
    }
    if (reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_MISSING")) {
      return "1分钟精确执行边界不可用";
    }
    if (isRecord(profile) && profile.hard_blocked === true) {
      return hardBlockSummaryForSignal(signal);
    }
    return text(signal && signal.side, "") === "sell"
      ? "本条卖出信号当前不可执行"
      : "本条买入不纳入操作计划";
  }

  function blockedPositionNextAction(signal, recommendation, profile) {
    const reasons = new Set(
      recommendationReasonCodes(signal, recommendation, profile),
    );
    if (
      lifecycleStageForSignal(signal) === "invalidated"
      || reasons.has("structure_invalidated")
      || reasons.has("STRUCTURE_INVALIDATED")
      || reasons.has("BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR")
      || reasons.has("CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP")
    ) {
      return "不追价、不执行本条买入，等待新的5分钟结构";
    }
    if (
      reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED")
      || reasons.has("ONE_MINUTE_SEGMENT_BOUNDARY_MISSING")
    ) {
      return "保留5分钟结构观察，等待新的有效1分钟区间套";
    }
    if (reasons.has("WARMUP_CONVERGENCE_GATE_FAILED")) {
      return "保持观察，等待5分钟暖机重新收敛后再评估买入";
    }
    if (
      reasons.has("HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED")
      || reasons.has("QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH")
      || reasons.has("QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH")
    ) {
      return "等待行情数据校验通过后再评估买入";
    }
    if (reasons.has("three_buy_lacks_tick_clearance")) {
      return "等待新的、满足最小价位间隔的三买结构";
    }
    return "保持观察，待限制原因解决后重新评估";
  }

  function positionRecommendationLabel(
    signal,
    fallback = "结构风险参考待人工核对",
    observedAt = new Date(),
  ) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const recommendation = positionRecommendationForSignal(safeSignal);
    const status = text(recommendation.status, "");
    const side = text(safeSignal.side || recommendation.side, "");
    const percent = text(recommendation.recommended_percent, "").trim();
    const reasons = new Set(
      recommendationReasonCodes(safeSignal, recommendation, profile),
    );
    if (status === "BLOCKED") {
      return blockedPositionReason(safeSignal, recommendation, profile);
    }
    if (
      status === "RECOMMENDED"
      && side === "buy"
      && percent
      && (
        recommendation.basis === "STRUCTURAL_RISK_MODEL_UPPER_BOUND"
        || recommendation.basis === "ACCOUNT_EQUITY_UPPER_BOUND"
        || reasons.has("STRUCTURAL_RISK_BUDGET_SIZED")
        || reasons.has("CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED")
      )
    ) {
      const priceBasis = reasons.has("CURRENT_PRICE_STRUCTURAL_RISK_BUDGET_SIZED")
        ? "按当前价至5分钟防守位测算"
        : "按5分钟结构锚点至防守位测算";
      return `结构风险参考比例：${percent}% 以内（${priceBasis}；仅作结构模型比较）`;
    }
    if (status === "RECOMMENDED" && side === "sell" && percent) {
      const relation = reasons.has("SAME_OR_HIGHER_STRUCTURE_FULL_EXIT")
        ? "按5分钟同级或更高级别卖点完整退出规则"
        : reasons.has("LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION")
          ? "按5分钟低级别或不同结构卖点段差规则"
          : "按已核对的5分钟结构级别规则";
      return `结构退出参考比例：${percent}%（${relation}；仅作结构模型比较）`;
    }
    if (status === "CONDITIONAL" && side === "sell") {
      return "结构退出参考：卖点与目标结构的级别关系待人工核对；同级或更高级别卖点按完整退出规则复核，低级别或不同结构仅作段差处理；关系未确认前不生成退出比例";
    }
    const label = text(recommendation.label, fallback);
    if (/(?:\u8d26\u6237|\u6743\u76ca|\u8d44\u91d1|\u73b0\u91d1|\u6301\u4ed3|\u4ed3\u4f4d|\u6301\u6709\u6570\u91cf|\u7ec4\u5408\u70ed\u5ea6)/.test(label)) {
      return side === "sell"
        ? "结构退出参考待人工核对"
        : "结构风险参考待人工核对";
    }
    return label;
  }

  function buyRiskMultiplierText(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    if (text(safeSignal.side, "") !== "buy") return null;
    const multiplier = Number(safeSignal.risk_multiplier);
    if (!Number.isFinite(multiplier) || multiplier <= 0) return null;
    return text(safeSignal.risk_multiplier, String(multiplier));
  }

  function selectedRiskReferenceLabel(signal) {
    const recommendation = positionRecommendationLabel(signal);
    const multiplier = buyRiskMultiplierText(signal);
    return multiplier === null
      ? recommendation
      : `${recommendation} · 买入风险缩放系数 ×${multiplier}`;
  }

  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function laterIsoTime(first, second) {
    const firstText = text(first, "");
    const secondText = text(second, "");
    const firstAt = Date.parse(firstText);
    const secondAt = Date.parse(secondText);
    if (Number.isFinite(firstAt) && Number.isFinite(secondAt)) {
      return secondAt > firstAt ? secondText : firstText;
    }
    if (Number.isFinite(firstAt)) return firstText;
    if (Number.isFinite(secondAt)) return secondText;
    return firstText || secondText || null;
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

  function emptySignalDetail(snapshot, query, filters = {}) {
    const generic = "这表示当前快照没有匹配项，不等于扫描成功率或未来收益判断。";
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const requestedSegmentState = text(filters.segmentState, "all");
    if (
      !text(query, "").trim()
      && ["present", "current"].includes(requestedSegmentState)
    ) {
      const source = Array.isArray(safeSnapshot.unified_signals)
        ? safeSnapshot.unified_signals
        : Array.isArray(safeSnapshot.signals) ? safeSnapshot.signals : [];
      const matchingSegments = source
        .filter(isCurrentSelectionSignal)
        .filter((signal) => requestedSegmentState === "present"
          ? segmentDifferenceEvidenceStatusForSignal(signal) === "present"
          : currentSegmentDifferenceReadyForSignal(signal));
      if (matchingSegments.length) {
        const buys = matchingSegments.filter((signal) => text(signal.side, "") === "buy").length;
        const sells = matchingSegments.filter((signal) => text(signal.side, "") === "sell").length;
        return requestedSegmentState === "current"
          ? `当前有 ${matchingSegments.length} 个5分钟操作候选已完成有效的1分钟区间套定位（买点 ${buys} / 卖点 ${sells}），但被其他筛选条件隐藏；点击“查看当前定位”可清除这些筛选。`
          : `当前5分钟操作候选中有 ${matchingSegments.length} 个保留1分钟区间套证据（买点 ${buys} / 卖点 ${sells}），但被其他筛选条件隐藏。`;
      }
      return requestedSegmentState === "present"
        ? "当前5分钟操作候选中没有1分钟区间套证据；5分钟主信号仍保留，但尚未完成精确定位。"
        : "当前5分钟操作候选中没有已完成且仍有效的1分钟区间套精确定位；历史定位不会计入当前结果。";
    }
    const code = exactCoverageCodeForQuery(snapshot, query);
    if (code === null) return generic;
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
      return `${code} 来自人工关注、自选或持续信号监控，但QMT本轮未能解析其原生品种类型；系统已失败关闭，不会把未知品种加入交易结构线索队列。`;
    }
    if (
      monitorExclusion
      && monitorExclusion.reason_code === "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
    ) {
      const instrumentType = text(monitorExclusion.qmt_instrument_type, "非股票/ETF");
      return `${code} 来自人工关注、自选或持续信号监控，但QMT原生品种类型为 ${instrumentType}，不是可交易A股股票或场内ETF；它不会进入交易结构线索队列。`;
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

  function screeningScopeFacts(runtimeHealth, snapshot = null) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const scope = isRecord(safeSnapshot.screening_scope)
      ? safeSnapshot.screening_scope
      : {};
    const mode = text(health.screening_scope_mode || scope.mode, "UNKNOWN");
    const cohort = Math.max(
      0,
      Number(health.validation_cohort_size || scope.validation_cohort_size) || 0,
    );
    const effectiveLimit = Math.max(
      0,
      Number(
        health.effective_monitor_universe_limit
        || scope.effective_monitor_universe_limit,
      ) || 0,
    );
    return {
      mode,
      cohort,
      effectiveLimit,
      validation: mode === "VALIDATION_COHORT",
    };
  }

  function screeningScopeLabel(runtimeHealth, snapshot = null) {
    const scope = screeningScopeFacts(runtimeHealth, snapshot);
    if (scope.validation) {
      return `${scope.cohort || scope.effectiveLimit || 12}只小样本验证`;
    }
    if (scope.mode === "FULL_MARKET") return "显式全市场运行";
    if (scope.mode === "LARGE_SCOPE") {
      return `显式大范围运行 · 上限 ${scope.effectiveLimit || "待定"} 只`;
    }
    return "运行范围待确认";
  }

  function scanCoverageText(audit, snapshot = null, runtimeHealth = null) {
    const safeAudit = isRecord(audit) ? audit : {};
    const warmupSensitive = Math.max(
      0,
      Number(safeAudit.warmup_sensitive_symbol_count) || 0,
    );
    const tradeLevelUnconverged = Math.max(
      0,
      Number(safeAudit.trade_level_warmup_unconverged_symbol_count) || 0,
    );
    const tradeLevelFailClosed = Math.max(
      0,
      Number(safeAudit.trade_level_warmup_fail_closed_symbol_count) || 0,
    );
    const contextOnlySensitive = Math.max(
      0,
      Number(safeAudit.warmup_context_only_sensitive_symbol_count) || 0,
    );
    const warmupParts = [];
    if (warmupSensitive > 0) {
      warmupParts.push(`历史边界敏感 ${warmupSensitive}只`);
      if (tradeLevelUnconverged > 0) {
        warmupParts.push(
          tradeLevelFailClosed === tradeLevelUnconverged
            ? `5m未收敛 ${tradeLevelUnconverged}只，已失败关闭`
            : `5m未收敛 ${tradeLevelUnconverged}只，其中 ${tradeLevelFailClosed}只已失败关闭`,
        );
      }
      if (contextOnlySensitive > 0) {
        warmupParts.push(`上下文/1m差异 ${contextOnlySensitive}只`);
      }
    }
    const decisionOutcomeCounts = isRecord(
      safeAudit.stock_decision_outcome_counts,
    ) ? safeAudit.stock_decision_outcome_counts : {};
    const emittedFiveMinute = Math.max(
      0,
      Number(decisionOutcomeCounts.CURRENT_5M_STRUCTURAL_SIGNAL_EMITTED) || 0,
    );
    const noCurrentFiveMinutePoint = Math.max(
      0,
      Number(decisionOutcomeCounts.NO_CURRENT_5M_STRUCTURAL_POINT) || 0,
    );
    const noCurrentExecutableFiveMinutePoint = Math.max(
      0,
      Number(
        decisionOutcomeCounts.NO_CURRENT_EXECUTABLE_5M_STRUCTURAL_POINT,
      ) || 0,
    );
    const decisionOutcomeParts = [];
    if (
      emittedFiveMinute
      || noCurrentFiveMinutePoint
      || noCurrentExecutableFiveMinutePoint
    ) {
      decisionOutcomeParts.push(`当前5m严格信号 ${emittedFiveMinute}只`);
      if (noCurrentFiveMinutePoint) {
        decisionOutcomeParts.push(`无当前5m严格点 ${noCurrentFiveMinutePoint}只`);
      }
      if (noCurrentExecutableFiveMinutePoint) {
        decisionOutcomeParts.push(
          `有5m点但当前不可执行 ${noCurrentExecutableFiveMinutePoint}只`,
        );
      }
    }
    if (screeningScopeFacts(runtimeHealth, snapshot).validation) {
      return [
        `${screeningScopeLabel(runtimeHealth, snapshot)} · 当前验证范围固定`,
        ...decisionOutcomeParts,
        ...warmupParts,
      ].join(" · ");
    }
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
      if (decisionOutcomeParts.length) parts.push(...decisionOutcomeParts);
      const withoutSignal = decisionOutcomeParts.length
        ? null
        : completedWithoutSignalCount(snapshot);
      if (withoutSignal !== null && withoutSignal > 0) {
        parts.push(`已分析无当前结构信号 ${withoutSignal}`);
      }
      parts.push(...warmupParts);
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

  function scanQualityText(snapshot, runtimeHealth = null) {
    const safeSnapshot = isRecord(snapshot) ? snapshot : {};
    const audit = isRecord(safeSnapshot.scan_audit) ? safeSnapshot.scan_audit : {};
    const quality = isRecord(safeSnapshot.data_quality) ? safeSnapshot.data_quality : {};
    if (screeningScopeFacts(runtimeHealth, safeSnapshot).validation) {
      if (safeSnapshot.available !== true) return "小样本等待首次结果";
      if (quality.stale === true) return "小样本结果已过期";
      return quality.complete === true ? "小样本结果完整" : "小样本结果待复核";
    }
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
    if (diagnostics.schema !== "chanlun-sector-member-history-diagnostics") {
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
    const rawResolutionRatio = safeAudit.sector_resolution_ratio;
    const rawCompletionRatio = safeAudit.sector_completion_ratio;
    const providedResolutionRatio = (
      rawResolutionRatio === null
      || rawResolutionRatio === undefined
      || rawResolutionRatio === ""
    ) ? Number.NaN : Number(rawResolutionRatio);
    const providedCompletionRatio = (
      rawCompletionRatio === null
      || rawCompletionRatio === undefined
      || rawCompletionRatio === ""
    ) ? Number.NaN : Number(rawCompletionRatio);
    const resolved = Math.min(discovered, completed + excluded);
    const providedRatio = Number.isFinite(providedResolutionRatio)
      ? providedResolutionRatio
      : providedCompletionRatio;
    const ratio = Number.isFinite(providedRatio)
      ? Math.min(1, Math.max(0, providedRatio))
      : discovered > 0 ? Math.min(1, resolved / discovered) : 0;
    const percentage = new Intl.NumberFormat("zh-CN", {
      style: "percent",
      maximumFractionDigits: 1,
    }).format(ratio);
    const exclusionText = hasExcluded ? ` · 资格排除 ${excluded}` : "";
    return `发现 ${discovered} · 完成 ${completed}${exclusionText} · 失败 ${failed} · 解析完成率 ${percentage}`;
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

  function fullDateTimeText(value) {
    if (!value) return "暂不可用";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value, "暂不可用");
    const values = Object.fromEntries(
      new Intl.DateTimeFormat("zh-CN-u-hc-h23", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).formatToParts(parsed).map((part) => [part.type, part.value]),
    );
    return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
  }

  function realtimeNotificationDisplayTime(value) {
    const safe = isRecord(value) ? value : {};
    return safe.notification_delivered_at
      || safe.delivered_at
      || safe.notification_delivery_updated_at
      || safe.delivery_updated_at
      || safe.notification_detected_at
      || safe.detected_at
      || safe.notification_recorded_at
      || safe.recorded_at
      || safe.notification_signal_available_at
      || safe.signal_available_at
      || safe.notification_signal_time
      || safe.signal_time
      || safe.observed_at;
  }

  function realtimeNotificationTimeLabel(value) {
    const safe = isRecord(value) ? value : {};
    const status = safe.notification_delivery_status || safe.delivery_status;
    if (status === "delivered") return "送达时间";
    if (status === "simulated") return "演练记录时间";
    if (status === "failed") return "投递更新时间";
    if (status === "expired") return "过期记录时间";
    return "通知记录时间";
  }

  function realtimeNotificationPriceText(value) {
    const safe = isRecord(value) ? value : {};
    const price = text(
      safe.notification_current_price ?? safe.current_price,
      "暂不可用",
    );
    const source = text(
      safe.notification_current_price_source ?? safe.current_price_source,
      "",
    );
    const observedAt = safe.notification_current_price_at
      || safe.current_price_at;
    if (source === "realtime_tick" && observedAt) {
      return `通知时当前价 ${price}（获取 ${fullDateTimeText(observedAt)}）`;
    }
    const sourceLabel = {
      latest_completed_1m_close: "最近1分钟收盘价",
      latest_completed_5m_close: "最近5分钟收盘价",
      latest_completed_bar_close: "最近已完成K线收盘价",
    }[source];
    return sourceLabel
      ? `${sourceLabel} ${price}`
      : `通知记录价 ${price}`;
  }

  function dailyPreselectionText(runtimeHealth) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const scope = screeningScopeFacts(health);
    const scopeLabel = screeningScopeLabel(health);
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
    const sells = Math.max(
      0,
      Number(health.daily_preselection_sell_candidate_count) || 0,
    );
    if (scope.validation) {
      if (health.daily_preselection_ready === true) {
        return `${scopeLabel} · 当前验证名单已就绪`;
      }
      if (status === "target_session_stale") {
        if (
          reason === "PRESELECTION_CLOSE_CUTOFF_INCOMPLETE"
          && health.snapshot_available === true
        ) {
          return `${scopeLabel} · 盘中快照可用，15:05 后更新收盘候选`;
        }
        return `${scopeLabel} · 等待当前验证名单更新`;
      }
      if (["coverage_in_progress", "awaiting_first_snapshot"].includes(status)) {
        return `${scopeLabel} · 正在准备当前小样本`;
      }
      if (status === "review_blocked") {
        return reason === "HUMAN_REVIEW_MATERIALIZATION_FAILED"
          ? `${scopeLabel} · 复核材料待重建`
          : `${scopeLabel} · 名单待人工复核`;
      }
      return `${scopeLabel} · 尚未就绪，详情见运行诊断`;
    }
    if (health.daily_preselection_ready === true) {
      return `已就绪 · 适用 ${target} · 买点 ${buys} / 卖点 ${sells} / 全部 ${candidates}`;
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
    const scope = screeningScopeFacts(health);
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
    const sells = Math.max(
      0,
      Number(health.daily_preselection_sell_candidate_count) || 0,
    );
    const parts = [
      ...(scope.validation ? [screeningScopeLabel(health)] : []),
      `内部状态 ${statusLabel(status)}`,
      `原因 ${reasonLabel(reason)}`,
      ...(
        scope.validation
          ? []
          : [`结构线索 ${candidates}（买点 ${buys} / 卖点 ${sells}）`]
      ),
    ];
    if (health.daily_preselection_target_session) {
      parts.push(`适用 ${text(health.daily_preselection_target_session)}`);
    }
    if (health.daily_preselection_market_data_as_of) {
      parts.push(`数据截止 ${timeText(health.daily_preselection_market_data_as_of)}`);
    }
    if (
      scope.validation
      && health.validation_snapshot_priority_only === true
    ) {
      parts.push("盘中调度 仅运行5分钟候选与按需1分钟定位，归档扫描等待15:05");
    }
    return parts.join(" · ");
  }

  function notificationDeliveryText(runtimeHealth, auxiliaryMonitor) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const primary = health.notification_operationally_verified === true
      ? "通知送达已验证"
      : health.notification_dispatcher_configured === true
        ? "通知已配置，送达尚未验证"
        : "仅页面提醒";
    const auxiliary = isRecord(auxiliaryMonitor) ? auxiliaryMonitor : {};
    const auxiliaryDelivery = isRecord(auxiliary.notification_delivery)
      ? auxiliary.notification_delivery
      : {};
    if (
      auxiliary.available !== true
      && auxiliary.notification_configured !== true
      && Object.keys(auxiliaryDelivery).length === 0
    ) return primary;
    const delivered = Math.max(
      0,
      Number(auxiliaryDelivery.delivered_event_count) || 0,
    );
    const auxiliaryText = auxiliaryDelivery.operationally_verified === true
      ? `跨市场通知送达已验证${delivered ? `（${delivered} 条）` : ""}`
      : ["degraded", "unavailable"].includes(
        text(auxiliaryDelivery.status, ""),
      )
        ? `跨市场通知异常（${reasonLabel(text(auxiliaryDelivery.reason_code, "NOTIFICATION_HEALTH_UNAVAILABLE"))}）`
        : auxiliary.notification_configured === true
          ? "跨市场通知已配置，等待首个事件"
          : "跨市场仅页面提醒";
    return `${primary} · ${auxiliaryText}`;
  }

  function priorityMonitorText(runtimeHealth, liveOverlay, auxiliaryMonitor) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const overlay = isRecord(liveOverlay) ? liveOverlay : {};
    const priorityStatus = text(health.priority_monitor_status, "unavailable");
    const candidateStatus = text(health.candidate_monitor_status, "unavailable");
    const alertStatus = text(
      health.realtime_alert_status,
      priorityStatus === "not_due" ? "not_due" : "unavailable",
    );
    const candidateLane = isRecord(health.candidate_monitor_five_minute)
      ? health.candidate_monitor_five_minute
      : {};
    const candidateUniverse = Math.max(0, Number(candidateLane.universe_count) || 0);
    const candidateCurrent = Math.max(0, Number(candidateLane.current_count) || 0);
    const sectorSourceMode = text(health.priority_monitor_sector_source_mode, "");
    const preselectionContinuityActive = health.preselection_continuity_active === true;
    const sectorScopePreparing = [
      "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED",
      "UNCLASSIFIED_SECTOR_FAIL_CLOSED",
    ].includes(sectorSourceMode);
    const liveSignalCount = Math.max(0, Number(overlay.signal_count) || 0);
    // priority_live_overlay.signal_count 是当前进入 1m 精确定位通道的
    // 5m 结构数量，不是本轮新增买卖点或新增通知数量。
    const signalText = liveSignalCount
      ? `1分钟通道跟踪 ${liveSignalCount} 条5m结构`
      : "1分钟通道暂无结构跟踪";
    const deliveryText = notificationDeliveryText(health, auxiliaryMonitor);
    if (
      alertStatus === "ready_idle"
      || candidateStatus === "idle_no_candidates"
    ) {
      return `就绪但当前空闲 · 当前没有符合板块门控、已有5分钟信号或人工关注范围的监听对象 · 5分钟候选 ${candidateCurrent}/${candidateUniverse} 只 · 1分钟定位不会提前启动 · ${deliveryText}`;
    }
    if (
      alertStatus === "ready"
      && priorityStatus === "verified"
      && candidateStatus === "verified"
    ) {
      const readinessText = preselectionContinuityActive
        ? "优先通道正常 · 使用上一交易日已认证预选范围过渡"
        : sectorScopePreparing
          ? "优先通道正常 · 支持板块范围准备中"
          : "正常";
      const scopeText = preselectionContinuityActive
        ? "范围：已认证预选候选/已有信号，全部按当前规则实时重算"
        : sectorScopePreparing
          ? "当前已核验：人工关注/自选/已有信号"
          : "范围：人工关注/自选/已有信号/支持板块";
      return `${readinessText} · ${scopeText} · 5分钟候选 ${candidateCurrent}/${candidateUniverse} 只 · ${signalText} · ${deliveryText}`;
    }
    if (
      priorityStatus === "verified"
      && !["verified", "not_due", "disabled"].includes(candidateStatus)
    ) {
      const candidateText = `5分钟候选${statusLabel(candidateStatus)}`;
      const scopeText = preselectionContinuityActive
        ? "当前按新规则轮转复查上一交易日已认证预选范围"
        : sectorScopePreparing
          ? "当前已核验：人工关注/自选/新鲜已有信号 · 支持板块范围准备中"
          : "当前优先复查：人工关注/自选/新鲜已有信号";
      return `优先通道正常 · ${candidateText} · ${scopeText} · 5分钟候选 ${candidateCurrent}/${candidateUniverse} 只 · ${signalText} · ${deliveryText}`;
    }
    if (alertStatus === "not_due" || priorityStatus === "not_due") {
      return `非交易时段 · 开盘后盯盘范围：人工关注/自选/已有信号/支持板块候选 · ${deliveryText}`;
    }
    if (priorityStatus === "disabled") return "未启用";
    const alertReason = reasonLabel(
      text(health.realtime_alert_reason_code, "PRIORITY_MONITOR_DEGRADED"),
    );
    const scopeText = preselectionContinuityActive
      ? "上一交易日已认证预选范围已接入，当前规则复查尚未全部完成"
      : sectorScopePreparing
        ? "当前可核验：人工关注/自选/已有信号 · 支持板块范围准备中"
        : "范围：人工关注/自选/已有信号/支持板块";
    return `时效保障未就绪 · ${alertReason} · ${scopeText} · 5分钟候选 ${candidateCurrent}/${candidateUniverse} 只 · ${deliveryText}`;
  }

  function priorityMonitorDiagnosticsText(runtimeHealth, liveOverlay, auxiliaryMonitor) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const overlay = isRecord(liveOverlay) ? liveOverlay : {};
    const scope = screeningScopeFacts(health);
    const priorityStatus = text(health.priority_monitor_status, "unavailable");
    const priorityReasonCodes = Array.isArray(health.priority_monitor_reason_codes)
      ? health.priority_monitor_reason_codes
      : [];
    const priorityReasons = priorityReasonCodes.length
      ? priorityReasonCodes.map(reasonLabel).join(" / ")
      : priorityStatus === "verified"
        ? "本轮即时复查已完成"
        : priorityStatus === "not_due"
          ? reasonLabel("NON_TRADING_SESSION_NOT_DUE")
          : priorityStatus === "disabled"
            ? "即时复查未启用"
            : reasonLabel("PRIORITY_MONITOR_UNAVAILABLE");
    const candidateStatus = text(health.candidate_monitor_status, "unavailable");
    const candidateReasons = Array.isArray(health.candidate_monitor_reason_codes)
      && health.candidate_monitor_reason_codes.length
      ? health.candidate_monitor_reason_codes.map(reasonLabel).join(" / ")
      : candidateStatus === "verified"
        ? "节奏覆盖已验证"
        : candidateStatus === "not_due"
          ? reasonLabel("NON_TRADING_SESSION_NOT_DUE")
          : candidateStatus === "disabled"
            ? "5分钟候选轮换未启用"
            : "状态原因未提供";
    const priorityCount = Math.max(
      0,
      Number(health.priority_monitor_last_code_count) || 0,
    );
    const candidateLane = isRecord(health.candidate_monitor_five_minute)
      ? health.candidate_monitor_five_minute
      : {};
    const candidateUniverse = Math.max(0, Number(candidateLane.universe_count) || 0);
    const candidateCurrent = Math.max(0, Number(candidateLane.current_count) || 0);
    const candidateMissing = Math.max(0, Number(candidateLane.missing_count) || 0);
    const candidateOverdue = Math.max(0, Number(candidateLane.overdue_count) || 0);
    const candidateTarget = Math.max(0, Number(candidateLane.target_seconds) || 0);
    const freshSegmentCount = Math.max(
      0,
      Number(health.priority_monitor_immediate_universe_count) || 0,
    );
    const liveSignalCount = Math.max(0, Number(overlay.signal_count) || 0);
    const delivery = isRecord(health.notification_delivery)
      ? health.notification_delivery
      : {};
    const candidateCadenceText = candidateStatus === "not_due"
      ? `5分钟候选轮换 ${statusLabel(candidateStatus)}（${candidateReasons}）· 非交易时段不计算当前缺失与逾期`
      : `5分钟候选轮换 ${statusLabel(candidateStatus)}（${candidateReasons}）· 当前 ${candidateCurrent}/${candidateUniverse} 只 · 缺失 ${candidateMissing} · 逾期 ${candidateOverdue}${candidateTarget ? ` · 目标 ${Math.round(candidateTarget / 60)} 分钟` : ""}`;
    const parts = [
      `A股实时预警 ${statusLabel(text(health.realtime_alert_status, "unavailable"))}（${reasonLabel(text(health.realtime_alert_reason_code, "PRIORITY_MONITOR_UNAVAILABLE"))}）`,
      `即时复查 ${statusLabel(priorityStatus)}（${priorityReasons}）· 最近 ${priorityCount} 只`,
      candidateCadenceText,
      `1分钟精确定位队列 待定位的当前5分钟候选 ${freshSegmentCount} 只 · 持续轮转直至结构被替换`,
      scope.validation
        ? `实时预警范围：仅处理当前 ${scope.cohort || scope.effectiveLimit || 12} 只固定小样本`
        : "实时预警范围：人工关注、自选、已有信号和当前支持板块候选；全市场覆盖用于选股归档，不承诺每只股票5分钟实时预警",
      `1分钟通道当前结构 ${liveSignalCount} 条（非新增通知计数）`,
      health.notification_operationally_verified === true
        ? `A股通知送达 已验证（${reasonLabel(text(delivery.reason_code, "DELIVERY_SUCCESS_PROVEN"))}）`
        : health.notification_dispatcher_configured === true
          ? `A股通知已配置但送达未验证（${reasonLabel(text(delivery.reason_code, "REALTIME_NOTIFICATION_DELIVERY_UNVERIFIED"))}）`
          : "A股仅页面提醒，未配置主动通知",
    ];
    const auxiliary = isRecord(auxiliaryMonitor) ? auxiliaryMonitor : {};
    const auxiliaryDelivery = isRecord(auxiliary.notification_delivery)
      ? auxiliary.notification_delivery
      : {};
    if (
      auxiliary.available === true
      || auxiliary.notification_configured === true
      || Object.keys(auxiliaryDelivery).length > 0
    ) {
      const delivered = Math.max(
        0,
        Number(auxiliaryDelivery.delivered_event_count) || 0,
      );
      parts.push(
        auxiliaryDelivery.operationally_verified === true
          ? `跨市场通知送达 已验证（${delivered} 条；${reasonLabel(text(auxiliaryDelivery.reason_code, "DELIVERY_SUCCESS_PROVEN"))}）`
          : auxiliary.notification_configured === true
            ? `跨市场通知已配置但送达未验证（${reasonLabel(text(auxiliaryDelivery.reason_code, "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"))}）`
            : "跨市场仅页面提醒，未配置主动通知",
      );
    }
    if (health.priority_monitor_last_at) {
      parts.push(`最近运行 ${timeText(health.priority_monitor_last_at)}`);
    }
    return parts.join(" · ");
  }

  function segmentScopeText(runtimeHealth, segmentDifferenceCount) {
    const health = isRecord(runtimeHealth) ? runtimeHealth : {};
    const witnessCount = Math.max(0, Number(segmentDifferenceCount) || 0);
    const freshCount = Math.max(
      0,
      Number(health.priority_monitor_immediate_universe_count) || 0,
    );
    const parts = [
      witnessCount > 0
        ? `当前 ${witnessCount} 个5分钟操作候选已完成1分钟区间套段差见证`
        : "当前没有5分钟操作候选完成1分钟区间套精确定位",
    ];
    if (freshCount > 0) {
      parts.push(`另有 ${freshCount} 只当前5分钟候选正在等待1分钟定位`);
    }
    parts.push("5分钟决定交易级别，1分钟只确定精确买卖位置");
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
    const unexplainedMember = value.match(/^UNEXPLAINED_MEMBER_HISTORY:(.+)$/);
    if (unexplainedMember) {
      return `${unexplainedMember[1]} 的板块成员历史存在未解释缺口`;
    }
    const memberCoverage = value.match(
      /^catalog_members=(\d+); universe_members=(\d+); required=(\d+)$/,
    );
    if (memberCoverage) {
      return `目录成分 ${memberCoverage[1]} 个、候选范围成分 ${memberCoverage[2]} 个，最低要求 ${memberCoverage[3]} 个`;
    }
    if (/^sha256:[0-9a-f]{64}$/i.test(value)) {
      return `研究参数版本 ${value.slice(0, 19)}…`;
    }
    const comparisonWidth = value.match(/^comparison_leg_width_(\d+)$/);
    if (comparisonWidth) return `力度比较采用跨 ${comparisonWidth[1]} 个结构段口径`;
    return REASON_LABELS[value] || STATUS_LABELS[value] || `诊断代码：${value}`;
  }

  function hardBlockSummaryForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const reasons = new Set(uniqueText(profile.hard_block_reason_codes));
    if (reasons.has("HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED")) {
      return "行情数据完整性未通过";
    }
    if (
      reasons.has("structure_conflict")
      || reasons.has("same_or_higher_structure_conflict")
    ) {
      return "同级或更高级反向结构冲突";
    }
    if (reasons.has("WARMUP_CONVERGENCE_GATE_FAILED")) {
      return "5分钟结构暖机尚未收敛";
    }
    if (reasons.has("three_buy_lacks_tick_clearance")) {
      return "三买离开中枢的价格空间不足";
    }
    if (reasons.has("structure_invalidated")) return "结构已失效";
    return "关键操作条件未通过";
  }

  function hardBlockDetailForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : {};
    const umbrellaReasons = new Set([
      "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
      "structure_conflict",
      "WARMUP_CONVERGENCE_GATE_FAILED",
    ]);
    const details = uniqueText(profile.hard_block_reason_codes)
      .filter((reason) => !umbrellaReasons.has(reason))
      .map(reasonLabel)
      .slice(0, 2);
    return details.length
      ? `阻断依据：${details.join("；")}`
      : hardBlockSummaryForSignal(safeSignal);
  }

  function warmupDisplayState(signal) {
    const warmup = isRecord(signal && signal.warmup) ? signal.warmup : {};
    const rows = Array.isArray(warmup.by_frequency)
      ? warmup.by_frequency.filter(isRecord)
      : [];
    const fiveMinute = rows.find((row) => row.frequency === "5m");
    return {
      overallConverged: warmup.converged === true,
      fiveMinuteConverged: fiveMinute
        ? fiveMinute.converged === true
        : typeof warmup.converged === "boolean"
          ? warmup.converged
          : null,
    };
  }

  function dailyHigherTimeframeReasonCodes(values) {
    return uniqueText(values).filter((value) => (
      !value.startsWith("M_")
      && !value.startsWith("W_")
      && !value.includes("MONTHLY")
      && !value.includes("WEEKLY")
    ));
  }

  function prefixedLabels(prefix, values) {
    return uniqueText(values).map((value) => `${prefix}：${reasonLabel(value)}`);
  }

  function defaultFrequencyForSignal(signal) {
    // 5分钟是统一买卖级别；1分钟仅在用户主动查看区间套精确定位时切换。
    // 生命周期推进不能再把主图悄悄跳到1分钟。
    void signal;
    return "5m";
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
    if (
      sources.includes("ACTIVE_WATCHLIST_MONITOR")
      || sources.includes("WATCHLIST_MONITOR")
    ) labels.push("自选监控");
    if (
      sources.includes("MANUAL_ATTENTION_MONITOR")
      || sources.includes("HOLDING_MONITOR")
      || sources.includes("VIRTUAL_HOLDING_MONITOR")
    ) labels.push("人工关注组监控");
    if (sources.includes("PREVIOUS_SIGNAL_MONITOR")) labels.push("持续信号监控");
    if (sources.includes("INCREMENTAL_SCAN_SCOPE")) labels.push("增量监控");
    if (sources.includes("DECISION_RULE_RECHECK")) labels.push("规则变更重检");
    if (safeSignal.realtime_notification === true || sources.includes("REALTIME_NOTIFICATION")) {
      labels.push("实时通知");
    }
    if (sources.includes("US_AUXILIARY_MONITOR")) labels.push("美股监听");
    return labels.length ? labels.join(" + ") : "来源待确认";
  }

  function decisionSummaryForSignal(signal, observedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const stage = lifecycleStageForSignal(safeSignal);
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const profile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : null;
    const positionRecommendation = positionRecommendationForSignal(safeSignal);
    const positionBlocked = positionRecommendation.status === "BLOCKED";
    const waitingSegmentDifference = positionRecommendation.basis
      === "ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED";
    const recommendation = text(profile && profile.recommendation, "");
    const pointLabel = POINT_LABELS[safeSignal.point_type] || "买卖点";
    const setupLockState = setupLockStateForSignal(safeSignal);
    const allowed = !positionBlocked && (recommendation
      ? recommendation === "READY"
      : safeSignal.entry_allowed === true || safeSignal.exit_allowed === true);
    let tone = "neutral";
    let title = "继续观察";
    if (!stage) {
      tone = "unknown";
      title = "数据未知";
    } else if (stage === "invalidated") {
      tone = "blocked";
      title = "结构已失效";
    } else if (stage === "closed") {
      tone = "blocked";
      title = "跟踪已关闭";
    } else if (stage === "formed") {
      tone = recommendation === "BLOCKED" ? "blocked" : "waiting";
      title = `5分钟${pointLabel}几何候选，尚未达到操作确认`;
    } else if (stage === "approaching") {
      tone = recommendation === "BLOCKED" ? "blocked" : "waiting";
      title = `5分钟${pointLabel}结构仍在形成`;
    } else if (
      recommendation === "WAITING_SEGMENT_DIFFERENCE"
      || waitingSegmentDifference
    ) {
      tone = "waiting";
      title = `5分钟${pointLabel}已确认，等待1分钟区间套精确定位`;
    } else if (positionBlocked) {
      tone = "blocked";
      title = blockedPositionTitle(
        safeSignal,
        positionRecommendation,
        profile,
      );
    } else if (recommendation === "BLOCKED") {
      tone = "blocked";
      title = hardBlockSummaryForSignal(safeSignal);
    } else if (recommendation === "CAUTION") {
      tone = "waiting";
      title = setupLockState === "pending"
        ? `5分钟${pointLabel}已达到操作确认，需人工复核环境`
        : setupLockState === "locked"
          ? `5分钟${pointLabel}操作确认，末端结构已封存，谨慎人工复核`
          : `5分钟${pointLabel}已达到操作确认，结构证据状态待核对`;
    } else if (recommendation === "READY") {
      tone = "action";
      title = setupLockState === "pending"
        ? `5分钟${pointLabel}已达到操作确认，待人工复核`
        : setupLockState === "locked"
          ? `5分钟${pointLabel}操作确认，末端结构已封存，待人工复核`
          : `5分钟${pointLabel}已达到操作确认，结构证据状态待核对`;
    } else if (recommendation === "WAITING_STRUCTURE") {
      tone = "waiting";
      title = `5分钟${pointLabel}结构仍在形成`;
    } else if (
      recommendation === "GEOMETRY_AWAITING_CONFIRMATION"
      || recommendation === "FORMED_AWAITING_LOCK"
    ) {
      tone = "waiting";
      title = `5分钟${pointLabel}仅为几何候选，尚未达到操作确认`;
    } else if (allowed || stage === "executable") {
      tone = "action";
      title = "强提示待人工复核";
    } else if (stage === "triggered") {
      tone = "waiting";
      title = "5分钟操作确认，等待人工复核";
    } else if (stage === "armed") {
      tone = "waiting";
      title = "旧版等待态，下一次计算将迁移";
    } else if (stage === "active") {
      tone = "action";
      title = "结构持续跟踪";
    } else if (stage === "monitoring") {
      tone = "waiting";
      title = "实时监听，等待结构事件";
    }
    const reasons = Array.isArray(safeSignal.decision_reasons)
      ? safeSignal.decision_reasons.filter(Boolean)
      : [];
    const baseDetail = positionBlocked
      ? positionRecommendationLabel(safeSignal, "结构风险参考待人工核对", observedAt)
      : recommendation === "BLOCKED"
      ? hardBlockDetailForSignal(safeSignal)
      : profile
      ? text(profile.recommendation_label, reasons[0] ? reasonLabel(reasons[0]) : "等待剩余结构条件")
      : allowed ? "程序条件已形成强提示，仍须人工识别" : (reasons[0] ? reasonLabel(reasons[0]) : "等待剩余结构条件");
    const siblingSummary = siblingStructureContextSummary(safeSignal);
    return {
      tone,
      title,
      detail: siblingSummary ? `${baseDetail}；${siblingSummary}` : baseDetail,
      invalidation: text(setup.invalidation_price, "未提供"),
      structuralStop: text(setup.invalidation_price ?? safeSignal.structural_stop, "未提供"),
      riskMultiplier: text(safeSignal.risk_multiplier, "未提供"),
      positionRecommendation: positionRecommendationLabel(
        safeSignal,
        "结构风险参考待人工核对",
        observedAt,
      ),
    };
  }

  function periodPathForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const daily = isRecord(safeSignal.context_d) ? safeSignal.context_d : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const trigger = segmentDifferenceForSignal(safeSignal);
    const segmentEvidenceStatus = segmentDifferenceEvidenceStatusForSignal(safeSignal);
    const segmentBoundaryStatus = segmentDifferenceBoundaryStatusForSignal(safeSignal);
    const segmentDifferenceEvidenceCurrent = segmentDifferenceEvidenceCurrentForReview(
      safeSignal,
    );
    const setupKnown = Object.keys(setup).length > 0;
    const triggerKnown = trigger !== null && Object.keys(trigger).length > 0;
    const executionProfile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : null;

    if (safeSignal.synthetic_notification_projection === true) {
      const stage = lifecycleStageForSignal(safeSignal);
      const closed = ["invalidated", "closed"].includes(stage);
      const sourceFrequency = text(
        safeSignal.notification_source_frequency,
        text(setup.source_frequency, "未知周期"),
      );
      const recordedSegmentPoint = POINT_LABELS[
        safeSignal.notification_segment_difference_point_type
      ] || "1分钟结构点";
      const recordedSegmentEvidence = segmentPointEvidenceLabel(
        trigger,
        recordedSegmentPoint,
      );
      const segmentValidUntil = fullDateTimeText(
        safeSignal.notification_segment_difference_valid_until,
      );
      const point = POINT_LABELS[safeSignal.point_type]
        || (safeSignal.side === "sell" ? "退出结构" : "结构提示");
      const notificationEvidence = [
        `实时通知已留存 · ${realtimeNotificationTimeLabel(safeSignal)} ${fullDateTimeText(realtimeNotificationDisplayTime(safeSignal))} · ${realtimeNotificationPriceText(safeSignal)}`,
        `结构锚点 ${fullDateTimeText(safeSignal.notification_structure_anchor_time)} · 操作确认 ${fullDateTimeText(safeSignal.notification_structure_confirmed_at)} · 信号可用 ${fullDateTimeText(safeSignal.notification_signal_available_at)}`,
        `监听发现 ${fullDateTimeText(safeSignal.notification_detected_at)} · 递归层级 L${Number(safeSignal.recursive_level) || 0}`,
      ];
      return [
        {
          frequency: "d",
          state: "待人工核对",
          tone: "unknown",
          summary: "实时通知未携带可据此下结论的日线环境",
          boundary: "请在当前日线图核对",
          evidence: notificationEvidence,
        },
        {
          frequency: "30m",
          state: "待人工核对",
          tone: "neutral",
          summary: `通知记录的大级别方向 ${DIRECTION_LABELS[context.direction] || "未知"}`,
          boundary: "当前图表会继续更新，不是历史因果锁",
          evidence: notificationEvidence,
        },
        {
          frequency: "5m",
          state: sourceFrequency === "5m" ? (closed ? "已失效" : "结构通知已记录") : "待人工核对",
          tone: closed ? "blocked" : sourceFrequency === "5m" ? "supportive" : "unknown",
          summary: sourceFrequency === "5m"
            ? `${point} · 来源 5m 实时结构通知`
            : `本通知来源 ${sourceFrequency}，未据此推断 5m 买卖点已确认`,
          boundary: `结构防守价 ${text(safeSignal.structural_stop, "未提供")}`,
          evidence: notificationEvidence,
        },
        {
          frequency: "1m",
          state: closed
            ? "已失效"
            : segmentEvidenceStatus === "present" && triggerKnown
              ? segmentBoundaryStatus === "current"
                ? "区间套精确定位有效"
                : segmentBoundaryStatus === "expired"
                  ? "历史区间套定位已过"
                  : segmentBoundaryStatus === "unavailable"
                    ? "历史区间套定位边界缺失"
                    : segmentBoundaryStatus === "not_applicable"
                      ? segmentDifferenceEvidenceCurrent
                        ? "卖出区间套精确定位有效"
                        : "历史卖出区间套定位"
                      : "区间套定位边界待核对"
                    : "等待1分钟区间套",
          tone: closed
            ? "blocked"
            : segmentEvidenceStatus === "present" && triggerKnown
              ? segmentDifferenceEvidenceCurrent ? "supportive" : "neutral"
              : "neutral",
          summary: segmentEvidenceStatus === "present" && triggerKnown
            ? segmentDifferenceEvidenceCurrent
              ? `${recordedSegmentEvidence} · 1分钟区间套精确定位已确认`
              : `${recordedSegmentEvidence} · 1分钟历史区间套证据保留（不计入当前定位）`
                  : "5分钟信号已成立；等待1分钟区间套后才进入精确执行候选",
          boundary: segmentEvidenceStatus === "present" && triggerKnown
            ? segmentBoundaryStatus === "current"
              ? `买入定位窗口有效至 ${segmentValidUntil}；精确执行候选已解锁，仍须人工复核`
              : segmentBoundaryStatus === "expired"
                ? `买入定位窗口有效至 ${segmentValidUntil}，现已过期；仅保留历史定位证据`
                : segmentBoundaryStatus === "not_applicable"
                  ? segmentDifferenceEvidenceCurrent
                    ? "卖出区间套精确位置有效；核对持有结构级别后人工复核"
                    : "卖出区间套仅保留为历史定位证据；不计入当前精确位置"
                  : "区间套证据保留，买入定位边界需人工核对"
                : "5分钟信号已首报；精确执行需等待1分钟区间套",
          evidence: notificationEvidence,
        },
      ];
    }

    const contextPeriod = (frequency, value, label) => {
      const known = Object.keys(value).length > 0;
      const adverse = known && (value.hard_block === true || value.disposition === "hostile");
      const blocked = adverse && !executionProfile;
      const supportive = known && value.disposition === "supportive";
      const tone = blocked ? "blocked" : supportive ? "supportive" : adverse ? "waiting" : known ? "neutral" : "unknown";
      const state = blocked ? "阻断" : supportive ? "支持" : adverse ? "逆风提示" : known ? "中性" : "未知";
      const reasons = uniqueText(value.reason_codes);
      const technical = isRecord(value.same_period_technical_evidence)
        ? value.same_period_technical_evidence
        : null;
      const maText = technical
        ? ` · MA5 ${numberText(technical.ma5)} / MA10 ${numberText(technical.ma10)}`
        : executionProfile ? " · MA证据暂不可用" : " · 本周期线段中枢";
      const fractalLabels = { top: "顶分型", bottom: "底分型", none: "分型待判定" };
      const fractalStateLabels = {
        forming: "形成中",
        confirmed: "已确认",
        pen_endpoint_pending_lock: "笔端点待锁定",
        pen_locked: "已延伸为锁定笔",
        continuation: "延续中",
        unresolved: "待判定",
      };
      const fractalText = technical
        ? ` · ${fractalLabels[technical.fractal_type] || "分型待判定"}${fractalStateLabels[technical.fractal_state] || "待判定"}`
        : "";
      return {
        frequency,
        state,
        tone,
        summary: known
          ? `方向 ${DIRECTION_LABELS[value.direction] || "待判定"} · 主导 ${POINT_LABELS[value.dominant_point_type] || "无主导点"}${maText}${fractalText}`
          : `${label}结构证据未提供`,
        boundary: blocked
          ? reasonLabel(reasons[0])
          : adverse && executionProfile
            ? "仅降低环境等级，不否定5分钟操作确认"
            : known ? "没有关键限制" : "环境证据未提供",
        evidence: reasons.map(reasonLabel),
      };
    };

    let setupState = "未知";
    let setupTone = "unknown";
    const formationState = setupFormationStateForSignal(safeSignal);
    const lockState = setupLockStateForSignal(safeSignal);
    const lifecycleStage = lifecycleStageForSignal(safeSignal);
    if (lifecycleStage === "invalidated") {
      setupState = formationState === "confirmed"
        ? "已确认后失效"
        : formationState === "geometry_ready" ? "候选已失效" : "已失效";
      setupTone = "blocked";
    } else if (formationState === "confirmed") {
      // 周期节点的主状态仍然只回答 5 分钟买卖点是否已达到操作确认。
      // 锁定进度属于证据审计状态，放在下方摘要中，避免把 pending 误读成
      // “买卖点还没有确认”，也避免全列表出现同一条醒目的复核中提示。
      setupState = lockState === "locked"
        ? "5分钟操作确认·末端已封存"
        : lockState === "pending"
          ? "5分钟操作确认"
          : "5分钟操作确认·证据状态待核对";
      setupTone = "supportive";
    } else if (formationState === "geometry_ready") {
      setupState = setup.contains_unlocked_segment === true
        ? "候选待锁定"
        : "候选待确认";
      setupTone = "waiting";
    } else if (formationState === "forming") {
      setupState = "形成中";
      setupTone = "waiting";
    } else if (setup.status === "invalidated") {
      setupState = "已失效";
      setupTone = "blocked";
    } else if (!setupKnown) {
      setupState = "未知";
    }

    let triggerState = "等待1分钟区间套";
    let triggerTone = "neutral";
    if (triggerKnown && trigger.status === "invalidated") {
      triggerState = "已失效";
      triggerTone = "blocked";
    } else if (triggerKnown && segmentEvidenceStatus === "present") {
      triggerState = segmentBoundaryStatus === "current"
        ? "区间套精确定位有效"
        : segmentBoundaryStatus === "expired"
          ? "历史区间套定位已过"
          : segmentBoundaryStatus === "unavailable"
            ? "历史区间套定位边界缺失"
            : segmentBoundaryStatus === "not_applicable"
              ? segmentDifferenceEvidenceCurrent
                ? "卖出区间套精确定位有效"
                : "历史卖出区间套定位"
              : "区间套定位边界待核对";
      triggerTone = segmentDifferenceEvidenceCurrent ? "supportive" : "neutral";
    }

    const setupEvidence = uniqueText(setup.evidence_codes);
    const terminalSummary = terminalSegmentSummary(setup);
    const terminalRange = terminalSegmentRange(setup);
    const triggerEvidence = triggerKnown ? uniqueText(trigger.evidence_codes) : [];
    const center = setup.center_ordinal === null || setup.center_ordinal === undefined
      ? "中枢序号不适用"
      : `第 ${text(setup.center_ordinal)} 中枢`;
    const containsFormingSegment = setup.contains_forming_segment === true
      || (
        !Object.prototype.hasOwnProperty.call(setup, "contains_forming_segment")
        && setup.contains_unfinished_segment === true
        && formationState === "forming"
      );
    const unfinished = lifecycleStage === "invalidated"
      ? ""
      : formationState === "confirmed" && lockState === "pending"
        ? " · 操作确认已成立 · 末端结构仍会随新K更新（不影响当前复核）"
      : formationState === "geometry_ready" && lockState === "pending"
        ? " · 仅为几何候选 · 操作确认尚未完成"
        : containsFormingSegment ? " · 含正在形成的线段" : "";
    const triggerPoint = triggerKnown
      ? segmentPointEvidenceLabel(trigger, "段差位置")
      : null;

    return [
      contextPeriod("d", daily, "日线"),
      contextPeriod("30m", context, "30分钟"),
      {
        frequency: "5m",
        state: setupState,
        tone: setupTone,
        summary: setupKnown
          ? `${terminalSummary ? `${terminalSummary} · ` : ""}${pointLabelForSignal({
            ...safeSignal,
            point_type: setup.point_type || safeSignal.point_type,
          })} · 严格笔→递归中枢 · 第 ${text(setup.recursive_level, "0")} 层 · ${center}${unfinished}`
          : "操作级别设置未提供",
        boundary: setup.invalidation_price === null || setup.invalidation_price === undefined || setup.invalidation_price === ""
          ? (terminalRange || "失效价未提供")
          : `${terminalRange ? `${terminalRange} · ` : ""}失效价 ${text(setup.invalidation_price)}`,
        evidence: setupEvidence.map(reasonLabel),
      },
      {
        frequency: "1m",
        state: triggerState,
        tone: triggerTone,
        summary: triggerKnown && segmentEvidenceStatus === "present"
          ? segmentDifferenceEvidenceCurrent
            ? `${triggerPoint} · 1分钟区间套精确定位已确认`
            : `${triggerPoint} · 1分钟历史区间套证据保留（不计入当前定位）`
          : "尚未取得1分钟区间套（5分钟信号保留，精确执行未解锁）",
        boundary: triggerKnown && segmentEvidenceStatus === "present"
          ? segmentBoundaryStatus === "current"
            ? "买入定位窗口仍有效；已进入精确执行候选，仍须人工复核"
            : segmentBoundaryStatus === "expired"
              ? "买入定位窗口已过，仅保留历史区间套证据；不追价"
              : segmentBoundaryStatus === "not_applicable"
                ? segmentDifferenceEvidenceCurrent
                  ? "卖出区间套精确位置有效；核对持有结构级别后人工复核"
                  : "卖出区间套仅保留为历史定位证据；不计入当前精确位置"
                : "区间套证据已保留；定位边界需人工核对"
          : "5分钟信号已可复核；未完成1分钟区间套前不生成执行比例",
        evidence: triggerEvidence.map(reasonLabel),
      },
    ];
  }

  function exactReasonCodes(value, expected) {
    if (!Array.isArray(value) || value.length !== expected.length) return false;
    const actual = new Set(value);
    return actual.size === expected.length
      && expected.every((code) => actual.has(code));
  }

  function sellOnlyEntryGateDeclaration(higherRisk, signal, screeningPolicy) {
    const safeRisk = isRecord(higherRisk) ? higherRisk : {};
    const safeSignal = isRecord(signal) ? signal : {};
    const safePolicy = isRecord(screeningPolicy) ? screeningPolicy : {};
    const sourceFieldsPresent = [
      "sector_higher_timeframe_source_mode",
      "sector_strict_same_5m_warmup_evidence",
      "sector_strict_same_5m_source_coverage_evidence",
      "sector_research_bridge_parameter_set_id",
    ].some((field) => Object.prototype.hasOwnProperty.call(safeRisk, field));
    const exactSellOnlyBoundary = ["1sell", "2sell", "3sell"].includes(
      text(safeSignal.point_type, ""),
    )
      && safeSignal.side === "sell"
      && safeSignal.selection_path === "INDIVIDUAL_THREE_PROGRAM"
      && safeSignal.entry_allowed === false
      && safeSignal.technical_entry_allowed === false
      && safeRisk.new_entry_requires_all_green === false
      && safeRisk.market_gate === "UNRESOLVED"
      && safeRisk.sector_gate === "UNRESOLVED"
      && safeRisk.symbol_gate === "UNRESOLVED"
      && safePolicy.sell_only_higher_timeframe_evidence_policy
        === SELL_ONLY_HIGHER_TIMEFRAME_EVIDENCE_POLICY
      && !sourceFieldsPresent;
    if (!exactSellOnlyBoundary) return "";

    if (
      exactReasonCodes(safeRisk.market_reason_codes, [SELL_ONLY_ENTRY_GATE_REASON])
      && exactReasonCodes(safeRisk.sector_reason_codes, [SELL_ONLY_ENTRY_GATE_REASON])
      && exactReasonCodes(safeRisk.symbol_reason_codes, [SELL_ONLY_ENTRY_GATE_REASON])
      && exactReasonCodes(safeRisk.reason_codes, [SELL_ONLY_ENTRY_GATE_REASON])
    ) {
      return SELL_ONLY_PRESENTATION_CURRENT;
    }
    if (
      exactReasonCodes(safeRisk.market_reason_codes, ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"])
      && exactReasonCodes(
        safeRisk.sector_reason_codes,
        ["HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED"],
      )
      && exactReasonCodes(safeRisk.symbol_reason_codes, ["HIGHER_TIMEFRAME_GATE_NOT_ATTACHED"])
      && exactReasonCodes(safeRisk.reason_codes, [
        "HIGHER_TIMEFRAME_GATE_NOT_ATTACHED",
        "HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED",
      ])
    ) {
      return SELL_ONLY_PRESENTATION_LEGACY;
    }
    return "";
  }

  function sectorHigherTimeframeSourceEvidence(higherRisk, signal = {}) {
    const safeRisk = isRecord(higherRisk) ? higherRisk : {};
    const safeSignal = isRecord(signal) ? signal : {};
    const sector = isRecord(safeSignal.sector) ? safeSignal.sector : {};
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
    const sourceFieldsPresent = [
      "sector_higher_timeframe_source_mode",
      "sector_strict_same_5m_warmup_evidence",
      "sector_strict_same_5m_source_coverage_evidence",
      "sector_research_bridge_parameter_set_id",
    ].some((field) => Object.prototype.hasOwnProperty.call(safeRisk, field));
    const sectorReasonCodes = uniqueText(safeRisk.sector_reason_codes);
    const providerUnavailable = safeRisk.sector_gate === "UNRESOLVED"
      && sectorReasonCodes.some((code) => [
        "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
        "QMT_SECTOR_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
      ].includes(code));
    const etfProxyNotApplicable = !sourceFieldsPresent
      && safeSignal.selection_path === "ETF_PROXY"
      && text(sector.sector_id, "").startsWith("etf-proxy:")
      && sector.eligible === true
      && sector.hard_block === false
      && uniqueText(sector.reason_codes).includes("ETF_PROXY_SECTOR_NOT_REQUIRED")
      && safeRisk.sector_gate === "UNRESOLVED"
      && sectorReasonCodes.includes("QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE");
    const sellOnlyEntryGateNotApplicable = [
      SELL_ONLY_PRESENTATION_CURRENT,
      SELL_ONLY_PRESENTATION_LEGACY,
    ].includes(safeSignal.presentation_sell_only_higher_timeframe_entry_gate);
    if (!mode) {
      const declaredUnavailable = !sourceFieldsPresent && providerUnavailable;
      if (sellOnlyEntryGateNotApplicable) {
        return {
          cardLabel: "卖点无需买入高级别门",
          risk: [
            "月线、周线、日线高级别风险门只用于新买入核验；当前为纯卖出结构，因此按策略不调用该提供器。日线、30分钟环境与卖出证据仍然保留。",
          ],
          blocking: [],
          raw: uniqueText([
            ...uniqueText(safeRisk.reason_codes),
            ...uniqueText(safeRisk.market_reason_codes),
            ...sectorReasonCodes,
            ...uniqueText(safeRisk.symbol_reason_codes),
          ]),
          contractInvalid: false,
        };
      }
      return {
        cardLabel: etfProxyNotApplicable
          ? "ETF代理无需行业板块"
          : declaredUnavailable ? "来源暂不可用" : "来源证据不完整",
        risk: etfProxyNotApplicable
          ? ["ETF代理路径不要求行业板块高级别来源；市场与标的自身风险门仍然有效"]
          : declaredUnavailable
            ? ["板块高级别提供器当前不可用；相关结论保持失败关闭，仅展示候选供人工复核"]
            : [],
        blocking: etfProxyNotApplicable
          ? []
          : [
            declaredUnavailable
              ? "板块高级别来源尚未取得，不能据此解除风险门"
              : "板块高级别来源字段不完整，不能据此解除风险门",
          ],
        raw: sectorReasonCodes,
        contractInvalid: !etfProxyNotApplicable && !declaredUnavailable,
      };
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
          "板块研究数据来源：严格同一5m基底；日线与30分钟均由该基底因果派生，当前执行只使用日线与30分钟",
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
        contractInvalid: inconsistent || coverageMalformed,
      };
    }
    if (mode === SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE) {
      const bridgeMalformed = !bridgeId.startsWith("sha256:");
      const strictCauseInvalid = strictWarmup === null
        || strictWarmup.reason_code !== "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT";
      const greenConflict = safeRisk.sector_gate === "GREEN";
      return {
        cardLabel: "原生日线研究桥（最高为琥珀色）",
        risk: [
          "板块研究数据来源：QMT 原生日线用于长期历史审计；30分钟仍由同一5分钟基底派生，当前执行只使用日线与30分钟",
          "研究限制：原生日线与5m/30m非线性聚合尚未调和 · 仅供研究 · 绿色结论最多降为琥珀色 · 不自动下单",
          strictLine,
          coverageLine,
          `研究桥参数：${bridgeId || "缺失"}`,
        ],
        blocking: [
          ...(greenConflict
            ? ["板块研究桥不能产生绿色结论；当前绿色字段矛盾，继续失败关闭"]
            : []),
          ...(bridgeMalformed || strictCauseInvalid
            ? ["板块研究桥身份或启用原因不完整，不能据此解除风险门"]
            : []),
          ...(coverageMalformed
            ? ["板块严格5m历史边界证据缺失或与暖机证据矛盾，不能据此解除风险门"]
            : []),
        ],
        raw,
        contractInvalid: greenConflict
          || bridgeMalformed
          || strictCauseInvalid
          || coverageMalformed,
      };
    }
    return {
      cardLabel: "来源模式未认证",
      risk: ["板块高级别来源模式未认证；原始模式标识已保留在下方审计代码中"],
      blocking: ["板块高级别来源模式未认证，继续失败关闭"],
      raw,
      contractInvalid: true,
    };
  }

  function evidenceGroupsForSignal(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const stage = lifecycleStageForSignal(safeSignal);
    const executionProfile = isRecord(safeSignal.execution_profile)
      ? safeSignal.execution_profile
      : null;
    const positionRecommendation = positionRecommendationForSignal(safeSignal);
    const positionBlocked = positionRecommendation.status === "BLOCKED";
    const hardProfileReasons = executionProfile
      ? uniqueText(executionProfile.hard_block_reason_codes)
      : [];
    // 同一底层原因可能既解释完整性硬门，又保留在高级别诊断来源中。
    // 展示时硬门具有更高语义优先级；继续把同一码渲染成“环境提示”会让用户
    // 误以为存在两个独立问题。原始审计代码仍完整保留在 raw 组中。
    const advisoryProfileReasons = executionProfile
      ? uniqueText(executionProfile.advisory_reason_codes)
        .filter((code) => !hardProfileReasons.includes(code))
      : [];
    const higherTimeframeAdvisoryReasons = advisoryProfileReasons.filter((code) => (
      code === "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN"
      || /^(MARKET|SECTOR|SYMBOL)_GATE_/.test(code)
      || /^(M|W|D)_/.test(code)
      || code === "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
      || code === "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
      || code === "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"
      || code === "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    ));
    const directAdvisoryProfileReasons = advisoryProfileReasons.filter(
      (code) => !higherTimeframeAdvisoryReasons.includes(code),
    );
    const daily = isRecord(safeSignal.context_d) ? safeSignal.context_d : {};
    const context = isRecord(safeSignal.context_30m) ? safeSignal.context_30m : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const lockState = setupLockStateForSignal(safeSignal);
    const formationState = text(
      setup.formation_state,
      stage === "formed" ? "geometry_ready" : stage === "approaching" ? "forming" : "",
    );
    const trigger = segmentDifferenceForSignal(safeSignal);
    const dailyCodes = uniqueText(daily.reason_codes);
    const contextCodes = uniqueText(context.reason_codes);
    const setupEvidence = uniqueText(setup.evidence_codes);
    const setupPendingEvidence = setupEvidence.filter((code) => (
      MISSING_REASON_CODES.has(code)
    ));
    const setupConfirmedEvidence = setupEvidence.filter((code) => !MISSING_REASON_CODES.has(code));
    const setupMissing = uniqueText(setup.missing_conditions);
    const triggerEvidence = trigger ? uniqueText(trigger.evidence_codes) : [];
    const triggerMissing = trigger ? uniqueText(trigger.missing_conditions) : [];
    const decisions = uniqueText(safeSignal.decision_reasons);
    const selectionSources = uniqueText(safeSignal.selection_sources);
    const higherRisk = isRecord(safeSignal.higher_timeframe_risk)
      ? safeSignal.higher_timeframe_risk
      : {};
    const allMarketRiskReasons = uniqueText(higherRisk.market_reason_codes);
    const allSectorRiskReasons = uniqueText(higherRisk.sector_reason_codes);
    const allSymbolRiskReasons = uniqueText(higherRisk.symbol_reason_codes);
    const mergedRiskReasons = uniqueText(higherRisk.reason_codes);
    const marketRiskReasons = dailyHigherTimeframeReasonCodes(allMarketRiskReasons);
    const sectorRiskReasons = dailyHigherTimeframeReasonCodes(allSectorRiskReasons);
    const symbolRiskReasons = dailyHigherTimeframeReasonCodes(allSymbolRiskReasons);
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
      ? higherRisk.market_period_diagnostics.filter((row) => isRecord(row) && row.period === "D")
      : [];
    const sectorDiagnostics = Array.isArray(higherRisk.sector_period_diagnostics)
      ? higherRisk.sector_period_diagnostics.filter((row) => isRecord(row) && row.period === "D")
      : [];
    const symbolDiagnostics = Array.isArray(higherRisk.symbol_period_diagnostics)
      ? higherRisk.symbol_period_diagnostics.filter((row) => isRecord(row) && row.period === "D")
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
    const sectorSourceEvidence = sectorHigherTimeframeSourceEvidence(
      higherRisk,
      safeSignal,
    );
    const riskGateLine = (subject, gate, reasons) => {
      const labels = reasons.map(reasonLabel);
      return `${subject}风险门 ${statusLabel(gate, "未知状态")}：${labels.length ? labels.join("；") : "无附加拒绝原因"}`;
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
        ? `映射供给 ${MAPPING_SUPPLY_LABELS[supply.classification] || "未分类"} · 低级别点 ${numberText(supply.point_evidence_count)}（一卖 ${numberText(pointCounts["1sell"])} / 二卖 ${numberText(pointCounts["2sell"])} / 三卖 ${numberText(pointCounts["3sell"])} / 三买 ${numberText(pointCounts["3buy"])}）· 分型内一二卖 ${numberText(supply.in_top_interval_sell12_count)} / 已完成中枢 ${numberText(supply.completed_in_top_interval_sell12_count)}`
        : row.state === "NONE"
          ? "映射供给：当前周期无活动顶分型，不适用"
          : "映射供给：当前契约字段缺失，失败关闭";
      return `${subject}${periodLabel(row.period)}：${statusLabel(row.state, "尚未解决")} · 完成K线 ${numberText(row.completed_bar_count)} · ${interval} · ${evidenceEnd} · ${mapping} · ${supplyText}${suffix.length ? ` · ${suffix.join("；")}` : ""}`;
    });
    const sessionEvidenceLines = (subject, evidence) => {
      if (!isRecord(evidence)) {
        return [`${subject}1分钟会话证据：当前契约字段缺失 · 高周期环境不可判定，不关闭5分钟主信号`];
      }
      if (evidence.status === "UNAVAILABLE") {
        return [`${subject}1分钟会话证据：不可用 · 高周期环境失败关闭，不关闭5分钟主信号`];
      }
      const issues = Array.isArray(evidence.issues)
        ? evidence.issues.filter(isRecord)
        : [];
      return issues.map((issue) => {
        if (issue.code === "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING") {
          return `${subject}1分钟缺失交易日 ${text(issue.session, "未知")}：观测 ${numberText(issue.observed_rows)} 根 · 历史停牌状态未获认证 · 不自动填补 · 高周期环境失败关闭，不关闭5分钟主信号`;
        }
        if (issue.code === "QMT_ONE_MINUTE_SESSION_GRID_INVALID") {
          return `${subject}1分钟会话网格异常 ${text(issue.session, "未知")}：观测 ${numberText(issue.observed_rows)} 根 · 高周期环境失败关闭，不关闭5分钟主信号`;
        }
        return `${subject}1分钟会话异常 ${text(issue.session, "未知")}：${reasonLabel(issue.code)} · 高周期环境失败关闭，不关闭5分钟主信号`;
      });
    };
    const higherTimeframeWarmupLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      const verdict = evidence.converged === true ? "一致" : "未一致";
      return [
        `${subject}高级别历史暖机（仅审计）：${verdict} · 完整 ${numberText(evidence.full_daily_bar_count)} 根日线 / 对照后缀 ${numberText(evidence.suffix_daily_bar_count)} 根 · 要求 ${numberText(evidence.required_daily_bar_count)} 根 · ${reasonLabel(evidence.reason_code)} · 不参与买入放行`,
      ];
    };
    const nativeDailyLines = (subject, evidence) => {
      if (!isRecord(evidence)) return [];
      const passed = evidence.all_overlap_ohlcv_within_declared_tolerance === true;
      return [
        `${subject}原生日线左历史复核：${passed ? "通过" : "失败关闭"} · 原生日线 ${numberText(evidence.native_daily_bar_count)} 根 / 1分钟派生日线 ${numberText(evidence.one_minute_daily_bar_count)} 根 · 重叠 ${numberText(evidence.overlap_session_count)} 个交易日（${text(evidence.first_overlap_session, "未知")} 至 ${text(evidence.last_overlap_session, "未知")}）· 容许价差 ${numberText(evidence.price_tolerance_quanta)} 个量化单位 / 实测最大 ${numberText(evidence.max_observed_price_difference_quanta)} · 原生日线只补左历史，30分钟仍由1分钟派生 · ${statusLabel(evidence.live_status, "不自动下单")}`,
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
        `${subject}原生日线交易日覆盖：${statusLabel(evidence.status, "尚未解决")} · 原生日线 ${numberText(evidence.native_daily_bar_count)} 根 / 日历应有 ${numberText(evidence.expected_calendar_session_count)} 个交易日 · 日历有而日线缺 ${missingText} · 日线有而日历缺 ${nativeOnlyText} · 缺失未证明为停牌、不自动填补 · 失败关闭`,
      ];
    };
    const missingDecisions = decisions.filter((code) => (
      MISSING_REASON_CODES.has(code)
    ));
    const blockingDecisions = executionProfile
      ? decisions.filter((code) => hardProfileReasons.includes(code))
      : decisions.filter((code) => (
        !MISSING_REASON_CODES.has(code)
        && code !== "five_minute_setup_formed_awaiting_lock"
        && code !== "five_minute_geometry_candidate_awaiting_confirmation"
      ));
    const established = [
      ...(selectionSources.length || typeof safeSignal.sector_triggered === "boolean"
        ? [`候选来源：${selectionLabelForSignal(safeSignal)}`]
        : []),
      ...(safeSignal.formal_selection_required === false
        ? ["选股口径：实时技术监听不要求离线正式研究；该项不构成阻断"]
        : safeSignal.formal_selection_required === true
          ? ["选股口径：本通道要求正式研究，结果以完整审计证据为准"]
          : []),
      ...(daily.hard_block === true || daily.disposition === "hostile"
        ? []
        : prefixedLabels("日线", dailyCodes)),
      ...(context.hard_block === true || context.disposition === "hostile"
        ? []
        : prefixedLabels("30分钟", contextCodes)),
      ...prefixedLabels("5分钟", setupConfirmedEvidence),
      ...(formationState === "geometry_ready"
        ? [`5分钟：${POINT_LABELS[safeSignal.point_type] || "买卖点"}离开/回抽几何已出现（仅候选，尚未达到操作确认）`]
        : []),
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
      ...(trigger ? [] : ["1分钟：区间套尚未出现（5分钟信号保留，精确执行未解锁）"]),
      ...missingDecisions.map(reasonLabel),
      ...(warmup.converged === false
        ? warmupReasons
          .filter((code) => (
            !code.endsWith("TAIL_STABLE")
            && !hardProfileReasons.includes(code)
          ))
          .map(reasonLabel)
        : []),
      ...(warmup.converged === false ? warmupDifferenceLines : []),
    ];
    const dailyBlocking = !executionProfile && (daily.hard_block === true || daily.disposition === "hostile")
      ? dailyCodes.map(reasonLabel)
      : [];
    const contextBlocking = !executionProfile && (context.hard_block === true || context.disposition === "hostile")
      ? contextCodes.map(reasonLabel)
      : [];
    const nextByStage = {
      observed: "等待 5分钟形成可审计买卖点设置",
      approaching: "等待 5分钟设置闭合并确认",
      formed: "仅出现买卖点几何候选；等待达到操作确认",
      armed: "旧版等待态；下一次计算按5分钟操作确认迁移",
      triggered: lockState === "pending"
        ? "5分钟操作确认已成立；等待1分钟区间套后进入精确执行候选"
        : lockState === "locked"
          ? "5分钟买卖点操作确认且末端结构已封存；等待1分钟区间套精确定位"
          : "5分钟操作确认已记录、结构证据状态待核对；等待1分钟区间套精确定位",
      executable: "人工复核中枢、走势类型、级别与买卖点",
      active: "跟踪反向买卖点与结构止损",
      invalidated: "信号已失效，等待新的结构设置",
      closed: "本次跟踪已经结束",
    };
    const invalidation = text(setup.invalidation_price, "未提供");
    const structuralStop = text(
      setup.invalidation_price ?? safeSignal.structural_stop,
      "未提供",
    );
    const riskMultiplier = buyRiskMultiplierText(safeSignal);
    return {
      established: uniqueText(established),
      missing: uniqueText(missing),
      blocking: uniqueText([
      ...dailyBlocking,
      ...contextBlocking,
      ...blockingDecisions.map(reasonLabel),
      ...(positionBlocked ? [positionRecommendationLabel(safeSignal)] : []),
      ...(!executionProfile ? sectorSourceEvidence.blocking : []),
      ...(!executionProfile && higherRisk.market_gate && higherRisk.market_gate !== "GREEN"
        ? [riskGateLine("市场", higherRisk.market_gate, marketRiskReasons)]
        : []),
      ...(!executionProfile && higherRisk.sector_gate && higherRisk.sector_gate !== "GREEN"
        ? [riskGateLine("板块", higherRisk.sector_gate, sectorRiskReasons)]
        : []),
      ...(!executionProfile && higherRisk.symbol_gate && higherRisk.symbol_gate !== "GREEN"
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
      next: [
        positionBlocked
          ? blockedPositionNextAction(
            safeSignal,
            positionRecommendation,
            executionProfile,
          )
          : nextByStage[stage] || "等待新的可审计结构事实",
      ],
      risk: [
        `5分钟失效价：${invalidation}`,
        `结构防守价：${structuralStop}`,
        ...(riskMultiplier === null
          ? []
          : [`买入风险缩放系数：×${riskMultiplier}（仅供结构模型比较）`]),
        ...(positionBlocked ? [] : [positionRecommendationLabel(safeSignal)]),
        ...(executionProfile
          ? [
            `环境等级：${text(executionProfile.context_grade_label, "待判定")}；当前执行只使用日线与30分钟分级`,
            ...(text(safeSignal.side, "") === "buy" && !positionBlocked
              ? [`买入环境缩放系数：×${text(executionProfile.context_risk_scale, "0.50")}（仅供结构模型比较）`]
              : []),
          ]
          : []),
        ...(higherTimeframeAdvisoryReasons.length
          ? [
            `月/周/日高级别研究：当前证据未形成可用于方向结论的完整历史；仅作环境审计，不阻断5分钟买卖点（${higherTimeframeAdvisoryReasons.length}条原始诊断保留在审计代码中）`,
          ]
          : []),
        ...directAdvisoryProfileReasons.map((code) => `环境提示：${reasonLabel(code)}`),
        ...(!executionProfile ? periodDiagnosticLines("市场", marketDiagnostics) : []),
        ...(!executionProfile ? periodDiagnosticLines("板块", sectorDiagnostics) : []),
        ...(!executionProfile ? periodDiagnosticLines("个股", symbolDiagnostics) : []),
        ...(!executionProfile ? sessionEvidenceLines("市场", marketSessionEvidence) : []),
        ...(!executionProfile ? sessionEvidenceLines("板块", sectorSessionEvidence) : []),
        ...(!executionProfile ? sessionEvidenceLines("个股", symbolSessionEvidence) : []),
        ...(!executionProfile ? higherTimeframeWarmupLines("市场", marketMwdWarmup) : []),
        ...(!executionProfile ? higherTimeframeWarmupLines("板块", sectorMwdWarmup) : []),
        ...(!executionProfile ? higherTimeframeWarmupLines("个股", symbolMwdWarmup) : []),
        ...nativeDailyLines("市场", marketNativeDaily),
        ...nativeDailyLines("个股", symbolNativeDaily),
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
        ...allMarketRiskReasons,
        ...allSectorRiskReasons,
        ...allSymbolRiskReasons,
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

  function unavailableUsMonitor(reasonCode = "US_MONITOR_UNAVAILABLE") {
    return {
      schema: "chanlun-us-realtime-monitor",
      source_schema: "chanlun-attention-group-monitor",
      market: "us",
      market_scope: "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS",
      decision_mode: "STRICT_STRUCTURE_OBSERVATION_ONLY",
      auxiliary_only: true,
      full_market_screening: false,
      selection_candidates: false,
      available: false,
      ready: false,
      status: "unavailable",
      reason_code: reasonCode,
      symbols: [],
      declared_count: 0,
      monitored_count: 0,
      covered_count: 0,
      active_count: 0,
      closed_count: 0,
      awaiting_count: 0,
      failed_count: 0,
      research_only: true,
      no_order_execution: true,
      manual_review_required: true,
    };
  }

  function normalizeUsMonitor(value) {
    const safe = isRecord(value) ? value : null;
    if (safe === null) {
      return unavailableUsMonitor("US_MONITOR_UNAVAILABLE");
    }
    if (
      safe.schema !== "chanlun-us-realtime-monitor"
      || safe.source_schema !== "chanlun-attention-group-monitor"
      || safe.market !== "us"
      || safe.market_scope !== "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS"
      || safe.decision_mode !== "STRICT_STRUCTURE_OBSERVATION_ONLY"
      || safe.auxiliary_only !== true
      || safe.full_market_screening !== false
      || safe.selection_candidates !== false
      || safe.research_only !== true
      || safe.no_order_execution !== true
      || safe.manual_review_required !== true
      || !Array.isArray(safe.symbols)
    ) {
      return unavailableUsMonitor("US_MONITOR_CONTRACT_INVALID");
    }
    if (safe.available !== true) {
      return {
        ...unavailableUsMonitor(text(safe.reason_code, "US_MONITOR_UNAVAILABLE")),
        ...safe,
        available: false,
        ready: false,
        symbols: [],
        declared_count: 0,
        monitored_count: 0,
        covered_count: 0,
        active_count: 0,
        closed_count: 0,
        awaiting_count: 0,
        failed_count: 0,
      };
    }
    const allowedStatuses = new Set([
      "monitoring",
      "market_closed",
      "warming_up",
      "awaiting_first_run",
      "error",
    ]);
    const symbols = [];
    for (const raw of safe.symbols) {
      if (
        !isRecord(raw)
        || raw.market !== "us"
        || !text(raw.code, "").trim()
        || !text(raw.name, "").trim()
        || !allowedStatuses.has(raw.status)
        || !["MANUAL_ATTENTION", "WATCHLIST"].includes(raw.monitoring_scope)
        || !Array.isArray(raw.groups)
        || raw.groups.some((group) => !text(group, "").trim())
      ) {
        return unavailableUsMonitor("US_MONITOR_CONTRACT_INVALID");
      }
      symbols.push({ ...raw, groups: raw.groups.slice() });
    }
    const activeCount = symbols.filter((row) => row.status === "monitoring").length;
    const closedCount = symbols.filter((row) => row.status === "market_closed").length;
    const awaitingCount = symbols.filter(
      (row) => ["warming_up", "awaiting_first_run"].includes(row.status),
    ).length;
    const failedCount = symbols.filter((row) => row.status === "error").length;
    return {
      ...safe,
      symbols,
      declared_count: symbols.length,
      monitored_count: activeCount,
      covered_count: symbols.length - failedCount,
      active_count: activeCount,
      closed_count: closedCount,
      awaiting_count: awaitingCount,
      failed_count: failedCount,
    };
  }

  function inferSignalMarket(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const explicit = text(safeSignal.market, "").trim().toLowerCase();
    if (explicit) return explicit;
    const code = text(safeSignal.code, "").trim().toUpperCase();
    if (code.endsWith(".US")) return "us";
    if (code.startsWith("HK.") || code.endsWith(".HK")) return "hk";
    return "a";
  }

  function shanghaiDateKey(value) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function activeElapsedSecondsForReview(signal, startedRaw, endedRaw) {
    const started = new Date(startedRaw);
    const ended = new Date(endedRaw);
    if (
      !startedRaw
      || !endedRaw
      || Number.isNaN(started.getTime())
      || Number.isNaN(ended.getTime())
      || ended < started
    ) return null;
    let elapsed = (ended.getTime() - started.getTime()) / 1000;
    if (
      inferSignalMarket(signal) === "a"
      && shanghaiDateKey(started) === shanghaiDateKey(ended)
    ) {
      const session = shanghaiDateKey(started);
      const lunchStarted = new Date(`${session}T11:31:00+08:00`).getTime();
      const lunchEnded = new Date(`${session}T13:01:00+08:00`).getTime();
      const overlapStarted = Math.max(started.getTime(), lunchStarted);
      const overlapEnded = Math.min(ended.getTime(), lunchEnded);
      if (overlapEnded > overlapStarted) {
        elapsed -= (overlapEnded - overlapStarted) / 1000;
      }
    }
    return Math.max(0, elapsed);
  }

  function signalAgeSecondsForReview(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const startedRaw = setup.available_at
      || setup.confirmed_at
      || safeSignal.notification_signal_available_at
      || safeSignal.signal_available_at
      || safeSignal.notification_signal_time
      || safeSignal.signal_time;
    const endedRaw = safeSignal.notification_detected_at
      || safeSignal.detected_at
      || safeSignal.monitor_observed_at
      || safeSignal.observed_at;
    return activeElapsedSecondsForReview(safeSignal, startedRaw, endedRaw);
  }

  function immediateFiveMinuteSignalFreshForReview(signal, observedAt) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const started = new Date(setup.available_at);
    const ended = new Date(
      safeSignal.monitor_observed_at
      || safeSignal.observed_at
      || observedAt,
    );
    if (
      !setup.available_at
      || Number.isNaN(started.getTime())
      || Number.isNaN(ended.getTime())
      || ended < started
    ) return false;
    if (inferSignalMarket(safeSignal) !== "a") {
      return ended.getTime() - started.getTime() <= 10 * 60 * 1000;
    }
    const session = shanghaiDateKey(started);
    if (session !== shanghaiDateKey(ended)) return false;
    const sessionCloses = [];
    for (const openedAt of ["09:31:00", "13:01:00"]) {
      const firstClose = new Date(`${session}T${openedAt}+08:00`).getTime();
      for (let index = 0; index < 120; index += 1) {
        sessionCloses.push(firstClose + index * 60 * 1000);
      }
    }
    const tradingMinutes = sessionCloses.filter(
      (closeAt) => started.getTime() < closeAt && closeAt <= ended.getTime(),
    ).length;
    return tradingMinutes <= 10;
  }

  function signalCardLifecycleLabel(signal, observedAt = new Date()) {
    const safeSignal = isRecord(signal) ? signal : {};
    const stage = lifecycleStageForSignal(safeSignal);
    return lifecycleLabel(stage);
  }

  function signalCardTimeText(signal) {
    const safeSignal = isRecord(signal) ? signal : {};
    const setup = isRecord(safeSignal.setup_5m) ? safeSignal.setup_5m : {};
    const monitorAt = safeSignal.monitor_observed_at;
    const appendDistinctTime = (parts, label, value) => {
      if (!value) return;
      const rendered = timeText(value);
      if (!parts.some((part) => part.endsWith(` ${rendered}`))) {
        parts.push(`${label} ${rendered}`);
      }
    };
    if (setup.status === "provisional") {
      const anchorAt = setup.anchor_at || setup.terminal_segment_end_at;
      const candidateAt = setup.available_at || setup.terminal_segment_available_at;
      const parts = [];
      if (anchorAt) {
        parts.push(`${setup.formation_state === "geometry_ready" ? "5m候选结构" : "5m候选锚点"} ${timeText(anchorAt)}`);
      }
      appendDistinctTime(
        parts,
        setup.formation_state === "geometry_ready" ? "几何可用" : "数据截止",
        candidateAt,
      );
      appendDistinctTime(parts, "复查", monitorAt);
      if (parts.length) return parts.join(" · ");
    }
    const signalAt = setup.status === "confirmed"
      ? setup.available_at || setup.confirmed_at
      : setup.available_at
        || setup.confirmed_at
        || safeSignal.signal_available_at
        || safeSignal.notification_signal_available_at
        || safeSignal.notification_signal_time
        || safeSignal.realtime_notification_signal_time
        || safeSignal.signal_time;
    if (signalAt) {
      const parts = [`5m信号 ${timeText(signalAt)}`];
      appendDistinctTime(parts, "复查", monitorAt);
      return parts.join(" · ");
    }
    if (setup.terminal_segment_end_at) {
      const parts = [`5m结构 ${timeText(setup.terminal_segment_end_at)}`];
      appendDistinctTime(parts, "复查", monitorAt);
      return parts.join(" · ");
    }
    return `最近复查 ${timeText(safeSignal.monitor_observed_at || safeSignal.observed_at)}`;
  }

  function currentChartUrls(market, code) {
    const safeMarket = encodeURIComponent(text(market, "a"));
    const safeCode = encodeURIComponent(text(code, ""));
    const intervals = { d: "D", "30m": "30", "5m": "5", "1m": "1" };
    return Object.fromEntries(Object.entries(intervals).map(([frequency, interval]) => [
      frequency,
      `/?market=${safeMarket}&code=${safeCode}&layout=single&intervals=${interval}`,
    ]));
  }

  function usMonitorSignal(position, monitor) {
    const safe = isRecord(position) ? position : {};
    const snapshot = isRecord(monitor) ? monitor : {};
    const code = text(safe.code, "");
    const status = text(safe.status, "awaiting_first_run");
    const statusReasons = {
      monitoring: "us_realtime_monitoring",
      market_closed: "us_market_closed",
      warming_up: "us_monitor_warming_up",
      awaiting_first_run: "us_monitor_awaiting_first_run",
      error: "us_monitor_error",
    };
    return {
      signal_id: `us-monitor:${code}`,
      code,
      name: text(safe.name, code),
      market: "us",
      point_type: "",
      side: "",
      tower: "monitor",
      recursive_level: 0,
      lifecycle_stage: status === "error" ? "invalidated" : "monitoring",
      observed_at: snapshot.last_completed_at || snapshot.last_run_at || null,
      sector: {
        sector_id: "market:us",
        sector_name: "美股（不参与板块筛选）",
      },
      context_d: {},
      context_30m: {},
      setup_5m: {},
      segment_difference_1m: null,
      structural_stop: null,
      risk_multiplier: null,
      entry_allowed: false,
      exit_allowed: false,
      decision_reasons: [statusReasons[status] || "us_monitor_status_unknown"],
      selection_sources: [
        "US_AUXILIARY_MONITOR",
        safe.monitoring_scope === "MANUAL_ATTENTION"
          ? "MANUAL_ATTENTION_MONITOR"
          : "WATCHLIST_MONITOR",
      ],
      chart_urls: currentChartUrls("us", code),
      us_monitor_projection: true,
      us_monitor_status: status,
      us_monitor_scope: safe.monitoring_scope,
      us_monitor_groups: Array.isArray(safe.groups) ? safe.groups.slice() : [],
      physical_timeframe_recursive: true,
    };
  }

  function mergeUsMonitorSignals(signals, monitor) {
    const rows = Array.isArray(signals) ? signals.slice() : [];
    const symbols = isRecord(monitor) && Array.isArray(monitor.symbols)
      ? monitor.symbols
      : [];
    const presentCodes = new Set(
      rows
        .filter((signal) => inferSignalMarket(signal) === "us")
        .map((signal) => text(signal.code, "").toUpperCase()),
    );
    for (const symbolRow of symbols) {
      const code = text(symbolRow && symbolRow.code, "").toUpperCase();
      if (!code || presentCodes.has(code)) continue;
      const projected = usMonitorSignal(symbolRow, monitor);
      if (!isCurrentSelectionSignal(projected)) continue;
      rows.push(projected);
      presentCodes.add(code);
    }
    return rows;
  }

  function normalizeRealtimeNotifications(value) {
    const safe = isRecord(value) ? value : {};
    if (
      safe.schema !== REALTIME_REVIEW_SCHEMA
      || !Array.isArray(safe.events)
      || safe.credentials_exposed !== false
      || safe.real_account_accessed !== false
      || safe.real_order_transport_enabled !== false
      || safe.automated_order_authorized !== false
      || safe.live_status !== "LIVE_DISABLED"
    ) {
      return {
        schema: REALTIME_REVIEW_SCHEMA,
        events: [],
        event_count: 0,
        pending_review_count: 0,
        delivery_counts: {},
        available: false,
      };
    }
    const events = safe.events.filter((event) => (
      isRecord(event)
      && event.schema === "chanlun-realtime-review-notification"
      && typeof event.notification_id === "string"
      && event.notification_id.startsWith("sha256:")
      && event.notification_id.length === 71
      && ["buy", "sell"].includes(event.side)
      && ["pending", "delivered", "simulated", "failed", "expired"].includes(event.delivery_status)
      && typeof event.market === "string"
      && typeof event.code === "string"
      && event.code
      && isRecord(event.chart_urls)
      && [...FREQUENCIES].every((frequency) => (
        typeof event.chart_urls[frequency] === "string"
        && event.chart_urls[frequency].trim()
      ))
      && event.review_required === true
      && event.automated_action_authorized === false
      && event.real_order_transport_enabled === false
      && event.live_status === "LIVE_DISABLED"
    )).map((event) => ({
      ...event,
      setup_lock_state: ["pending", "locked"].includes(event.setup_lock_state)
        ? event.setup_lock_state
        : "unknown",
      chart_urls: { ...event.chart_urls },
      selection_sources: Array.isArray(event.selection_sources)
        ? event.selection_sources.slice()
        : [],
    })).sort((left, right) => (
      text(realtimeNotificationDisplayTime(right), "")
        .localeCompare(text(realtimeNotificationDisplayTime(left), ""))
    ));
    return {
      ...safe,
      available: true,
      events,
      event_count: events.length,
      delivery_counts: isRecord(safe.delivery_counts)
        ? { ...safe.delivery_counts }
        : {},
    };
  }

  function realtimeNotificationSignal(event) {
    const pointType = text(event.point_type, "");
    const market = text(event.market, "a").toLowerCase();
    const sourceFrequency = text(event.source_frequency, "5m");
    const segmentFrequency = text(event.segment_difference_frequency, "1m");
    const setupStatus = ["invalidated", "closed"].includes(event.new_stage)
      ? "invalidated"
      : "confirmed";
    const segmentPresent = event.segment_difference_present === true
      && text(event.segment_difference_point_type, "") !== "";
    const segmentStatus = text(
      event.segment_difference_status,
      segmentPresent ? "unknown" : "absent",
    );
    const segmentEvidenceStatus = text(
      event.segment_difference_evidence_status,
      segmentPresent ? "present" : "absent",
    );
    const segmentBoundaryStatus = text(
      event.segment_difference_boundary_status,
      !segmentPresent
        ? "absent"
        : event.side === "sell" ? "not_applicable" : segmentStatus,
    );
    const segmentCurrent = segmentPresent
      && segmentStatus === "current"
      && event.segment_difference_current === true;
    const segmentDifference = segmentPresent ? {
      status: setupStatus,
      point_type: event.segment_difference_point_type,
      source_frequency: segmentFrequency,
      point_id: event.segment_difference_evidence_id,
      confirmed_at: event.segment_difference_confirmed_at,
      available_at: event.segment_difference_available_at,
      anchor_at: event.segment_difference_anchor_time,
      recursive_level: Number(event.segment_difference_recursive_level) || 0,
      divergence_kind: event.segment_difference_divergence_kind || null,
      evidence_codes: ["realtime_segment_difference_recorded"],
    } : null;
    const selectionSources = Array.from(new Set([
      "REALTIME_NOTIFICATION",
      ...(Array.isArray(event.selection_sources) ? event.selection_sources : []),
      ...(market === "us" ? ["US_AUXILIARY_MONITOR"] : []),
    ]));
    const setupFrequency = sourceFrequency === "1m" ? "5m" : sourceFrequency;
    return {
      signal_id: event.notification_id,
      code: event.code,
      name: event.name || event.code,
      market,
      point_type: pointType,
      side: event.side,
      tower: "notification",
      recursive_level: Number(event.recursive_level) || 0,
      lifecycle_stage: [
        "triggered", "executable", "invalidated", "closed", "active",
      ].includes(event.new_stage) ? event.new_stage : "triggered",
      observed_at: event.detected_at || event.observed_at || event.recorded_at,
      sector: market === "us"
        ? { sector_id: "market:us", sector_name: "美股（不参与板块筛选）" }
        : { sector_id: "realtime-notification:a", sector_name: "A股实时通知" },
      context_d: {},
      context_30m: {
        direction: event.big_direction || "neutral",
        disposition: "neutral",
        reason_codes: [],
      },
      setup_5m: {
        status: setupStatus,
        formation_state: setupStatus === "confirmed" ? "confirmed" : "geometry_ready",
        lock_state: ["pending", "locked"].includes(event.setup_lock_state)
          ? event.setup_lock_state
          : "unknown",
        actionable: setupStatus === "confirmed",
        point_type: pointType,
        source_frequency: setupFrequency,
        point_id: event.evidence_id,
        confirmed_at: event.structure_confirmed_at || event.signal_available_at || event.signal_time,
        available_at: event.signal_available_at || event.signal_time,
        anchor_at: event.structure_anchor_time || event.anchor_time,
        recursive_level: Number(event.recursive_level) || 0,
        invalidation_price: event.invalidation_price,
        evidence_codes: ["realtime_notification_recorded"],
      },
      segment_difference_1m: segmentDifference,
      structural_stop: event.invalidation_price,
      risk_multiplier: null,
      position_recommendation: isRecord(event.position_recommendation)
        ? { ...event.position_recommendation }
        : null,
      entry_allowed: false,
      exit_allowed: false,
      decision_reasons: [
        "realtime_notification_requires_human_review",
        ...(segmentStatus === "expired" ? ["ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"] : []),
        ...(segmentStatus === "unavailable" ? ["ONE_MINUTE_SEGMENT_BOUNDARY_MISSING"] : []),
      ],
      selection_sources: selectionSources,
      chart_urls: { ...event.chart_urls },
      realtime_notification: true,
      notification_id: event.notification_id,
      notification_source: event.source,
      notification_delivery_status: event.delivery_status,
      notification_delivery_reason: event.delivery_reason,
      notification_recorded_at: event.recorded_at,
      notification_structure_anchor_time: event.structure_anchor_time || event.anchor_time,
      notification_structure_confirmed_at: event.structure_confirmed_at || event.signal_available_at || event.signal_time,
      notification_setup_lock_state: ["pending", "locked"].includes(event.setup_lock_state)
        ? event.setup_lock_state
        : "unknown",
      notification_signal_available_at: event.new_stage === "segment_enriched"
        ? laterIsoTime(
          event.signal_available_at || event.signal_time,
          event.segment_difference_available_at,
        )
        : event.signal_available_at || event.signal_time,
      notification_event_kind: event.new_stage === "segment_enriched"
        ? "ONE_MINUTE_SEGMENT_ENRICHMENT"
        : "FIVE_MINUTE_LIFECYCLE",
      notification_detected_at: event.detected_at || event.observed_at || event.recorded_at,
      notification_delivery_updated_at: event.delivery_updated_at || event.recorded_at,
      notification_delivered_at: event.delivered_at || null,
      notification_signal_time: event.signal_available_at || event.signal_time,
      notification_current_price: event.current_price,
      notification_current_price_source: event.current_price_source,
      notification_current_price_at: event.current_price_at || null,
      notification_reference_price: event.reference_price,
      notification_source_frequency: sourceFrequency,
      notification_trigger_frequency: null,
      notification_segment_difference_frequency: segmentFrequency,
      notification_segment_difference_present: segmentPresent,
      notification_segment_difference_status: segmentStatus,
      notification_segment_difference_current: segmentCurrent,
      notification_segment_difference_evidence_status: segmentEvidenceStatus,
      notification_segment_difference_boundary_status: segmentBoundaryStatus,
      notification_segment_difference_point_type: event.segment_difference_point_type,
      notification_segment_difference_divergence_kind: event.segment_difference_divergence_kind || null,
      notification_segment_difference_valid_until: event.segment_difference_valid_until || null,
      physical_timeframe_recursive: true,
      synthetic_notification_projection: true,
    };
  }

  function mergeRealtimeNotifications(signals, notificationSnapshot) {
    let rows = (Array.isArray(signals) ? signals : [])
      .filter(isCurrentSelectionSignal)
      .map((signal) => ({ ...signal }));
    const events = isRecord(notificationSnapshot)
      && Array.isArray(notificationSnapshot.events)
      ? notificationSnapshot.events
      : [];
    const sameOccurrence = (signal, event) => {
      const setup = isRecord(signal && signal.setup_5m) ? signal.setup_5m : {};
      const segment = segmentDifferenceForSignal(signal) || {};
      const evidenceId = text(event && event.evidence_id, "");
      if (
        evidenceId
        && [segment.point_id, setup.point_id]
          .map((value) => text(value, ""))
          .includes(evidenceId)
      ) return true;
      const signalTime = text(
        event && (event.signal_available_at || event.signal_time),
        "",
      );
      return Boolean(
        signalTime
        && [setup.available_at, setup.confirmed_at, signal && signal.observed_at]
          .map((value) => text(value, ""))
          .includes(signalTime)
      );
    };
    const matchesEvent = (signal, event) => {
      const market = text(event.market, "a").toLowerCase();
      const pointType = text(event.point_type, "");
      return inferSignalMarket(signal) === market
        && text(signal.code, "") === text(event.code, "")
        && text(signal.side, "") === text(event.side, "")
        && sameOccurrence(signal, event)
        && (
          !pointType
          || text(signal.point_type, "") === pointType
          || text(signal.setup_5m && signal.setup_5m.point_type, "") === pointType
        );
    };
    // 失效/结束通知只作为同一结构的墓碑；它们完整保留在人工复核历史，
    // 但不能成为当前选股行，也不能让更早的触发通知把旧结构重新带回列表。
    const terminalEvents = events.filter((event) => (
      ["invalidated", "closed"].includes(text(event.new_stage, ""))
    ));
    if (terminalEvents.length) {
      rows = rows.filter((signal) => (
        !terminalEvents.some((event) => matchesEvent(signal, event))
      ));
    }
    const matched = new Set();
    for (const event of events) {
      if (["invalidated", "closed"].includes(text(event.new_stage, ""))) continue;
      const market = text(event.market, "a").toLowerCase();
      const index = rows.findIndex((signal, rowIndex) => (
        !matched.has(rowIndex) && matchesEvent(signal, event)
      ));
      if (index >= 0) {
        matched.add(index);
        rows[index] = {
          ...rows[index],
          market,
          realtime_notification: true,
          notification_id: event.notification_id,
          notification_source: event.source,
          notification_delivery_status: event.delivery_status,
          notification_delivery_reason: event.delivery_reason,
          notification_recorded_at: event.recorded_at,
          notification_structure_anchor_time: event.structure_anchor_time || event.anchor_time,
          notification_structure_confirmed_at: event.structure_confirmed_at || event.signal_available_at || event.signal_time,
          notification_setup_lock_state: ["pending", "locked"].includes(event.setup_lock_state)
            ? event.setup_lock_state
            : "unknown",
          notification_signal_available_at: event.new_stage === "segment_enriched"
            ? laterIsoTime(
              event.signal_available_at || event.signal_time,
              event.segment_difference_available_at,
            )
            : event.signal_available_at || event.signal_time,
          notification_detected_at: event.detected_at || event.observed_at || event.recorded_at,
          notification_delivery_updated_at: event.delivery_updated_at || event.recorded_at,
          notification_delivered_at: event.delivered_at || null,
          notification_signal_time: event.signal_available_at || event.signal_time,
          notification_current_price: event.current_price,
          notification_current_price_source: event.current_price_source,
          notification_current_price_at: event.current_price_at || null,
          notification_reference_price: event.reference_price,
          position_recommendation: isRecord(event.position_recommendation)
            ? { ...event.position_recommendation }
            : rows[index].position_recommendation,
        };
      }
    }
    return rows;
  }

  function expandSignalCatalogTransport(value) {
    if (value.signal_transport === undefined && value.signal_catalog === undefined) {
      return value;
    }
    const catalog = isRecord(value.signal_catalog) ? value.signal_catalog : null;
    const fields = catalog && Array.isArray(catalog.fields) ? catalog.fields : [];
    const values = catalog && isRecord(catalog.values) ? catalog.values : null;
    if (
      value.signal_transport !== SIGNAL_CATALOG_TRANSPORT
      || !catalog
      || catalog.schema !== SIGNAL_CATALOG_SCHEMA
      || fields.length !== SIGNAL_CATALOG_FIELDS.length
      || fields.some((field, index) => field !== SIGNAL_CATALOG_FIELDS[index])
      || !values
      || SIGNAL_CATALOG_FIELDS.some((field) => !Array.isArray(values[field]))
    ) {
      throw new Error("snapshot_signal_catalog_invalid");
    }
    const expandRows = (rows) => {
      if (!Array.isArray(rows)) throw new Error("snapshot_signal_catalog_invalid");
      return rows.map((source) => {
        if (!isRecord(source) || !Array.isArray(source.signal_catalog_refs)) {
          throw new Error("snapshot_signal_catalog_invalid");
        }
        const references = source.signal_catalog_refs;
        if (references.length !== SIGNAL_CATALOG_FIELDS.length) {
          throw new Error("snapshot_signal_catalog_invalid");
        }
        const row = { ...source };
        delete row.signal_catalog_refs;
        SIGNAL_CATALOG_FIELDS.forEach((field, index) => {
          const reference = references[index];
          if (
            !Number.isSafeInteger(reference)
            || reference < 0
            || reference >= values[field].length
          ) {
            throw new Error("snapshot_signal_catalog_invalid");
          }
          row[field] = values[field][reference];
        });
        return row;
      });
    };
    const output = {
      ...value,
      signals: expandRows(value.signals),
      manual_attention_signals: expandRows(value.manual_attention_signals || []),
    };
    delete output.signal_catalog;
    delete output.signal_transport;
    return output;
  }

  function normalizeSnapshot(value) {
    if (!isRecord(value) || value.schema !== SCHEMA) {
      throw new Error("snapshot_schema_invalid");
    }
    value = expandSignalCatalogTransport(value);
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
    const screeningPolicy = isRecord(value.screening_policy)
      ? value.screening_policy
      : {};
    const normalizeSignal = (row) => {
      const signal = { ...row };
      delete signal.presentation_sell_only_higher_timeframe_entry_gate;
      delete signal.presentation_sibling_structure_context;
      const lifecycleStage = lifecycleStageForSignal(signal);
      if (!Object.prototype.hasOwnProperty.call(REVIEW_STAGE_ORDER, lifecycleStage)) {
        throw new Error("snapshot_lifecycle_stage_invalid");
      }
      signal.lifecycle_stage = lifecycleStage;
      const chartUrls = isRecord(signal.chart_urls) ? signal.chart_urls : null;
      if (
        chartUrls === null
        || [...FREQUENCIES].some((frequency) => (
          typeof chartUrls[frequency] !== "string"
          || !chartUrls[frequency].trim()
          || /[?&]frequency=/.test(chartUrls[frequency])
        ))
      ) {
        throw new Error("snapshot_chart_urls_invalid");
      }
      const sellOnlyDeclaration = sellOnlyEntryGateDeclaration(
        signal.higher_timeframe_risk,
        signal,
        screeningPolicy,
      );
      if (sellOnlyDeclaration) {
        signal.presentation_sell_only_higher_timeframe_entry_gate = sellOnlyDeclaration;
      }
      return signal;
    };
    const signals = value.signals
      .filter(isRecord)
      .map(normalizeSignal)
      .filter(isCurrentSelectionSignal);
    const manualAttentionSignals = Array.isArray(value.manual_attention_signals)
      ? value.manual_attention_signals
        .filter(isRecord)
        .map(normalizeSignal)
        .filter(isCurrentSelectionSignal)
      : [];
    const countsByStage = {};
    const countsByPointType = Object.fromEntries(
      POINT_TYPES.map((pointType) => [pointType, 0]),
    );
    signals.forEach((signal) => {
      const stage = text(signal.lifecycle_stage, "unknown");
      countsByStage[stage] = (countsByStage[stage] || 0) + 1;
      const pointType = text(signal.point_type, "");
      if (Object.prototype.hasOwnProperty.call(countsByPointType, pointType)) {
        countsByPointType[pointType] += 1;
      }
    });
    const sectorTriggerSignalCount = signals.filter((signal) => (
      Array.isArray(signal.selection_sources)
      && signal.selection_sources.includes("QMT_SECTOR_TRIGGER")
    )).length;
    if (signals.some((signal) => {
      const risk = isRecord(signal.higher_timeframe_risk)
        ? signal.higher_timeframe_risk
        : {};
      return sectorHigherTimeframeSourceEvidence(risk, signal).contractInvalid === true;
    })) {
      throw new Error("snapshot_sector_source_invalid");
    }
    const realtimeNotifications = normalizeRealtimeNotifications(
      value.realtime_notifications,
    );
    const usMonitor = normalizeUsMonitor(value.us_monitor);
    const contextualSignals = annotateSiblingStructureContexts(signals);
    const notificationSignals = mergeRealtimeNotifications(
      contextualSignals,
      realtimeNotifications,
    );
    const unifiedSignals = annotateSiblingStructureContexts(
      mergeUsMonitorSignals(notificationSignals, usMonitor),
    );
    return {
      ...value,
      counts_by_stage: countsByStage,
      counts_by_point_type: countsByPointType,
      presentation_signal_count: signals.length,
      sector_trigger_signal_count: sectorTriggerSignalCount,
      total_qualified_signal_count: signals.length,
      us_monitor: usMonitor,
      realtime_notifications: realtimeNotifications,
      manual_attention_signals: manualAttentionSignals,
      sectors: value.sectors.filter(isRecord).map((row) => ({ ...row })),
      signals: contextualSignals,
      unified_signals: unifiedSignals,
      data_quality: { ...value.data_quality },
      errors: Array.isArray(value.errors) ? value.errors.slice() : [],
    };
  }

  function filterSignals(signals, filters = {}) {
    const pointType = text(filters.pointType, "all");
    const lifecycle = text(filters.lifecycle, "all");
    const sectorId = text(filters.sectorId, "all");
    const market = text(filters.market, "all");
    const source = text(filters.source, "all");
    const reviewStage = text(filters.reviewStage, "all");
    const segmentState = text(filters.segmentState, "all");
    const query = text(filters.query, "").trim().toLocaleLowerCase("zh-CN");
    return (Array.isArray(signals) ? signals : []).filter((signal) => {
      if (!isRecord(signal)) return false;
      if (!isCurrentSelectionSignal(signal)) return false;
      const signalPoint = text(signal.point_type, "");
      const signalSide = text(signal.side, signalPoint.endsWith("buy") ? "buy" : signalPoint.endsWith("sell") ? "sell" : "");
      if (pointType === "buy" && signalSide !== "buy") return false;
      if (pointType === "sell" && signalSide !== "sell") return false;
      if (!["all", "buy", "sell"].includes(pointType) && signalPoint !== pointType) return false;
      if (lifecycle !== "all" && lifecycleStageForSignal(signal) !== lifecycle) return false;
      const sector = isRecord(signal.sector) ? signal.sector : {};
      const signalMarket = inferSignalMarket(signal);
      if (market !== "all" && signalMarket !== market) return false;
      // 美股不使用 A 股 QMT 板块门；选择具体板块时仍完整保留美股线索。
      if (
        sectorId !== "all"
        && signalMarket !== "us"
        && text(sector.sector_id, "unclassified") !== sectorId
      ) return false;
      const sources = Array.isArray(signal.selection_sources)
        ? signal.selection_sources.map((value) => text(value, ""))
        : [];
      if (source === "notification" && signal.realtime_notification !== true) return false;
      if (source === "screening" && signal.synthetic_notification_projection === true) return false;
      if (["attention", "holding"].includes(source) && !sources.some((value) => ["MANUAL_ATTENTION_MONITOR", "HOLDING_MONITOR", "VIRTUAL_HOLDING_MONITOR"].includes(value))) return false;
      if (source === "watchlist" && !sources.some((value) => ["ACTIVE_WATCHLIST_MONITOR", "WATCHLIST_MONITOR", "US_AUXILIARY_MONITOR"].includes(value))) return false;
      const stage = lifecycleStageForSignal(signal);
      if (reviewStage === "forming" && !["observed", "approaching"].includes(stage)) return false;
      if (reviewStage === "notified" && !["triggered", "executable"].includes(stage)) return false;
      if (reviewStage === "tracking" && !["monitoring", "active"].includes(stage)) return false;
      if (reviewStage === "closed" && !["invalidated", "closed"].includes(stage)) return false;
      const segmentEvidenceStatus = segmentDifferenceEvidenceStatusForSignal(signal);
      const segmentBoundaryStatus = segmentDifferenceBoundaryStatusForSignal(signal);
      if (segmentState === "present" && segmentEvidenceStatus !== "present") return false;
      if (segmentState === "current" && !currentSegmentDifferenceReadyForSignal(signal)) return false;
      if (
        segmentState === "historical"
        && (
          segmentEvidenceStatus !== "present"
          || !["expired", "unavailable", "unknown"].includes(segmentBoundaryStatus)
        )
      ) return false;
      if (segmentState === "absent" && segmentEvidenceStatus !== "absent") return false;
      if (!query) return true;
      return [signal.code, signal.name, sector.sector_name, POINT_LABELS[signal.point_type], signalMarket === "us" ? "美股" : "A股"]
        .map((part) => text(part, "").toLocaleLowerCase("zh-CN"))
        .some((part) => part.includes(query));
    });
  }

  function reviewPriorityForSignal(signal, observedAt = new Date()) {
    if (!isRecord(signal)) return null;
    if (signal.realtime_notification === true) {
      if (signal.notification_delivery_status === "failed") return 110;
      return 100;
    }
    const raw = signal.review_priority;
    const priority = typeof raw === "number"
      ? raw
      : typeof raw === "string" && raw.trim()
        ? Number(raw)
        : Number.NaN;
    if (Number.isFinite(priority) && priority >= 0) {
      return priority;
    }

    // 实时选股响应是有界页面投影，旧进程可能尚未携带 review_priority。
    // 此处仅镜像人工复核的排序分，不改变信号、生命周期或下单权限。
    const stage = lifecycleStageForSignal(signal);
    const risk = isRecord(signal.higher_timeframe_risk)
      ? signal.higher_timeframe_risk
      : null;
    const warmup = isRecord(signal.warmup) ? signal.warmup : null;
    if (!REVIEW_PRIORITY_LIFECYCLE_STAGES.has(stage) || !risk || !warmup) return null;
    const gates = ["market_gate", "sector_gate", "symbol_gate"]
      .map((key) => text(risk[key], "UNRESOLVED"));
    if (gates.some((gate) => !REVIEW_PRIORITY_RISK_GATES.has(gate))) return null;

    const profile = isRecord(signal.execution_profile) ? signal.execution_profile : {};
    const recommendation = text(profile.recommendation, "");
    const positionRecommendation = positionRecommendationForSignal(signal);
    const positionBlocked = positionRecommendation.status === "BLOCKED";
    const contextGrade = text(profile.context_grade, "UNRESOLVED");
    const exactGreen = !positionBlocked && (
      recommendation === "READY"
      || (!recommendation && Boolean(signal.entry_allowed || signal.exit_allowed))
    );
    const confidence = positionBlocked
      ? "LOW"
      : exactGreen
      ? "HIGH"
      : ["C", "UNRESOLVED"].includes(contextGrade)
        ? "LOW"
        : recommendation === "CAUTION" || ["observed", "triggered", "executable"].includes(stage)
          ? "MEDIUM"
          : ["formed", "armed"].includes(stage)
            ? "LOW"
            : "UNRESOLVED";
    let positionStatus = text(positionRecommendation.status, "");
    if (!REVIEW_PRIORITY_POSITION_BANDS[positionStatus]) {
      positionStatus = recommendation === "BLOCKED"
        ? "BLOCKED"
        : exactGreen
          ? "RECOMMENDED"
          : recommendation === "CAUTION"
            ? "CONDITIONAL"
            : ["WAITING_STRUCTURE", "GEOMETRY_AWAITING_CONFIRMATION", "WAITING_SEGMENT_DIFFERENCE"].includes(recommendation)
              ? "NOT_ACTIONABLE"
              : "";
    }
    const selectionSources = uniqueText(signal.selection_sources);
    const actionableSellReview = text(signal.side, "") === "sell"
      && ["triggered", "executable", "active"].includes(stage)
      && (["CONDITIONAL", "RECOMMENDED"].includes(positionStatus) || exactGreen);
    const manualAttention = selectionSources.some((source) => [
      "MANUAL_ATTENTION_MONITOR",
      "HOLDING_MONITOR",
      "VIRTUAL_HOLDING_MONITOR",
    ].includes(source));
    if (actionableSellReview && manualAttention) {
      positionStatus = "MANUAL_ATTENTION_SELL_REVIEW";
    } else if (
      actionableSellReview
      && immediateFiveMinuteSignalFreshForReview(signal, observedAt)
    ) {
      positionStatus = "STRUCTURAL_SELL_REVIEW";
    }
    const band = REVIEW_PRIORITY_POSITION_BANDS[positionStatus];
    if (!band) return null;

    // 优先级首先表达“是否值得立即人工查看”，诊断码数量只属于证据丰富度，
    // 不能因为一条真实信号携带了更多审计事实而把推荐项扣到阻断项之后。
    const stageAdjustment = {
      executable: 5,
      triggered: 5,
      observed: 3,
      armed: 3,
      formed: 2,
      approaching: 1,
    }[stage] || 0;
    const gateAdjustment = gates.filter((gate) => gate === "GREEN").length;
    const reviewPenalty = signal.monitor_only === true ? 2 : 0;
    const score = band.base
      + REVIEW_PRIORITY_CONFIDENCE[confidence]
      + (exactGreen ? 2 : 0)
      + stageAdjustment
      + gateAdjustment
      - reviewPenalty;
    return Math.min(band.max, Math.max(band.min, score));
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
    const pointOrder = new Map(
      POINT_REVIEW_ORDER.map((value, index) => [value, index]),
    );
    const reviewRank = (signal) => {
      const stage = lifecycleStageForSignal(signal);
      const base = REVIEW_STAGE_ORDER[stage] ?? Number.MAX_SAFE_INTEGER;
      const setup = isRecord(signal && signal.setup_5m) ? signal.setup_5m : {};
      // 已确认但被日线、30m 或板块环境挡住的设置，仍比同通道刚刚形成、
      // 尚在防重绘审计缓冲中的结构更确定。把它放在两者之前，避免默认选中
      // 较弱证据后让用户误以为操作确认从未出现。
      if (stage === "observed" && setup.status === "confirmed") {
        return REVIEW_STAGE_ORDER.armed + 0.5;
      }
      return base;
    };
    return (Array.isArray(signals) ? signals : []).slice().sort((left, right) => {
      const leftPriority = reviewPriorityForSignal(left);
      const rightPriority = reviewPriorityForSignal(right);
      if (leftPriority !== rightPriority) {
        if (leftPriority === null) return 1;
        if (rightPriority === null) return -1;
        return rightPriority - leftPriority;
      }
      const leftStage = reviewRank(left);
      const rightStage = reviewRank(right);
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
    // 图表是当前可见队列的投影，并非独立自选列表。若在此回退到全部信号，板块或
    // 买卖点筛选结果为空时会展示左侧已不存在的陈旧标的。因此直接清空图表，保证
    // 列表与所有图表窗口始终一致。
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

  function chartUrlsForSignal(signal, options) {
    const safeSignal = isRecord(signal) ? signal : {};
    const embedded = isRecord(options) && options.embedded === true;
    const supplied = isRecord(safeSignal.chart_urls) ? safeSignal.chart_urls : {};
    const appendQueryValue = (url, key, value) => {
      const hashIndex = url.indexOf("#");
      const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
      const hash = hashIndex >= 0 ? url.slice(hashIndex) : "";
      const separator = base.includes("?") ? (/[?&]$/.test(base) ? "" : "&") : "?";
      return `${base}${separator}${encodeURIComponent(key)}=${encodeURIComponent(value)}${hash}`;
    };
    const setQueryValue = (url, key, value) => {
      const hashIndex = url.indexOf("#");
      const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
      const hash = hashIndex >= 0 ? url.slice(hashIndex) : "";
      const queryIndex = base.indexOf("?");
      const path = queryIndex >= 0 ? base.slice(0, queryIndex) : base;
      const query = queryIndex >= 0 ? base.slice(queryIndex + 1) : "";
      const encodedKey = encodeURIComponent(key);
      const keepValue = value !== null && value !== undefined;
      const encodedValue = keepValue ? encodeURIComponent(value) : "";
      let matched = false;
      const pairs = query.split("&").filter(Boolean).reduce((result, pair) => {
        const rawKey = pair.split("=", 1)[0];
        let decodedKey = rawKey;
        try {
          decodedKey = decodeURIComponent(rawKey.replace(/\+/g, " "));
        } catch (_error) {
          // Preserve malformed unrelated entries; only normalize the owned key.
        }
        if (decodedKey !== key) {
          result.push(pair);
        } else if (!matched && keepValue) {
          result.push(`${encodedKey}=${encodedValue}`);
        }
        if (decodedKey === key) matched = true;
        return result;
      }, []);
      if (!matched && keepValue) pairs.push(`${encodedKey}=${encodedValue}`);
      const nextQuery = pairs.length ? `?${pairs.join("&")}` : "";
      return `${path}${nextQuery}${hash}`;
    };
    const withInitialSidebarState = (url) => {
      if (/[?&]chart_sidebar=/.test(url)) return url;
      return appendQueryValue(url, "chart_sidebar", "collapsed");
    };
    const withDefaultMacdStudy = (url) => {
      if (/[?&]default_study=MACD_HTF(?:&|#|$)/.test(url)) return url;
      return appendQueryValue(url, "default_study", "MACD_HTF");
    };
    const normalized = (frequency) => {
      const url = supplied[frequency];
      return setQueryValue(
        withDefaultMacdStudy(withInitialSidebarState(url)),
        "chart_embed",
        embedded ? "decision-support" : null,
      );
    };
    return {
      "d": normalized("d"),
      "30m": normalized("30m"),
      "5m": normalized("5m"),
      "1m": normalized("1m"),
    };
  }

  function setChartLayout(rootElement, requested) {
    const layout = LAYOUTS.has(requested) ? requested : "focus";
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
        ? `共 ${numberText(snapshot.sectors.length)} 个 QMT GICS3/GICS4 分层板块`
        : `${shortlisted ? "符合要求并进入扫描" : "未通过结构门槛"} · ${rank === null ? "无有效排序" : `#${numberText(rank)}`} · 强度 ${horizontalStrength}`;
      const gate = sectorId === "all"
        ? "仅按结构筛选，不使用板块涨跌幅"
        : sector.hard_block === true
          ? `暂不纳入扫描：${reasonLabel(reasonCodes[0] || "原因未提供")}`
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
    const reviewPriority = reviewPriorityForSignal(signal);
    if (reviewPriority !== null) {
      card.dataset.reviewPriority = String(reviewPriority);
    }
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
      element(documentRef, "em", "is-market", inferSignalMarket(signal) === "us" ? "美股" : "A股"),
      element(documentRef, "b", "", pointLabelForSignal(signal)),
      element(documentRef, "em", "", signalCardLifecycleLabel(signal)),
      element(documentRef, "em", "", selectionLabelForSignal(signal)),
    );
    const segmentEvidenceStatus = segmentDifferenceEvidenceStatusForSignal(signal);
    const segmentBoundaryStatus = segmentDifferenceBoundaryStatusForSignal(signal);
    const segmentDifferenceEvidenceCurrent = segmentDifferenceEvidenceCurrentForReview(signal);
    const segmentBadge = segmentEvidenceStatus === "present" ? ({
      current: ["is-segment", "1m 区间套定位 · 买入位置有效"],
      expired: ["is-segment is-historical", "历史1m定位 · 买入窗口已过"],
      unavailable: ["is-segment is-historical", "历史1m定位 · 买入边界缺失"],
      unknown: ["is-segment is-historical", "1m 区间套定位 · 边界待核对"],
      not_applicable: segmentDifferenceEvidenceCurrent
        ? ["is-segment", "1m 区间套卖出定位 · 精确位置有效"]
        : ["is-segment is-historical", "历史1m卖出定位 · 仅供复核"],
    }[segmentBoundaryStatus]) : null;
    if (segmentBadge) {
      tags.append(element(documentRef, "em", segmentBadge[0], segmentBadge[1]));
    }
    if (signal.realtime_notification === true) {
      const deliveryLabels = {
        pending: "通知待投递",
        delivered: "通知已送达",
        simulated: "通知演练",
        failed: "通知投递失败",
        expired: "通知已过期",
      };
      tags.append(element(
        documentRef,
        "em",
        "is-notification",
        deliveryLabels[signal.notification_delivery_status] || "实时通知待复核",
      ));
    }
    const siblingContextBadge = siblingStructureContextBadge(signal);
    if (siblingContextBadge) {
      tags.append(element(
        documentRef,
        "em",
        "is-structure-context",
        siblingContextBadge,
      ));
    }
    const sector = isRecord(signal.sector) ? signal.sector : {};
    const dispositionLabel = (context) => {
      const disposition = text(context && context.disposition, "待判定");
      return DISPOSITION_LABELS[disposition] || disposition;
    };
    const setup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    const fiveMinutePeriod = periodPathForSignal(signal).find(
      (period) => period.frequency === "5m",
    );
    const monitorStatusLabels = {
      monitoring: "监听正常，等待新的结构事件",
      market_closed: "当前休市，开市后自动恢复监听",
      warming_up: "多周期结构正在暖机",
      awaiting_first_run: "等待首次监听完成",
      error: "监听异常，等待下一轮恢复",
    };
    const evidence = element(documentRef, "span", "es-signal-card__evidence");
    const executionProfile = isRecord(signal.execution_profile)
      ? signal.execution_profile
      : null;
    const contextGrade = executionProfile
      ? text(executionProfile.context_grade_label, "环境待判定")
      : "";
    evidence.textContent = signal.realtime_notification === true
      ? `${text(sector.sector_name, "实时通知")} · ${realtimeNotificationTimeLabel(signal)} ${fullDateTimeText(realtimeNotificationDisplayTime(signal))} · ${realtimeNotificationPriceText(signal)} · 已进入人工复核`
      : signal.us_monitor_projection === true
        ? `${text(sector.sector_name)} · ${monitorStatusLabels[signal.us_monitor_status] || "监听状态待核对"} · 不受 A 股板块筛选`
        : `${text(sector.sector_name, "未分类")} · ${contextGrade ? `${contextGrade} · ` : ""}日线 ${dispositionLabel(signal.context_d)} · 30m ${dispositionLabel(signal.context_30m)} · 5m ${text(fiveMinutePeriod && fiveMinutePeriod.state, "未知")} · 1m ${segmentEvidenceStatus === "present" ? ({
          current: "区间套精确定位有效",
          expired: "历史定位窗口已过",
          unavailable: "历史定位边界缺失",
          unknown: "区间套定位边界待核对",
          not_applicable: segmentDifferenceEvidenceCurrent
            ? "区间套卖出精确位置有效"
            : "历史卖出定位证据（不计入当前）",
        }[segmentBoundaryStatus] || "区间套定位证据已出现") : "等待1分钟区间套（精确执行未解锁）"}`;
    const meta = element(documentRef, "span", "es-signal-card__meta");
    meta.append(element(
      documentRef,
      "time",
      "",
      signal.realtime_notification === true
        ? fullDateTimeText(realtimeNotificationDisplayTime(signal))
        : signalCardTimeText(signal),
    ));
    const invalidation = setup.invalidation_price ?? signal.structural_stop;
    const supplied = (value) => value !== null && value !== undefined && String(value).trim() !== "";
    const riskParts = [];
    const positionRecommendation = positionRecommendationForSignal(signal);
    const positionBlocked = positionRecommendation.status === "BLOCKED";
    card.classList.toggle("is-position-blocked", positionBlocked);
    if (
      !positionBlocked
      && executionProfile
      && executionProfile.recommendation === "CAUTION"
    ) {
      riskParts.push("谨慎人工复核");
    }
    if (
      !positionBlocked
      && executionProfile
      && executionProfile.hard_blocked === true
    ) {
      riskParts.push(hardBlockSummaryForSignal(signal));
    }
    const warmupState = warmupDisplayState(signal);
    if (
      signal.us_monitor_projection !== true
      && signal.synthetic_notification_projection !== true
      && warmupState.fiveMinuteConverged === false
    ) riskParts.push("5分钟暖机未收敛");
    else if (
      signal.us_monitor_projection !== true
      && signal.synthetic_notification_projection !== true
      && !warmupState.overallConverged
    ) riskParts.push("环境周期暖机存在差异");
    if (supplied(invalidation)) riskParts.push(`防守 ${text(invalidation)}`);
    if (supplied(signal.structural_stop) && String(signal.structural_stop) !== String(invalidation)) {
      riskParts.push(`止损 ${text(signal.structural_stop)}`);
    }
    const riskMultiplier = Number(signal.risk_multiplier);
    if (Number.isFinite(riskMultiplier) && riskMultiplier > 0 && riskMultiplier !== 1) {
      riskParts.push(`风险 ×${numberText(riskMultiplier)}`);
    }
    if (isRecord(signal.position_recommendation)) {
      riskParts.push(positionRecommendationLabel(signal));
    }
    const siblingContextSummary = siblingStructureContextSummary(signal);
    card.append(identity, tags, evidence);
    if (siblingContextSummary) {
      const siblingContext = element(
        documentRef,
        "span",
        "es-signal-card__structure-context",
        siblingContextSummary,
      );
      siblingContext.dataset.relation = text(
        signal.presentation_sibling_structure_context
          && signal.presentation_sibling_structure_context.relation,
        "unknown",
      );
      card.append(siblingContext);
    }
    card.append(meta);
    if (riskParts.length) {
      const risk = element(documentRef, "span", "es-signal-card__risk");
      risk.textContent = riskParts.join(" · ");
      card.append(risk);
    }
    if (typeof onSelect === "function") card.addEventListener("click", () => onSelect(signal));
    return card;
  }

  function renderSignalWorkspace(
    container,
    signals,
    selectedSignalId,
    onSelect,
    options = {},
  ) {
    if (!container || !container.ownerDocument) return null;
    const documentRef = container.ownerDocument;
    const fragment = documentRef.createDocumentFragment();
    let selectedCard = null;
    const requestedLimit = Number(options.limit);
    const limit = Number.isFinite(requestedLimit) && requestedLimit > 0
      ? Math.floor(requestedLimit)
      : signals.length;
    const visibleSignals = signals.slice(0, limit);
    for (const signal of visibleSignals) {
      const card = signalCard(
        documentRef,
        signal,
        text(signal.signal_id, "") === selectedSignalId,
        onSelect,
      );
      if (text(signal.signal_id, "") === selectedSignalId) selectedCard = card;
      fragment.append(card);
    }
    if (visibleSignals.length < signals.length) {
      const loadMore = element(documentRef, "button", "es-signal-list__more");
      loadMore.type = "button";
      loadMore.textContent = `继续显示（${visibleSignals.length} / ${signals.length}）`;
      if (typeof options.onLoadMore === "function") {
        loadMore.addEventListener("click", options.onLoadMore);
      }
      fragment.append(loadMore);
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
        ["1m", "等待区间套", "5分钟信号保留，精确执行未解锁", "未完成1分钟区间套前不生成执行比例"],
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
        blocking: "当前没有关键限制或结构冲突",
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
    setNodeText(rootElement, "[data-selected-point]", pointLabelForSignal(signal));
    setNodeText(
      rootElement,
      "[data-selected-stage]",
      lifecycleLabel(lifecycleStageForSignal(signal)),
    );
    setNodeText(rootElement, "[data-selected-tower]", "严格笔 → 线段 → 递归中枢 / 全层级");
    const selectedSetup = isRecord(signal.setup_5m) ? signal.setup_5m : {};
    setNodeText(
      rootElement,
      "[data-selected-stop]",
      text(selectedSetup.invalidation_price ?? signal.structural_stop, "未提供"),
    );
    setNodeText(
      rootElement,
      "[data-selected-risk]",
      selectedRiskReferenceLabel(signal),
    );

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
    const embeddedUrls = chartUrlsForSignal(signal, { embedded: true });
    for (const frequency of ["d", "30m", "5m", "1m"]) {
      const frame = rootElement.querySelector(`[data-chart-frame="${frequency}"]`);
      const link = rootElement.querySelector(`[data-chart-link="${frequency}"]`);
      if (frame && frame.getAttribute("src") !== embeddedUrls[frequency]) {
        frame.setAttribute("src", embeddedUrls[frequency]);
      }
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
      blocking: "当前没有关键限制或结构冲突",
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
      uniqueText(groups.raw.map(reasonLabel)),
      "当前没有补充诊断说明",
    );
  }

  return {
    LIFECYCLE_LABELS,
    POINT_LABELS,
    POINT_REVIEW_ORDER,
    POINT_TYPES,
    SCHEMA,
    annotateSiblingStructureContexts,
    chartUrlsForSignal,
    completedWithoutSignalCount,
    currentPreciseExecutionReadyForSignal,
    currentSegmentDifferenceReadyForSignal,
    dailyPreselectionDiagnosticsText,
    dailyPreselectionText,
    decisionSummaryForSignal,
    defaultFrequencyForSignal,
    evidenceGroupsForSignal,
    emptySignalDetail,
    filterSignals,
    fiveMinuteTradeSignalConfirmedForSignal,
    fullDateTimeText,
    groupSignalsBySector,
    hardBlockSummaryForSignal,
    lifecycleLabel,
    lifecycleStageForSignal,
    isCurrentSelectionSignal,
    signalQueueCountText,
    signalQueueFacts,
    setupFormationStateForSignal,
    setupLockStateForSignal,
    terminalSegmentRange,
    terminalSegmentSummary,
    manualFocusState,
    mergeRealtimeNotifications,
    mergeUsMonitorSignals,
    memberHistoryDiagnosticsText,
    normalizeSnapshot,
    normalizeRealtimeNotifications,
    normalizeUsMonitor,
    pointLabelForSignal,
    positionRecommendationLabel,
    positionRecommendationForSignal,
    segmentDifferenceEvidenceCurrentForReview,
    segmentDifferenceReadyForSignal,
    preciseExecutionReadyForSignal,
    inferSignalMarket,
    realtimeNotificationSignal,
    realtimeNotificationDisplayTime,
    realtimeNotificationPriceText,
    realtimeNotificationTimeLabel,
    reviewPriorityForSignal,
    signalAgeSecondsForReview,
    usMonitorSignal,
    renderChartWorkspace,
    renderSectorWorkspace,
    renderSignalWorkspace,
    resolveFocusState,
    resolveSelectedSignalId,
    selectionLabelForSignal,
    segmentScopeText,
    segmentDifferenceBoundaryStatusForSignal,
    segmentDifferenceEvidenceStatusForSignal,
    segmentDifferenceStatusForSignal,
    periodPathForSignal,
    priorityMonitorDiagnosticsText,
    priorityMonitorText,
    reasonLabel,
    scanCoverageText,
    scanQualityText,
    scanTimingText,
    screeningScopeFacts,
    screeningScopeLabel,
    sectorCoverageText,
    sectorEvidenceText,
    selectedSectorCount,
    setChartLayout,
    setEvidencePanelOpen,
    setTheaterMode,
    signalCardLifecycleLabel,
    signalCardTimeText,
    sortSignalsForReview,
    statusLabel,
    text,
    timeText,
  };
});
