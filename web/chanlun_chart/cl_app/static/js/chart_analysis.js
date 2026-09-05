(function (root, factory) {
  'use strict';

  const api = factory(root);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ChartAnalysis = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const MMD_LABELS = Object.freeze({
    '1buy': '一类买点',
    '2buy': '二类买点',
    '3buy': '三类买点',
    '1sell': '一类卖点',
    '2sell': '二类卖点',
    '3sell': '三类卖点',
  });

  const BC_LABELS = Object.freeze({
    pz: '盘整背驰',
    qs: '趋势背驰',
    consolidation: '盘整背驰',
    trend: '趋势背驰',
    bi: '笔背驰',
    xd: '线段背驰',
  });

  const MARKET_TIMEZONES = Object.freeze({
    a: 'Asia/Shanghai',
    hk: 'Asia/Shanghai',
    futures: 'Asia/Shanghai',
    fx: 'Asia/Shanghai',
    us: 'America/New_York',
    ny_futures: 'America/New_York',
  });

  const DIRECTION_LABELS = Object.freeze({
    up: '向上',
    down: '向下',
    zd: '震荡',
  });

  const FORMAL_DIRECTION_REASON_LABELS = Object.freeze({
    formal_units_unavailable: '当前级别没有可用结构单元',
    current_suffix_has_no_formal_trend: '当前尾部尚未形成同级走势类型',
    current_suffix_is_outside_formal_trend: '当前尾部已经超出上一条正式走势',
    current_formal_movement_is_consolidation: '当前同级走势为盘整',
    current_formal_trend_has_ended: '上一条正式走势已经结束',
    direction_change_lacks_first_or_second_point: '几何方向虽已反转，但尚无同级一类或小转大二类点支撑',
    no_formal_level_reaches_current_suffix: '尚无递归级别覆盖当前结构尾部',
    current_directional_trend: '当前尾部由同级双中枢趋势覆盖',
    direction_change_supported_by_first_or_second_point: '本次反转已由同级一类或小转大二类点确认',
  });

  const LAYER_CONFIG_KEYS = Object.freeze({
    bi: { key: 'bi' },
    xd: { key: 'xd' },
  });

  function numeric(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function toSeconds(value) {
    const number = numeric(value);
    if (number === null) return null;
    return Math.abs(number) >= 100000000000 ? Math.round(number / 1000) : Math.round(number);
  }

  function formatPrice(value) {
    const number = numeric(value);
    if (number === null) return '--';
    const absolute = Math.abs(number);
    let minimumFractionDigits = 2;
    let maximumFractionDigits = 4;
    if (absolute >= 1000) maximumFractionDigits = 2;
    if (absolute > 0 && absolute < 1) maximumFractionDigits = 6;
    try {
      return number.toLocaleString('zh-CN', {
        minimumFractionDigits,
        maximumFractionDigits,
      });
    } catch (_) {
      return number.toFixed(maximumFractionDigits);
    }
  }

  function formatSignedPercent(current, reference) {
    const currentNumber = numeric(current);
    const referenceNumber = numeric(reference);
    if (currentNumber === null || referenceNumber === null || referenceNumber === 0) return '--';
    const percent = ((currentNumber - referenceNumber) / Math.abs(referenceNumber)) * 100;
    const sign = percent >= 0 ? '+' : '';
    return `${sign}${percent.toFixed(2)}%`;
  }

  function formatResolution(value) {
    const resolution = String(value || '').trim().toUpperCase();
    const higher = {
      '120': '2小时',
      '180': '3小时',
      '240': '4小时',
      '360': '6小时',
      '480': '8小时',
      '720': '12小时',
      '1D': '日线',
      '2D': '2日线',
      '3D': '3日线',
      '1W': '周线',
      '1M': '月线',
      '3M': '季线',
      '12M': '年线',
    };
    if (higher[resolution]) return higher[resolution];
    if (/^\d+S$/.test(resolution)) return `${resolution.slice(0, -1)} 秒`;
    if (/^\d+$/.test(resolution)) return `${Number(resolution)} 分钟`;
    return resolution || '--';
  }

  function formatTimestamp(value, timeZone) {
    const seconds = toSeconds(value);
    if (seconds === null) return '--';
    try {
      const parts = new Intl.DateTimeFormat('zh-CN', {
        timeZone: timeZone || undefined,
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).formatToParts(new Date(seconds * 1000));
      const values = {};
      parts.forEach((part) => { values[part.type] = part.value; });
      return `${values.month}-${values.day} ${values.hour}:${values.minute}`;
    } catch (_) {
      return new Date(seconds * 1000).toISOString().slice(5, 16).replace('T', ' ');
    }
  }

  function shapePoints(shape) {
    if (!shape) return [];
    if (Array.isArray(shape.points)) return shape.points.filter(Boolean);
    return shape.points ? [shape.points] : [];
  }

  function shapeTime(shape) {
    const points = shapePoints(shape);
    let latest = null;
    points.forEach((item) => {
      const value = toSeconds(item && item.time);
      if (value !== null && (latest === null || value > latest)) latest = value;
    });
    return latest;
  }

  function latestShape(items) {
    let latest = null;
    let latestTime = null;
    (Array.isArray(items) ? items : []).forEach((item) => {
      const time = shapeTime(item);
      if (time !== null && (latestTime === null || time > latestTime)) {
        latest = item;
        latestTime = time;
      }
    });
    return latest;
  }

  function summarizeLine(items) {
    const item = latestShape(items);
    const points = shapePoints(item);
    if (!item || points.length < 2) {
      return {
        exists: false,
        text: '尚未形成',
        meta: '等待至少两个有效端点',
        direction: '',
        status: '',
        startPrice: null,
        endPrice: null,
      };
    }
    const startPrice = numeric(points[0].price);
    const endPrice = numeric(points[points.length - 1].price);
    let direction = '横向';
    if (startPrice !== null && endPrice !== null) {
      if (endPrice > startPrice) direction = '向上';
      if (endPrice < startPrice) direction = '向下';
    }
    const explicitState = String(item.state || '').trim().toLowerCase();
    let status;
    let endpointStatus;
    if (explicitState === 'locked' || item.locked === true) {
      status = '已锁定';
      endpointStatus = '端点已锁定，不再回改';
    } else if (explicitState === 'formed') {
      status = '几何已成形待锁定';
      endpointStatus = '端点尚未锁定，仍可能回改';
    } else if (explicitState === 'forming') {
      status = '形成中';
      endpointStatus = '结构尚未成形';
    } else if (String(item.linestyle) === '1') {
      // 兼容旧快照；新版服务端必须显式传递 state/locked。
      status = '形成中';
      endpointStatus = '结构尚未成形';
    } else {
      status = '已锁定';
      endpointStatus = '端点已锁定，不再回改';
    }
    return {
      exists: true,
      text: `${direction} · ${status}`,
      meta: `${formatPrice(startPrice)} → ${formatPrice(endPrice)} · ${endpointStatus}`,
      direction,
      status,
      startPrice,
      endPrice,
      time: shapeTime(item),
    };
  }

  function segmentAuditText(segment, emptyText) {
    if (!segment || typeof segment !== 'object') return emptyText;
    const direction = DIRECTION_LABELS[String(segment.direction || '').toLowerCase()]
      || String(segment.direction || '方向待定');
    return `${direction} · ${formatPrice(segment.start_price)} → ${formatPrice(segment.end_price)}`;
  }

  function centerPointLabel(pointType) {
    const value = String(pointType || '').toLowerCase();
    return MMD_LABELS[value] || String(pointType || '三类点');
  }

  function centerLifecycleText(item, towerLabel) {
    const phase = String(item && item.completion_phase || '').toUpperCase();
    const expectedType = item && (
      item.completion_point_type || item.expected_completion_point_type
    );
    const pointLabel = centerPointLabel(expectedType);
    const scope = `${towerLabel}级`;

    if (phase === 'OPERATIONAL_THIRD_CLASS_POINT') {
      return {
        status: '已完成',
        qualification: `${scope}${pointLabel}已正式确认；末端结构仍会随新K更新`,
        evidence: `${scope}${pointLabel}已按最新完成线段正式确认`,
        requirement: '操作确认已满足；末端结构尚未封存',
        associatedPoint: `${scope}${pointLabel}（操作确认，末端结构未封存）`,
        tone: 'complete',
      };
    }
    if (phase === 'FORMAL_THIRD_CLASS_POINT') {
      return {
        status: '已完成',
        qualification: `${scope}${pointLabel}操作确认完成，末端结构已封存`,
        evidence: `${scope}${pointLabel}末端结构已封存`,
        requirement: '操作确认与同级别末端封存条件均已满足',
        associatedPoint: `${scope}${pointLabel}（操作确认，末端已封存）`,
        tone: 'complete',
      };
    }
    if (phase === 'GEOMETRIC_THIRD_CLASS_POINT') {
      return {
        status: `${pointLabel}候选待锁定`,
        qualification: `${scope}${pointLabel}的离开与首次回抽几何已出现，但所在线段尚未锁定`,
        evidence: `${scope}${pointLabel}仅为非交易几何候选`,
        requirement: '尚未正式确认；不得把几何候选当作可操作买卖点',
        associatedPoint: `${scope}${pointLabel}（候选待锁定）`,
        tone: 'forming',
      };
    }
    if (phase === 'NON_TRADABLE_OBSERVATION') {
      return {
        status: '观察证据',
        qualification: `${scope}中枢观察不产生买卖点`,
        evidence: '仅保留非交易结构观察',
        requirement: '等待同级别结构正式确认',
        associatedPoint: '无可交易关联买卖点',
        tone: 'neutral',
      };
    }
    if (phase === 'AWAITING_SAME_LEVEL_RETURN') {
      return {
        status: '形成中',
        qualification: `离开已出现；等待${scope}首次回抽确认`,
        evidence: `尚无${scope}${pointLabel}确认`,
        requirement: expectedType === '3sell'
          ? `等待${scope}向上回抽不回中枢下沿`
          : `等待${scope}向下回抽不回中枢上沿`,
        associatedPoint: `尚无${scope}关联买卖点`,
        tone: 'forming',
      };
    }
    if (phase === 'AWAITING_SAME_LEVEL_DEPARTURE') {
      return {
        status: '形成中',
        qualification: `主体已形成；等待${scope}有效离开及首次回抽`,
        evidence: `尚无${scope}三类点确认`,
        requirement: `等待${scope}有效离开，再观察首次回抽`,
        associatedPoint: `尚无${scope}关联买卖点`,
        tone: 'forming',
      };
    }
    return {
      status: '结构契约无效',
      qualification: '缺少同级别中枢完成阶段',
      evidence: '当前中枢未提供 completion_phase',
      requirement: '重新计算当前严格结构快照',
      associatedPoint: '关联买卖点不可判定',
      tone: 'neutral',
    };
  }

  function coreDirectionText(values) {
    const directions = Array.isArray(values) ? values : [];
    if (directions.length !== 3) return '未提供主体三段';
    const labels = directions.map((value) => (
      DIRECTION_LABELS[String(value || '').toLowerCase()] || ''
    ));
    if (labels.some((value) => !value)) return '主体三段方向无效';
    return labels.join(' → ');
  }

  function summarizeZone(items, latestClose, levelLabel, options) {
    const settings = options || {};
    const candidates = Array.isArray(items) ? items : [];
    let latest = null;
    let latestTime = null;

    candidates.forEach((item) => {
      const time = shapeTime(item);
      if (!latest || (time !== null && (latestTime === null || time > latestTime))) {
        latest = item;
        latestTime = time;
      }
    });

    if (!latest) {
      return {
        exists: false,
        text: `尚无${levelLabel}`,
        meta: '等待出现可计算的重叠区间',
        levelLabel,
        status: '尚未形成',
        position: '位置待定',
        low: null,
        high: null,
        time: null,
        tone: 'neutral',
        tower: settings.tower === 'bi' ? '笔' : '线段',
        recursiveLevel: settings.periodLabel || '当前周期',
        zd: '--',
        zg: '--',
        completion: '尚未形成',
        enteringSegment: '未提供进入段',
        coreDirections: '未提供主体三段',
        leavingSegment: '未提供离开段',
        confirmationSegment: '尚无同级别确认回抽',
        associatedPoint: '暂无关联买卖点',
        completionEvidence: '尚无同级别完成依据',
        completionRequirement: '等待形成可计算的中枢结构',
      };
    }

    const prices = shapePoints(latest)
      .map((item) => numeric(item && item.price))
      .filter((value) => value !== null);
    const core = latest.core && typeof latest.core === 'object' ? latest.core : {};
    const explicitZd = numeric(
      latest.zd === undefined ? core.zd_price : latest.zd,
    );
    const explicitZg = numeric(
      latest.zg === undefined ? core.zg_price : latest.zg,
    );
    const low = explicitZd === null ? (prices.length ? Math.min.apply(null, prices) : null) : explicitZd;
    const high = explicitZg === null ? (prices.length ? Math.max.apply(null, prices) : null) : explicitZg;
    const renderKind = String(latest.render_kind || '');
    // 生命周期元数据把同级回抽绑定到这个确切中枢，并保留三类点几何成立后
    // 等待线段锁定的盘中状态。
    const tower = String(settings.tower || latest.tower || '').toLowerCase();
    const towerLabel = tower === 'bi' ? '笔' : '线段';
    const lifecycle = centerLifecycleText(latest, towerLabel);
    const status = lifecycle.status;
    const effectiveLevelLabel = renderKind === 'center_preview'
      ? '线段中枢预览'
      : levelLabel;
    const qualification = lifecycle.qualification;
    // 集合归属是中枢层级的唯一权威来源。
    const recursiveLevel = numeric(latest.recursive_level);
    let position = '位置待定';
    if (latestClose !== null && low !== null && high !== null) {
      if (latestClose > high) position = '上方';
      else if (latestClose < low) position = '下方';
      else position = '中枢内';
    }
    let positionMeta = '现价位置待定';
    if (position === '上方') {
      const distance = latestClose - high;
      const percent = high ? (distance / Math.abs(high)) * 100 : null;
      positionMeta = `高于上沿 ${formatPrice(distance)}${percent === null ? '' : `（${percent.toFixed(2)}%）`}`;
    } else if (position === '下方') {
      const distance = low - latestClose;
      const percent = low ? (distance / Math.abs(low)) * 100 : null;
      positionMeta = `低于下沿 ${formatPrice(distance)}${percent === null ? '' : `（${percent.toFixed(2)}%）`}`;
    } else if (position === '中枢内') {
      const lowDistance = latestClose - low;
      const highDistance = high - latestClose;
      positionMeta = lowDistance <= highDistance
        ? `位于区间内 · 距下沿 ${formatPrice(lowDistance)}`
        : `位于区间内 · 距上沿 ${formatPrice(highDistance)}`;
    }
    return {
      exists: true,
      text: `${formatPrice(low)}–${formatPrice(high)}`,
      meta: `${qualification ? `${qualification} · ` : ''}${status} · ${positionMeta}`,
      levelLabel: effectiveLevelLabel,
      qualification,
      status,
      position,
      low,
      high,
      time: latestTime,
      tone: lifecycle.tone,
      tower: towerLabel,
      recursiveLevel: settings.periodLabel || (
        recursiveLevel === null ? '当前周期' : `L${recursiveLevel}`
      ),
      zd: formatPrice(low),
      zg: formatPrice(high),
      completion: status,
      enteringSegment: segmentAuditText(latest.entering_segment, '未提供进入段'),
      coreDirections: coreDirectionText(latest.core_directions),
      leavingSegment: segmentAuditText(latest.leaving_segment, '未提供离开段'),
      confirmationSegment: segmentAuditText(
        latest.completion_return_segment,
        '尚无同级别确认回抽',
      ),
      associatedPoint: lifecycle.associatedPoint,
      completionEvidence: lifecycle.evidence,
      completionRequirement: lifecycle.requirement,
    };
  }
  function latestSignal(groups, bars, kind, timeZone, latestClose) {
    const candidates = [];
    (Array.isArray(groups) ? groups : []).forEach((items) => {
      (Array.isArray(items) ? items : []).forEach((item) => candidates.push(item));
    });
    const latest = latestShape(candidates);
    if (!latest) {
      return {
        empty: true,
        label: kind === 'mmd' ? '暂无买卖点' : '暂无背驰',
        levelLabel: '',
        recency: '',
        tone: 'neutral',
        price: null,
        time: null,
        meta: '当前已加载数据中未发现记录',
      };
    }

    const points = shapePoints(latest);
    const point = points.length ? points[points.length - 1] : {};
    const latestTime = shapeTime(latest);
    const loadedTimes = (Array.isArray(bars) ? bars : [])
      .map((bar) => toSeconds(bar && bar.time))
      .filter((time) => time !== null);
    const windowStart = loadedTimes.length ? Math.min.apply(null, loadedTimes) : null;
    const windowEnd = loadedTimes.length ? Math.max.apply(null, loadedTimes) : null;
    let recency = '时间位置待定';
    if (latestTime !== null && windowStart !== null && latestTime < windowStart) {
      recency = '当前加载区间之前';
    } else if (latestTime !== null && windowEnd !== null && latestTime > windowEnd) {
      recency = '晚于当前加载 K 线';
    } else if (latestTime !== null && loadedTimes.length) {
      const laterBars = loadedTimes.filter((time) => time > latestTime).length;
      recency = laterBars === 0 ? '最近一根 K 线' : `距当前 ${laterBars} 根 K 线`;
    }

    const rawText = String(latest.text || '').trim();
    const normalizedText = rawText.toLowerCase();
    const explicitLevelLabel = String(latest.level_label || '').trim();
    const normalizedLevel = String(latest.level || '').trim().toLowerCase();
    const levelLabel = explicitLevelLabel || (
      normalizedLevel.indexOf('xd') >= 0 || normalizedLevel.indexOf('segment') >= 0
        ? '线段'
        : (normalizedLevel.indexOf('bi') >= 0 || normalizedLevel.indexOf('pen') >= 0 ? '笔' : '结构')
    );

    let label = rawText || (kind === 'mmd' ? '买卖点' : '背驰');
    let isBuy = false;
    let isSell = false;
    if (kind === 'mmd') {
      label = MMD_LABELS[normalizedText] || label;
      isBuy = normalizedText.endsWith('buy');
      isSell = normalizedText.endsWith('sell');
    } else {
      label = BC_LABELS[normalizedText] || label;
    }

    const price = numeric(point && point.price);
    const tone = kind === 'mmd' ? (isBuy ? 'buy' : (isSell ? 'sell' : 'neutral')) : 'warning';
    return {
      empty: false,
      label,
      levelLabel,
      recency,
      tone,
      price,
      time: latestTime,
      meta: `${levelLabel} · ${recency} · ${formatTimestamp(latestTime, timeZone)} · 信号价 ${formatPrice(price)} · 现价较信号 ${formatSignedPercent(latestClose, price)}`,
    };
  }

  function zonePositionLabel(zone) {
    if (!zone || !zone.exists) return '';
    if (zone.position === '中枢内') return `${zone.levelLabel}内`;
    if (zone.position === '位置待定') return `${zone.levelLabel}位置待定`;
    return `${zone.levelLabel}${zone.position}`;
  }

  function completionLabel(line) {
    return line.status === '形成中' ? '未完成' : '已完成';
  }

  function summarizeFormalDirection(formalDirection) {
    const value = formalDirection && typeof formalDirection === 'object'
      ? formalDirection
      : { direction: 'neutral', reason_codes: [] };
    const reasons = Array.isArray(value.reason_codes) ? value.reason_codes : [];
    const reasonText = reasons
      .map((reason) => FORMAL_DIRECTION_REASON_LABELS[reason] || reason)
      .join('；');
    const lacksSupport = reasons.includes('direction_change_lacks_first_or_second_point');
    const isConsolidation = reasons.includes('current_formal_movement_is_consolidation');
    const hasEnded = reasons.includes('current_formal_trend_has_ended');
    const levelText = Number.isInteger(value.structural_level)
      ? `L${value.structural_level}`
      : '当前级别';
    let label = '正式方向待确认';
    let tone = lacksSupport ? 'pending' : 'neutral';
    if (value.direction === 'up') {
      label = '正式上涨';
      tone = 'buy';
    } else if (value.direction === 'down') {
      label = '正式下跌';
      tone = 'sell';
    } else if (lacksSupport) {
      label = '反转待一/二类点确认';
    } else if (isConsolidation) {
      label = '盘整，方向待确认';
    } else if (hasEnded) {
      label = '原正式走势已结束';
    }
    const provenance = Number.isInteger(value.structural_level)
      ? `${levelText} · `
      : '';
    return {
      direction: value.direction,
      label,
      tone,
      lacksSupport,
      detail: `${provenance}${reasonText || '等待严格结构形成正式方向'}`,
    };
  }

  function structureNarrative(bi, xd, biZone, xdZone, formalDirection) {
    if (!bi.exists && !xd.exists) {
      return {
        headline: '等待形成可解释的笔或线段',
        detail: '当前数据不足，暂不判断方向或中枢位置。',
      };
    }

    let headline = '';
    if (xd.exists && bi.exists) {
      const relation = xd.direction === bi.direction ? '同向运行' : '反向运行';
      headline = `线段${xd.direction}${completionLabel(xd)}，当前笔${bi.direction}${relation}`;
    } else if (xd.exists) {
      headline = `当前线段${xd.direction}${completionLabel(xd)}`;
    } else {
      headline = `当前笔${bi.direction}${completionLabel(bi)}`;
    }

    const zonePositions = [biZone, xdZone].map(zonePositionLabel).filter(Boolean);
    const positionFact = zonePositions.length
      ? `现价位于${zonePositions.join('、')}`
      : '双中枢位置尚不足以计算';
    const mutableFact = [bi, xd].some((line) => line.exists && line.status === '形成中')
      ? '形成中的笔或线段边界仍可能变化。'
      : '当前笔与线段均已闭合，仍需等待后续结构更新。';
    const formal = formalDirection ? summarizeFormalDirection(formalDirection) : null;
    if (!formal) return { headline, detail: `${positionFact}；${mutableFact}` };
    if (formal.lacksSupport) {
      return {
        headline: '几何反转待同级一/二类点确认',
        detail: `${formal.detail}；当前笔、线段只表示几何运行。${positionFact}；${mutableFact}`,
      };
    }
    return {
      headline: `${formal.label}；${headline}`,
      detail: `${formal.detail}；${positionFact}；${mutableFact}`,
    };
  }

  function zoneConfirmation(zone) {
    if (!zone || !zone.exists || zone.low === null || zone.high === null) return '';
    const range = `${formatPrice(zone.low)}–${formatPrice(zone.high)}`;
    if (zone.position === '下方') return `观察现价能否重新进入${zone.levelLabel} ${range}`;
    if (zone.position === '上方') return `观察回踩能否守住${zone.levelLabel}上沿 ${formatPrice(zone.high)}`;
    if (zone.position === '中枢内') {
      return `观察价格能否有效离开${zone.levelLabel} ${range}，并通过回抽确认`;
    }
    return `等待${zone.levelLabel}与现价位置完成计算`;
  }

  function buildPlan(bi, xd, biZone, xdZone, formalDirection) {
    const now = [];
    const formal = formalDirection ? summarizeFormalDirection(formalDirection) : null;
    if (formal) now.push(formal.label);
    if (xd.exists) now.push(`线段${xd.direction}（${xd.status}）`);
    if (bi.exists) now.push(`当前笔${bi.direction}（${bi.status}）`);
    const zonePositions = [biZone, xdZone]
      .map(zonePositionLabel)
      .filter(Boolean);
    if (zonePositions.length) now.push(`现价位于${zonePositions.join('、')}`);

    let wait = '等待至少一笔形成并闭合，再检查线段与中枢。';
    if (bi.exists || xd.exists) {
      const conditions = [];
      if (bi.exists && bi.status === '形成中') {
        conditions.push(`先等待当前${bi.direction}笔成形并锁定`);
      } else if (xd.exists && xd.status === '形成中') {
        conditions.push(`先等待当前${xd.direction}线段成形`);
      } else if (xd.exists && xd.status === '几何已成形待锁定') {
        conditions.push(`先等待当前${xd.direction}线段端点锁定`);
      } else {
        conditions.push('等待下一笔或线段形成');
      }
      const primaryZone = xdZone.exists ? xdZone : biZone;
      const zoneCondition = zoneConfirmation(primaryZone);
      if (zoneCondition) conditions.push(zoneCondition);
      wait = `${conditions.join('；')}。条件未出现前，不升级结构判断。`;
    }
    if (formal && formal.lacksSupport) {
      wait = `等待同级一类或小转大二类点确认反转；${wait}`;
    } else if (formal && formal.direction === 'neutral') {
      wait = `等待当前级别建立可发布的正式方向；${wait}`;
    }

    const boundaryFacts = [];
    if (bi.exists && bi.status !== '已锁定' && bi.startPrice !== null) {
      const action = bi.direction === '向上' ? '跌破' : (bi.direction === '向下' ? '突破' : '越过');
      boundaryFacts.push(`现价${action}当前${bi.direction}笔起点 ${formatPrice(bi.startPrice)}`);
    } else if (bi.exists && bi.endPrice !== null) {
      boundaryFacts.push(`现价重新越过最近已闭合笔端点 ${formatPrice(bi.endPrice)}`);
    }
    if (xdZone.exists && xdZone.low !== null && xdZone.high !== null) {
      boundaryFacts.push(`现价重新跨越线段中枢边界 ${formatPrice(xdZone.low)} / ${formatPrice(xdZone.high)}`);
    } else if (biZone.exists && biZone.low !== null && biZone.high !== null) {
      boundaryFacts.push(`现价重新跨越笔中枢边界 ${formatPrice(biZone.low)} / ${formatPrice(biZone.high)}`);
    }
    const boundary = boundaryFacts.length
      ? `${boundaryFacts.join('，或')}时，触发结构重算；这些位置不是自动开仓、平仓或止损价。`
      : '结构端点或中枢尚不足以计算；暂不设置开仓、平仓或止损边界。';

    return {
      now: now.length ? `${now.join('；')}。` : '当前数据不足，尚不能形成结构事实。',
      wait,
      boundary,
    };
  }
  function summarizeBaseChartData(data, context) {
    const source = data || {};
    const options = context || {};
    const bars = Array.isArray(source.bars) ? source.bars : [];
    const latestBar = bars.length ? bars[bars.length - 1] : null;
    const latestClose = numeric(latestBar && latestBar.close);
    const timeZone = options.timeZone;
    const bi = summarizeLine(source.bis);
    const xd = summarizeLine(source.xds);
    const biZone = summarizeZone([], latestClose, '笔中枢观察', {
      tower: 'bi',
      periodLabel: '当前周期',
    });
    const xdZone = summarizeZone([], latestClose, '严格中枢', {
      tower: 'xd',
      periodLabel: '当前周期 L0',
    });
    const mmd = latestSignal([], bars, 'mmd', timeZone, latestClose);
    const bc = latestSignal([], bars, 'bc', timeZone, latestClose);
    const plan = buildPlan(bi, xd, biZone, xdZone);
    const narrative = structureNarrative(bi, xd, biZone, xdZone);

    return {
      hasData: Boolean(latestBar),
      price: formatPrice(latestClose),
      barTime: latestBar ? formatTimestamp(latestBar.time, timeZone) : '--',
      barState: latestBar ? (latestBar.isBarClosed === false ? '收盘待确认' : '已收盘') : '等待数据',
      resolutionLabel: formatResolution(options.resolution),
      bi,
      xd,
      biZone,
      xdZone,
      mmd,
      bc,
      verdict: narrative.headline,
      verdictDetail: narrative.detail,
      plan,
    };
  }

  const STRICT_POINT_TYPES = Object.freeze([
    '1buy', '2buy', '3buy', '1sell', '2sell', '3sell',
  ]);
  const STRICT_POINT_TYPE_SET = new Set(STRICT_POINT_TYPES);

  function strictFrequencyFromResolution(value) {
    const resolution = String(value == null ? '' : value).trim().toUpperCase();
    const fixed = {
      '10S': '10s', '30S': '30s',
      '120': '120m', '180': '3h', '240': '4h',
      '360': '6h', '480': '8h', '720': '12h',
      '1D': 'd', '2D': '2d', '3D': '3d', '1W': 'w', '1M': 'm',
      '3M': 'q', '12M': 'y',
    };
    if (fixed[resolution]) return fixed[resolution];
    if (/^[1-9][0-9]*$/.test(resolution)) return `${resolution}m`;
    return resolution.toLowerCase();
  }

  function normalizeStrictSymbol(value) {
    return String(value == null ? '' : value)
      .replace(/^[^:]+:/, '')
      .trim()
      .toUpperCase();
  }

  function emptyStrictPointCounts() {
    const counts = {};
    STRICT_POINT_TYPES.forEach((pointType) => {
      counts[pointType] = { confirmed: 0, formed: 0, approaching: 0 };
    });
    return counts;
  }

  function strictEmptySummary(source, options, state, detail) {
    const base = summarizeBaseChartData(source, options);
    const recovering = state === 'recovering';
    return {
      ...base,
      state,
      statusDetail: detail,
      sourceClosedAt: null,
      structureRevision: null,
      snapshotRevision: null,
      renderRevision: null,
      priceBasisRevision: null,
      structurePriceQuantum: null,
      formalDirection: 'neutral',
      formalDirectionLabel: '正式方向待确认',
      formalDirectionDetail: '等待同一上下文的严格结构证据',
      formalDirectionTone: 'neutral',
      formalDirectionLevel: null,
      formalDirectionTrendId: null,
      formalDirectionSupportPointId: null,
      formalDirectionReasonCodes: [],
      trends: [],
      completedTrends: [],
      pendingMovements: [],
      formalCenters: [],
      centerPreviews: [],
      centerProjections: [],
      observations: [],
      confirmedPoints: [],
      formedPoints: [],
      approachingPoints: [],
      divergences: [],
      pointCounts: emptyStrictPointCounts(),
      verdict: recovering ? '正在自动恢复严格缠论结构' : '正在同步严格缠论结构',
      verdictDetail: detail,
      plan: {
        now: recovering
          ? '严格结构权威快照正在自动重取，完成前不展示不完整结论。'
          : '严格结构与当前图表尚未完成同步。',
        wait: '等待同一标的、周期和末根闭合时间的权威严格快照。',
        boundary: '同步完成前不生成结构边界或交易判断。',
      },
    };
  }

  function strictSourceClosedAt(source, bars) {
    if (Object.prototype.hasOwnProperty.call(source, 'times')) {
      if (!Array.isArray(source.times) || source.times.length === 0) {
        throw new Error('严格结构原始末根时间无效');
      }
      const sourceClose = toSeconds(source.times[source.times.length - 1]);
      if (!Number.isInteger(sourceClose)) {
        throw new Error('严格结构原始末根时间无效');
      }
      return sourceClose;
    }
    return toSeconds(bars[bars.length - 1] && bars[bars.length - 1].time);
  }

  function strictStringArray(value) {
    return Array.isArray(value)
      && value.every((item) => typeof item === 'string' && item.length > 0);
  }

  function strictSameIds(left, right) {
    return left.length === right.length
      && left.every((item, index) => item === right[index]);
  }

  function validateStrictCenterContract(item, level, allowPartialPhysical) {
    if (!item || item.schema !== 'chanlun-chart-center'
      || item.structural_level !== level
      || !['segment', 'stroke_observation', 'trend_type'].includes(item.source_kind)
      || !strictStringArray(item.core_unit_ids)
      || item.core_unit_ids.length !== 3
      || new Set(item.core_unit_ids).size !== 3
      || item.core_component_count !== 3
      || !strictStringArray(item.initial_unit_ids)
      || !strictSameIds(item.initial_unit_ids, item.core_unit_ids)
      || !strictStringArray(item.establishment_segment_ids)) {
      throw new Error('严格中枢核心契约无效');
    }
    const recursive = item.source_kind === 'trend_type';
    if (recursive) {
      if (item.establishment_leave_unit_id !== null
        || item.initial_exit_unit_id !== null
        || item.minimum_lifecycle_role_count !== 3
        || item.lifecycle_role_count < 3
        || item.establishment_component_count !== 3
        || item.overlap_component_count < 3
        || !strictSameIds(item.establishment_segment_ids, item.core_unit_ids)) {
        throw new Error('递归中枢三走势契约无效');
      }
      return;
    }

    const entryId = item.entry_unit_id;
    const leaveId = item.establishment_leave_unit_id;
    const partial = allowPartialPhysical === true
      && item.render_kind === 'center_preview'
      && item.state === 'forming'
      && leaveId === null;
    if (typeof entryId !== 'string' || entryId.length === 0
      || (!partial && (typeof leaveId !== 'string' || leaveId.length === 0))
      || (partial && item.initial_exit_unit_id !== null)
      || (!partial && item.initial_exit_unit_id !== leaveId)
      || item.minimum_lifecycle_role_count !== 5) {
      throw new Error('物理中枢必须具备进入段和独立离开段');
    }
    const expected = [entryId, ...item.core_unit_ids];
    if (!partial) expected.push(leaveId);
    if (new Set(expected).size !== expected.length
      || !strictSameIds(item.establishment_segment_ids, expected)
      || item.establishment_component_count !== expected.length
      || item.overlap_component_count < expected.length
      || item.lifecycle_role_count < expected.length) {
      throw new Error('物理中枢必须由五个正宽重叠角色建立');
    }
  }

  function validatePendingMovementContract(item, level, formalUnitIds, ownedUnitIds) {
    if (!item || item.schema !== 'chanlun-chart-pending-movement'
      || item.render_kind !== 'pending_movement'
      || item.structural_level !== level
      || item.state !== 'pending'
      || item.classification !== 'unresolved'
      || !['entire_stream', 'prefix', 'bridge', 'suffix'].includes(item.role)
      || !['up', 'down'].includes(item.direction)
      || item.geometric_direction !== item.direction
      || item.semantic_direction !== null
      || item.direction_status !== 'pending'
      || item.formal_direction_confirmed !== false
      || item.tradable !== false
      || item.recursive_eligible !== false
      || item.divergence_eligible !== false
      || !strictStringArray(item.constituent_unit_ids)
      || item.constituent_unit_ids.length === 0
      || new Set(item.constituent_unit_ids).size !== item.constituent_unit_ids.length) {
      throw new Error('待定走势分区契约无效');
    }
    item.constituent_unit_ids.forEach((unitId) => {
      if (formalUnitIds.has(unitId) || ownedUnitIds.has(unitId)) {
        throw new Error('正式走势与待定走势不得重复拥有同一单元');
      }
      ownedUnitIds.add(unitId);
    });
  }

  function validateStrictSnapshot(snapshot, source, options, validationOptions) {
    const validation = validationOptions || {};
    if (!snapshot || snapshot.schema !== 'chanlun-chart-structure') {
      throw new Error('严格结构数据契约不匹配');
    }
    const requiredStrings = [
      'symbol', 'source_frequency', 'display_frequency',
      'price_basis_revision', 'structure_price_quantum',
      'strict_config_revision', 'structure_revision',
      'snapshot_revision', 'render_revision',
    ];
    requiredStrings.forEach((field) => {
      if (typeof snapshot[field] !== 'string' || snapshot[field].length === 0) {
        throw new Error(`严格结构字段缺失：${field}`);
      }
    });
    if (!Number.isInteger(snapshot.source_closed_at)) {
      throw new Error('严格结构末根时间不是秒级整数');
    }
    if (!Number.isFinite(Number(snapshot.structure_price_quantum))
      || Number(snapshot.structure_price_quantum) <= 0) {
      throw new Error('严格结构价格量子无效');
    }
    if (
      !Array.isArray(snapshot.stroke_center_observations)
      || !Array.isArray(snapshot.levels)
    ) {
      throw new Error('严格结构集合无效');
    }
    const formalDirection = snapshot.formal_direction;
    if (!formalDirection || !['up', 'down', 'neutral'].includes(formalDirection.direction)
      || !Array.isArray(formalDirection.reason_codes)
      || formalDirection.reason_codes.length === 0
      || formalDirection.reason_codes.some((value) => typeof value !== 'string' || !value)
      || (formalDirection.structural_level !== null
        && !Number.isInteger(formalDirection.structural_level))) {
      throw new Error('严格结构正式方向证据无效');
    }
    snapshot.stroke_center_observations.forEach((item) => {
      if (!item || item.render_kind !== 'center_observation'
        || item.source_kind !== 'stroke_observation') {
        throw new Error('严格笔中枢观察契约无效');
      }
      validateStrictCenterContract(item, 0, false);
    });

    const expectedFrequency = strictFrequencyFromResolution(options.resolution);
    if (snapshot.display_frequency !== expectedFrequency
      || snapshot.source_frequency !== expectedFrequency) {
      throw new Error('严格结构周期与当前图表周期不一致');
    }
    const expectedSymbol = normalizeStrictSymbol(options.symbol);
    if (expectedSymbol && normalizeStrictSymbol(snapshot.symbol) !== expectedSymbol) {
      throw new Error('严格结构标的与当前图表标的不一致');
    }
    const bars = Array.isArray(source.bars) ? source.bars : [];
    if (!bars.length) throw new Error('严格结构缺少当前图表 K 线');
    const loadedClose = strictSourceClosedAt(source, bars);
    if (
      loadedClose !== snapshot.source_closed_at
      && !(
        validation.allowOlderSourceClose === true
        && snapshot.source_closed_at < loadedClose
      )
    ) {
      throw new Error('严格结构末根闭合时间与当前图表不一致');
    }
    snapshot.levels.forEach((level) => {
      if (!level || !Number.isInteger(level.structural_level)
        || typeof level.label !== 'string' || level.label.length === 0
        || level.origin !== 'current_chart_recursive') {
        throw new Error('严格结构级别无效');
      }
      const levelDirection = level.formal_direction;
      if (!levelDirection
        || !['up', 'down', 'neutral'].includes(levelDirection.direction)
        || !Array.isArray(levelDirection.reason_codes)
        || levelDirection.reason_codes.length === 0
        || (levelDirection.structural_level !== null
          && levelDirection.structural_level !== level.structural_level)) {
        throw new Error('严格结构级别正式方向证据无效');
      }
      [
        'centers', 'center_previews', 'center_projections', 'current_trends',
        'pending_movements', 'completed_trend_snapshots',
        'confirmed_points', 'approaching_points', 'divergences',
      ].forEach((field) => {
        if (!Array.isArray(level[field])) throw new Error(`严格结构级别集合无效：${field}`);
      });
      const expectedSourceKind = level.structural_level === 0 ? 'segment' : 'trend_type';
      level.centers.forEach((item) => {
        if (!item || item.render_kind !== 'formal_center'
          || item.source_kind !== expectedSourceKind) {
          throw new Error('严格正式中枢来源契约无效');
        }
        validateStrictCenterContract(item, level.structural_level, false);
      });
      level.center_previews.forEach((item) => {
        if (!item || item.render_kind !== 'center_preview'
          || item.source_kind !== expectedSourceKind) {
          throw new Error('严格中枢预览来源契约无效');
        }
        validateStrictCenterContract(item, level.structural_level, true);
      });
      level.center_projections.forEach((item) => {
        if (!item || item.render_kind !== 'center_projection'
          || item.source_kind !== expectedSourceKind) {
          throw new Error('严格中枢投影来源契约无效');
        }
        validateStrictCenterContract(item, level.structural_level, false);
      });
      level.current_trends.concat(level.completed_trend_snapshots).forEach((trend) => {
        if (!trend || trend.render_kind !== 'strict_trend'
          || trend.geometric_direction !== trend.direction
          || !['up', 'down', null].includes(trend.semantic_direction)
          || ![
            'formal', 'awaiting_reversal_support', 'consolidation',
            'ended', 'geometric_candidate',
          ].includes(trend.direction_status)
          || trend.formal_direction_confirmed !== (trend.direction_status === 'formal')
          || !Array.isArray(trend.direction_reason_codes)) {
          throw new Error('严格走势方向资格字段无效');
        }
      });
      for (let index = 1; index < level.current_trends.length; index += 1) {
        const previous = level.current_trends[index - 1];
        const current = level.current_trends[index];
        const previousTail = previous.points?.[previous.points.length - 1];
        const currentHead = current.points?.[0];
        if (
          previous.direction === current.direction
          || !Number.isInteger(previousTail?.price_tick)
          || !Number.isInteger(currentHead?.price_tick)
          || previousTail.price_tick !== currentHead.price_tick
          || !Number.isInteger(previousTail?.time)
          || !Number.isInteger(currentHead?.time)
          || currentHead.time < previousTail.time
        ) {
          throw new Error('严格当前走势必须保持上下交替的因果连接链');
        }
      }
      const formalUnitIds = new Set();
      level.current_trends.forEach((trend) => {
        if (!strictStringArray(trend.constituent_unit_ids)) {
          throw new Error('严格走势来源单元契约无效');
        }
        trend.constituent_unit_ids.forEach((unitId) => formalUnitIds.add(unitId));
      });
      const pendingUnitIds = new Set();
      level.pending_movements.forEach((item) => {
        validatePendingMovementContract(
          item,
          level.structural_level,
          formalUnitIds,
          pendingUnitIds,
        );
      });
      [
        ['confirmed_points', 'point_confirmed', 'confirmed'],
        ['approaching_points', 'point_approaching', 'approaching'],
      ].forEach(([field, renderKind, status]) => {
        level[field].forEach((point) => {
          const pointType = String(point && point.point_type || '').toLowerCase();
          const expectedSide = pointType.endsWith('buy') ? 'buy' : 'sell';
          const expectedConfirmed = status === 'confirmed';
          const validFormation = expectedConfirmed
            ? point && point.formation_state === 'confirmed'
            : point && ['forming', 'geometry_ready', 'formed'].includes(point.formation_state);
          const validLockState = expectedConfirmed
            ? point && (point.lock_state === 'locked' || (
              point.lock_state === 'pending'
              && point.operational_confirmation === true
              && point.strict_status === 'approaching'
              && point.contains_forming_segment === false
              && point.contains_unlocked_segment === true
            ))
            : point && point.lock_state === 'pending';
          if (!point
            || point.render_kind !== renderKind
            || point.status !== status
            || !validFormation
            || !validLockState
            || typeof point.contains_forming_segment !== 'boolean'
            || typeof point.contains_unlocked_segment !== 'boolean'
            || point.structural_level !== level.structural_level
            || !STRICT_POINT_TYPE_SET.has(pointType)
            || point.side !== expectedSide
            || !Array.isArray(point.points)
            || point.points.length !== 1) {
            throw new Error('严格买卖点契约无效');
          }
        });
      });
    });
    return snapshot;
  }

  function summarizeStrictCenter(item, qualification) {
    const core = item && item.core && typeof item.core === 'object' ? item.core : {};
    return {
      centerId: item.center_id || null,
      previewId: item.preview_id || null,
      renderId: item.render_id,
      bodyRevision: item.body_revision,
      structuralLevel: item.structural_level,
      sourceKind: item.source_kind,
      state: item.state,
      tradable: item.tradable === true,
      qualification,
      zd: numeric(core.zd_price),
      zg: numeric(core.zg_price),
      establishedAt: toSeconds(item.established_at),
      availableAt: toSeconds(item.available_at),
      completedAt: toSeconds(item.completed_at),
      entryUnitId: item.entry_unit_id || null,
      establishmentLeaveUnitId: item.establishment_leave_unit_id || null,
      initialExitUnitId: item.initial_exit_unit_id || null,
      minimumLifecycleRoleCount: numeric(item.minimum_lifecycle_role_count),
      lifecycleRoleCount: numeric(item.lifecycle_role_count),
      overlapComponentCount: numeric(item.overlap_component_count),
      establishmentSegmentIds: Array.isArray(item.establishment_segment_ids)
        ? item.establishment_segment_ids.slice()
        : [],
      coreUnitIds: Array.isArray(item.core_unit_ids) ? item.core_unit_ids.slice() : [],
      initialUnitIds: Array.isArray(item.initial_unit_ids) ? item.initial_unit_ids.slice() : [],
      bodyUnitIds: Array.isArray(item.body_unit_ids) ? item.body_unit_ids.slice() : [],
      extensionUnitIds: Array.isArray(item.extension_unit_ids) ? item.extension_unit_ids.slice() : [],
      pendingLeaveUnitId: item.pending_leave_unit_id || null,
      completionLeaveUnitId: item.completion_leave_unit_id || null,
      completionReturnUnitId: item.completion_return_unit_id || null,
      completionDirection: item.completion_direction || null,
      boundaryDivergenceId: item.boundary_divergence_id || null,
      boundaryAnchorUnitId: item.boundary_anchor_unit_id || null,
      enteringSegment: item.entering_segment || null,
      leavingSegment: item.leaving_segment || null,
    };
  }

  function summarizeStrictTrend(item) {
    const direction = String(item.direction || '').toLowerCase();
    const semanticDirection = item.semantic_direction === null
      ? null
      : String(item.semantic_direction || '').toLowerCase();
    const directionStatus = String(item.direction_status || '').toLowerCase();
    const geometricLabel = DIRECTION_LABELS[direction]
      ? `几何${DIRECTION_LABELS[direction]}`
      : '几何方向待定';
    const semanticLabel = semanticDirection && DIRECTION_LABELS[semanticDirection]
      ? DIRECTION_LABELS[semanticDirection]
      : null;
    let directionLabel = `${geometricLabel}（候选）`;
    if (directionStatus === 'formal') {
      directionLabel = `正式${semanticLabel || DIRECTION_LABELS[direction] || '方向待定'}`;
    } else if (directionStatus === 'awaiting_reversal_support') {
      directionLabel = `${semanticLabel ? `走势定义${semanticLabel}` : geometricLabel} · 待同级一/二类点`;
    } else if (directionStatus === 'consolidation') {
      directionLabel = `盘整 · ${geometricLabel}`;
    } else if (directionStatus === 'ended') {
      directionLabel = `${semanticLabel ? `走势定义${semanticLabel}` : geometricLabel} · 已结束`;
    }
    return {
      trendId: item.trend_id,
      renderId: item.render_id,
      structuralLevel: item.structural_level,
      sourceKind: item.source_kind,
      state: item.state,
      kind: item.kind,
      direction,
      geometricDirection: direction,
      semanticDirection,
      directionStatus,
      directionLabel,
      formalDirectionConfirmed: item.formal_direction_confirmed === true,
      formalSupportPointId: item.formal_support_point_id || null,
      directionReasonCodes: Array.isArray(item.direction_reason_codes)
        ? item.direction_reason_codes.slice()
        : [],
      tradable: item.tradable === true,
      centerIds: Array.isArray(item.center_ids) ? item.center_ids.slice() : [],
      confirmedAt: toSeconds(item.confirmed_at),
      availableAt: toSeconds(item.available_at),
    };
  }

  function summarizePendingMovement(item) {
    return {
      partitionId: item.partition_id,
      renderId: item.render_id,
      structuralLevel: item.structural_level,
      sourceKind: item.source_kind,
      state: item.state,
      classification: item.classification,
      role: item.role,
      direction: item.direction,
      constituentUnitIds: item.constituent_unit_ids.slice(),
      leftTrendId: item.left_trend_id || null,
      rightTrendId: item.right_trend_id || null,
      availableAt: toSeconds(item.available_at),
      tradable: false,
      recursiveEligible: false,
      divergenceEligible: false,
    };
  }

  function strictEvidenceText(item) {
    const values = [];
    const divergence = item && item.divergence;
    if (divergence && typeof divergence === 'object') {
      const source = String(divergence.strength_source || '').toUpperCase();
      const kind = String(divergence.kind || 'divergence');
      values.push(`${source || '结构'} ${kind} 背驰证据`);
    }
    if (Array.isArray(item && item.evidence_codes) && item.evidence_codes.length) {
      values.push(item.evidence_codes.join('、'));
    }
    return values.length ? values.join(' · ') : '严格结构证据已记录';
  }

  function summarizeStrictPoint(item) {
    const rawType = String(item.point_type || '').toLowerCase();
    return {
      pointId: item.point_id,
      renderId: item.render_id,
      pointType: rawType,
      pointLabel: MMD_LABELS[rawType],
      side: item.side,
      status: item.status,
      strictStatus: item.strict_status || item.status,
      operationalConfirmation: item.operational_confirmation === true,
      confirmationBasis: item.confirmation_basis || null,
      formationState: item.formation_state,
      lockState: item.lock_state,
      actionable: item.actionable === true,
      containsFormingSegment: item.contains_forming_segment === true,
      containsUnlockedSegment: item.contains_unlocked_segment === true,
      terminalSegmentRole: item.terminal_segment_role || null,
      terminalSegmentState: item.terminal_segment_state || null,
      variant: item.variant,
      structuralLevel: item.structural_level,
      sourceKind: item.source_kind,
      priceBasisRevision: item.price_basis_revision,
      anchorAt: toSeconds(item.anchor_at),
      confirmedAt: toSeconds(item.confirmed_at),
      availableAt: toSeconds(item.available_at),
      anchorPrice: numeric(item.anchor_price),
      invalidationPrice: numeric(item.invalidation_price),
      centerId: item.center_id || null,
      centerOrdinal: numeric(item.center_ordinal),
      parentPointId: item.parent_point_id || null,
      relatedPointIds: Array.isArray(item.related_point_ids) ? item.related_point_ids.slice() : [],
      smallToLargeCarrierUnitIds: Array.isArray(item.small_to_large_carrier_unit_ids)
        ? item.small_to_large_carrier_unit_ids.slice() : [],
      missingConditions: Array.isArray(item.missing_conditions) ? item.missing_conditions.slice() : [],
      evidenceCodes: Array.isArray(item.evidence_codes) ? item.evidence_codes.slice() : [],
      evidenceText: strictEvidenceText(item),
      tradable: item.tradable === true,
    };
  }

  function summarizeStrictDivergence(item, levelLabel) {
    const kind = String(item.kind || '').toLowerCase();
    const direction = String(item.direction || '').toLowerCase();
    const metrics = item && item.metrics && typeof item.metrics === 'object' ? item.metrics : {};
    return {
      divergenceId: item.divergence_id,
      renderId: item.render_id,
      kind,
      label: BC_LABELS[kind] || kind,
      direction,
      directionLabel: DIRECTION_LABELS[direction] || '方向待定',
      structuralLevel: item.structural_level,
      levelLabel,
      sourceKind: item.source_kind,
      priceBasisRevision: item.price_basis_revision,
      compareUnitId: item.compare_unit_id,
      signalUnitId: item.signal_unit_id,
      comparisonWidth: numeric(item.comparison_width),
      compareLegUnitIds: Array.isArray(item.compare_leg_unit_ids)
        ? item.compare_leg_unit_ids.slice() : [],
      signalLegUnitIds: Array.isArray(item.signal_leg_unit_ids)
        ? item.signal_leg_unit_ids.slice() : [],
      anchorAt: toSeconds(item.anchor_at),
      anchorPrice: numeric(item.anchor_price),
      confirmedAt: toSeconds(item.confirmed_at),
      availableAt: toSeconds(item.available_at),
      strengthSource: metrics.strength_source || null,
      strengthDecayCount: numeric(metrics.strength_decay_count),
      tradable: item.tradable === true,
    };
  }

  function strictLatestSignal(points, bars, timeZone, latestClose, kind) {
    const signalItems = points.map((item) => ({
      ...item,
      text: kind === 'mmd'
        ? item.point_type
        : (item.kind || (item.divergence && item.divergence.kind)),
      level: item.level_label || `L${item.structural_level}`,
    }));
    return latestSignal([signalItems], bars, kind, timeZone, latestClose);
  }

  function summarizeStrictChartData(source, options, snapshot) {
    const base = summarizeBaseChartData(source, options);
    const bars = Array.isArray(source.bars) ? source.bars : [];
    const latestBar = bars.length ? bars[bars.length - 1] : null;
    const latestClose = numeric(latestBar && latestBar.close);
    const formalCenters = [];
    const centerPreviews = [];
    const centerProjections = [];
    const trends = [];
    const completedTrends = [];
    const pendingMovements = [];
    const rawConfirmedPoints = [];
    const rawApproachingPoints = [];
    const rawDivergences = [];
    const pointCounts = emptyStrictPointCounts();

    snapshot.levels.forEach((level) => {
      level.centers.forEach((item) => {
        formalCenters.push(summarizeStrictCenter(item, '正式严格中枢'));
      });
      level.center_previews.forEach((item) => {
        centerPreviews.push(summarizeStrictCenter(
          item,
          item.operational_confirmation === true
            ? '买卖点操作确认已完成；末端结构仍会随新K更新'
            : item.state === 'completed'
            ? '离开/回抽几何已出现；仍是候选，尚未正式确认'
            : '形成中预览，不可直接交易',
        ));
      });
      level.center_projections.forEach((item) => {
        centerProjections.push(summarizeStrictCenter(item, '未确认投影，不可直接交易'));
      });
      level.current_trends.forEach((item) => trends.push(summarizeStrictTrend(item)));
      level.completed_trend_snapshots.forEach((item) => completedTrends.push(summarizeStrictTrend(item)));
      level.pending_movements.forEach((item) => {
        pendingMovements.push(summarizePendingMovement(item));
      });
      level.confirmed_points.forEach((item) => {
        rawConfirmedPoints.push(item);
        const pointType = String(item.point_type || '').toLowerCase();
        if (pointCounts[pointType]) pointCounts[pointType].confirmed += 1;
      });
      level.approaching_points.forEach((item) => {
        rawApproachingPoints.push(item);
        const pointType = String(item.point_type || '').toLowerCase();
        if (pointCounts[pointType]) {
          const lane = ['geometry_ready', 'formed'].includes(item.formation_state)
            ? 'formed'
            : 'approaching';
          pointCounts[pointType][lane] += 1;
        }
      });
      level.divergences.forEach((item) => {
        rawDivergences.push({ ...item, level_label: level.label });
      });
    });

    const observations = snapshot.stroke_center_observations.map((item) => (
      summarizeStrictCenter(item, '严格笔中枢观察，不可直接交易')
    ));
    const confirmedPoints = rawConfirmedPoints.map(summarizeStrictPoint);
    const formedPoints = rawApproachingPoints
      .filter((item) => ['geometry_ready', 'formed'].includes(item.formation_state))
      .map(summarizeStrictPoint);
    const approachingPoints = rawApproachingPoints
      .filter((item) => item.formation_state === 'forming')
      .map(summarizeStrictPoint);
    const divergences = rawDivergences.map((item) => (
      summarizeStrictDivergence(item, item.level_label)
    ));
    // 基础笔/线段和所有高级结构消费同一严格运行时；不存在旧中枢回退源。
    const bi = summarizeLine(source.bis);
    const xd = summarizeLine(source.xds);
    const biZone = summarizeZone(
      snapshot.stroke_center_observations,
      latestClose,
      '笔中枢观察',
      {
      tower: 'bi',
      periodLabel: '当前周期',
      },
    );
    const levelZero = snapshot.levels.find((level) => level.structural_level === 0);
    const currentLevelCenters = levelZero
      ? levelZero.centers.concat(levelZero.center_previews, levelZero.center_projections)
      : [];
    const xdZone = summarizeZone(
      currentLevelCenters,
      latestClose,
      '严格中枢',
      {
        tower: 'xd',
        periodLabel: levelZero ? levelZero.label : '当前周期 L0',
      },
    );
    const mmd = strictLatestSignal(
      rawConfirmedPoints.concat(rawApproachingPoints),
      bars,
      options.timeZone,
      latestClose,
      'mmd',
    );
    const bc = strictLatestSignal(
      rawDivergences,
      bars,
      options.timeZone,
      latestClose,
      'bc',
    );
    const formalDirection = snapshot.formal_direction;
    const formalDirectionSummary = summarizeFormalDirection(formalDirection);
    const narrative = structureNarrative(
      bi,
      xd,
      biZone,
      xdZone,
      formalDirection,
    );
    const plan = buildPlan(bi, xd, biZone, xdZone, formalDirection);

    return {
      ...base,
      state: 'ready',
      statusDetail: `严格结构已同步 · ${snapshot.structure_revision.slice(0, 18)}`,
      sourceClosedAt: snapshot.source_closed_at,
      structureRevision: snapshot.structure_revision,
      snapshotRevision: snapshot.snapshot_revision,
      renderRevision: snapshot.render_revision,
      priceBasisRevision: snapshot.price_basis_revision,
      structurePriceQuantum: snapshot.structure_price_quantum,
      formalDirection: formalDirection.direction,
      formalDirectionLabel: formalDirectionSummary.label,
      formalDirectionDetail: formalDirectionSummary.detail,
      formalDirectionTone: formalDirectionSummary.tone,
      formalDirectionLevel: formalDirection.structural_level,
      formalDirectionTrendId: formalDirection.trend_id,
      formalDirectionSupportPointId: formalDirection.support_point_id,
      formalDirectionReasonCodes: formalDirection.reason_codes.slice(),
      trends,
      completedTrends,
      pendingMovements,
      formalCenters,
      centerPreviews,
      centerProjections,
      observations,
      confirmedPoints,
      formedPoints,
      approachingPoints,
      divergences,
      pointCounts,
      bi,
      xd,
      biZone,
      xdZone,
      mmd,
      bc,
      verdict: narrative.headline,
      verdictDetail: narrative.detail,
      plan,
    };
  }

  function summarizeChartData(data, context) {
    const source = data || {};
    const options = context || {};
    if (!Object.prototype.hasOwnProperty.call(source, 'strict_structure_mode')) {
      return strictEmptySummary(source, options, 'syncing', '严格结构传输状态缺失');
    }
    const mode = source.strict_structure_mode;
    if (mode === 'unavailable') {
      const code = source.strict_structure_error && source.strict_structure_error.code
        ? source.strict_structure_error.code
        : 'strict_evidence_invalid';
      const cachedSnapshot = options.cachedStrictSnapshot;
      if (cachedSnapshot) {
        try {
          validateStrictSnapshot(
            cachedSnapshot,
            source,
            options,
            { allowOlderSourceClose: true },
          );
          const cachedSummary = summarizeStrictChartData(source, options, cachedSnapshot);
          return {
            ...cachedSummary,
            state: 'stale',
            statusDetail: `严格结构暂时重算失败，沿用最近一次有效结果：${code}`,
            verdictDetail: `当前为最近一次有效严格结构（${code}），等待后端恢复后自动更新。${cachedSummary.verdictDetail}`,
            plan: {
              ...cachedSummary.plan,
              now: '保留同标的同周期最近一次有效严格结构，不据瞬时失败清空判断。',
              wait: '等待下一份同上下文权威严格快照自动替换。',
            },
          };
        } catch (_) { /* 缓存属于其他标的、周期或未来数据时不得复用 */ }
      }
      return strictEmptySummary(source, options, 'recovering', `严格结构自动恢复中：${code}`);
    }
    let snapshot = null;
    if (mode === 'replace') snapshot = source.strict_structure;
    else if (mode === 'unchanged') snapshot = options.cachedStrictSnapshot;
    else {
      return strictEmptySummary(source, options, 'syncing', '严格结构传输状态无效');
    }
    if (!snapshot) {
      return strictEmptySummary(source, options, 'syncing', '严格结构权威快照缺失');
    }
    try {
      validateStrictSnapshot(snapshot, source, options);
      return summarizeStrictChartData(source, options, snapshot);
    } catch (error) {
      return strictEmptySummary(
        source,
        options,
        'syncing',
        error && error.message ? error.message : '严格结构上下文不匹配',
      );
    }
  }

  const browserState = {
    initialized: false,
    managerId: null,
    renderToken: 0,
    symbolInfoCache: new Map(),
    timer: null,
  };
  const OVERVIEW_COLLAPSE_STORAGE_KEY = 'chart_analysis_overview_collapsed';

  function element(id) {
    return root && root.document ? root.document.getElementById(id) : null;
  }

  function setText(id, value) {
    const target = element(id);
    if (target) target.textContent = value == null ? '' : String(value);
  }

  function setTone(id, tone) {
    const target = element(id);
    if (target) target.setAttribute('data-tone', tone || 'neutral');
  }

  function splitIdentity(identity) {
    const raw = String(identity && identity.symbol || '');
    const separator = raw.indexOf(':');
    const market = separator >= 0 ? raw.slice(0, separator).toLowerCase() : '';
    const code = separator >= 0 ? raw.slice(separator + 1) : raw;
    return { market, code: code || '--' };
  }

  function currentManager(detail) {
    const managers = root && root.__cm ? root.__cm : {};
    if (detail && detail.managerId && managers[detail.managerId]) {
      browserState.managerId = String(detail.managerId);
      return managers[detail.managerId];
    }
    if (browserState.managerId && managers[browserState.managerId]) {
      return managers[browserState.managerId];
    }
    const ids = Object.keys(managers).sort();
    if (!ids.length) return null;
    browserState.managerId = ids[0];
    return managers[ids[0]];
  }

  function managerIdentity(manager) {
    try {
      if (manager && typeof manager.getCurrentChartIdentity === 'function') {
        const identity = manager.getCurrentChartIdentity();
        if (identity) return identity;
      }
      if (manager && manager.widget && typeof manager.widget.symbolInterval === 'function') {
        return manager.widget.symbolInterval();
      }
    } catch (_) { /* 图表仍在初始化 */ }
    return null;
  }

  function dataForManager(manager, identity, detail) {
    const provider = manager && manager.udf_datafeed && manager.udf_datafeed._historyProvider;
    const map = provider && provider.bars_result;
    if (!map || typeof map.get !== 'function') return null;
    const keys = [];
    if (detail && detail.key) keys.push(String(detail.key));
    if (identity && identity.symbol && identity.interval) {
      keys.push(`${String(identity.symbol).toLowerCase()}${String(identity.interval).toLowerCase()}`);
    }
    for (let index = 0; index < keys.length; index += 1) {
      const value = map.get(keys[index]);
      if (value) return value;
    }
    return null;
  }

  function renderSignal(prefix, signal) {
    setText(`${prefix}-state`, signal.label);
    setText(`${prefix}-meta`, signal.meta);
    setTone(`${prefix}-row`, signal.tone);
  }

  function renderZoneAudit(prefix, zone) {
    setText(`${prefix}-tower`, zone.tower);
    setText(`${prefix}-level`, zone.recursiveLevel);
    setText(`${prefix}-bounds`, `ZD ${zone.zd} / ZG ${zone.zg}`);
    setText(`${prefix}-completion`, zone.completion);
    setText(`${prefix}-entry`, zone.enteringSegment);
    setText(`${prefix}-core`, zone.coreDirections);
    setText(`${prefix}-exit`, zone.leavingSegment);
    setText(`${prefix}-return`, zone.confirmationSegment);
    setText(`${prefix}-point`, zone.associatedPoint);
    setText(`${prefix}-evidence`, zone.completionEvidence);
    setText(`${prefix}-requirement`, zone.completionRequirement);
  }

  function renderSummary(summary, identity, symbolInfo) {
    const parsed = splitIdentity(identity);
    const overview = element('ca-overview');
    if (overview) overview.setAttribute('data-ready', summary.hasData ? 'true' : 'false');

    setText('ca-current-name', symbolInfo && symbolInfo.description ? symbolInfo.description : parsed.code);
    setText('ca-current-symbol', parsed.code);
    setText('ca-current-interval', summary.resolutionLabel);
    setText('ca-current-price', summary.price);
    setText('ca-bar-time', summary.hasData ? `${summary.barTime} · ${summary.barState}` : '等待K线数据');
    setText('ca-data-state', summary.barState);
    setTone('ca-data-state', summary.barState === '收盘待确认' ? 'pending' : (summary.hasData ? 'closed' : 'loading'));
    setText('ca-structure-verdict', summary.verdict);
    setText('ca-structure-detail', summary.verdictDetail);
    setText('ca-bi-state', summary.bi.text);
    setText('ca-bi-meta', summary.bi.meta);
    setText('ca-xd-state', summary.xd.text);
    setText('ca-xd-meta', summary.xd.meta);
    setText('ca-formal-direction-state', summary.formalDirectionLabel);
    setText('ca-formal-direction-meta', summary.formalDirectionDetail);
    setTone('ca-formal-direction-row', summary.formalDirectionTone);
    setText('ca-bi-zone-state', summary.biZone.text);
    setText('ca-bi-zone-meta', summary.biZone.meta);
    setTone('ca-bi-zone-row', summary.biZone.tone);
    setText('ca-xd-zone-state', summary.xdZone.text);
    setText('ca-xd-zone-meta', summary.xdZone.meta);
    setTone('ca-xd-zone-row', summary.xdZone.tone);
    renderZoneAudit('ca-bi-zone', summary.biZone);
    renderZoneAudit('ca-xd-zone', summary.xdZone);
    renderSignal('ca-mmd', summary.mmd);
    renderSignal('ca-bc', summary.bc);
    setText('ca-plan-now', summary.plan.now);
    setText('ca-plan-wait', summary.plan.wait);
    setText('ca-plan-boundary', summary.plan.boundary);
    setText('ca-analysis-updated', summary.hasData ? `${summary.resolutionLabel}结构已重算` : '等待结构计算');
  }

  function layerIsVisible(manager, layer) {
    const config = manager && manager.cl_show_config;
    if (!config || typeof config !== 'object') return false;
    const definition = LAYER_CONFIG_KEYS[layer];
    return Boolean(definition && config[definition.key] !== false);
  }

  function persistLayerConfig(manager) {
    if (!root || !root.localStorage || !manager || !manager.id) return;
    const resolution = String(manager._curResolution || '1').trim().toLowerCase() || '_';
    try {
      const key = `cl_show_config_${manager.id}_${resolution}`;
      const value = JSON.stringify(manager.cl_show_config);
      if (root.AccountPreferences && typeof root.AccountPreferences.setItem === 'function') {
        root.AccountPreferences.setItem(key, value);
      } else {
        root.localStorage.setItem(key, value);
      }
    } catch (_) { /* display preferences are optional */ }
  }

  function setLayerVisibility(manager, layer, visible) {
    if (!manager || !manager.cl_show_config || typeof manager.cl_show_config !== 'object') return false;
    const config = manager.cl_show_config;
    const enabled = Boolean(visible);
    const definition = LAYER_CONFIG_KEYS[layer];
    if (!definition) return false;
    config[definition.key] = enabled;
    persistLayerConfig(manager);
    if (typeof manager.debouncedDrawChanlun === 'function') manager.debouncedDrawChanlun();
    return true;
  }

  function syncLayerControls(manager) {
    if (!root || !root.document) return;
    root.document.querySelectorAll('[data-chart-layer]').forEach((button) => {
      const active = layerIsVisible(manager, button.dataset.chartLayer);
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    setText(
      'ca-layer-status',
      manager ? '图层开关仅控制当前图表显示，不改变结构计算结果。' : '等待当前图表初始化。',
    );
  }

  function cachedSymbolInfo(manager, identity, token, data, options) {
    const key = `${identity.symbol}|${identity.interval}`;
    if (browserState.symbolInfoCache.has(key)) {
      return Promise.resolve(browserState.symbolInfoCache.get(key));
    }
    try {
      if (!manager.chart || typeof manager.chart.symbolExt !== 'function') return Promise.resolve(null);
      return Promise.resolve(manager.chart.symbolExt()).then((info) => {
        if (info) browserState.symbolInfoCache.set(key, info);
        if (token === browserState.renderToken && info) {
          const timeZone = info.timezone || options.timeZone;
          renderSummary(summarizeChartData(data, {
            ...options,
            resolution: identity.interval,
            timeZone,
          }), identity, info);
        }
        return info;
      }).catch(() => null);
    } catch (_) {
      return Promise.resolve(null);
    }
  }

  function refresh(detail) {
    const manager = currentManager(detail || {});
    const identity = managerIdentity(manager);
    if (!manager || !identity) {
      const market = root.Utils && typeof root.Utils.get_market === 'function' ? root.Utils.get_market() : '';
      const code = root.Utils && typeof root.Utils.get_code === 'function' ? root.Utils.get_code() : '--';
      renderSummary(summarizeChartData({}, { resolution: '' }), {
        symbol: `${market}:${code}`,
        interval: '',
      }, null);
      syncLayerControls(null);
      return false;
    }

    const data = dataForManager(manager, identity, detail || {});
    const parsed = splitIdentity(identity);
    const options = {
      resolution: identity.interval,
      timeZone: MARKET_TIMEZONES[parsed.market],
      symbol: identity.symbol,
      cachedStrictSnapshot: manager._strictStructureSnapshot || null,
    };
    const token = ++browserState.renderToken;
    const symbolCacheKey = `${identity.symbol}|${identity.interval}`;
    const cachedInfo = browserState.symbolInfoCache.get(symbolCacheKey) || null;
    renderSummary(summarizeChartData(data || {}, options), identity, cachedInfo);
    if (data && !cachedInfo) cachedSymbolInfo(manager, identity, token, data, options);
    syncLayerControls(manager);
    return Boolean(data);
  }

  function readOverviewCollapsed() {
    try {
      const stored = root.AccountPreferences && typeof root.AccountPreferences.getItem === 'function'
        ? root.AccountPreferences.getItem(OVERVIEW_COLLAPSE_STORAGE_KEY)
        : root.localStorage.getItem(OVERVIEW_COLLAPSE_STORAGE_KEY);
      return stored === null ? true : stored !== '0';
    } catch (_) {
      return true;
    }
  }

  function setOverviewCollapsed(collapsed, persist) {
    const overview = element('ca-overview');
    const body = element('ca-overview-body');
    const button = element('ca-overview-toggle');
    if (!overview || !body || !button) return;
    overview.classList.toggle('is-collapsed', Boolean(collapsed));
    body.hidden = Boolean(collapsed);
    button.setAttribute('aria-expanded', String(!collapsed));
    button.setAttribute('aria-label', collapsed ? '展开当前结构解读' : '收起当前结构解读');
    button.title = collapsed ? '展开当前结构解读' : '收起当前结构解读';
    setText('ca-overview-toggle-label', collapsed ? '展开' : '收起');
    if (persist) {
      try {
        if (root.AccountPreferences && typeof root.AccountPreferences.setItem === 'function') {
          root.AccountPreferences.setItem(OVERVIEW_COLLAPSE_STORAGE_KEY, collapsed ? '1' : '0');
        } else {
          root.localStorage.setItem(OVERVIEW_COLLAPSE_STORAGE_KEY, collapsed ? '1' : '0');
        }
      } catch (_) { /* 本地存储可能不可用 */ }
    }
  }
  function init() {
    if (!root || !root.document || browserState.initialized) return;
    browserState.initialized = true;
    const overviewToggle = element('ca-overview-toggle');
    setOverviewCollapsed(readOverviewCollapsed(), false);
    if (overviewToggle) {
      overviewToggle.addEventListener('click', () => {
        setOverviewCollapsed(overviewToggle.getAttribute('aria-expanded') === 'true', true);
      });
    }
    root.document.querySelectorAll('[data-chart-layer]').forEach((button) => {
      button.addEventListener('click', () => {
        const manager = currentManager({});
        if (!manager) {
          syncLayerControls(null);
          return;
        }
        const layer = button.dataset.chartLayer;
        setLayerVisibility(manager, layer, !layerIsVisible(manager, layer));
        syncLayerControls(manager);
      });
    });
    root.addEventListener('chanlun-bars-ready', (event) => {
      refresh(event && event.detail ? event.detail : {});
    });
    const refreshButton = element('ca-refresh-analysis');
    if (refreshButton) {
      refreshButton.addEventListener('click', () => refresh({}));
    }
    refresh({});
    browserState.timer = root.setInterval(() => {
      if (!root.document.hidden) refresh({});
    }, 5000);
  }

  const api = {
    formatPrice,
    formatResolution,
    formatTimestamp,
    summarizeChartData,
    setLayerVisibility,
    layerIsVisible,
    refresh,
    init,
  };

  if (root && root.document) {
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
      init();
    }
  }

  return api;
});
