"use strict";

(function startHumanReviewScreening() {
  const ALERT_LABELS = {
    POSSIBLE_30M_BUY: "可能的 30m 买点",
    POSSIBLE_30M_EXIT: "可能的 30m 战略退出",
    POSSIBLE_SELL_REVIEW: "卖点待人工判断（30m退出或5m短差）",
    POSSIBLE_5M_TACTICAL_SELL: "可能的 5m 短差卖出",
    POSSIBLE_5M_TACTICAL_BUYBACK: "可能的 5m 短差回补",
  };
  const CHECKLIST_LABELS = {
    HUMAN_CONFIRM_30M_CENTER_AND_LEVEL: "确认 30m 中枢及其递归级别",
    HUMAN_CONFIRM_SAME_LEVEL_AND_CENTER_DECOMPOSITION: "结合同级别分解与中枢分解",
    HUMAN_CONFIRM_30M_TREND_TYPE: "确认 30m 走势类型",
    HUMAN_CONFIRM_BUY_OR_SELL_POINT: "确认具体一、二、三类买卖点",
    HUMAN_CONFIRM_5M_TACTICAL_CONTEXT: "确认 5m 短差上下文",
    HUMAN_CONFIRM_1M_LOCATOR: "用 1m 完成精确定位",
    HUMAN_CONFIRM_HIGHER_TIMEFRAME_RISK: "核对日、周、月高级别风险",
    HUMAN_DEFINE_INVALIDATION_AND_ANY_PAPER_PLAN: "定义失效条件及模拟观察计划",
  };
  const FEEDBACK_LABELS = {
    CONFIRMED: "中枢确认", REJECTED: "中枢否决", UNCERTAIN: "不确定",
    UP: "上涨", DOWN: "下跌", CONSOLIDATION: "盘整", "30M": "30m", "5M": "5m", "1M": "1m", OTHER: "其他级别",
    BUY_1: "一买", BUY_2: "二买", BUY_3: "三买", SELL_1: "一卖", SELL_2: "二卖", SELL_3: "三卖", NONE: "不是买卖点",
    WATCH: "继续观察", REJECT: "拒绝", PAPER_OBSERVE: "模拟观察", NEEDS_MORE_DATA: "需要更多数据",
    SAME_LEVEL: "同级别分解", CENTER: "中枢分解", COMBINED: "两者结合",
  };
  const REVIEW_LANE_LABELS = {
    ACTIONABLE_REVIEW: "可行动复核",
    POSITION_MANAGEMENT: "持仓与虚拟仓管理",
    WATCHLIST: "观察池",
    RESEARCH_ARCHIVE: "研究归档",
  };
  const PAPER_STATUS_LABELS = {
    PENDING: "虚拟意图已建立，等待后续合法 1m K 线",
    OBSERVATION_ONLY: "仅保留模拟观察，不进入待成交",
    BLOCKED_BY_RISK_GATE: "高级别风险门阻断，未进入待成交",
    VIRTUAL_FILLED: "虚拟成交已记录",
    CANCELLED: "虚拟意图已撤销",
    OPERATIONS_CANCELLED: "执行数据门已撤销可放弃买入",
    EXPIRED: "买入有效期已结束",
    EXECUTION_REJECTED: "市场执行条件拒绝虚拟成交",
    CAPITAL_REJECTED: "虚拟资金或槽位约束拒绝成交",
    PORTFOLIO_REJECTED: "虚拟组合约束拒绝成交",
  };
  const PAPER_TAG_LABELS = {
    PENDING: "虚拟待成交",
    OBSERVATION_ONLY: "仅模拟观察",
    BLOCKED_BY_RISK_GATE: "风险门阻断",
    VIRTUAL_FILLED: "虚拟已成交",
    CANCELLED: "虚拟已撤销",
    OPERATIONS_CANCELLED: "执行门撤买",
    EXPIRED: "买入已过期",
    EXECUTION_REJECTED: "执行条件拒绝",
    CAPITAL_REJECTED: "资金约束拒绝",
    PORTFOLIO_REJECTED: "组合约束拒绝",
  };
  const MAPPING_SUPPLY_LABELS = {
    LOWER_STRUCTURE_UNAVAILABLE: "低级别结构不可用",
    NO_LOWER_POINT_EVIDENCE: "没有低级别买卖点证据",
    ONLY_THIRD_CLASS_POINTS: "只有三类点，缺少形成分型的一/二类卖点",
    SELL12_OUTSIDE_TOP_FRACTAL: "一/二类卖点均在本次顶分型区间外",
    SELL12_CENTER_INCOMPLETE: "分型内一/二类卖点对应中枢尚未完成",
    HIGHEST_MAPPING_NOT_UNIQUE: "最高层级中枢映射不唯一",
    UNIQUE_MAPPING: "已唯一映射",
  };
  const PAPER_REASON_LABELS = {
    HISTORICAL_SOURCE_REVIEW_ONLY: "历史回放只记录人工识别，禁止连接当前行情形成虚拟意图",
    CURRENT_PAPER_SOURCE_UNAVAILABLE: "当前盘中/前向证据不可用，虚拟路径失败关闭",
    SOURCE_SUPERSEDED_FOR_PAPER: "该报告已被更新快照替代，只允许留存人工识别",
    SOURCE_MARKET_SESSION_UNAVAILABLE_FOR_PAPER: "来源报告缺少可验证的行情会话，虚拟路径失败关闭",
    CURRENT_MARKET_DATA_SESSION_UNAVAILABLE_FOR_PAPER: "当前行情会话水位不可验证，虚拟路径失败关闭",
    SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER: "来源归档不是当前最新行情会话，只允许因果复核与人工留痕",
    FORWARD_SCHEDULER_NOT_READY_FOR_PAPER: "前向计划任务契约未就绪，只记录人工判断，不建立虚拟意图",
    FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER: "前向计划任务观察已过期，只记录人工判断，不建立虚拟意图",
    FORWARD_OPERATIONS_CLOCK_INVALID_FOR_PAPER: "前向运行时钟不可验证，虚拟路径失败关闭",
    SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER: "尚未获得同日 QMT Capture 回执，只记录人工判断；回执就绪后可重试晋升为虚拟意图",
    QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY: "板块排序引用的精确 QMT 目录修订尚不可用，只记录人工判断，不建立新虚拟买入",
    QMT_RANKING_CATALOG_NAME_MISMATCH_FOR_PAPER_ENTRY: "板块排序名称与精确 QMT 目录不一致，禁止建立新虚拟买入",
    QMT_RANKING_CATALOG_SYMBOL_NOT_MEMBER_FOR_PAPER_ENTRY: "候选股票不属于板块排序引用的精确 QMT 目录成分，禁止建立新虚拟买入",
    QMT_RANKING_CATALOG_SECTOR_UNRESOLVED_FOR_PAPER_ENTRY: "板块排序标识无法在精确 QMT 目录中解析，禁止建立新虚拟买入",
    QMT_RANKING_CATALOG_NOT_EXACT_FOR_PAPER_ENTRY: "板块排序、名称和成分归属未在同一份 QMT 目录修订上闭环，禁止建立新虚拟买入",
    HUMAN_STRUCTURE_CONFIRMATION_INCOMPLETE: "尚未同时确认中枢和对应递归级别",
    HUMAN_TREND_TYPE_CONFIRMATION_INCOMPLETE: "人工尚未确认走势类型",
    HUMAN_CONFIRM_TREND_TYPE_BEFORE_VIRTUAL_INTENT: "建立虚拟意图前必须先完成走势类型判断",
    HUMAN_POINT_SIDE_CONTRADICTS_PROGRAM_CLUE: "人工买卖方向与程序粗筛线索相反",
    CONTRADICTORY_REVIEW_CANNOT_CREATE_VIRTUAL_INTENT: "相反方向判断只保留反馈，不生成虚拟意图",
    EXPECTED_REVIEW_LEVEL_30M_OR_5M: "该卖点线索需人工确认是 30m 退出还是 5m 短差",
    SIGNAL_LIFECYCLE_ALREADY_CONSUMED: "该结构信号生命周期已有终态结果，禁止复用",
    NEW_STRUCTURE_REQUIRED_FOR_NEW_VIRTUAL_CYCLE: "再次建立虚拟周期必须等待新的结构信号",
    FIXED_ONE_LOT_TACTICAL_TARGET_BELOW_TRADING_UNIT: "一手诊断账本下，5m 短差目标低于最小交易单位",
    TACTICAL_REVIEW_OBSERVATION_ONLY: "5m 短差当前只作人工观察，不动用 30m 战略仓",
    BUY_NOT_TRIGGERED_BY_CURRENT_QMT_SECTOR: "该买入线索不是当前 QMT 板块触发",
    MONITOR_ONLY_NEW_ENTRY_PROHIBITED: "监控补充来源不得创建新的战略买入",
    VIRTUAL_STRATEGIC_CYCLE_ALREADY_OPEN: "该标的已有虚拟战略周期",
    ONE_SECURITY_ONE_STRATEGIC_SLOT: "同一标的只允许占用一个战略槽位",
    HIGHER_TIMEFRAME_GATE_NOT_GREEN: "月/周/日高级别风险门未全部转绿",
    HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED: "板块月/周/日风险门证据未接入",
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
    WARMUP_CONVERGENCE_REQUIRED_FOR_VIRTUAL_ENTRY: "暖机双窗口尚未一致，不能建立新的虚拟买入",
    WARMUP_DIVERGENCE_IS_NOT_HUMAN_OVERRIDABLE: "暖机属于数据充分性门，不能由人工结构判断覆盖",
    BUY_EXECUTION_BOUNDARY_EVIDENCE_MISSING: "缺少自包含的确认 K 与 1m 买入边界证据",
    STRUCTURE_ANCHOR_IS_NOT_A_BUY_PRICE_CAP: "结构锚点价不能替代确认 K 最高买入价",
    BUY_ENTRY_TTL_EXPIRED_BEFORE_HUMAN_CONFIRMATION: "人工确认时买入有效期已经结束",
    NO_CAUSAL_1M_EXECUTION_BAR_REMAINS_BEFORE_TTL: "有效期内已没有可因果使用的下一根完整 1m K 线",
    NEW_STRUCTURE_REQUIRED_NO_PRICE_CHASING: "不得追价，需等待新的结构信号",
    SELL_REVIEW_HAS_NO_VIRTUAL_POSITION: "当前没有可供卖出的虚拟持仓",
    HUMAN_CONFIRMED_PAPER_OBSERVE: "人工已确认 30m 买点并申请虚拟观察",
    HUMAN_CONFIRMED_VIRTUAL_EXIT: "人工已确认 30m 战略退出",
    BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL: "买入意图在有效期内未成交",
    BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR: "首个合法 1m 执行柱已高于确认 K 最高买入价",
    OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT: "执行数据不完整，可放弃买入已撤销",
    OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE: "停牌/ST/退市等证券状态门阻断，可放弃买入已撤销",
    SECURITY_GATE_CLOSED: "证券交易状态门关闭",
    SECURITY_STATUS_INCOMPLETE: "证券交易状态证据不完整",
    INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES: "计入费用后虚拟现金不足",
    NO_FREE_VIRTUAL_STRATEGIC_SLOT: "没有空闲虚拟战略槽位",
    VIRTUAL_ENTRY_EXCEEDS_ONE_SLOT_NOTIONAL_CAP: "虚拟买入超过单槽资金上限",
    VIRTUAL_ACCOUNT_EXPOSURE_CAP_EXCEEDED: "虚拟账户总敞口超过上限",
    VIRTUAL_SYMBOL_ALREADY_OCCUPIES_STRATEGIC_SLOT: "该标的已占用虚拟战略槽位",
    SUPERSEDED_BY_LATER_HUMAN_FEEDBACK: "已由同一信号生命周期内更新的人工复核撤销",
  };

  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function timeText(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(parsed);
  }

  const WARMUP_CONVERGENCE_STATUS_LABELS = {
    STABLE_ALL_PREFIXES: "全部合格前缀稳定",
    CONVERGED_ONLY_WITH_LONGER_HISTORY: "仅较长历史开始收敛",
    NON_MONOTONIC: "非单调（双窗口可能假稳定）",
    INSUFFICIENT_PREFIXES: "合格前缀不足",
  };

  function warmupConvergenceDisclosureLine(label, evidence) {
    if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) {
      return null;
    }
    const observations = Array.isArray(evidence.observations)
      ? evidence.observations
      : [];
    const counts = observations
      .map((row) => Number(row && row.bar_count))
      .filter((value) => Number.isFinite(value) && value > 0);
    const observedCount = Number.isInteger(evidence.observation_count)
      ? evidence.observation_count
      : observations.length;
    const range = counts.length
      ? `（${Math.min(...counts)}–${Math.max(...counts)} 根日线）`
      : "";
    const status = WARMUP_CONVERGENCE_STATUS_LABELS[evidence.status]
      || text(evidence.status, "未分类");
    return `${label}多前缀暖机诊断：${status} · 合格前缀 ${observedCount} 个${range} · 仅诊断，不改变现有双窗口交易门`;
  }

  function deepWarmupDiagnosticPresentation(diagnostic) {
    if (!diagnostic || typeof diagnostic !== "object" || diagnostic.selected !== true) {
      return {
        selected: false,
        tone: "neutral",
        tag: null,
        headline: "未进入本轮有界深度暖机诊断",
        lines: ["只对现有排序中的前 16 个买入候选执行；不影响排名、风险门或模拟资格。"],
      };
    }
    const status = text(diagnostic.status, "PARTIAL");
    const frequencies = Array.isArray(diagnostic.frequencies)
      ? diagnostic.frequencies
      : [];
    const lines = frequencies.map((row) => {
      const prefixCounts = Array.isArray(row.prefix_bar_counts)
        ? row.prefix_bar_counts.join("/")
        : "—";
      const reasons = Array.isArray(row.reason_codes) && row.reason_codes.length
        ? ` · ${row.reason_codes.map(paperReasonLabel).join(" / ")}`
        : "";
      return `${text(row.frequency)}：${WARMUP_CONVERGENCE_STATUS_LABELS[row.status] || text(row.status, "不可用")} · 前缀 ${prefixCounts} · 可用 ${Number(row.available_bar_count || 0)} 根${reasons}`;
    });
    const nonMonotonic = status === "NON_MONOTONIC"
      || frequencies.some((row) => row && row.status === "NON_MONOTONIC");
    const partial = status === "PARTIAL"
      || frequencies.some((row) => row && row.status === "UNAVAILABLE");
    return {
      selected: true,
      tone: nonMonotonic || partial ? "warning" : "ok",
      tag: nonMonotonic ? "深暖机非单调" : partial ? "深暖机部分可用" : "深暖机已审计",
      headline: nonMonotonic
        ? `第 ${Number(diagnostic.rank || 0)} 位诊断候选 · 存在非单调前缀`
        : partial
          ? `第 ${Number(diagnostic.rank || 0)} 位诊断候选 · 部分周期不可用`
          : `第 ${Number(diagnostic.rank || 0)} 位诊断候选 · 多周期前缀已审计`,
      lines: lines.length
        ? lines
        : ["诊断文件已绑定，但没有可展示的周期结果。"],
    };
  }

  function mappingSupplyDisclosureLines(label, diagnostic) {
    const supply = diagnostic && diagnostic.mapping_supply
      && typeof diagnostic.mapping_supply === "object"
      && !Array.isArray(diagnostic.mapping_supply)
      ? diagnostic.mapping_supply
      : null;
    if (!supply) {
      if (diagnostic && diagnostic.state === "NONE") {
        return [`${label}映射供给：无活动顶部结构，不适用次级卖点映射`];
      }
      return [`${label}映射供给：旧证据未保存明细`];
    }
    const counts = supply.point_type_counts
      && typeof supply.point_type_counts === "object"
      && !Array.isArray(supply.point_type_counts)
      ? supply.point_type_counts
      : {};
    const lines = [
      `${label}映射供给：${MAPPING_SUPPLY_LABELS[supply.classification] || text(supply.classification, "未分类")} · 低级别点 ${Number(supply.point_evidence_count || 0)}（一卖 ${Number(counts["1sell"] || 0)} / 二卖 ${Number(counts["2sell"] || 0)} / 三卖 ${Number(counts["3sell"] || 0)} / 三买 ${Number(counts["3buy"] || 0)}）· 分型内一二卖 ${Number(supply.in_top_interval_sell12_count || 0)} / 已完成中枢 ${Number(supply.completed_in_top_interval_sell12_count || 0)}`,
    ];
    const diagnosticCounts = supply.diagnostic_buy_point_type_counts
      && typeof supply.diagnostic_buy_point_type_counts === "object"
      && !Array.isArray(supply.diagnostic_buy_point_type_counts)
      ? supply.diagnostic_buy_point_type_counts
      : null;
    if (!diagnosticCounts) {
      lines.push(`${label}方向诊断：旧证据未记录一买/二买，不能解释为真实零`);
      return lines;
    }
    const diagnosticEvidence = Array.isArray(
      supply.diagnostic_buy_point_evidence,
    ) ? supply.diagnostic_buy_point_evidence : null;
    const identityDisclosure = diagnosticEvidence === null
      ? "稳定身份未随旧证据保存"
      : `稳定身份 ${diagnosticEvidence.length} 个`;
    lines.push(
      `${label}方向诊断：一买 ${Number(diagnosticCounts["1buy"] || 0)} / 二买 ${Number(diagnosticCounts["2buy"] || 0)} · ${identityDisclosure} · 仅供人工识别，不参与卖点映射、风险门或订单`,
    );
    return lines;
  }

  function paperReasonLabel(value) {
    const reason = String(value || "PAPER_REASON_UNAVAILABLE");
    if (PAPER_REASON_LABELS[reason]) return PAPER_REASON_LABELS[reason];
    if (reason.startsWith("EXPECTED_REVIEW_LEVEL_")) {
      return `该线索要求确认 ${reason.slice("EXPECTED_REVIEW_LEVEL_".length).toLowerCase()} 级别`;
    }
    if (reason.startsWith("MARKET_GATE_")) {
      return `市场高级别风险门：${reason.slice("MARKET_GATE_".length)}`;
    }
    if (reason.startsWith("SECTOR_GATE_")) {
      return `板块高级别风险门：${reason.slice("SECTOR_GATE_".length)}`;
    }
    if (reason.startsWith("SYMBOL_GATE_")) {
      return `个股高级别风险门：${reason.slice("SYMBOL_GATE_".length)}`;
    }
    return reason;
  }

  Object.assign(PAPER_REASON_LABELS, {
    QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT: "月/周/日风险门历史不足 480 根已完成日线，已失败关闭",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED: "月/周/日风险门完整前缀与 320 根后缀结论不一致，已失败关闭",
    QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE: "月/周/日风险门双窗口复算一致",
  });

  function deferredEvidenceDisclosure(candidate, label, evidenceId) {
    if (!candidate || candidate.evidence_detail_available !== true) return null;
    const failed = candidate.evidence_detail_error;
    return {
      tag: failed ? `${label}加载失败` : `${label}按需加载`,
      lines: [
        failed
          ? `完整${label}读取失败：${text(failed)}；候选摘要仍保持 REVIEW_REQUIRED / LIVE_DISABLED。`
          : `完整${label}将在选择该候选后读取；首屏只传输已哈希绑定的轻量摘要。`,
      ],
      factIds: evidenceId ? [evidenceId] : [],
    };
  }

  function sectorSourceDisclosure(candidate) {
    const evidence = candidate
      && candidate.sector_higher_timeframe_evidence
      && typeof candidate.sector_higher_timeframe_evidence === "object"
      ? candidate.sector_higher_timeframe_evidence
      : null;
    if (!evidence) {
      const deferred = deferredEvidenceDisclosure(
        candidate,
        "板块高级别证据",
        candidate && candidate.sector_higher_timeframe_evidence_id,
      );
      if (deferred) return deferred;
      if (
        candidate
        && candidate.sector_risk_gate_attestation === "LEGACY_OMITTED_FAIL_CLOSED"
      ) {
        return {
          tag: "板块门旧归档未证明",
          lines: [
            "旧归档未单独携带板块风险门；系统仅按 UNRESOLVED 失败关闭读取，不能用于虚拟买入",
          ],
          factIds: [],
        };
      }
      return { tag: "", lines: [], factIds: [] };
    }
    const warmup = evidence.strict_same_5m_warmup_evidence
      && typeof evidence.strict_same_5m_warmup_evidence === "object"
      ? evidence.strict_same_5m_warmup_evidence
      : {};
    const strictLine = `严格5m暖机 ${warmup.converged === true ? "一致" : "未通过"} · 完整 ${Number(warmup.full_daily_bar_count || 0)} / 要求 ${Number(warmup.required_daily_bar_count || 0)} 根日线 · ${paperReasonLabel(warmup.reason_code)}`;
    const convergenceLines = [
      warmupConvergenceDisclosureLine(
        "板块当前来源",
        evidence.warmup_convergence_evidence,
      ),
      warmupConvergenceDisclosureLine(
        "严格5m同源",
        evidence.strict_same_5m_warmup_convergence_evidence,
      ),
    ].filter(Boolean);
    const states = evidence.states
      && typeof evidence.states === "object"
      && !Array.isArray(evidence.states)
      ? evidence.states
      : null;
    const diagnostics = Array.isArray(evidence.period_diagnostics)
      ? evidence.period_diagnostics
      : null;
    const hasDecisionFacts = typeof evidence.sector_id === "string"
      && Boolean(evidence.observed_at)
      && typeof evidence.gate === "string"
      && states !== null
      && diagnostics !== null;
    const decisionLines = [];
    if (hasDecisionFacts) {
      decisionLines.push(
        `板块决策身份：${evidence.sector_id} · 观测 ${timeText(evidence.observed_at)}`,
        `板块 M/W/D：${["M", "W", "D"].map((period) => (
          `${period} ${HIGHER_TIMEFRAME_STATE_LABELS[states[period]] || text(states[period], "未证明")}`
        )).join(" · ")} → ${text(evidence.gate, "UNRESOLVED")}`,
      );
      diagnostics.forEach((diagnostic) => {
        const mapping = diagnostic.mapping_unique === true
          ? `映射 ${text(diagnostic.mapped_center_id, "无活动中枢")}`
          : `映射未唯一（${(diagnostic.mapping_candidate_ids || []).length} 个候选）`;
        const label = `板块${text(diagnostic.period)}`;
        decisionLines.push(
          `${label}：已完成 ${Number(diagnostic.completed_bar_count || 0)} 根 · 证据截止 ${timeText(diagnostic.evidence_bar_end)} · ${mapping}`,
          ...mappingSupplyDisclosureLines(label, diagnostic),
        );
      });
      const reasons = Array.isArray(evidence.reason_codes)
        ? evidence.reason_codes.map(paperReasonLabel)
        : [];
      if (reasons.length) decisionLines.push(`板块原因：${reasons.join("；")}`);
    } else {
      decisionLines.push(
        "旧版板块证据仅保存来源模式与暖机；板块 M/W/D 状态、逐周期结构映射和方向诊断未认证",
      );
    }
    if (evidence.source_mode === "PAGE_PARITY_SAME_5M_BASE") {
      return {
        tag: "同一5m基底",
        lines: [
          "板块高级别来源：M/W/D 与 30m 均由同一5m基底因果派生",
          strictLine,
          ...convergenceLines,
          ...decisionLines,
        ],
        factIds: evidence.evidence_id ? [evidence.evidence_id] : [],
      };
    }
    if (
      evidence.source_mode
      === "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH"
    ) {
      return {
        tag: "原生日线研究桥（AMBER上限）",
        lines: [
          "板块高级别来源：QMT 原生日线构造 M/W/D，30m 仍由5m基底派生",
          "研究桥尚未与5m/30m非线性聚合调和 · RESEARCH_ONLY / LIVE_DISABLED · GREEN 最多降为 AMBER",
          strictLine,
          ...convergenceLines,
          `研究桥参数：${String(evidence.research_bridge_parameter_set_id || "缺失")}`,
          ...decisionLines,
        ],
        factIds: evidence.evidence_id ? [evidence.evidence_id] : [],
      };
    }
    return {
      tag: "板块来源未认证",
      lines: [`板块高级别来源模式未认证：${String(evidence.source_mode || "缺失")}`],
      factIds: evidence.evidence_id ? [evidence.evidence_id] : [],
    };
  }

  function sectorNameDisclosure(candidate) {
    const name = text(candidate && candidate.sector_name, "板块名称待映射");
    const attestation = text(
      candidate && candidate.sector_name_attestation,
      "UNRESOLVED",
    );
    const membershipAttestation = text(
      candidate && candidate.sector_membership_attestation,
      attestation,
    );
    const capturedAt = timeText(candidate && candidate.sector_name_captured_at);
    const factIds = [
      candidate && candidate.sector_name_entry_sha256,
      candidate && candidate.sector_name_catalog_revision,
    ].filter(Boolean);
    if (membershipAttestation === "POINT_IN_TIME_SAME_SESSION") {
      const exactRankingSource = candidate
        && candidate.sector_ranking_catalog_attestation
        === "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH";
      return {
        label: `${name}〔同日快照〕`,
        tag: "板块归属点时可证",
        line: `QMT板块：${name} · 同交易日且${exactRankingSource ? "排序观测" : "信号"}时点前已采集，候选股票属于该快照 · ${capturedAt}`,
        factIds,
      };
    }
    if (membershipAttestation === "SAME_SESSION_SYMBOL_NOT_MEMBER") {
      return {
        label: `${name}〔同日目录，个股非成分〕`,
        tag: "板块归属矛盾",
        line: `QMT板块：${name} · 同日快照存在，但候选股票不在该板块成员中 · ${capturedAt}`,
        factIds,
      };
    }
    if (
      membershipAttestation
      === "LATEST_CATALOG_FALLBACK_NOT_POINT_IN_TIME"
    ) {
      return {
        label: `${name}〔最新目录，非点时〕`,
        tag: "板块归属非点时",
        line: `QMT板块：${name} · 候选股票属于最新可用目录 · ${capturedAt} · 不是历史板块成分证明`,
        factIds,
      };
    }
    if (membershipAttestation === "LATEST_CATALOG_SYMBOL_NOT_MEMBER") {
      return {
        label: `${name}〔最新目录，个股非成分〕`,
        tag: "板块归属未证明",
        line: `QMT板块：${name} · 最新可用目录中候选股票不属于该板块 · ${capturedAt}`,
        factIds,
      };
    }
    if (attestation === "UNCLASSIFIED") {
      return {
        label: name,
        tag: "板块未归类",
        line: `QMT板块：${name} · 未获得可复核的行业归属`,
        factIds: [],
      };
    }
    return {
      label: name,
      tag: "板块名未证明",
      line: `QMT板块：${name} · 名称及点时归属未证明`,
      factIds,
    };
  }

  function sectorRankingDisclosure(candidate) {
    const evidence = candidate
      && candidate.sector_ranking_evidence
      && typeof candidate.sector_ranking_evidence === "object"
      ? candidate.sector_ranking_evidence
      : null;
    if (!evidence) {
      const deferred = deferredEvidenceDisclosure(
        candidate,
        "板块排序证据",
        candidate && candidate.sector_ranking_evidence_id,
      );
      if (deferred) return deferred;
      return {
        tag: "板块排序旧归档未附",
        lines: ["该候选没有自包含的板块排序证据，不能从最终名次反推排序原因"],
        factIds: [],
      };
    }
    const ordinal = Number.isInteger(evidence.ordinal)
      ? `综合第 ${evidence.ordinal} 名`
      : "未进入合格排序";
    const components = evidence.rank_components
      && typeof evidence.rank_components === "object"
      ? Object.entries(evidence.rank_components)
        .map(([name, value]) => `${name}=${Number(value)}`)
      : [];
    const structural = components.length
      ? `结构分 ${Number(evidence.rank_score || 0)}（${components.join(" / ")}）`
      : `结构分 ${Number(evidence.rank_score || 0)}（历史摘要未附分项）`;
    const strength = evidence.horizontal_strength === null
      || evidence.horizontal_strength === undefined
      ? "横向强度未解决，未以中性值替代"
      : `横向强度 ${text(evidence.horizontal_strength)} · 横向第 ${text(evidence.horizontal_rank)} 名 · 观测 ${timeText(evidence.strength_observed_at)} · 锚点 ${text(evidence.strength_anchor_session)} · 成员 ${Number(evidence.strength_member_count || 0)}`;
    const full = evidence.source_profile === "LIVE_FULL_RANKING";
    const catalogAttestation = text(
      candidate && candidate.sector_ranking_catalog_attestation,
      "NOT_APPLICABLE",
    );
    const catalogLine = {
      EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH: "排序、板块名称和候选成分归属指向同一份QMT目录修订",
      EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION: "排序引用的QMT目录修订在排序观测时点前的本地账本中不可用；未改配到同日其他快照",
      EXACT_REVISION_NAME_MISMATCH: "排序证据中的板块名称与同一QMT目录修订不一致，保持失败关闭",
      EXACT_REVISION_SYMBOL_NOT_MEMBER: "同一QMT目录修订中候选股票不属于该板块，保持失败关闭",
      EXACT_REVISION_SECTOR_ID_UNRESOLVED: "排序引用的QMT目录修订中找不到该板块ID，保持失败关闭",
    }[catalogAttestation];
    const catalogMismatch = full
      && catalogAttestation !== "NOT_APPLICABLE"
      && catalogAttestation !== "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH";
    return {
      tag: catalogMismatch
        ? "板块排序目录未闭环"
        : (full ? "板块排序分项可复核" : "板块排序摘要可复核"),
      lines: [
        `板块排序：${ordinal} · ${structural}`,
        strength,
        full
          ? "结构排序分项来自统一实时决策核心；横向强度只参与排序，不覆盖板块资格门"
          : "历史触发账本保留总分、名次和横向强度；旧账本没有分项，系统未反推补造",
        ...(catalogLine ? [catalogLine] : []),
      ],
      factIds: [
        evidence.evidence_id,
        evidence.strength_source_revision,
        evidence.strength_evidence_revision,
        evidence.sector_catalog_revision,
      ].filter(Boolean),
    };
  }

  const HIGHER_TIMEFRAME_STATE_LABELS = {
    NONE: "无活动顶部风险",
    FORMED: "顶部结构已形成",
    FORMED_UNRESOLVED: "顶部结构已形成、映射未唯一",
    PEN_RISK_CONFIRMED: "向下笔风险已确认",
    INTERMEDIATE: "顶部风险演化中",
    RESOLVED_CONTINUATION: "风险解除、延续确认",
    UNRESOLVED: "证据未解决",
  };

  function marketSymbolHigherTimeframeDisclosure(candidate) {
    const evidence = candidate
      && candidate.market_symbol_higher_timeframe_evidence
      && typeof candidate.market_symbol_higher_timeframe_evidence === "object"
      ? candidate.market_symbol_higher_timeframe_evidence
      : null;
    if (!evidence) {
      const deferred = deferredEvidenceDisclosure(
        candidate,
        "市场/个股月周日证据",
        candidate && candidate.market_symbol_higher_timeframe_evidence_id,
      );
      if (deferred) return deferred;
      return {
        tag: "M/W/D仅门色",
        lines: [
          "旧归档或研究近似仅保留最终门色；市场与个股 M/W/D 分项、完成K线数量及结构映射未证明",
        ],
        factIds: [],
      };
    }

    const sideEntries = [
      ["市场", evidence.market],
      ["个股", evidence.symbol_evidence],
    ];
    const supportCount = sideEntries.filter(([, side]) => (
      side && side.source_support && typeof side.source_support === "object"
    )).length;
    const lines = [];
    if (supportCount === 0) {
      lines.push("M/W/D结构诊断已保存；1m会话、暖机及原生日线核对未随候选保存");
    } else if (supportCount < sideEntries.length) {
      lines.push("M/W/D结构诊断已保存；市场/个股仅部分具有数据来源支持证据");
    }
    const factIds = [];
    sideEntries.forEach(([label, side]) => {
      const states = side && typeof side.states === "object" ? side.states : {};
      lines.push(
        `${label} M/W/D：${["M", "W", "D"].map((period) => (
          `${period} ${HIGHER_TIMEFRAME_STATE_LABELS[states[period]] || text(states[period], "未证明")}`
        )).join(" · ")} → ${text(side && side.gate, "UNRESOLVED")}`,
      );
      const diagnostics = side && Array.isArray(side.period_diagnostics)
        ? side.period_diagnostics
        : [];
      diagnostics.forEach((diagnostic) => {
        const mapping = diagnostic.mapping_unique === true
          ? `映射 ${text(diagnostic.mapped_center_id, "无活动中枢")}`
          : `映射未唯一（${(diagnostic.mapping_candidate_ids || []).length} 个候选）`;
        lines.push(
          `${label}${text(diagnostic.period)}：已完成 ${Number(diagnostic.completed_bar_count || 0)} 根 · 证据截止 ${timeText(diagnostic.evidence_bar_end)} · ${mapping}`,
        );
        lines.push(
          ...mappingSupplyDisclosureLines(
            `${label}${text(diagnostic.period)}`,
            diagnostic,
          ),
        );
      });
      const reasons = side && Array.isArray(side.reason_codes)
        ? side.reason_codes.map(paperReasonLabel)
        : [];
      if (reasons.length) lines.push(`${label}原因：${reasons.join("；")}`);
      const support = side && side.source_support
        && typeof side.source_support === "object"
        ? side.source_support
        : null;
      if (!support) return;
      if (support.support_id) factIds.push(support.support_id);
      const session = support.session_evidence;
      if (session && session.status === "EXACT") {
        const issues = Array.isArray(session.issues) ? session.issues : [];
        lines.push(
          issues.length
            ? `${label}1m交易日证据：${issues.map((issue) => `${text(issue.session)} ${paperReasonLabel(issue.code)}`).join("；")}`
            : `${label}1m交易日证据：精确检查完成，未发现缺日或 240 根网格异常`,
        );
      } else if (session && session.status === "UNAVAILABLE") {
        lines.push(`${label}1m交易日证据：精确受影响日期不可用，保持失败关闭解释`);
      }
      const warmup = support.warmup_evidence;
      if (warmup && typeof warmup === "object") {
        lines.push(
          `${label}M/W/D暖机：${warmup.converged === true ? "双窗口一致" : "未通过"} · 完整 ${Number(warmup.full_daily_bar_count || 0)} / 要求 ${Number(warmup.required_daily_bar_count || 0)} / 后缀 ${Number(warmup.suffix_daily_bar_count || 0)} 根 · ${paperReasonLabel(warmup.reason_code)}`,
        );
      }
      const convergenceLine = warmupConvergenceDisclosureLine(
        `${label}M/W/D`,
        support.warmup_convergence_evidence,
      );
      if (convergenceLine) lines.push(convergenceLine);
      const nativeDaily = support.native_daily_reconciliation_evidence;
      if (nativeDaily && typeof nativeDaily === "object") {
        lines.push(
          `${label}原生日线调和：与1m派生日线重合 ${Number(nativeDaily.overlap_session_count || 0)} 日（${text(nativeDaily.first_overlap_session)} 至 ${text(nativeDaily.last_overlap_session)}）· 价格差异 ${Number(nativeDaily.price_difference_count || 0)} 项 · 仅将原生日线用于1m基底之前的左侧历史`,
        );
      }
    });
    return {
      tag: (
        supportCount === sideEntries.length
          ? "M/W/D结构与来源可复核"
          : supportCount === 0
            ? "M/W/D结构可复核·来源未附"
            : "M/W/D结构可复核·来源部分"
      ),
      lines,
      factIds: [
        ...(evidence.evidence_id ? [evidence.evidence_id] : []),
        ...factIds,
      ],
    };
  }
  const FEEDBACK_VALUE_FIELDS = [
    "center_judgement",
    "trend_judgement",
    "level_judgement",
    "point_judgement",
    "decomposition_judgement",
    "center_expansion_judgement",
    "nine_segment_upgrade_judgement",
    "locator_judgement",
    "disposition",
    "notes",
  ];

  function feedbackMatchesLatest(values, latest) {
    if (!values || !latest) return false;
    return FEEDBACK_VALUE_FIELDS.every((field) => (
      String(values[field] || "") === String(latest[field] || "")
    ));
  }

  function paperEventStatus(event) {
    const kind = String((event && event.kind) || "");
    const byKind = {
      FILL: "VIRTUAL_FILLED",
      CANCEL: "CANCELLED",
      OPERATIONS_CANCEL: "OPERATIONS_CANCELLED",
      EXECUTION_REJECT: "EXECUTION_REJECTED",
      CAPITAL_REJECT: "CAPITAL_REJECTED",
      PORTFOLIO_REJECT: "PORTFOLIO_REJECTED",
    };
    const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
    return byKind[kind] || String(payload.status || kind || "NOT_REQUESTED");
  }

  function paperEventReasons(event) {
    const payload = event && event.payload && typeof event.payload === "object" ? event.payload : {};
    const values = Array.isArray(payload.reason_codes) ? [...payload.reason_codes] : [];
    if (payload.reason_code) values.push(payload.reason_code);
    return [...new Set(values.filter(Boolean).map(paperReasonLabel))];
  }

  function paperPathDecision(snapshot, candidate) {
    const events = Array.isArray(candidate && candidate.paper_events) ? candidate.paper_events : [];
    const latestEvent = events.length ? events[events.length - 1] : null;
    if (latestEvent) {
      const status = paperEventStatus(latestEvent);
      const reasons = paperEventReasons(latestEvent);
      return {
        status,
        headline: PAPER_STATUS_LABELS[status] || `虚拟账本状态：${status}`,
        reasons: reasons.length ? reasons : ["账本已记录该状态，未附加原因码"],
      };
    }
    const sourceEligible = snapshot && snapshot.paper_observation_eligible === true;
    const candidateEligible = !candidate
      || candidate.paper_observation_eligible === undefined
      || candidate.paper_observation_eligible === true;
    if (!sourceEligible || !candidateEligible) {
      const reasonCode = !sourceEligible
        ? snapshot && snapshot.paper_observation_reason
        : candidate && candidate.paper_observation_reason;
      const reasons = [paperReasonLabel(reasonCode)];
      const sourceSession = snapshot && snapshot.paper_observation_source_session;
      const currentSession = snapshot && snapshot.paper_observation_current_market_session;
      if (sourceSession && currentSession) {
        reasons.push(`来源行情会话 ${sourceSession}；当前行情会话 ${currentSession}`);
      } else if (sourceSession) {
        reasons.push(`来源行情会话 ${sourceSession}；当前行情会话尚未证明`);
      }
      return {
        status: "REVIEW_ONLY",
        headline: "仅记录人工识别，不建立虚拟意图",
        reasons,
      };
    }
    const latest = candidate && candidate.latest_feedback;
    if (!latest) {
      return {
        status: "AWAITING_HUMAN_REVIEW",
        headline: "尚未人工复核，未申请虚拟观察",
        reasons: ["先确认中枢、走势类型、30m/5m 级别和具体买卖点"],
      };
    }
    if (latest.disposition !== "PAPER_OBSERVE") {
      return {
        status: "NOT_REQUESTED",
        headline: "人工处置未申请虚拟观察",
        reasons: [`当前处置：${FEEDBACK_LABELS[latest.disposition] || latest.disposition || "未指定"}`],
      };
    }
    if (!String(latest.point_judgement || "").match(/^(BUY|SELL)_/)) {
      return {
        status: "NOT_REQUESTED",
        headline: "模拟观察尚未形成虚拟意图",
        reasons: ["必须先人工确认具体一、二、三类买卖点"],
      };
    }
    return {
      status: "FAIL_CLOSED",
      headline: "虚拟意图状态未能从账本确认",
      reasons: ["请刷新报告；在账本状态可验证前保持失败关闭"],
    };
  }

  function boot() {
    const root = document.getElementById("es-dashboard");
    const workspace = document.getElementById("hr-workspace");
    const chartWorkspace = document.getElementById("es-chart-workspace");
    if (!root || !workspace || !chartWorkspace || root.dataset.humanReviewInitialized === "true") return;
    const markoutAudit = window.HumanReviewMarkoutAudit;
    if (!markoutAudit) {
      const status = document.getElementById("hr-status");
      const detail = document.getElementById("hr-status-detail");
      if (status) status.textContent = "页面样本审计模块缺失";
      if (detail) detail.textContent = "保持 REVIEW_REQUIRED / LIVE_DISABLED，刷新资源后再复核";
      return;
    }
    root.dataset.humanReviewInitialized = "true";

    const byId = (id) => document.getElementById(id);
    const state = {
      mode: root.dataset.defaultMode === "live" ? "live" : "human-review",
      source: "latest",
      snapshot: null,
      selectedCandidateId: null,
      detailCache: new Map(),
      detailLoading: new Map(),
      query: "",
      // Start with today's actionable and open-position workload.  The lane
      // is display-only; it never suppresses source rows or changes a signal.
      alertType: "all",
      reviewLane: "focus",
      reviewState: "all",
      loading: false,
      submitting: false,
      focusedFrequency: "30m",
      formBindingKey: null,
      pendingFeedbackRequestId: null,
    };

    function forwardReasonLabel(value) {
      const reason = text(value, "FORWARD_STATUS_UNAVAILABLE");
      const labels = {
        READY: "完整",
        COVERAGE_INCOMPLETE: "全市场覆盖未完成",
        SCREENING_DISABLED: "筛选后台未启用",
        SCREENING_REVIEW_READINESS_UNAVAILABLE: "筛选复核门不可用",
        SAME_SESSION_SECTOR_CAPTURE_UNAVAILABLE_BEFORE_CLOSE: "同日盘前板块快照缺失",
        SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN: "同日板块快照回执未证明",
        REQUIRED_CAPTURE_MISSING: "当日板块快照缺失",
        SECTOR_LEDGER_UNAVAILABLE: "板块账本不可用",
        SECTOR_CAPTURE_LEDGER_INVALID: "板块账本无效",
        FORWARD_SESSION_NOT_DUE: "当前会话尚未到期",
        CAPTURE_NOT_DUE: "09:10 前，尚未到 Capture",
        NON_WEEKDAY_SESSION_NOT_DUE: "非工作日，不要求交付",
        NON_TRADING_SESSION_NOT_DUE: "权威日历确认休市，不要求交付",
        TRADING_SESSION_EVIDENCE_UNAVAILABLE: "交易日证据尚未发布，暂不判断交付缺失",
        TRADING_SESSION_EVIDENCE_INVALID: "交易日证据校验失败，已拒绝判断",
        CAPTURE_MISSING_AFTER_DUE: "Capture 到期未交付",
        CAPTURE_FAILED: "Capture 执行失败",
        CAPTURED_WAITING_FOR_EVALUATION: "Capture 已完成，等待 15:20 Evaluate",
        EVALUATION_PENDING: "Evaluate 覆盖等待窗口内",
        EVALUATION_MISSING_AFTER_DEADLINE: "Evaluate 截止后仍未归档",
        DATA_BLOCKED: "Evaluate 被数据门阻断",
        EVALUATION_BLOCKED: "Evaluate 决策归档被阻断",
        EVALUATED_WITHOUT_CAPTURE_EVENT: "Evaluate 缺少 Capture 链上证据",
        DATA_READY_EVENT_MISSING: "Evaluate 缺少完整行情数据门事件",
        FORWARD_EVENT_SEQUENCE_INVALID: "Capture、数据门与 Evaluate 顺序无效",
        CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED: "Capture 未记录实现来源，禁止 Evaluate",
        IMPLEMENTATION_CHANGED_SINCE_CAPTURE: "Capture 后源码发生变化，禁止 Evaluate",
        CURRENT_IMPLEMENTATION_PROVENANCE_UNAVAILABLE: "当前实现来源无法验证，禁止 Evaluate",
        IMPLEMENTATION_PROVENANCE_UNATTESTED: "前向事件未完整记录实现来源",
        MIXED_IMPLEMENTATION_PROVENANCE: "Capture、数据门与 Evaluate 实现来源不一致",
        CAPTURE_EVENT_EVIDENCE_UNPROVEN: "Capture 事件未绑定可验证 QMT 回执",
        DATA_READY_EVENT_EVIDENCE_INVALID: "1m/5m 完整行情证据无效",
        EVALUATION_ARTIFACTS_UNAVAILABLE: "Evaluate 不可变归档证据不可用",
        EVALUATION_VALUATION_EVIDENCE_MISSING: "Evaluate 缺少当日虚拟估值证据",
        EVALUATION_ARTIFACT_EVIDENCE_INVALID: "Evaluate 归档对象校验失败",
        FORWARD_LEDGER_INVALID: "前向哈希链账本无效",
        FORWARD_ARCHIVE_READINESS_UNAVAILABLE: "归档前置门不可用",
        FORWARD_DELIVERY_READINESS_UNAVAILABLE: "逐日交付门不可用",
        FORWARD_SCHEDULER_MONITOR_DISABLED: "前向任务契约监控未启用",
        SCHEDULED_TASK_OBSERVATION_UNAVAILABLE: "无法读取前向计划任务",
        SCHEDULED_TASK_MISSING: "前向计划任务未安装",
        SCHEDULED_TASK_DISABLED: "前向计划任务已禁用",
        SCHEDULED_TASK_PRINCIPAL_MISMATCH: "前向任务登录契约失配（需要 S4U）",
        SCHEDULED_TASK_ACTION_MISMATCH: "前向任务动作契约失配",
        SCHEDULED_TASK_RECOVERY_MISMATCH: "前向任务重试/时限契约失配",
        SCHEDULED_TASK_TRIGGER_MISMATCH: "前向任务交易日/时刻契约失配",
      };
      return labels[reason] || reason;
    }

    function setText(id, value) {
      const node = byId(id);
      if (node) node.textContent = text(value);
    }

    function setHashIdentity(id, value, label) {
      const node = byId(id);
      if (!node) return;
      const identity = markoutAudit.hashIdentity(value);
      node.textContent = identity.short;
      if (identity.attested) {
        node.title = identity.full;
        node.setAttribute("aria-label", `${label} ${identity.full}`);
        return;
      }
      node.removeAttribute("title");
      node.setAttribute("aria-label", `${label}未认证`);
    }

    function setNodeText(selector, value) {
      const node = chartWorkspace.querySelector(selector);
      if (node) node.textContent = text(value);
    }

    function replaceList(container, values, emptyText) {
      if (!container) return;
      const fragment = document.createDocumentFragment();
      const rows = Array.isArray(values) && values.length ? values : [emptyText];
      rows.forEach((value) => {
        const item = document.createElement("li");
        item.textContent = text(value);
        fragment.append(item);
      });
      container.replaceChildren(fragment);
    }

    function setMode(requested) {
      state.mode = requested === "live" ? "live" : "human-review";
      root.dataset.screeningMode = state.mode;
      const liveWorkspaces = byId("es-live-workspaces");
      if (liveWorkspaces) liveWorkspaces.classList.toggle("is-review-mode", state.mode === "human-review");
      document.querySelectorAll("[data-screening-mode]").forEach((button) => {
        const active = button.dataset.screeningMode === state.mode;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      setText(
        "hr-mode-description",
        state.mode === "human-review"
          ? "QMT 板块先行，程序提前定位，人负责确认；所有候选均为 REVIEW_REQUIRED。"
          : "查看实时结构雷达；程序信号仍是研究提示，不具备订单权限。",
      );
      window.dispatchEvent(new CustomEvent("chanlun-screening-mode-change", { detail: { mode: state.mode } }));
      if (state.mode === "human-review" && !state.snapshot) void requestSnapshot();
    }

    function setStatus(kind, title, detail) {
      const boundary = workspace.querySelector(".hr-boundary");
      if (boundary) boundary.dataset.state = kind;
      setText("hr-status", title);
      setText("hr-status-detail", detail);
    }

    function filteredCandidates() {
      const rows = state.snapshot && Array.isArray(state.snapshot.review_queue)
        ? state.snapshot.review_queue
        : [];
      const query = state.query.trim().toLowerCase();
      return rows.filter((row) => {
        const reviewed = Array.isArray(row.feedback_history) && row.feedback_history.length > 0;
        const inFocusLane = ["ACTIONABLE_REVIEW", "POSITION_MANAGEMENT"].includes(row.review_lane);
        if (state.reviewLane === "focus" && !inFocusLane) return false;
        if (state.reviewLane !== "all" && state.reviewLane !== "focus" && row.review_lane !== state.reviewLane) return false;
        if (state.alertType !== "all" && row.alert_type !== state.alertType) return false;
        if (state.reviewState === "pending" && reviewed) return false;
        if (state.reviewState === "reviewed" && !reviewed) return false;
        if (!query) return true;
        return [row.symbol, row.sector_name, row.alert_type]
          .some((value) => text(value, "").toLowerCase().includes(query));
      });
    }

    function currentCandidate() {
      if (!state.snapshot) return null;
      return state.snapshot.review_queue.find((row) => row.candidate_id === state.selectedCandidateId) || null;
    }

    async function ensureCandidateDetail(candidate) {
      if (
        !candidate
        || candidate.evidence_detail_available !== true
        || candidate.evidence_detail_loaded === true
        || !root.dataset.humanReviewDetailEndpoint
        || !state.snapshot
      ) return;
      const sourceHash = state.snapshot.source_content_sha256;
      const key = `${sourceHash}:${candidate.candidate_id}`;
      const cached = state.detailCache.get(key);
      if (cached) {
        Object.assign(candidate, cached, {
          evidence_detail_loaded: true,
          evidence_detail_loading: false,
          evidence_detail_error: null,
        });
        return;
      }
      if (state.detailLoading.has(key)) return state.detailLoading.get(key);
      candidate.evidence_detail_loading = true;
      candidate.evidence_detail_error = null;
      const pending = (async () => {
        try {
          const url = new URL(
            root.dataset.humanReviewDetailEndpoint,
            window.location.origin,
          );
          url.searchParams.set("candidate_id", candidate.candidate_id);
          url.searchParams.set("source_content_sha256", sourceHash);
          const response = await fetch(`${url.pathname}${url.search}`, {
            cache: "no-store",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          const payload = await response.json();
          const detail = payload && payload.data;
          if (
            !response.ok
            || !payload
            || payload.ok !== true
            || !detail
            || detail.schema !== "chanlun-v3-human-review-candidate-detail-web/v1"
            || detail.candidate_id !== candidate.candidate_id
            || detail.source_content_sha256 !== sourceHash
            || detail.highest_status !== "REVIEW_REQUIRED"
            || detail.automated_order_authorized !== false
            || detail.live_status !== "LIVE_DISABLED"
          ) {
            throw new Error((payload && payload.code) || "candidate_detail_failed");
          }
          state.detailCache.set(key, detail);
          Object.assign(candidate, detail, {
            evidence_detail_loaded: true,
            evidence_detail_loading: false,
            evidence_detail_error: null,
          });
        } catch (error) {
          candidate.evidence_detail_loading = false;
          candidate.evidence_detail_error = text(
            error && error.message,
            "candidate_detail_failed",
          );
        } finally {
          state.detailLoading.delete(key);
          if (state.selectedCandidateId === candidate.candidate_id) {
            renderSelected();
          }
        }
      })();
      state.detailLoading.set(key, pending);
      return pending;
    }

    function renderHeader() {
      const snapshot = state.snapshot;
      if (!snapshot) return;
      const queue = snapshot.review_queue || [];
      const symbolCount = new Set(queue.map((row) => row.symbol)).size;
      const sample = snapshot.sample || {};
      setText("hr-candidate-count", snapshot.review_queue_count);
      setText("hr-symbol-count", symbolCount);
      setText("hr-reviewed-count", snapshot.reviewed_candidate_count);
      setText("hr-virtual-intent-count", snapshot.virtual_intent_count);
      setText("hr-virtual-pending-count", snapshot.virtual_pending_intent_count);
      setText("hr-virtual-cancelled-count", snapshot.virtual_cancelled_intent_count);
      setText(
        "hr-virtual-operations-cancelled-count",
        snapshot.virtual_operations_cancelled_intent_count,
      );
      setText("hr-virtual-capital-rejected-count", snapshot.virtual_capital_rejected_intent_count);
      setText("hr-virtual-reserved-sell-quantity", snapshot.virtual_reserved_sell_quantity);
      setText("hr-virtual-fill-count", snapshot.virtual_fill_count);
      setText("hr-virtual-position-count", snapshot.virtual_open_position_count);
      const selectionEvidence = snapshot.paper_entry_selection_attestation || {};
      const selectionSource = snapshot.paper_entry_selection_source_audit || {};
      const selectionStatus = text(selectionEvidence.status, "UNAVAILABLE");
      const selectionSourceStatus = text(selectionSource.status, "UNAVAILABLE");
      const selectionLabel = selectionStatus === "COMPLETE"
        && selectionSourceStatus === "COMPLETE"
        ? `${Number(selectionEvidence.verified_catalog_binding_count || 0)}/${Number(selectionSource.required_live_ranked_buy_intent_count || 0)} 精确 QMT 目录`
        : selectionStatus === "NO_SELECTION_ATTESTATIONS"
          && selectionSourceStatus === "NO_REQUIRED_SELECTION_INTENTS"
          ? "尚无需证明的虚拟买入"
          : selectionStatus === "INCOMPLETE_CATALOG_ARCHIVE"
            ? "QMT 目录归档不完整"
            : selectionSourceStatus === "INCOMPLETE_SOURCE_ARCHIVE"
              ? "筛选来源归档不完整"
              : selectionSourceStatus === "LEGACY_UNATTESTED_LIVE_RANKED_BUY"
                ? "旧虚拟买入缺板块准入证明"
                : selectionStatus === "INVALID" || selectionSourceStatus === "INVALID"
                  ? "板块准入证据无效"
                  : "尚未验证";
      setText("hr-entry-selection-evidence-status", selectionLabel);
      const executionEvidence = snapshot.paper_execution_evidence || {};
      const executionEvidenceStatus = text(executionEvidence.status, "UNAVAILABLE");
      const executionEvidenceLabel = executionEvidenceStatus === "COMPLETE"
        ? `${Number(executionEvidence.verified_fill_count || 0)}/${Number(executionEvidence.fill_count || 0)} 完整`
        : executionEvidenceStatus === "NO_FILLS"
          ? "无虚拟成交"
          : executionEvidenceStatus === "LEGACY_FACT_ONLY"
            ? "旧成交缺少精确 1m 柱"
            : executionEvidenceStatus === "MISSING"
              ? "成交证据对象缺失"
              : executionEvidenceStatus === "INVALID"
                ? "成交证据无效"
                : "尚未验证";
      setText("hr-execution-evidence-status", executionEvidenceLabel);
      const executionRejectionEvidence = snapshot.paper_execution_rejection_evidence || {};
      const executionRejectionStatus = text(executionRejectionEvidence.status, "UNAVAILABLE");
      const executionRejectionLabel = executionRejectionStatus === "COMPLETE"
        ? `${Number(executionRejectionEvidence.verified_rejection_count || 0)}/${Number(executionRejectionEvidence.rejection_count || 0)} 完整`
        : executionRejectionStatus === "NO_REJECTIONS"
          ? "无时价拒绝"
          : executionRejectionStatus === "MISSING"
            ? "证据缺失"
            : executionRejectionStatus === "INVALID"
              ? "证据无效"
              : executionRejectionStatus;
      setText("hr-execution-rejection-evidence-status", executionRejectionLabel);
      const capitalRejectionEvidence = snapshot.paper_capital_rejection_evidence || {};
      const capitalRejectionEvidenceStatus = text(capitalRejectionEvidence.status, "UNAVAILABLE");
      const capitalRejectionEvidenceLabel = capitalRejectionEvidenceStatus === "COMPLETE"
        ? `${Number(capitalRejectionEvidence.verified_rejection_count || 0)}/${Number(capitalRejectionEvidence.rejection_count || 0)} 完整`
        : capitalRejectionEvidenceStatus === "NO_REJECTIONS"
          ? "无资金拒绝"
          : capitalRejectionEvidenceStatus === "MISSING"
            ? "缺失"
            : capitalRejectionEvidenceStatus === "INVALID"
              ? "无效"
              : capitalRejectionEvidenceStatus;
      setText("hr-capital-rejection-evidence-status", capitalRejectionEvidenceLabel);
      const capitalDecisionAudit = snapshot.paper_capital_decision_audit || {};
      const capitalDecisionAuditStatus = text(capitalDecisionAudit.status, "UNAVAILABLE");
      const capitalDecisionAuditLabel = capitalDecisionAuditStatus === "COMPLETE"
        ? `${Number(capitalDecisionAudit.verified_rejection_count || 0)}/${Number(capitalDecisionAudit.rejection_count || 0)} 已复算`
        : capitalDecisionAuditStatus === "NO_REJECTIONS"
          ? "无资金拒绝"
          : capitalDecisionAuditStatus === "INVALID"
            ? "账本前缀不一致"
            : capitalDecisionAuditStatus;
      setText("hr-capital-decision-audit-status", capitalDecisionAuditLabel);
      const portfolioDecisionAudit = snapshot.paper_portfolio_decision_audit || {};
      const portfolioDecisionAuditStatus = text(portfolioDecisionAudit.status, "UNAVAILABLE");
      const portfolioDecisionAuditLabel = portfolioDecisionAuditStatus === "COMPLETE"
        ? `${Number(portfolioDecisionAudit.verified_rejection_count || 0)}/${Number(portfolioDecisionAudit.rejection_count || 0)} 已复算`
        : portfolioDecisionAuditStatus === "NO_REJECTIONS"
          ? "无组合拒绝"
          : portfolioDecisionAuditStatus === "INVALID"
            ? "账本前缀不一致"
            : portfolioDecisionAuditStatus;
      setText("hr-portfolio-decision-audit-status", portfolioDecisionAuditLabel);
      const portfolioFillDecisionAudit = snapshot.paper_portfolio_fill_decision_audit || {};
      const portfolioFillDecisionAuditStatus = text(portfolioFillDecisionAudit.status, "UNAVAILABLE");
      const portfolioFillDecisionAuditLabel = portfolioFillDecisionAuditStatus === "COMPLETE"
        ? `${Number(portfolioFillDecisionAudit.verified_approved_fill_count || 0)}/${Number(portfolioFillDecisionAudit.approved_fill_count || 0)} 已复算`
        : portfolioFillDecisionAuditStatus === "NO_APPROVED_FILLS"
          ? "无组合成交"
          : portfolioFillDecisionAuditStatus === "INVALID"
            ? "成交裁决不一致"
            : portfolioFillDecisionAuditStatus;
      setText("hr-portfolio-fill-decision-audit-status", portfolioFillDecisionAuditLabel);
      const paperCapabilities = snapshot.paper_execution_capabilities || {};
      setText(
        "hr-signal-lifecycle-status",
        paperCapabilities.terminal_signal_lifecycle_one_shot_enforced === true
          ? "终态买卖点不可复用"
          : "未证明",
      );
      setText(
        "hr-tactical-execution-status",
        paperCapabilities.fixed_one_lot_tactical_review_only === true
          ? "固定 100 股（一手）仅观察；不覆盖多手部分成交"
          : "未证明",
      );
      setText(
        "hr-entry-price-boundary-status",
        paperCapabilities.strategic_buy_confirmation_bar_price_cap_enforced === true
          && paperCapabilities.strategic_buy_no_chase_reject_independent_of_volume === true
          && paperCapabilities.strategic_buy_entire_bar_strict_cross_enforced === true
          && paperCapabilities.strategic_buy_five_percent_bar_volume_cap_enforced === true
          && paperCapabilities.adverse_observed_bar_extreme_fill_price_enforced === true
          && paperCapabilities.completed_bar_close_fill_timestamp_enforced === true
          && paperCapabilities.strategic_buy_one_locator_bar_ttl_enforced === true
          && paperCapabilities.strategic_buy_causal_full_1m_window_prechecked === true
          && paperCapabilities.current_review_queue_raw_1m_boundaries_self_contained === true
          && paperCapabilities.structure_anchor_never_used_as_execution_cap === true
          ? "确认 K 高点 / 越价即拒 / 整柱严格穿价 / 5% / 不利极值 / 收盘确认"
          : "未证明",
      );
      setText(
        "hr-strategic-exit-persistence-status",
        paperCapabilities.persistent_strategic_sell_never_expires === true
          && paperCapabilities.persistent_sell_five_percent_bar_volume_cap_enforced === true
          && paperCapabilities.adverse_observed_bar_extreme_fill_price_enforced === true
          && paperCapabilities.completed_bar_close_fill_timestamp_enforced === true
          ? "跨日持续 / 5% 容量 / 不利极值 / 收盘确认"
          : "未证明",
      );
      setText(
        "hr-execution-chronology-status",
        paperCapabilities.optional_buy_data_fault_cancelled === true
          && paperCapabilities.optional_buy_security_gate_cancelled === true
          && paperCapabilities.execution_fact_incomplete_optional_buy_cancelled === true
          && paperCapabilities.operations_cancellation_exact_evidence_audited === true
          && paperCapabilities.persistent_exit_independent_symbol_continues === true
          && paperCapabilities.persistent_exit_security_blocked_remains_pending === true
          && paperCapabilities.persistent_exit_fact_incomplete_remains_pending === true
          && ["COMPLETE", "NO_CANCELLATIONS"].includes(
            text(
              (snapshot.paper_operations_cancellation_evidence || {}).status,
              "INVALID",
            ),
          )
          ? "执行门失败撤买，持久退出继续"
          : "未证明",
      );
      const pendingContinuity = snapshot.paper_pending_continuity || {};
      const continuityStatus = text(pendingContinuity.status, "UNPROVEN");
      const continuityLabel = continuityStatus === "NO_PENDING_INTENTS"
        ? "无待成交意图"
        : continuityStatus === "COMPLETE"
          ? `${Number(pendingContinuity.covered_intent_session_count || 0)}/${Number(pendingContinuity.required_intent_session_count || 0)} 交易日（240 根 1m/日）`
          : continuityStatus === "CAUSAL_GAPS"
            ? `${Number(pendingContinuity.gap_intent_count || 0)} 个意图缺完整 240 根 1m 网格`
            : "当前待成交集合尚无连续性回执";
      setText("hr-pending-continuity-status", continuityLabel);
      const paperAccounting = snapshot.paper_accounting || {};
      const accountingStatus = text(paperAccounting.status, "UNAVAILABLE");
      const accountingLabel = accountingStatus === "NO_FILLS"
        ? "费用与现金口径已冻结 · 无成交"
        : accountingStatus === "OPEN_POSITIONS_UNMARKED"
          ? "现金已重建 · 持仓待日终估值"
          : accountingStatus === "CLOSED_BOOK_NO_DAILY_EQUITY"
            ? "现金已重建 · 缺逐日净值"
            : accountingStatus === "EXECUTION_EVIDENCE_UNVERIFIED"
              ? "成交证据未完整验证"
              : accountingStatus === "CONSTRAINT_VIOLATION"
                ? "虚拟资金约束异常"
                : accountingStatus === "PARAMETER_SNAPSHOT_INVALID"
                  ? "冻结参数无效"
                  : "尚未建立";
      setText("hr-paper-accounting-status", accountingLabel);
      setText("hr-paper-cash-balance", text(paperAccounting.cash_balance, "—"));
      setText("hr-paper-total-fees", text(paperAccounting.total_fees, "—"));
      const paperValuation = snapshot.paper_valuation || {};
      const valuationStatus = text(paperValuation.status, "NOT_STARTED");
      const valuationSourceAvailable = paperValuation.source_provenance_available === true;
      const latestValuation = valuationStatus === "COMPLETE"
        && paperValuation.equity_curve_available === true
        ? (paperValuation.latest || {})
        : {};
      const valuationLabel = valuationStatus === "COMPLETE"
        ? `${Number(paperValuation.complete_valuation_count || 0)} 个日终净值点 · 截至 ${text(latestValuation.session, "会话未知")}（仍不可评价绩效）`
        : valuationStatus === "INVALID"
          ? "估值证据无效"
          : valuationStatus === "SOURCE_UNVERIFIED"
            ? "日终净值来源未验证"
            : valuationStatus === "CONTINUITY_UNVERIFIED"
              ? "日终净值连续性未验证"
              : valuationStatus === "INCOMPLETE_CURVE"
                ? "日终净值序列缺失"
                : valuationSourceAvailable
                  ? "估值来源已接通 · 尚无日终净值点"
                  : "估值来源未接通";
      setText("hr-paper-valuation-status", valuationLabel);
      setText("hr-paper-market-value", text(latestValuation.market_value, "—"));
      setText("hr-paper-equity", text(latestValuation.equity, "—"));
      const markout = snapshot.forward_markout || {};
      setText("hr-markout-cohort-status", markoutAudit.cohortLabel(markout));
      ["5", "10", "20"].forEach((horizon) => {
        setText(`hr-markout-${horizon}`, markoutAudit.horizonLabel(markout, horizon));
      });
      const lineage = snapshot.forward_warmup_structure_lineage || {};
      const lineageStatus = text(lineage.status, "NOT_AVAILABLE");
      const qualifiedLineageSessions = Number(lineage.qualified_session_count || 0);
      const recordedLineageSessions = Number(lineage.recorded_session_count || 0);
      const lineageEvents = Number(lineage.structure_event_count || 0);
      const lineageLabel = lineageStatus === "INVALID"
        ? "证据无效"
        : lineageStatus === "NO_QUALIFIED_SESSIONS"
          ? "0 个合格会话 · 等待完整前向日"
          : lineageStatus === "NOT_RECORDED_LEGACY"
            ? `${qualifiedLineageSessions} 个合格旧会话 · 当时未记录`
            : lineageStatus === "RECORDED_WITH_LEGACY_SESSIONS"
              ? `${recordedLineageSessions}/${qualifiedLineageSessions} 日已记录 · ${lineageEvents} 个结构事件`
              : lineageStatus === "RECORDED"
                ? `${recordedLineageSessions} 日已记录 · ${lineageEvents} 个结构事件`
                : "尚未开始逐日记录";
      setText("hr-forward-lineage-status", lineageLabel);
      setText("hr-sector-captured", timeText(snapshot.sector_catalog_captured_at));
      const receiptAudit = snapshot.sector_capture_receipts || {};
      const receiptEntries = Number(receiptAudit.entry_count || 0);
      const validReceipts = Number(receiptAudit.valid_receipt_count || 0);
      const receiptStatus = text(receiptAudit.status, "UNAVAILABLE");
      const receiptLabel = receiptStatus === "COMPLETE"
        ? `${validReceipts}/${receiptEntries} 完整`
        : receiptStatus === "LEGACY_RECEIPT_GAPS"
          ? `${validReceipts}/${receiptEntries}（历史缺口）`
          : receiptStatus === "REQUIRED_CAPTURE_MISSING"
            ? `${validReceipts}/${receiptEntries}（当日板块快照缺失）`
          : receiptStatus === "REQUIRED_RECEIPT_GAPS"
            ? `${validReceipts}/${receiptEntries}（当日回执缺失）`
            : receiptStatus === "RECEIPT_COVERAGE_UNPROVEN"
              ? `${validReceipts}/${receiptEntries}（回执未证明）`
          : receiptStatus === "INVALID_RECEIPTS_PRESENT"
              ? `${validReceipts}/${receiptEntries}（回执无效）`
              : "未建立";
      setText("hr-sector-receipts", receiptLabel);
      const forwardOperations = snapshot.forward_operations || {};
      const forwardScheduler = forwardOperations.scheduler || {};
      const qmtRuntime = forwardOperations.qmt_runtime || {};
      const archiveGate = forwardOperations.archive_gate || {};
      const delivery = forwardOperations.delivery || {};
      const screeningReady = archiveGate.screening_review_ready === true;
      const archiveReady = archiveGate.ready === true;
      const deliveryReady = delivery.ready === true;
      setText(
        "hr-qmt-runtime-status",
        qmtRuntime.ready === true
          ? "APP_RUNTIME / QMT ready"
          : forwardReasonLabel(qmtRuntime.reason_code),
      );
      setText(
        "hr-forward-scheduler-status",
        forwardScheduler.ready === true
          ? "APP_RUNTIME / schedule / retry ready"
          : forwardReasonLabel(forwardScheduler.reason_code),
      );
      setText(
        "hr-forward-screening-status",
        screeningReady
          ? "完整"
          : forwardReasonLabel(archiveGate.screening_review_reason_code),
      );
      setText(
        "hr-forward-archive-status",
        archiveReady ? "完整" : forwardReasonLabel(archiveGate.reason_code),
      );
      setText(
        "hr-forward-delivery-status",
        `${text(forwardOperations.session, "会话未确定")} · ${
          deliveryReady ? "Capture + Evaluate 已交付" : forwardReasonLabel(delivery.reason_code)
        }`,
      );
      setText(
        "hr-sample",
        snapshot.source_kind === "forward"
          ? text(sample.forward_session, "前向会话")
          : `${text(sample.effective_start)} 至 ${text(sample.requested_end)}`,
      );
      const sourceCurrentness = snapshot.source_currentness || {};
      const sourceCurrentnessLabels = {
        CURRENT: "当前行情会话",
        STALE: `已过期：${text(sourceCurrentness.source_session, "来源未知")} / 当前 ${text(sourceCurrentness.current_market_session, "未知")}`,
        CURRENT_RELEASE_SIDECAR: "当前正式研究版本",
        LEGACY_FALLBACK: "旧历史制品回退",
        UNPROVEN: "当前会话未证明",
      };
      setText(
        "hr-source-currentness",
        sourceCurrentnessLabels[sourceCurrentness.status]
          || text(sourceCurrentness.status, "待验证"),
      );
      setHashIdentity("hr-source-hash", snapshot.source_content_sha256, "来源指纹");
      setHashIdentity("hr-decision-core-id", snapshot.decision_core_id, "决策核心");
      setHashIdentity(
        "hr-decision-source-id",
        snapshot.decision_source_snapshot_id,
        "源码快照",
      );
      const virtualLedgerSummary = `虚拟账本累计 ${Number(snapshot.virtual_intent_count || 0)} 个意图 / ${Number(snapshot.virtual_fill_count || 0)} 个成交`;
      setStatus(
        "ready",
        snapshot.source_kind === "forward" ? "前向模拟候选已验证" : "最近一年历史候选已验证",
        `${snapshot.review_queue_count} 条提醒 · QMT 板块先行 · 候选报告自身零订单/零成交 · ${virtualLedgerSummary} · 图表因果锁定`,
      );
      if (snapshot.source_kind === "live") {
        setText("hr-sample", timeText(sample.market_data_as_of));
        setStatus(
          "ready",
          "盘中实时复核候选已验证并归档",
          `${snapshot.review_queue_count} 条提醒 · 30m 战略/5m 短差/1m 定位 · 候选报告自身零订单/零成交 · ${virtualLedgerSummary}`,
        );
      }
      if (sourceCurrentness.status === "LEGACY_FALLBACK") {
        setStatus(
          "warning",
          "当前正式研究版本尚未发布人审页面制品",
          "页面正明确回退到旧历史筛选报告；可做因果复核，不得把它当作当前正式回测候选。REVIEW_REQUIRED / LIVE_DISABLED",
        );
      }
      if (
        ["live", "forward"].includes(snapshot.source_kind)
        && forwardOperations.session
        && !deliveryReady
      ) {
        const deliveryStatus = text(delivery.status, "not_ready");
        setStatus(
          ["not_due", "pending"].includes(deliveryStatus) ? "pending" : "warning",
          "候选报告可人工复核，但逐日前向交付尚未完成",
          `${forwardOperations.session} · ${forwardReasonLabel(delivery.reason_code)} · REVIEW_REQUIRED / LIVE_DISABLED`,
        );
      }
      if (
        ["live", "forward"].includes(snapshot.source_kind)
        && snapshot.paper_observation_eligible !== true
      ) {
        const sourceSession = text(snapshot.paper_observation_source_session, "来源会话未知");
        const currentSession = text(snapshot.paper_observation_current_market_session, "当前会话未证明");
        setStatus(
          "warning",
          "归档可做因果复核，但不能创建新的虚拟意图",
          `${paperReasonLabel(snapshot.paper_observation_reason)} · 来源 ${sourceSession} / 当前 ${currentSession} · REVIEW_REQUIRED / LIVE_DISABLED`,
        );
      }
      const available = new Set(snapshot.source_options || []);
      const historicalOption = byId("hr-source") && byId("hr-source").querySelector('option[value="historical"]');
      if (historicalOption) historicalOption.disabled = !available.has("historical");
      const liveOption = byId("hr-source") && byId("hr-source").querySelector('option[value="live"]');
      if (liveOption) liveOption.disabled = !available.has("live");
      const forwardOption = byId("hr-source") && byId("hr-source").querySelector('option[value="forward"]');
      if (forwardOption) forwardOption.disabled = !available.has("forward");
      replaceList(
        byId("hr-data-caveats"),
        [
          ...(snapshot.data_caveats || []),
          ...(paperCapabilities.fixed_one_lot_tactical_review_only === true
            ? ["虚拟人工观察固定为 100 股（一手）；本页面不验证多手订单的部分成交行为。"]
            : []),
          ...((markout.reason_codes || []).map((value) => `筛选观察：${value}`)),
          ...(((snapshot.forward_warmup_structure_lineage || {}).reason_codes || [])
            .map((value) => `暖机谱系：${value}`)),
        ],
        "本报告未提供额外数据限制说明。",
      );
    }

    function candidateCard(candidate, selected) {
      const button = document.createElement("button");
      const reviewed = Array.isArray(candidate.feedback_history) && candidate.feedback_history.length > 0;
      button.type = "button";
      button.className = "hr-candidate-card";
      button.classList.toggle("is-selected", selected);
      button.classList.toggle("is-reviewed", reviewed);
      button.setAttribute("role", "listitem");
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.dataset.candidateId = candidate.candidate_id;

      const top = document.createElement("span");
      top.className = "hr-candidate-card__top";
      const symbol = document.createElement("strong");
      symbol.textContent = candidate.symbol;
      const priority = document.createElement("b");
      priority.textContent = `原始优先级 ${candidate.review_priority}`;
      top.append(symbol, priority);

      const alert = document.createElement("span");
      alert.className = "hr-candidate-card__alert";
      alert.textContent = ALERT_LABELS[candidate.alert_type] || candidate.alert_type;
      const sector = document.createElement("span");
      sector.className = "hr-candidate-card__sector";
      const sectorName = sectorNameDisclosure(candidate);
      sector.textContent = `${sectorName.label} · ${timeText(candidate.review_available_at)}`;
      const tags = document.createElement("span");
      tags.className = "hr-candidate-card__tags";
      const sectorSource = sectorSourceDisclosure(candidate);
      const sectorRanking = sectorRankingDisclosure(candidate);
      const higherTimeframe = marketSymbolHigherTimeframeDisclosure(candidate);
      const deepWarmup = deepWarmupDiagnosticPresentation(
        candidate.deep_warmup_diagnostic,
      );
      [
        REVIEW_LANE_LABELS[candidate.review_lane],
        candidate.confidence,
        candidate.market_risk_gate,
        candidate.sector_risk_gate,
        candidate.symbol_risk_gate,
        sectorName.tag,
        sectorRanking.tag,
        sectorSource.tag,
        higherTimeframe.tag,
      ].forEach((value) => {
        if (!value) return;
        const tag = document.createElement("span");
        tag.textContent = text(value);
        tags.append(tag);
      });
      if (deepWarmup.tag) {
        const tag = document.createElement("span");
        tag.textContent = deepWarmup.tag;
        if (deepWarmup.tone === "warning") tag.className = "is-diagnostic-warning";
        tags.append(tag);
      }
      const horizontalRank = Number(candidate.sector_horizontal_rank);
      if (Number.isInteger(horizontalRank) && horizontalRank > 0) {
        const tag = document.createElement("span");
        tag.textContent = `板块横排 ${horizontalRank}`;
        tags.append(tag);
      }
      if (reviewed) {
        const tag = document.createElement("span");
        tag.className = "is-reviewed";
        tag.textContent = `已复核 ${candidate.feedback_history.length}`;
        tags.append(tag);
      }
      const paperEvents = Array.isArray(candidate.paper_events) ? candidate.paper_events : [];
      const latestPaper = paperEvents.length ? paperEvents[paperEvents.length - 1] : null;
      if (latestPaper && latestPaper.payload) {
        const tag = document.createElement("span");
        tag.className = "is-reviewed";
        const status = paperEventStatus(latestPaper);
        tag.textContent = PAPER_TAG_LABELS[status] || `虚拟账本 ${status}`;
        tags.append(tag);
      }
      button.append(top, alert, sector, tags);
      button.addEventListener("click", () => {
        state.selectedCandidateId = candidate.candidate_id;
        state.focusedFrequency = candidate.alert_type.startsWith("POSSIBLE_5M") ? "5m" : "30m";
        render();
        void ensureCandidateDetail(candidate);
      });
      return button;
    }

    function renderQueue() {
      const rows = filteredCandidates();
      const allRows = state.snapshot ? state.snapshot.review_queue : [];
      if (!rows.some((row) => row.candidate_id === state.selectedCandidateId)) {
        state.selectedCandidateId = rows.length ? rows[0].candidate_id : null;
      }
      const fragment = document.createDocumentFragment();
      rows.forEach((candidate) => fragment.append(candidateCard(
        candidate,
        candidate.candidate_id === state.selectedCandidateId,
      )));
      byId("hr-candidate-list").replaceChildren(fragment);
      setText("hr-visible-count", `${rows.length} / ${allRows.length}`);
      byId("hr-empty").hidden = rows.length !== 0;
    }

    function appendChartDefaults(url) {
      const parsed = new URL(url, window.location.origin);
      if (!parsed.searchParams.has("chart_sidebar")) parsed.searchParams.set("chart_sidebar", "collapsed");
      if (!parsed.searchParams.has("default_study")) parsed.searchParams.set("default_study", "MACD_HTF");
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }

    function updateChartWorkspace(candidate) {
      if (!candidate) return;
      const sectorName = sectorNameDisclosure(candidate);
      const sectorSource = sectorSourceDisclosure(candidate);
      const sectorRanking = sectorRankingDisclosure(candidate);
      const higherTimeframe = marketSymbolHigherTimeframeDisclosure(candidate);
      chartWorkspace.dataset.focusedFrequency = state.focusedFrequency;
      chartWorkspace.dataset.signalSide = candidate.alert_type.includes("SELL") || candidate.alert_type.includes("EXIT") ? "sell" : "buy";
      const urls = candidate.chart_urls || {};
      ["30m", "5m", "1m"].forEach((frequency) => {
        const raw = urls[frequency];
        if (!raw) return;
        const url = appendChartDefaults(raw);
        const frame = chartWorkspace.querySelector(`[data-chart-frame="${frequency}"]`);
        const link = chartWorkspace.querySelector(`[data-chart-link="${frequency}"]`);
        if (frame && frame.getAttribute("src") !== url) frame.setAttribute("src", url);
        if (link) link.setAttribute("href", url);
      });
      const workbench = chartWorkspace.querySelector("[data-chart-workbench]");
      if (workbench && urls[state.focusedFrequency]) workbench.setAttribute("href", appendChartDefaults(urls[state.focusedFrequency]));
      chartWorkspace.querySelectorAll("[data-focus-frequency]").forEach((button) => {
        const active = button.dataset.focusFrequency === state.focusedFrequency;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });

      setNodeText("[data-selected-code]", candidate.symbol);
      setNodeText("[data-selected-name]", sectorName.label);
      setNodeText("[data-selected-point]", ALERT_LABELS[candidate.alert_type] || candidate.alert_type);
      setNodeText("[data-selected-stage]", "待人工识别");
      setNodeText("[data-selected-tower]", "30m / 5m / 1m 因果复核");
      setNodeText("[data-selected-stop]", candidate.structural_invalidation_price);
      setNodeText("[data-decision-invalidation]", candidate.structural_invalidation_price);
      setNodeText(
        "[data-selected-risk]",
        `${candidate.market_risk_gate} / ${candidate.sector_risk_gate} / ${candidate.symbol_risk_gate} · ${sectorRanking.tag}${sectorSource.tag ? ` · ${sectorSource.tag}` : ""} · ${higherTimeframe.tag}`,
      );
      setNodeText("[data-decision-title]", "程序仅定位，等待人工确认");
      setNodeText("[data-decision-detail]", `图表已锁定在 ${timeText(candidate.review_available_at)}，不会产生订单。`);
      const decision = chartWorkspace.querySelector("[data-decision-card]");
      if (decision) decision.dataset.tone = "waiting";

      const periods = {
        "30m": ["人工判定", "确认中枢、走势类型及战略买卖点", `可见至 ${timeText(candidate.review_available_at)}`],
        "5m": ["辅助定位", "判断次级别离开、回抽与短差上下文", `失效 ${text(candidate.structural_invalidation_price)}`],
        "1m": ["精确定位", "只用已完成 1m K 线寻找细部位置", "禁止使用 tick 与未来 K 线"],
      };
      Object.entries(periods).forEach(([frequency, values]) => {
        const node = chartWorkspace.querySelector(`[data-period-node="${frequency}"]`);
        if (node) {
          node.dataset.tone = "waiting";
          node.setAttribute("aria-pressed", frequency === state.focusedFrequency ? "true" : "false");
        }
        setNodeText(`[data-period-state="${frequency}"]`, values[0]);
        setNodeText(`[data-period-summary="${frequency}"]`, values[1]);
        setNodeText(`[data-period-boundary="${frequency}"]`, values[2]);
      });

      const checklist = (candidate.review_checklist || []).map((code) => CHECKLIST_LABELS[code] || code);
      const warnings = candidate.warning_codes || [];
      const warningLabels = warnings.map(paperReasonLabel);
      replaceList(chartWorkspace.querySelector('[data-evidence-group="established"]'), [
        sectorName.line,
        ...sectorRanking.lines,
        `候选报告与参数身份已验证：${text(candidate.candidate_id)}`,
        `参考价：${text(candidate.reference_price)}`,
      ], "没有程序成立证据");
      replaceList(chartWorkspace.querySelector('[data-evidence-group="missing"]'), checklist, "无待人工确认项");
      replaceList(chartWorkspace.querySelector('[data-evidence-group="blocking"]'), warningLabels, "没有程序硬阻断");
      replaceList(chartWorkspace.querySelector('[data-evidence-group="next"]'), ["完成人工识别表单", "必要时进入模拟观察，禁止直接交易"], "等待复核");
      replaceList(chartWorkspace.querySelector('[data-evidence-group="risk"]'), [
        `市场风险门：${candidate.market_risk_gate}`,
        `板块风险门：${candidate.sector_risk_gate}`,
        `个股风险门：${candidate.symbol_risk_gate}`,
        ...higherTimeframe.lines,
        ...sectorSource.lines,
        `结构失效价：${text(candidate.structural_invalidation_price)}`,
      ], "风险边界未提供");
      replaceList(
        chartWorkspace.querySelector("[data-raw-evidence]"),
        [...new Set([
          ...(candidate.source_fact_ids || []),
          ...sectorName.factIds,
          ...sectorRanking.factIds,
          ...higherTimeframe.factIds,
          ...sectorSource.factIds,
        ])],
        "无来源事实",
      );
      setNodeText(
        "[data-evidence-count]",
        checklist.length
          + warnings.length
          + 1
          + sectorRanking.lines.length
          + higherTimeframe.lines.length
          + sectorSource.lines.length,
      );
      const evidenceToggle = chartWorkspace.querySelector("[data-evidence-toggle]");
      const theaterToggle = chartWorkspace.querySelector("[data-theater-toggle]");
      if (evidenceToggle) evidenceToggle.disabled = false;
      if (theaterToggle) theaterToggle.disabled = false;
    }

    function renderFeedbackHistory(candidate) {
      const container = byId("hr-feedback-history");
      const history = candidate && Array.isArray(candidate.feedback_history) ? candidate.feedback_history : [];
      setText("hr-feedback-count", history.length);
      const fragment = document.createDocumentFragment();
      history.slice().reverse().forEach((row) => {
        const article = document.createElement("article");
        article.className = "hr-feedback-entry";
        const heading = document.createElement("strong");
        heading.textContent = `${timeText(row.reviewed_at)} · ${FEEDBACK_LABELS[row.disposition] || row.disposition}`;
        const decisions = document.createElement("p");
        const primary = [
          row.center_judgement,
          row.trend_judgement,
          row.level_judgement,
          row.point_judgement,
          row.decomposition_judgement || "UNCERTAIN",
        ].map((value) => FEEDBACK_LABELS[value] || value);
        const binaryLabel = (value) => ({
          CONFIRMED: "确认", REJECTED: "否决", UNCERTAIN: "不确定",
        }[value || "UNCERTAIN"] || value || "不确定");
        decisions.textContent = [
          ...primary,
          `扩展 ${binaryLabel(row.center_expansion_judgement)}`,
          `九段 ${binaryLabel(row.nine_segment_upgrade_judgement)}`,
          `定位 ${binaryLabel(row.locator_judgement)}`,
        ].join(" · ");
        article.append(heading, decisions);
        if (row.notes) {
          const notes = document.createElement("p");
          notes.textContent = row.notes;
          article.append(notes);
        }
        fragment.append(article);
      });
      if (!history.length) {
        const empty = document.createElement("p");
        empty.textContent = "尚无人工复核记录。";
        fragment.append(empty);
      }
      container.replaceChildren(fragment);
    }

    function applyLatestFeedback(candidate) {
      const form = byId("hr-feedback-form");
      if (!form) return;
      const latest = candidate && candidate.latest_feedback;
      const bindingKey = candidate
        ? `${candidate.candidate_id}:${latest && latest.feedback_id ? latest.feedback_id : "new"}`
        : "none";
      if (state.formBindingKey === bindingKey) return;
      state.pendingFeedbackRequestId = null;
      const defaults = {
        center_judgement: "UNCERTAIN", trend_judgement: "UNCERTAIN", level_judgement: "UNCERTAIN",
        point_judgement: "UNCERTAIN", decomposition_judgement: "UNCERTAIN",
        center_expansion_judgement: "UNCERTAIN", nine_segment_upgrade_judgement: "UNCERTAIN",
        locator_judgement: "UNCERTAIN", disposition: "WATCH", notes: "",
      };
      Object.entries(latest || defaults).forEach(([name, value]) => {
        const control = form.elements.namedItem(name);
        if (control && Object.prototype.hasOwnProperty.call(defaults, name)) control.value = value || defaults[name];
      });
      if (!latest) {
        Object.entries(defaults).forEach(([name, value]) => {
          const control = form.elements.namedItem(name);
          if (control) control.value = value;
        });
      }
      state.formBindingKey = bindingKey;
    }

    function renderSelected() {
      const candidate = currentCandidate();
      const form = byId("hr-feedback-form");
      const fieldset = byId("hr-feedback-fields");
      if (fieldset) fieldset.disabled = !candidate || state.submitting;
      if (!candidate) {
        setText("hr-selected-sector", "请选择候选");
        setText("hr-selected-symbol", "—");
        setText("hr-selected-alert", "等待选择");
        replaceList(byId("hr-checklist"), [], "请选择候选");
        replaceList(byId("hr-warnings"), [], "请选择候选");
        setText("hr-deep-warmup-status", "请选择候选");
        replaceList(byId("hr-deep-warmup"), [], "请选择候选");
        setText("hr-paper-path-status", "请选择候选");
        replaceList(byId("hr-paper-path-reasons"), [], "请选择候选");
        renderFeedbackHistory(null);
        return;
      }
      if (
        candidate.evidence_detail_available === true
        && candidate.evidence_detail_loaded !== true
        && candidate.evidence_detail_loading !== true
      ) {
        void ensureCandidateDetail(candidate);
      }
      const sectorName = sectorNameDisclosure(candidate);
      setText("hr-selected-sector", `${sectorName.label} · ${candidate.confidence}`);
      setText("hr-selected-symbol", candidate.symbol);
      setText("hr-selected-alert", ALERT_LABELS[candidate.alert_type] || candidate.alert_type);
      setText(
        "hr-selected-priority",
        `${REVIEW_LANE_LABELS[candidate.review_lane] || "复核队列"} · 原始优先级 ${candidate.review_priority}`,
      );
      setText("hr-review-at", timeText(candidate.review_available_at));
      setText("hr-reference-price", candidate.reference_price);
      setText("hr-entry-price-cap", candidate.entry_price_cap);
      setText(
        "hr-entry-confirmed-at",
        timeText(candidate.entry_confirmation_bar_closed_at),
      );
      setText("hr-entry-valid-until", timeText(candidate.entry_valid_until));
      setText("hr-entry-attestation", candidate.entry_boundary_attestation);
      setText("hr-invalidation-price", candidate.structural_invalidation_price);
      setText("hr-market-risk", candidate.market_risk_gate);
      setText("hr-sector-risk", candidate.sector_risk_gate);
      setText("hr-symbol-risk", candidate.symbol_risk_gate);
      replaceList(byId("hr-checklist"), (candidate.review_checklist || []).map((code) => CHECKLIST_LABELS[code] || code), "无清单");
      replaceList(
        byId("hr-warnings"),
        (candidate.warning_codes || []).map(paperReasonLabel),
        "无程序警告",
      );
      const deepWarmup = deepWarmupDiagnosticPresentation(
        candidate.deep_warmup_diagnostic,
      );
      setText("hr-deep-warmup-status", deepWarmup.headline);
      replaceList(byId("hr-deep-warmup"), deepWarmup.lines, "尚无诊断证据");
      applyLatestFeedback(candidate);
      renderFeedbackHistory(candidate);
      const paperDecision = paperPathDecision(state.snapshot, candidate);
      setText("hr-paper-path-status", paperDecision.headline);
      replaceList(byId("hr-paper-path-reasons"), paperDecision.reasons, "未提供虚拟路径原因");
      const paperEligible = candidate.paper_observation_eligible === true;
      const disposition = form && form.elements ? form.elements.disposition : null;
      const paperOption = disposition
        ? disposition.querySelector('option[value="PAPER_OBSERVE"]')
        : null;
      // PAPER_OBSERVE is a human judgement, not an order authorization. It
      // remains recordable while the scheduler/Capture gate is red; the
      // backend hashes the feedback but withholds all virtual-intent changes.
      if (paperOption) paperOption.disabled = false;
      setText(
        "hr-feedback-status",
        paperEligible
          ? (candidate.latest_feedback ? "已载入最近一次复核，可修改后追加新记录。" : "未复核；确认后可进入虚拟观察。")
          : `当前候选只允许记录人工识别结果，不会创建新虚拟意图：${paperReasonLabel(candidate.paper_observation_reason || state.snapshot.paper_observation_reason)}。`,
      );
      if (candidate.latest_feedback && paperEligible) {
        setText("hr-feedback-status", `已载入最近复核；${paperDecision.headline}；LIVE_DISABLED。`);
      }
      if (candidate.paper_reconciliation_pending === true) {
        setText(
          "hr-feedback-status",
          candidate.paper_reconciliation_eligible === true
            ? "此前的模拟观察判断已留痕，运行证据现已就绪；保持表单不变再次保存，将以同一请求 ID 幂等补建虚拟意图。LIVE_DISABLED。"
            : `模拟观察判断已留痕，正在等待运行证据恢复；恢复后保持表单不变再次保存即可幂等补建虚拟意图：${paperReasonLabel(candidate.paper_observation_reason || state.snapshot.paper_observation_reason)}。`,
        );
      }
      updateChartWorkspace(candidate);
    }

    function render() {
      if (!state.snapshot) return;
      renderHeader();
      renderQueue();
      renderSelected();
    }

    async function requestSnapshot() {
      if (state.loading) return;
      state.loading = true;
      const refresh = byId("hr-refresh");
      if (refresh) refresh.disabled = true;
      setStatus("loading", "正在验证人工复核报告", "核对候选报告自身零订单/零成交边界、QMT 板块快照与反馈链；虚拟账本另行验证");
      try {
        const url = new URL(root.dataset.humanReviewEndpoint, window.location.origin);
        url.searchParams.set("source", state.source);
        const response = await fetch(`${url.pathname}${url.search}`, {
          cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" },
        });
        const payload = await response.json();
        if (!response.ok || !payload || payload.ok !== true || payload.data.schema !== root.dataset.humanReviewSchema) {
          throw new Error((payload && payload.code) || "human_review_snapshot_failed");
        }
        state.snapshot = payload.data;
        render();
      } catch (error) {
        setStatus("error", "人工复核队列暂不可用", text(error && error.message, "报告未通过安全验证"));
        console.error("human_review_snapshot_failed", error && error.name ? error.name : "Error");
      } finally {
        state.loading = false;
        if (refresh) refresh.disabled = false;
      }
    }

    async function submitFeedback(event) {
      event.preventDefault();
      if (state.submitting || !state.snapshot) return;
      const candidate = currentCandidate();
      const form = event.currentTarget;
      if (!candidate || !form.reportValidity()) return;
      state.submitting = true;
      const fieldset = byId("hr-feedback-fields");
      if (fieldset) fieldset.disabled = true;
      setText("hr-feedback-status", "正在写入哈希反馈账本…");
      const values = Object.fromEntries(new FormData(form).entries());
      if (!state.pendingFeedbackRequestId) {
        const retryRequestId = candidate.paper_reconciliation_pending === true
          && candidate.latest_feedback
          && feedbackMatchesLatest(values, candidate.latest_feedback)
          && candidate.latest_feedback.request_id
          ? String(candidate.latest_feedback.request_id)
          : null;
        const unique = window.crypto && typeof window.crypto.randomUUID === "function"
          ? window.crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        state.pendingFeedbackRequestId = retryRequestId || `human-review-${unique}`;
      }
      try {
        const response = await fetch(root.dataset.humanFeedbackEndpoint, {
          method: "POST",
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            ...values,
            candidate_id: candidate.candidate_id,
            source_content_sha256: state.snapshot.source_content_sha256,
            request_id: state.pendingFeedbackRequestId,
          }),
        });
        const payload = await response.json();
        if (!response.ok || !payload || payload.ok !== true) throw new Error((payload && payload.code) || "feedback_write_failed");
        state.pendingFeedbackRequestId = null;
        setText("hr-feedback-status", "复核记录已保存；LIVE_DISABLED，未产生任何订单。正在刷新反馈链…");
        await requestSnapshot();
        const superseded = payload.data && Array.isArray(payload.data.superseded_paper_intents)
          ? payload.data.superseded_paper_intents
          : [];
        const refreshedCandidate = currentCandidate();
        const paperDecision = paperPathDecision(state.snapshot, refreshedCandidate);
        const cancellationStatus = superseded.length
          ? `；已追加撤销 ${superseded.length} 个旧待成交虚拟意图`
          : "";
        const feedbackHeadline = refreshedCandidate
          && refreshedCandidate.paper_reconciliation_pending === true
          ? "人工判断已留痕，虚拟意图尚未建立；运行证据恢复后保持表单不变再次保存即可幂等补建"
          : paperDecision.headline;
        setText(
          "hr-feedback-status",
          `复核已保存；${feedbackHeadline}${cancellationStatus}；LIVE_DISABLED，未产生真实订单或撤单。`,
        );
      } catch (error) {
        setText("hr-feedback-status", `保存失败：${text(error && error.message, "请重试")}`);
      } finally {
        state.submitting = false;
        if (fieldset) fieldset.disabled = !currentCandidate();
      }
    }

    document.querySelectorAll("[data-screening-mode]").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.screeningMode));
    });
    byId("hr-source").addEventListener("change", (event) => {
      state.source = event.target.value || "latest";
      void requestSnapshot();
    });
    byId("hr-refresh").addEventListener("click", () => void requestSnapshot());
    byId("hr-search").addEventListener("input", (event) => { state.query = event.target.value; render(); });
    byId("hr-lane-filter").addEventListener("change", (event) => { state.reviewLane = event.target.value; render(); });
    byId("hr-alert-filter").addEventListener("change", (event) => { state.alertType = event.target.value; render(); });
    byId("hr-review-filter").addEventListener("change", (event) => { state.reviewState = event.target.value; render(); });
    byId("hr-feedback-form").addEventListener("submit", submitFeedback);
    chartWorkspace.querySelectorAll("[data-focus-frequency], [data-period-node]").forEach((button) => {
      button.addEventListener("click", () => {
        if (state.mode !== "human-review") return;
        state.focusedFrequency = button.dataset.focusFrequency || button.dataset.periodNode || "30m";
        renderSelected();
      });
    });

    setMode(state.mode);
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      sectorNameDisclosure,
      sectorRankingDisclosure,
      sectorSourceDisclosure,
      marketSymbolHigherTimeframeDisclosure,
      mappingSupplyDisclosureLines,
      warmupConvergenceDisclosureLine,
      deepWarmupDiagnosticPresentation,
      deferredEvidenceDisclosure,
      paperPathDecision,
      text,
      timeText,
    };
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
    else boot();
  }
})();
