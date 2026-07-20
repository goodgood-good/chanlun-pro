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
    'l2buy': '类二类买点',
    '3buy': '三类买点',
    'l3buy': '类三类买点',
    '1sell': '一类卖点',
    '2sell': '二类卖点',
    'l2sell': '类二类卖点',
    '3sell': '三类卖点',
    'l3sell': '类三类卖点',
  });

  const MMD_ALIASES = Object.freeze({
    '1b': '1buy', '2b': '2buy', 'l2b': 'l2buy', '3b': '3buy', 'l3b': 'l3buy',
    '1s': '1sell', '2s': '2sell', 'l2s': 'l2sell', '3s': '3sell', 'l3s': 'l3sell',
  });

  const BC_LABELS = Object.freeze({
    pz: '盘整背驰',
    qs: '趋势背驰',
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

  const LAYER_CONFIG_KEYS = Object.freeze({
    bi: { key: 'bi' },
    xd: { key: 'xd' },
    bi_zs: { key: 'zs_bi', parent: 'zs_all' },
    xd_zs: { key: 'zs_xd', parent: 'zs_all' },
    bi_mmd: { key: 'mmd_bi', parent: 'mmd' },
    xd_mmd: { key: 'mmd_xd', parent: 'mmd' },
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
      '1D': '日线',
      '2D': '2日线',
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
    const status = String(item.linestyle) === '1' ? '形成中' : '已完成';
    return {
      exists: true,
      text: `${direction} · ${status}`,
      meta: `${formatPrice(startPrice)} → ${formatPrice(endPrice)} · ${status === '形成中' ? '未闭合' : '已闭合'}`,
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

  function associatedPointText(values) {
    const labels = (Array.isArray(values) ? values : [])
      .map((value) => {
        const raw = typeof value === 'object' && value ? value.point_type : value;
        const canonical = MMD_ALIASES[String(raw || '').toLowerCase()] || String(raw || '').toLowerCase();
        return MMD_LABELS[canonical] || String(raw || '');
      })
      .filter(Boolean);
    return labels.length ? Array.from(new Set(labels)).join('、') : '暂无关联买卖点';
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
        recursiveLevel: settings.tower === 'bi' ? '观察层' : `L${settings.recursiveLevel || 0}`,
        zd: '--',
        zg: '--',
        completion: '尚未形成',
        enteringSegment: '未提供进入段',
        leavingSegment: '未提供离开段',
        associatedPoint: '暂无关联买卖点',
      };
    }

    const prices = shapePoints(latest)
      .map((item) => numeric(item && item.price))
      .filter((value) => value !== null);
    const explicitZd = numeric(latest.zd);
    const explicitZg = numeric(latest.zg);
    const low = explicitZd === null ? (prices.length ? Math.min.apply(null, prices) : null) : explicitZd;
    const high = explicitZg === null ? (prices.length ? Math.max.apply(null, prices) : null) : explicitZg;
    const status = typeof latest.done === 'boolean'
      ? (latest.done ? '已完成' : '形成中')
      : (String(latest.linestyle) === '1' ? '形成中' : '已完成');
    // The collection itself is authoritative.  Old cached payloads could carry
    // a stale ``tower`` value and must not relabel a bi center as an xd center.
    const tower = String(settings.tower || latest.tower || '').toLowerCase();
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
      meta: `${status} · ${positionMeta}`,
      levelLabel,
      status,
      position,
      low,
      high,
      time: latestTime,
      tone: status === '形成中' ? 'forming' : 'complete',
      tower: tower === 'bi' ? '笔' : '线段',
      recursiveLevel: recursiveLevel === null
        ? (tower === 'bi' ? '观察层' : `L${settings.recursiveLevel || 0}`)
        : `L${recursiveLevel}`,
      zd: formatPrice(low),
      zg: formatPrice(high),
      completion: status,
      enteringSegment: segmentAuditText(latest.entering_segment, '未提供进入段'),
      leavingSegment: segmentAuditText(latest.leaving_segment, '未提供离开段'),
      associatedPoint: associatedPointText(latest.associated_points),
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
    const normalizedLevel = String(latest.level || '').trim().toLowerCase();
    const levelLabel = normalizedLevel.indexOf('xd') >= 0 || normalizedLevel.indexOf('segment') >= 0
      ? '线段'
      : (normalizedLevel.indexOf('bi') >= 0 || normalizedLevel.indexOf('pen') >= 0 ? '笔' : '结构');

    let label = rawText || (kind === 'mmd' ? '买卖点' : '背驰');
    let isBuy = false;
    let isSell = false;
    if (kind === 'mmd') {
      const canonical = MMD_ALIASES[normalizedText] || normalizedText;
      label = MMD_LABELS[canonical] || label;
      isBuy = canonical.endsWith('buy');
      isSell = canonical.endsWith('sell');
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

  function structureNarrative(bi, xd, biZone, xdZone) {
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
    return {
      headline,
      detail: `${positionFact}；${mutableFact}`,
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

  function buildPlan(bi, xd, biZone, xdZone) {
    const now = [];
    if (xd.exists) now.push(`线段${xd.direction}（${xd.status === '形成中' ? '未闭合' : '已闭合'}）`);
    if (bi.exists) now.push(`当前笔${bi.direction}（${bi.status === '形成中' ? '未闭合' : '已闭合'}）`);
    const zonePositions = [biZone, xdZone]
      .map(zonePositionLabel)
      .filter(Boolean);
    if (zonePositions.length) now.push(`现价位于${zonePositions.join('、')}`);

    let wait = '等待至少一笔形成并闭合，再检查线段与中枢。';
    if (bi.exists || xd.exists) {
      const conditions = [];
      if (bi.exists && bi.status === '形成中') {
        conditions.push(`先等待当前${bi.direction}笔闭合`);
      } else if (xd.exists && xd.status === '形成中') {
        conditions.push(`先等待当前${xd.direction}线段闭合`);
      } else {
        conditions.push('等待下一笔或线段形成');
      }
      const primaryZone = xdZone.exists ? xdZone : biZone;
      const zoneCondition = zoneConfirmation(primaryZone);
      if (zoneCondition) conditions.push(zoneCondition);
      wait = `${conditions.join('；')}。条件未出现前，不升级结构判断。`;
    }

    const boundaryFacts = [];
    if (bi.exists && bi.status === '形成中' && bi.startPrice !== null) {
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
  function segmentZoneItems(source) {
    const recursiveZones = [];
    (Array.isArray(source && source.recursive_levels) ? source.recursive_levels : []).forEach((level) => {
      if (!level || Number(level.level) !== 0 || !Array.isArray(level.zss)) return;
      level.zss.forEach((zone) => recursiveZones.push({
        ...zone,
        tower: zone && zone.tower ? zone.tower : 'xd',
        recursive_level: zone && zone.recursive_level != null
          ? zone.recursive_level
          : Number(level.level),
      }));
    });
    if (recursiveZones.length) return recursiveZones;
    return Array.isArray(source && source.xd_zss) ? source.xd_zss : [];
  }
  function summarizeChartData(data, context) {
    const source = data || {};
    const options = context || {};
    const bars = Array.isArray(source.bars) ? source.bars : [];
    const latestBar = bars.length ? bars[bars.length - 1] : null;
    const latestClose = numeric(latestBar && latestBar.close);
    const timeZone = options.timeZone;
    const bi = summarizeLine(source.bis);
    const xd = summarizeLine(source.xds);
    const biZone = summarizeZone(source.bi_zss, latestClose, '笔中枢', {
      tower: 'bi',
      recursiveLevel: null,
    });
    const xdZone = summarizeZone(segmentZoneItems(source), latestClose, '线段中枢', {
      tower: 'xd',
      recursiveLevel: 0,
    });
    const mmd = latestSignal(
      [source.bi_mmds, source.xd_mmds, source.mmds],
      bars,
      'mmd',
      timeZone,
      latestClose,
    );
    const bc = latestSignal(
      [source.bi_bcs, source.xd_bcs, source.bcs],
      bars,
      'bc',
      timeZone,
      latestClose,
    );
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

  const browserState = {
    initialized: false,
    managerId: null,
    renderToken: 0,
    symbolInfoCache: new Map(),
    timer: null,
  };

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
    } catch (_) { /* chart is still initializing */ }
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
    setText(`${prefix}-exit`, zone.leavingSegment);
    setText(`${prefix}-point`, zone.associatedPoint);
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
    if (layer === 'recursive') {
      if (config.recursive_layers === false) return false;
      const maxLevel = Math.max(0, Number(manager._recMaxLevel) || 0);
      for (let level = 1; level <= maxLevel; level += 1) {
        if (config[`zs_L${level}`] === false) return false;
      }
      return true;
    }
    const definition = LAYER_CONFIG_KEYS[layer];
    return Boolean(definition && config[definition.key] !== false);
  }

  function persistLayerConfig(manager) {
    if (!root || !root.localStorage || !manager || !manager.id) return;
    const resolution = String(manager._curResolution || '1').trim().toLowerCase() || '_';
    try {
      root.localStorage.setItem(
        `cl_show_config_${manager.id}_${resolution}`,
        JSON.stringify(manager.cl_show_config),
      );
    } catch (_) { /* display preferences are optional */ }
  }

  function setLayerVisibility(manager, layer, visible) {
    if (!manager || !manager.cl_show_config || typeof manager.cl_show_config !== 'object') return false;
    const config = manager.cl_show_config;
    const enabled = Boolean(visible);
    if (layer === 'recursive') {
      const maxLevel = Math.max(0, Number(manager._recMaxLevel) || 0);
      config.recursive_layers = enabled;
      for (let level = 1; level <= maxLevel; level += 1) {
        for (const prefix of ['zs', 'xd', 'mmd', 'bc']) config[`${prefix}_L${level}`] = enabled;
      }
      if (enabled) config.zs_all = true;
    } else {
      const definition = LAYER_CONFIG_KEYS[layer];
      if (!definition) return false;
      config[definition.key] = enabled;
      if (enabled && definition.parent) config[definition.parent] = true;
    }
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
      return root.localStorage.getItem('chart_analysis_overview_collapsed') === '1';
    } catch (_) {
      return false;
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
    button.setAttribute('aria-label', collapsed ? '展开当前结构判读' : '收起当前结构判读');
    button.title = collapsed ? '展开当前结构判读' : '收起当前结构判读';
    if (persist) {
      try {
        root.localStorage.setItem('chart_analysis_overview_collapsed', collapsed ? '1' : '0');
      } catch (_) { /* storage may be unavailable */ }
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
