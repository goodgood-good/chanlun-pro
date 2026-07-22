// 缠论显示配置（cl_show_config）与独立周期画线开关（cl_independent_drawings）
// 按 ChartManager 实例和图表周期独立维护，key 形如 cl_show_config_<chartId>_<resolution>；
// 旧全局 key 仅在新 key 不存在时作为默认值迁移，老用户设置不丢失。

// 默认的缠论显示项配置
const CL_SHOW_DEFAULT = {
    schema_version: 2,
    fx: true,
    bi: true,
    xd: true,
    center_observation: true,
    center_all: true,
    trend_all: false,
    point_all: true,
    point_1buy: true,
    point_2buy: true,
    point_3buy: true,
    point_1sell: true,
    point_2sell: true,
    point_3sell: true,
    divergence_all: true,
};

function recursiveDisplayLevels(interval) {
    const key = String(interval || '').trim();
    const map = {
        '1': ['1m', '5m', '30m', '日线'],
        '1m': ['1m', '5m', '30m', '日线'],
        '5': ['5m', '30m', '日线'],
        '5m': ['5m', '30m', '日线'],
        '30': ['30m', '日线'],
        '30m': ['30m', '日线'],
        '1D': ['日线'],
        '1d': ['日线'],
        'D': ['日线'],
        'd': ['日线'],
    };
    const fallback = /^\d+$/.test(key) ? `${key}m` : (key || '当前周期');
    const labels = map[key] || [fallback];
    return labels.map((label, level) => ({ label, level }));
}

// 各市场的 TV 显示时区，与后端 services/constants.py:market_timezone 保持一致。
// 之前硬编码 "Asia/Shanghai" 导致美股 K 线按北京时区显示（NY 09:30 EDT → 21:30）。
// currency/currency_spot 在后端用 get_localzone()，前端改为读取浏览器 Intl IANA tz 等价回退。
const MARKET_TIMEZONE = {
    a: "Asia/Shanghai",
    hk: "Asia/Shanghai",
    fx: "Asia/Shanghai",
    us: "America/New_York",
    futures: "Asia/Shanghai",
    ny_futures: "Asia/Shanghai",
};
// Blob documents inherit the parent CSP, which blocks Charting Library's
// bootstrap inline scripts. The bundled sameorigin.html is served without
// that page CSP and is explicitly enabled in the widget options below.
const CHART_DISABLED_FEATURES = Object.freeze([
    "go_to_date",
    "use_blob_for_iframe_loading",
]);
const DEFAULT_STUDY_ALLOWLIST = new Set(["MACD_HTF"]);
function requestedDefaultStudies(search) {
    try {
        const params = new URLSearchParams(search || "");
        const selected = ["MACD_HTF"];
        params.getAll("default_study").forEach((value) => {
            const name = String(value || "").trim();
            if (DEFAULT_STUDY_ALLOWLIST.has(name) && !selected.includes(name)) selected.push(name);
        });
        return selected;
    } catch (e) {
        return ["MACD_HTF"];
    }
}
function _browserLocalTz() {
    try {
        return (Intl.DateTimeFormat().resolvedOptions().timeZone) || "Asia/Shanghai";
    } catch (e) {
        return "Asia/Shanghai";
    }
}
function getMarketTimezone(market) {
    if (market === "currency" || market === "currency_spot") {
        return _browserLocalTz();
    }
    return MARKET_TIMEZONE[market] || "Asia/Shanghai";
}

// 高频调试日志统一走 clog：仅 window.__chanlunDebug=true 时输出，避免生产 console 刷屏。
// 错误诊断（console.warn / console.error）不受此开关控制。
function clog(...args) {
    if (window.__chanlunDebug) console.log(...args);
}

// 旧配置只作为一次性迁移输入；返回值严格限定为当前周期的 schema v2。
// 总开关只 gate，不改写任何子项偏好。
function normalizeClShowConfig(config, interval) {
    const source = (config && typeof config === 'object') ? config : {};
    const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
    const output = Object.assign({}, CL_SHOW_DEFAULT);
    const legacyCenterAll = has('zs_all') ? source.zs_all : (has('zs') ? source.zs : undefined);
    for (const key of [
        'fx', 'bi', 'xd', 'center_observation', 'center_all', 'trend_all',
        'point_all', 'point_1buy', 'point_2buy', 'point_3buy',
        'point_1sell', 'point_2sell', 'point_3sell', 'divergence_all',
    ]) {
        if (has(key)) output[key] = source[key] !== false;
    }
    if (!has('center_all') && legacyCenterAll !== undefined) {
        output.center_all = legacyCenterAll !== false;
    }
    if (!has('center_observation')) {
        if (has('zs_bi')) output.center_observation = source.zs_bi !== false;
        else if (legacyCenterAll !== undefined) output.center_observation = legacyCenterAll !== false;
    }
    if (!has('point_all')) {
        if (has('point_confirmed')) output.point_all = source.point_confirmed !== false;
        else if (has('mmd')) output.point_all = source.mmd !== false;
    }
    if (!has('trend_all')) {
        output.trend_all = Object.keys(source).some(
            (key) => /^(trend|xd)_L[0-9]+$/.test(key) && source[key] === true,
        );
    }
    for (const { level } of recursiveDisplayLevels(interval)) {
        const centerKey = `center_L${level}`;
        const trendKey = `trend_L${level}`;
        const legacyCenterKey = level === 0 ? 'zs_xd' : `zs_L${level}`;
        const legacyTrendKey = `xd_L${level}`;
        output[centerKey] = has(centerKey)
            ? source[centerKey] !== false
            : (has(legacyCenterKey) ? source[legacyCenterKey] !== false : true);
        output[trendKey] = has(trendKey)
            ? source[trendKey] !== false
            : (has(legacyTrendKey) ? source[legacyTrendKey] !== false : true);
        for (const kind of ['consolidation', 'trend']) {
            const divergenceKey = `divergence_${kind}_L${level}`;
            output[divergenceKey] = has(divergenceKey)
                ? source[divergenceKey] !== false
                : true;
        }
    }
    return output;
}

function strictItemEnabled(cfg, item) {
    const config = cfg || {};
    const level = item.structural_level;
    if (item.render_kind === 'center_observation') return config.center_observation !== false;
    if (item.render_kind === 'formal_center' || item.render_kind === 'center_projection') {
        return config.center_all !== false && config[`center_L${level}`] !== false;
    }
    if (item.render_kind === 'strict_trend') {
        return config.trend_all !== false && config[`trend_L${level}`] !== false;
    }
    if (item.render_kind === 'point_confirmed') {
        return config.point_all !== false && config[`point_${item.point_type}`] !== false;
    }
    if (item.render_kind === 'strict_divergence') {
        return config.divergence_all !== false
            && config[`divergence_${item.kind}_L${level}`] !== false;
    }
    return false;
}

// resolution 归一为存储 key 后缀:去空白转小写(1D/1d 同一份),空/未知回退哨兵 '_'。
function _resolutionKey(resolution) {
    const r = (resolution === null || resolution === undefined) ? '' : String(resolution).trim();
    return r ? r.toLowerCase() : '_';
}

function loadClShowConfig(chartId, resolution) {
    try {
        const raw = localStorage.getItem('cl_show_config_' + chartId + '_' + _resolutionKey(resolution));
        if (raw) {
            const parsed = JSON.parse(raw);
            return normalizeClShowConfig(parsed, resolution);
        }
    } catch (e) {
        console.warn('[CHARTS] loadClShowConfig parse failed', e);
    }
    // 该周期从未配置 → null 哨兵,由 resolveClConfigForResolution 决定继承/迁移。
    return null;
}

function saveClShowConfig(chartId, resolution, cfg) {
    try {
        localStorage.setItem(
            'cl_show_config_' + chartId + '_' + _resolutionKey(resolution),
            JSON.stringify(normalizeClShowConfig(cfg, resolution)),
        );
    } catch (e) {
        console.warn('[CHARTS] saveClShowConfig failed', e);
    }
}

// 迁移回退:老用户此前按 chartId(无周期)存的配置,或更早的全局旧 key,作为"从未配过任何周期"时的初始基准,
// 保证升级后首个周期不丢现有设置。均 merge CL_SHOW_DEFAULT 补齐新增开关。
function _clShowConfigBaseline(chartId, resolution) {
    try {
        const raw = localStorage.getItem('cl_show_config_' + chartId);
        if (raw) {
            const parsed = JSON.parse(raw);
            return normalizeClShowConfig(parsed, resolution);
        }
        const legacy = localStorage.getItem('cl_show_config');
        if (legacy) {
            const parsed = JSON.parse(legacy);
            return normalizeClShowConfig(parsed, resolution);
        }
    } catch (e) {
        console.warn('[CHARTS] _clShowConfigBaseline parse failed', e);
    }
    return normalizeClShowConfig({}, resolution);
}

// 按周期解析应用配置:该周期已配过 → 用存储值(persist=false);未配过 → 继承切换前当前配置的副本,
// 无当前配置(首次构造 currentCfg=null)则用迁移基准;persist=true 表示需固化到该周期 key。
function resolveClConfigForResolution(chartId, resolution, currentCfg) {
    const loaded = loadClShowConfig(chartId, resolution);
    if (loaded !== null) {
        return { cfg: loaded, persist: false };
    }
    const base = currentCfg
        ? normalizeClShowConfig(currentCfg, resolution)
        : _clShowConfigBaseline(chartId, resolution);
    return { cfg: base, persist: true };
}

function loadClIndependentDrawings(chartId) {
    try {
        const raw = localStorage.getItem('cl_independent_drawings_' + chartId);
        if (raw !== null) {
            return JSON.parse(raw) === true;
        }
        const legacy = localStorage.getItem('cl_independent_drawings');
        if (legacy !== null) {
            return JSON.parse(legacy) === true;
        }
    } catch (e) {
        console.warn('[CHARTS] loadClIndependentDrawings parse failed', e);
    }
    return false;
}

function saveClIndependentDrawings(chartId, val) {
    try {
        localStorage.setItem('cl_independent_drawings_' + chartId, JSON.stringify(!!val));
    } catch (e) {
        console.warn('[CHARTS] saveClIndependentDrawings failed', e);
    }
}

const CHART_CONFIG = {
    COLORS: {
        DING: "#FA8072", DI: "#1E90FF", BI: "#708090", XD: "#00BFFF",
        BI_ZSS: "#708090", XD_ZSS: "#00BFFF",
        BCS: "#D1D4DC", BC_TEXT: "#fccbcd",
        MMD_UP: "#E64A19", MMD_DOWN: "#1565C0",
        AREA_POS: "#ef5350", AREA_NEG: "#26a69a",
    },
    LINE_STYLES: { SOLID: 0, DOTTED: 1, DASHED: 2 },
    CHART_TYPES: ["fxs", "bis", "xds"],
};

// 买卖点用「双 shape」:小 icon 箭头(定位准,尺寸可调)+text 类型标签(标明几买几卖)。
// 单个形状无法三者兼得:arrow_up/down 定位准+带文字但尺寸写死偏大;icon 小+定位准但装不了文字;
// text 能带文字但有横向宽度且无锚点控制、会偏离目标 K 线。
// 箭头码点取自随库 lt-icons-atlas 的 arrows/ 集(Font Awesome:f062=上箭头、f063=下箭头)。
const MMD_ICON = { buy: 0xf062, sell: 0xf063 };
// 尺寸/字号一处可调:段层为主级别略大并加粗、笔层数量多取小值;合并版居中。
const MMD_ICON_SIZE = { xd: 14, bi: 10, default: 12 };
const MMD_LABEL_FONTSIZE = { xd: 12, bi: 10, default: 11 };
// 买卖点图标/文字只在绘制层偏移,不改后端真实买卖点价格。买点下移、卖点上移,避免盖住 K 线。
// 偏移基准用「近 N 根 K 线平均振幅(ATR 式)」而非绝对价格百分比:
// 价格百分比(price × 0.25%)在高价 / 低波动标的(典型如港美股)上,相对可视波动过大,
// 买卖点会明显浮空、脱离 K 线高低点。改用平均振幅后跨标的 / 周期 / 缩放自适应。
// ATR 不可用时(无 K 线)回退到旧的价格百分比,保持兼容。
const MMD_ICON_ATR_RATIO = 0.8;          // 箭头离端点 ≈ 0.8 根典型 K 线高度
const MMD_LABEL_ATR_RATIO = 1.5;         // 文字标签在箭头外侧 ≈ 1.5 根
const MMD_ICON_PRICE_OFFSET = 0.0025;    // 回退:无 K 线时按价格百分比
const MMD_LABEL_PRICE_OFFSET = 0.0015;

const DEFAULT_COLORS = {
    bis: CHART_CONFIG.COLORS.BI, xds: CHART_CONFIG.COLORS.XD,
    bi_zss: CHART_CONFIG.COLORS.BI_ZSS, xd_zss: CHART_CONFIG.COLORS.XD_ZSS,
};

// ─────────────────────────────────────────────────────────────────────────
// 缠论研习院「学院缠图递归颜色」规范(chanlunschool.com/学院缠图递归颜色)。
// 绝对递归级别链:每个绝对级别一个固定颜色,**同一绝对级别在任何周期图上恒同色**——
// 这正是「递归颜色」的本质,便于跨周期一致辨认。颜色像素级提取自官方「画图级别颜色标准」
// 图(canvas 逐格扫描),详见 audit/recursive_colors_spec.md。
//   index: 0=15秒(白) 1=1FB橙 2=1FC黄 3=1F青 4=5F红 5=30F绿 6=日蓝 7=周粉 8=月橄榄 9=季棕
const LEVEL_COLOR_CHAIN = [
    "#FFFFFF", // 0  15秒(占位,基本不作基础色)
    "#FF8C00", // 1  1FB  橙(深橙 darkorange,从网站#FFC000琥珀加深:与线段的黄#FFFF00拉开色相+明度,更易分辨) —— 1分钟笔
    "#F2C94C", // 2  1FC  黄(柔和黄,从刺眼纯黄#FFFF00改柔,护眼+与笔的橙更分;同步柔化笔中枢/5m笔) —— 1分钟线段
    "#07C9E9", // 3  1F   青(cyan)
    "#FF0000", // 4  5F   红(red)
    "#66FF66", // 5  30F  绿(green)
    "#5B9BD5", // 6  日线 蓝(blue)
    "#FF99FF", // 7  周线 粉(pink)
    "#70AD46", // 8  月线 橄榄绿(olive)
    "#C35811", // 9  季线 棕(brown)
];
// 按链索引取色:溢出(深递归 > 9)在 [1..9] 区间循环,既不 undefined 又仍可辨。
function chainColor(idx) {
    if (idx <= 0) return LEVEL_COLOR_CHAIN[0];
    if (idx < LEVEL_COLOR_CHAIN.length) return LEVEL_COLOR_CHAIN[idx];
    const span = LEVEL_COLOR_CHAIN.length - 1; // 9
    return LEVEL_COLOR_CHAIN[1 + ((idx - 1) % span)];
}

// 图周期 → 该图「笔」在链上的索引 p。由 FREQ_CHAIN 反推得自洽(令日线恒落 index 6=蓝、
// 周线恒 7=粉…),故 15m/60m 等非标准周期也对齐到与标准周期相同的颜色锚点。
const CHART_BI_INDEX = { "1": 1, "5": 2, "15": 2, "30": 3, "60": 3, "1D": 4, "1W": 5, "1M": 6 };
function chartBiIndex(interval) {
    const p = CHART_BI_INDEX[interval];
    return (typeof p === "number") ? p : 1;
}

// 当前周期 → 各递归级别(L0/L1/L2/L3)的周期标签链。模块级:菜单与左侧级别快捷开关浮条共用。
const FREQ_CHAIN = {
    "1": ["1m", "5m", "30m", "日线"],
    "5": ["5m", "30m", "日线", "周线"],
    "15": ["15m", "60m", "日线", "周线"],
    "30": ["30m", "日线", "周线", "月线"],
    "60": ["60m", "日线", "周线", "月线"],
    "1D": ["日线", "周线", "月线", "年线"],
    "1W": ["周线", "月线", "年线", "10年"],
    "1M": ["月线", "年线", "10年", "30年"],
};

// 元素 → 相对「笔」的链偏移(见 spec §4):
//   笔 bis = +0、线段 xds = +1、笔中枢 bi_zss = +1(中枢色 = 构件级别 +1,笔中枢由笔构成)、
//   线段中枢 xd_zss / 递归 L0 = +2。
const ELEMENT_CHAIN_OFFSET = { bis: 0, xds: 1, bi_zss: 1, xd_zss: 2 };

// 基础元素(笔/线段/笔中枢/线段中枢)按当前周期取链色。替代旧 DYNAMIC_CHART_COLORS。
function getDynamicColor(interval, elementType) {
    const off = ELEMENT_CHAIN_OFFSET[elementType];
    if (typeof off === "number") return chainColor(chartBiIndex(interval) + off);
    return DEFAULT_COLORS[elementType] || "#FFFFFF";
}

// 递归层级中枢 Lk(L0 = 本周期线段中枢) → 链色 C[p+2+k]。
// 1m 图: L0=青(1F)/L1=红(5F)/L2=绿(30F)/L3=蓝(日线)…
function getRecursiveLevelColor(interval, level) {
    return chainColor(chartBiIndex(interval) + 2 + (level || 0));
}

// 递归层级走势类型「线段」线条与本级中枢同绝对级别 → 同色,直接复用 getRecursiveLevelColor:
//   recursive_levels[k].zslx_lines = 分支级 k 走势类型 = 构成第 k+1 周期中枢的构件,
//   按链 = C[p+2+k]。1m 图: L0线条=青(=5分钟线段)/L1线条=红(=30分钟线段)/L2线条=绿(=日线线段),
//   严格对齐网站「5分钟线段=青、30分钟线段=红…」。形状(线 vs 框)区分走势类型与中枢。

// 多周期叠加中枢(P7,已停用)残留路径的占位色;新核心高级别中枢走 getRecursiveLevelColor。
const HIGHER_ZS_COLORS = ["#5C6BC0", "#00897B", "#7E57C2", "#3949AB"];

function debounce(func, wait) {
    let timeout;
    return function (...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}

function getTVRegistry() {
    if (!window.ChanlunTVRegistry) {
        window.ChanlunTVRegistry = {
            chartManagers: new Map(),
            datafeeds: new Map(),
            widgets: new Map(),
            activeManagerId: null,
        };
    }
    return window.ChanlunTVRegistry;
}

const ChartUtils = {
    createShape(chart, points, options = {}) {
        const defaults = {
            lock: true, disableSelection: true, disableSave: true, disableUndo: true,
            showInObjectsTree: false, overrides: {},
        };
        const config = { ...defaults, ...options };
        try {
            if (!chart) return Promise.reject("Chart object is null");

            return config.shape === "trend_line" || config.shape === "rectangle" || config.shape === "circle"
                ? chart.createMultipointShape(points, config)
                : chart.createShape(points, config);
        } catch (e) {
            console.error("[DEBUG-CHARTS] Shape create failed:", e);
            return Promise.reject(e);
        }
    },
    createFxShape(chart, fx, options = {}) {
        const color = fx.text === "ding" ? CHART_CONFIG.COLORS.DING : CHART_CONFIG.COLORS.DI;
        return this.createShape(chart, fx.points, { shape: "circle", overrides: { backgroundColor: color, color: color, linewidth: 4, ...options.overrides }, ...options });
    },
    createLineShape(chart, line, options = {}) {
        return this.createShape(chart, line.points, { shape: "trend_line", overrides: { linestyle: parseInt(line.linestyle) || 0, linewidth: options.linewidth || 1, linecolor: options.color || CHART_CONFIG.COLORS.BI, ...options.overrides }, ...options });
    },
    createZhongshuShape(chart, zs, options = {}) {
        const { overrides = {}, ...shapeOptions } = options;
        const color = shapeOptions.color || CHART_CONFIG.COLORS.BI;
        const linewidth = shapeOptions.linewidth || 1;
        const transparency = 95;
        return this.createShape(chart, zs.points, { shape: "rectangle", ...shapeOptions, overrides: { linestyle: parseInt(zs.linestyle) || 0, linewidth, linecolor: color, backgroundColor: color, transparency, color, "trendline.linecolor": color, fillBackground: true, filled: true, ...overrides } });
    },
    // 买卖点偏移基准:近 N 根 K 线平均振幅(high-low)。波动越大基准越大,
    // 跨标的 / 周期 / 缩放自适应。无有效 K 线时返回 0,调用方回退到价格百分比。
    computeMmdOffsetBase(bars) {
        if (!Array.isArray(bars) || bars.length === 0) return 0;
        const slice = bars.slice(-60);
        let sum = 0, cnt = 0;
        for (const b of slice) {
            if (b && typeof b.high === "number" && typeof b.low === "number" && b.high >= b.low) {
                sum += b.high - b.low;
                cnt++;
            }
        }
        return cnt > 0 ? sum / cnt : 0;
    },
    // 买卖点是否为"买":统一口径(小写含 b),供偏移/颜色/箭头三处共用,杜绝大小写漂移。
    // bs_type 变体(1buy/2buy/3buy/类1buy/大写 1B…)均含 b;sell/S 不含 b。
    _mmdIsBuy(mmd) {
        return ((mmd && mmd.text) || "").toLowerCase().includes("b");
    },
    // 偏移点:优先用 ATR 基准(offsetBase × atrRatio);基准不可用时回退价格百分比。
    mmdOffsetPoint(mmd, atrRatio, priceRatioFallback, offsetBase = 0, fromPoint = null) {
        const src = fromPoint || mmd.points || {};
        const base = mmd.points || {};
        if (typeof src.price !== "number" || typeof base.price !== "number") return src;
        const off = offsetBase > 0
            ? offsetBase * atrRatio
            : Math.abs(base.price) * priceRatioFallback;
        // 买点下移(price-off)、卖点上移(price+off)。判定与颜色/箭头同口径(_mmdIsBuy):
        // 修历史 bug——此处曾用大写 includes("B"),而默认 branch_core 文本是小写 buy → 买点被误画到上方。
        return { ...src, price: this._mmdIsBuy(mmd) ? src.price - off : src.price + off };
    },
    // 买卖点箭头锚点:买点放到 K 线下方、卖点放到 K 线上方,避免和 high/low 重叠。
    mmdIconPoint(mmd, offsetBase = 0) {
        return this.mmdOffsetPoint(mmd, MMD_ICON_ATR_RATIO, MMD_ICON_PRICE_OFFSET, offsetBase);
    },
    // 买卖点标签锚点:文字在箭头外侧再让出一档,避免文字、箭头、K 线三者互相覆盖。
    mmdLabelPoint(mmd, offsetBase = 0) {
        const p = mmd.points || {};
        if (typeof p.price !== "number") return p;
        return this.mmdOffsetPoint(mmd, MMD_LABEL_ATR_RATIO, MMD_LABEL_PRICE_OFFSET, offsetBase, this.mmdIconPoint(mmd, offsetBase));
    },
    // 买卖点箭头:icon 单字形,尺寸可控、横向居中锚定到 K 线(定位与分型圆点一致,准确);
    // 使用绘制层偏移点,避免箭头贴住或覆盖 K 线;原始 mmd.points 仍保留真实买卖点位置。
    createMmdShape(chart, mmd, options = {}) {
        const { offsetBase = 0, ...rest } = options;
        const isBuy = this._mmdIsBuy(mmd);   // 统一口径(小写含 b):buy/1B/3buy… 含 b;sell/S 不含
        const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
        const isSplit = !!mmd.level;
        const isXd = isSplit && mmd.level === "xd";
        const isHi = isSplit && mmd.level !== "xd" && mmd.level !== "bi";  // 5m/30m/… 高级别买卖点
        const size = isHi ? MMD_ICON_SIZE.xd : (isSplit ? (isXd ? MMD_ICON_SIZE.xd : MMD_ICON_SIZE.bi) : MMD_ICON_SIZE.default);
        const icon = isBuy ? MMD_ICON.buy : MMD_ICON.sell;
        return this.createShape(chart, this.mmdIconPoint(mmd, offsetBase), {
            shape: "icon",
            icon,
            overrides: { color, size, "linetoolicon.color": color, "linetoolicon.size": size, ...rest.overrides },
            ...rest,
        });
    },
    // 买卖点文字标签:第二个 shape,标明级别+类型(段1B / 笔3B / 笔L3B…)以区分一二三类。
    // 标签单独纵向偏移;text 有横向宽度会向右展开,定位以箭头为准,标签仅作说明。
    createMmdLabelShape(chart, mmd, options = {}) {
        const { offsetBase = 0, ...rest } = options;
        const isBuy = this._mmdIsBuy(mmd);   // 统一口径, 与偏移/箭头一致
        const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
        const isSplit = !!mmd.level;
        const isXd = isSplit && mmd.level === "xd";
        const isHi = isSplit && mmd.level !== "xd" && mmd.level !== "bi";   // 5m/30m/… 高级别
        const fontsize = isHi ? MMD_LABEL_FONTSIZE.xd : (isSplit ? (isXd ? MMD_LABEL_FONTSIZE.xd : MMD_LABEL_FONTSIZE.bi) : MMD_LABEL_FONTSIZE.default);
        // 级别前缀:段/笔(线段/笔)、否则用 freq 标签(5m·/30m·/日线·)
        const levelPrefix = isSplit ? (isXd ? "段" : mmd.level === "bi" ? "笔" : (mmd.level + "·")) : "";
        const text = levelPrefix + mmd.text.replace(/[笔段]:/g, "");
        return this.createShape(chart, this.mmdLabelPoint(mmd, offsetBase), {
            shape: "text",
            text,
            overrides: {
                color,
                fontsize,
                bold: isXd,
                "linetooltext.color": color,
                "linetooltext.fontsize": fontsize,
                "linetooltext.bold": isXd,
                ...rest.overrides,
            },
            ...rest,
        });
    },
    createBcShape(chart, bc, options = {}) {
        const lvl = bc.level;   // 5m/30m/… 高级别背驰加 freq 前缀(段/笔不加)
        const prefix = (lvl && lvl !== "xd" && lvl !== "bi") ? (lvl + "·") : "";
        return this.createShape(chart, bc.points, { shape: "balloon", text: prefix + bc.text, overrides: { markerColor: CHART_CONFIG.COLORS.BCS, backgroundColor: CHART_CONFIG.COLORS.BCS, textColor: CHART_CONFIG.COLORS.BC_TEXT, transparency: 70, backgroundTransparency: 70, fontsize: 12, ...options.overrides }, ...options });
    },
};

function getInitialChartInterval(chartId) {
    try {
        const bootstrap = window.__chanlunUrlBootstrap;
        const index = Number.parseInt(String(chartId), 10) - 1;
        if (bootstrap && Array.isArray(bootstrap.intervals) && index >= 0) {
            const interval = bootstrap.intervals[index];
            if (typeof interval === "string" && interval) return interval;
        }
    } catch (e) { /* 回退到当前用户保存的周期 */ }
    try {
        const market = Utils.get_market();
        return Utils.get_local_data(`${market}_interval_${chartId}`) || "1";
    } catch (e) {
        return "1";
    }
}

function shouldLoadLastChart() {
    try {
        return !window.__chanlunUrlBootstrap;
    } catch (e) {
        return true;
    }
}

class ChartManager {
    constructor(id) {
        this.id = id;
        this.instanceId = `chart-manager-${id}`;
        this.obj_charts = {};
        this.widget = null;
        this.udf_datafeed = null;
        this.chart = null;
        this.debouncedDrawChanlun = debounce(() => this.draw_chanlun(), 300);
        this.macdStudyId = null;
        this._defaultStudiesPromise = null;
        this._defaultStudyIds = new Map();
        // 每个图表面板独立维护缠论显示配置与独立周期画线开关
        // 初始 resolution:优先本地已记录(切过周期即有),回退 "1";据此载入该周期显示配置,
        // 未配过则用迁移基准(不丢老用户旧配置)。this._curResolution 供切周期/ toggle 保存复用。
        let _initRes = '1';
        try {
            const _saved = getInitialChartInterval(this.id);
            if (_saved) _initRes = _saved;
        } catch (e) { /* 回退默认 '1' */ }
        this._curResolution = _initRes;
        const _r0 = resolveClConfigForResolution(this.id, _initRes, null);
        this.cl_show_config = _r0.cfg;
        if (_r0.persist) saveClShowConfig(this.id, _initRes, _r0.cfg);
        this.cl_independent_drawings = loadClIndependentDrawings(this.id);
        this._initialLoadDone = false; // 首次数据就绪前屏蔽 visibleRangeChange 重绘
        // 当前标的/周期的数据就绪代际。bars_result 更新早于 TradingView 接收 K 线，
        // 因此首次算法图形必须等当前代际 dataReady() 为 true 后才能创建。
        this._dataContextVersion = 0;
        this._tvDataReadyVersion = -1;
        this._tvDataReadyIdentity = null;
        this._pendingChanlunDrawVersion = null;
        this._pendingChanlunDrawIdentity = null;
        this._dataReadyProbeVersion = null;
        this._dataReadyProbeIdentity = null;
        this._intervalVersion = 0;
        this._drawingsCache = new Map();  // 按 symbol+interval 缓存用户画线状态
        this._intervalSwitchSeq = 0;
        this._drawingsRequestSeq = 0;
        this._latestAppliedBarTime = null;
        this._is_switching_interval = false;
        this._is_drawing_chanlun = false;
        this._drawingStatePhase = null;
        this._activeDrawingMutations = new Set();
        this.isApplyingDrawingState = false;
        this._pendingSaveState = null;
        this._saveScheduled = false;
        this._saveInFlight = null;
        this._drawingSaveInFlight = false;
        this._drawingSavePending = null;
        this._barReadyHandler = null;
        this._sse = null;  // SSE 实时推送连接(每图表一个; flag 关/不支持时为 null)
        this._visibilityHandler = null;  // 页面可见性兜底监听句柄
        this._onlineHandler = null;      // 网络恢复(online)监听句柄: 断网后立即重连 SSE 补断档
        this._needResetOnNextData = false; // reconnect 后置位: 下一 SSE 帧无条件全量补齐(治轮询竞态/服务端重启)
        this._watchdogTimer = null;      // 墙钟新鲜度看门狗定时器(SSE 帧驱动检测的失速兜底)
        this._resetState = {};           // per-resKey reset 记账: {lastResetSec, backoffLevel, lastViewLatestSec}
        this._disconnectedSinceMs = null; // es.onerror 记录的断开时刻(ms); onmessage 按断流时长判真断档(M-3)
        // reconcile 失败自动重试状态：count 累计失败次数，timer 已排队的句柄（防重复）
        this._reconcileRetry = { count: 0, timer: null };
        // reconcile 创建过的全部 entity id 集合，用于 sweep 时识别孤儿 shape。
        // safeRemove 静默失败或同一 key 两次 create 时 container 与 TV 会脱钩，
        // 孤儿 shape 残留为长斜线；sweep 强制 removeEntity 清除。
        // 用户手画的 shape 从未进入此 set，不会被误删。
        this._reconcileOwnedIds = new Set();
        // reconcile 精确状态守卫：{ 'symbolKey__type': { from, keys, unfinishedKeys } }
        // 完整几何 key、可视区起点和未完成状态都相同才跳过；不截断，避免最新中枢
        // 边界修正被误判成无变化。Set 比较复用 reconcile 已生成的 newKeys，无需额外排序。
        this._reconcileGuard = {};
        // full rebuild 后 500ms 补一次 verify-rebuild，让 TV 在稳定布局上重新落位 shape
        this._verifyRebuildTimer = null;
        this._verifyingUntil = null;  // performance.now() 时间戳，在此之前的 reconcile 属于 verify 内部
        // 严格结构使用独立、按图表实例隔离的 ownership 容器。状态在首次消费原子
        // strict_structure 时惰性初始化，避免 charts.js 单测或降级页面缺少辅助脚本时崩溃。
        this._strictContainers = new Map();
        this._strictScopes = new Set();
        this._strictDesiredByScope = new Map();
        this._strictPendingCreates = new Map();
        this._strictReconcileEpoch = null;
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        this._strictStructureStatus = { state: 'waiting', code: null };
    }

    enqueueLatestDrawingSave(taskFactory) {
        if (typeof taskFactory !== "function") {
            return Promise.reject(new TypeError("drawing save task must be a function"));
        }

        return new Promise((resolve, reject) => {
            if (!this._drawingSavePending) {
                this._drawingSavePending = { taskFactory, waiters: [] };
            } else {
                // Requests waiting behind the active write all observe the latest state.
                this._drawingSavePending.taskFactory = taskFactory;
            }
            this._drawingSavePending.waiters.push({ resolve, reject });
            this._drainDrawingSaveQueue();
        });
    }

    async _drainDrawingSaveQueue() {
        if (this._drawingSaveInFlight || !this._drawingSavePending) return;
        const job = this._drawingSavePending;
        this._drawingSavePending = null;
        this._drawingSaveInFlight = true;
        try {
            await job.taskFactory();
            job.waiters.forEach((waiter) => waiter.resolve());
        } catch (error) {
            if (typeof layer !== "undefined" && layer && typeof layer.msg === "function") {
                layer.msg("画线保存失败，请检查网络后重试");
            }
            job.waiters.forEach((waiter) => waiter.reject(error));
        } finally {
            this._drawingSaveInFlight = false;
            if (this._drawingSavePending) {
                this._drainDrawingSaveQueue();
            }
        }
    }
    getDrawingsCacheKey(symbol, interval) {
        const mode = this.cl_independent_drawings ? "ind" : "shared";
        const resolutionKey = this.cl_independent_drawings ? interval : "all";
        return `${symbol}_${resolutionKey}_${mode}`;
    }

    getCurrentChartIdentity() {
        if (!this.chart) return null;
        return {
            symbol: this.chart.symbol(),
            interval: this.chart.resolution(),
        };
    }

    createContextToken(symbol, interval) {
        return `${this.instanceId}:${symbol}:${interval}:${++this._drawingsRequestSeq}`;
    }

    beginContextSwitch(reason, symbol, interval) {
        const token = this.createContextToken(symbol, interval);
        this._activeContextToken = token;
        this._drawingsLoadToken = token;
        this._is_switching_interval = true;
        this._drawingStatePhase = reason;
        return token;
    }

    isTokenCurrent(token) {
        return !!token && token === this._activeContextToken;
    }

    finishContextSwitch(token) {
        if (!this.isTokenCurrent(token)) return;
        this._is_switching_interval = false;
        this._drawingStatePhase = null;
    }

    markDrawingMutationStart(phase) {
        const mutationPhase = phase || 'unknown';
        this._activeDrawingMutations.add(mutationPhase);
        this._is_drawing_chanlun = this._activeDrawingMutations.size > 0;
        this._drawingStatePhase = mutationPhase;
    }

    markDrawingMutationEnd(phase) {
        const mutationPhase = phase || 'unknown';
        this._activeDrawingMutations.delete(mutationPhase);
        this._is_drawing_chanlun = this._activeDrawingMutations.size > 0;
        this._drawingStatePhase = this._is_drawing_chanlun
            ? Array.from(this._activeDrawingMutations)[this._activeDrawingMutations.size - 1]
            : null;
    }

    shouldSuppressDrawingSave() {
        return this._is_switching_interval || this._activeDrawingMutations.size > 0 || this.isApplyingDrawingState;
    }

    setDrawingsCache(key, state) {
        this._drawingsCache.set(key, state);
        if (this._drawingsCache.size > 200) {
            const oldestKey = this._drawingsCache.keys().next().value;
            if (oldestKey !== undefined) {
                this._drawingsCache.delete(oldestKey);
            }
        }
    }

    isDrawingStateEmpty(state) {
        if (!state || !state.sources) return true;
        if (state.sources instanceof Map) {
            return state.sources.size === 0;
        }
        return Object.keys(state.sources).length === 0;
    }

    scheduleDrawingsSave(reason = 'unspecified') {
        if (!this.chart || this.shouldSuppressDrawingSave()) return Promise.resolve();
        if (typeof this.chart.getLineToolsState !== 'function') return Promise.resolve();

        try {
            this._pendingSaveState = this.chart.getLineToolsState();
        } catch (e) {
            console.debug('[DEBUG-CHARTS] getLineToolsState failed', e);
            return Promise.resolve();
        }

        if (this._saveScheduled) {
            return this._saveInFlight || Promise.resolve();
        }

        this._saveScheduled = true;
        this._saveInFlight = new Promise((resolve) => {
            setTimeout(async () => {
                this._saveScheduled = false;
                const state = this._pendingSaveState;
                this._pendingSaveState = null;

                if (!state || this.shouldSuppressDrawingSave()) {
                    this._saveInFlight = null;
                    resolve();
                    return;
                }

                try {
                    if (this.save_load_adapter && typeof this.save_load_adapter.saveLineToolsAndGroups === 'function') {
                        await this.save_load_adapter.saveLineToolsAndGroups('default', 'default', state, { reason });
                    } else if (typeof this.widget?.saveChartToServer === 'function') {
                        await this.widget.saveChartToServer();
                    }
                } catch (e) {
                    console.debug('[DEBUG-CHARTS] drawing save skipped', e);
                }

                if (this._pendingSaveState && !this.shouldSuppressDrawingSave()) {
                    this._saveInFlight = null;
                    resolve(this.scheduleDrawingsSave(reason));
                    return;
                }

                this._saveInFlight = null;
                resolve();
            }, 300);
        });

        return this._saveInFlight;
    }

    async applyUserDrawingsState(state, token, cacheKey) {
        if (!state || !this.chart || !this.isTokenCurrent(token)) {
            return false;
        }
        this.isApplyingDrawingState = true;
        this.markDrawingMutationStart('apply-user-drawings');
        try {
            this.chart.removeAllShapes();
            // removeAllShapes 清空画面后 obj_charts 仍保留旧 entity 记录，
            // 下次 reconcile 旧 key 命中 toKeep 分支不重建，导致图上空白只剩最新一段。
            // 同步置空 obj_charts，强制 reconcile 走全量重建路径。
            this.obj_charts = {};
            // removeAllShapes 已清掉所有用户图形 → 同步清空"已染色图形 id"集合,否则切标的/切周期
            // 长期累积陈旧 id(轻量内存泄漏,见审查 L2)。仅在用户图形确实被整块清除时清。
            if (this._coloredDrawings) this._coloredDrawings.clear();
            this._resetReconcileRetry();
            if (!this.isTokenCurrent(token)) {
                return false;
            }
            this.chart.applyLineToolsState(state);
            if (cacheKey) {
                this.setDrawingsCache(cacheKey, state);
            }
            this.debouncedDrawChanlun();
            return true;
        } finally {
            this.isApplyingDrawingState = false;
            this.markDrawingMutationEnd('apply-user-drawings');
        }
    }

    async reloadDrawingsForCurrentContext(reason) {
        if (!this.chart || !this.save_load_adapter || typeof this.save_load_adapter.loadLineToolsAndGroups !== 'function') {
            return;
        }

        const identity = this.getCurrentChartIdentity();
        if (!identity) return;

        const { symbol, interval } = identity;
        const token = this.beginContextSwitch(reason, symbol, interval);
        const cacheKey = this.getDrawingsCacheKey(symbol, interval);
        const cachedDrawings = this._drawingsCache.get(cacheKey);

        try {
            if (!this.isDrawingStateEmpty(cachedDrawings)) {
                await this.applyUserDrawingsState(cachedDrawings, token, cacheKey);
                return;
            }

            if (this.chart) {
                this.debouncedDrawChanlun();
            }

            const state = await this.save_load_adapter.loadLineToolsAndGroups('default', 'default', 'load', {
                resolution: interval,
                symbol,
                token,
            });

            if (!this.isTokenCurrent(token)) {
                return;
            }

            if (!this.isDrawingStateEmpty(state)) {
                await this.applyUserDrawingsState(state, token, cacheKey);
            }
        } catch (e) {
            console.error(`[DEBUG-CHARTS] Failed to reload drawings (${reason})`, e);
        } finally {
            this.finishContextSwitch(token);
        }
    }

    _resetDataReadyContext() {
        this._dataContextVersion = (this._dataContextVersion || 0) + 1;
        this._tvDataReadyVersion = -1;
        this._tvDataReadyIdentity = null;
        this._pendingChanlunDrawVersion = null;
        this._pendingChanlunDrawIdentity = null;
        this._dataReadyProbeVersion = null;
        this._dataReadyProbeIdentity = null;
        this._initialLoadDone = false;
        return this._dataContextVersion;
    }

    _chartDataReadyNow() {
        try {
            return !!(
                this.chart &&
                typeof this.chart.dataReady === 'function' &&
                this.chart.dataReady() === true
            );
        } catch (e) {
            return false;
        }
    }

    _currentDataIdentityKey() {
        const identity = this.getCurrentChartIdentity();
        if (!identity || !identity.symbol || !identity.interval) return null;
        return `${String(identity.symbol).toLowerCase()}|${String(identity.interval).toLowerCase()}`;
    }

    _requestChanlunDrawWhenReady() {
        const contextVersion = this._dataContextVersion || 0;
        const contextIdentity = this._currentDataIdentityKey();
        if (!contextIdentity) return false;
        this._pendingChanlunDrawVersion = contextVersion;
        this._pendingChanlunDrawIdentity = contextIdentity;

        if (
            this._tvDataReadyVersion === contextVersion &&
            this._tvDataReadyIdentity === contextIdentity &&
            this._chartDataReadyNow()
        ) {
            this._pendingChanlunDrawVersion = null;
            this._pendingChanlunDrawIdentity = null;
            this._initialLoadDone = true;
            this.debouncedDrawChanlun();
            return true;
        }

        if (!this.chart || typeof this.chart.dataReady !== 'function') return false;
        if (
            this._dataReadyProbeVersion === contextVersion &&
            this._dataReadyProbeIdentity === contextIdentity
        ) return false;
        this._dataReadyProbeVersion = contextVersion;
        this._dataReadyProbeIdentity = contextIdentity;
        try {
            const readyNow = this.chart.dataReady(
                () => this.handleDataReady(contextVersion, contextIdentity)
            );
            if (readyNow === true) {
                return this.handleDataReady(contextVersion, contextIdentity);
            }
        } catch (e) {
            if (
                this._dataReadyProbeVersion === contextVersion &&
                this._dataReadyProbeIdentity === contextIdentity
            ) {
                this._dataReadyProbeVersion = null;
                this._dataReadyProbeIdentity = null;
            }
        }
        return false;
    }

    handleBarsReadyEvent(event) {
        const detail = event?.detail || {};
        if (detail.managerId && detail.managerId !== this.instanceId) {
            return;
        }
        const identity = this.getCurrentChartIdentity();
        if (!identity) return;
        // 守卫放宽: detail.symbol(getBars 的 requestParams.symbol=symbolInfo.ticker) 与
        // identity.symbol(chart.symbol())是 TV 两个不同 API, 搜索切标的后可能差一个 "market:"
        // 前缀。去前缀等价比较, 避免 ready 事件被误吞导致 draw_chanlun 永不触发(切标的卡死根因之一)。
        if (detail.symbol) {
            const _ev = detail.symbol.toLowerCase().replace(/^[a-z]+:/, '');
            const _id = identity.symbol.toLowerCase().replace(/^[a-z]+:/, '');
            if (_ev !== _id) return;
        }
        if (detail.resolution && detail.resolution !== identity.interval.toLowerCase()) {
            return;
        }
        const wasInitialLoad = (
            this._tvDataReadyVersion !== (this._dataContextVersion || 0) ||
            this._tvDataReadyIdentity !== this._currentDataIdentityKey()
        );
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleBarsReadyEvent ✓ symbol=${detail.symbol} res=${detail.resolution} bars=${detail.bars || '?'} fxs=${detail.fxs || '?'} bis=${detail.bis || '?'} xds=${detail.xds || '?'} wasInitialLoad=${wasInitialLoad}`);
        // bars-ready 只表示 bars_result 已写入，TradingView 此时可能尚未接收 K 线。
        // 首次绘图必须等当前标的/周期 dataReady 后再执行；后续更新仍复用防抖入口。
        this._requestChanlunDrawWhenReady();
    }

    init() {
        // SSE 接管实时刷新(applyChanlunUpdate + feedRealtimeBar)后, TV 轮询降到 30s 仅作断线
        // 兜底, 大幅减少多标的 first=false 轮询撞长桥 6QPS 限流(实测轮询拉 10 根 6-24s)。
        const _sseOn = (typeof window !== 'undefined' && window.__CHANLUN_SSE_ENABLED === true);
        this.udf_datafeed = new Datafeeds.UDFCompatibleDatafeed("/tv", _sseOn ? 30000 : 3000, undefined, {
            managerId: this.instanceId,
        });

        const registry = getTVRegistry();
        registry.chartManagers.set(this.instanceId, this);
        registry.datafeeds.set(this.instanceId, this.udf_datafeed);
        registry.activeManagerId = this.instanceId;

        // GlobalTVDatafeeds 供 MACD_HTF 等自定义指标跨图表查找 bars 数据
        if (!window.GlobalTVDatafeeds) {
            window.GlobalTVDatafeeds = [];
        }
        if (window.GlobalTVDatafeeds.length > 10) {
            window.GlobalTVDatafeeds.shift();
        }
        window.GlobalTVDatafeeds.push(this.udf_datafeed);
        window.tvDatafeed = this.udf_datafeed; // 兼容旧版单图表引用

        // 诊断工具挂在 window，生产环境默认静默；设 window.__chanlunDebug=true 可开启详细日志。
        // 在 console 跑 __chanlunDiag() 一键 dump 当前图表状态，__chanlunDumpLines() 打印 shape 端点。
        const _diagCm = this;
        window.__chanlunDiag = function () {
            try {
                const cm = _diagCm;
                const ch = cm.chart;
                if (!ch) { console.log('[CHANLUN-DIAG] chart 未就绪'); return; }
                const owned = cm._reconcileOwnedIds || new Set();
                const tvShapes = ch.getAllShapes ? (ch.getAllShapes() || []) : [];
                const tvIds = new Set(tvShapes.map(s => s.id));
                const containerByType = {};
                const containerIds = new Set();
                Object.entries(cm.obj_charts || {}).forEach(([sk, types]) => {
                    Object.entries(types || {}).forEach(([t, arr]) => {
                        containerByType[`${sk}/${t}`] = (arr || []).length;
                        (arr || []).forEach(item => {
                            if (item && item.id != null && typeof item.id !== 'object') containerIds.add(item.id);
                        });
                    });
                });
                const ghostsInTv = tvShapes.filter(s => !containerIds.has(s.id));
                const ghostsByName = {};
                ghostsInTv.forEach(g => { ghostsByName[g.name || '<noname>'] = (ghostsByName[g.name || '<noname>'] || 0) + 1; });
                const ownedNotInTv = [...owned].filter(id => !tvIds.has(id));
                console.log('========= [CHANLUN-DIAG] manual dump =========');
                console.log('TV.getAllShapes.length =', tvShapes.length);
                console.log('owned ids =', owned.size, ' container ids =', containerIds.size);
                console.log('container by type:', containerByType);
                console.log('TV 里有但 container 没有 (ghosts):', ghostsInTv.length, ' by name:', ghostsByName);
                console.log('owned 有但 TV 没 (已删 但 owned 未清):', ownedNotInTv.length);
                if (ghostsInTv.length > 0) {
                    console.log('--- ghosts 详情(前 10):');
                    ghostsInTv.slice(0, 10).forEach(g => {
                        let pts = null;
                        try { pts = ch.getShapeById(g.id)?.getPoints?.(); } catch (e) {}
                        console.log({ id: g.id, name: g.name, points: pts });
                    });
                }
                console.log('=============================================');
                return { tvShapes: tvShapes.length, owned: owned.size, container: containerIds.size, ghosts: ghostsInTv.length };
            } catch (e) {
                console.error('[CHANLUN-DIAG] dump failed:', e);
            }
        };

        // 打印当前所有 xds/bis 的端点坐标，用于定位错位长线段的起止时间和价格
        window.__chanlunDumpLines = function (type = 'xds') {
            try {
                const cm = _diagCm;
                if (!cm.chart) { console.log('[CHANLUN-DIAG] chart 未就绪'); return; }
                const allInContainer = [];
                Object.entries(cm.obj_charts || {}).forEach(([sk, types]) => {
                    (types[type] || []).forEach(entry => allInContainer.push({ sk, entry }));
                });
                console.log(`========= [CHANLUN-DIAG] dump ${type} (count=${allInContainer.length}) =========`);
                allInContainer.forEach(({ sk, entry }, i) => {
                    const pts = entry.points || [];
                    let geom = '';
                    if (pts.length >= 2) {
                        const t0 = pts[0].time, p0 = pts[0].price;
                        const t1 = pts[pts.length - 1].time, p1 = pts[pts.length - 1].price;
                        const dt = t1 - t0;
                        const dpct = p0 ? (Math.abs(p1 - p0) / p0 * 100).toFixed(2) : '?';
                        geom = `[${new Date(t0 * 1000).toISOString().slice(0,16)} → ${new Date(t1 * 1000).toISOString().slice(0,16)}] dt=${dt}s p=${p0}→${p1} (${dpct}%)`;
                    }
                    // 从 TV 取 shape 实际端点，与 entry.points 对比可定位漂移问题
                    let tvPts = null;
                    try { tvPts = cm.chart.getShapeById(entry.id)?.getPoints?.(); } catch (e) {}
                    const tvGeom = tvPts && tvPts.length >= 2
                        ? `TV=[${new Date(tvPts[0].time * 1000).toISOString().slice(0,16)}→${new Date(tvPts[tvPts.length-1].time * 1000).toISOString().slice(0,16)}] p=${tvPts[0].price}→${tvPts[tvPts.length-1].price}`
                        : 'TV=<no points>';
                    console.log(`#${i} id=${entry.id} linestyle=${entry.isUnfinished ? 1 : 0} ${geom} | ${tvGeom}`);
                });
                console.log('=============================================');
            } catch (e) {
                console.error('[CHANLUN-DIAG] dump lines failed:', e);
            }
        };

        this._barReadyHandler = this.handleBarsReadyEvent.bind(this);
        window.addEventListener('chanlun-bars-ready', this._barReadyHandler);

        // K线可见性兜底: 浏览器会节流后台标签的定时器(TV轮询), 切回前台时
        // K线可能停在旧值; 这里主动 resetData 让图表补刷(连带触发缠论重绘)。
        // 缠论侧在后台由 SSE 推送(onmessage 不受定时器节流)已保持最新。
        this._visibilityHandler = () => {
            if (document.visibilityState !== 'visible') return;
            // SSE 连通(readyState OPEN)时, K线由 feedRealtimeBar、缠论由 applyChanlunUpdate
            // 经 SSE onmessage 实时喂入(onmessage 不受后台标签定时器节流), 后台期间数据已保持
            // 最新 → 切回前台无需 resetData。resetData 会整块清空 K线+缠论、重新 getBars 重绘,
            // 正是"切换页面再切回缠论闪一下"的根因。仅当 SSE 未连通(退回被节流的轮询)时才
            // resetData 兜底补刷后台停滞的 K线。
            try {
                if (this._sse && this._sse.readyState === 1) return;  // SSE OPEN → 不补刷, 不闪
                if (this.widget && typeof this.widget.activeChart === 'function') {
                    const ch = this.widget.activeChart();
                    if (ch && typeof ch.resetData === 'function') ch.resetData();
                }
            } catch (e) { /* ignore */ }
        };
        document.addEventListener('visibilitychange', this._visibilityHandler);

        // 网络恢复兜底: 断网期间 EventSource 进入重连退避(可能数秒~数十秒才自动重连),
        // 'online' 事件一触发就立即重连 SSE, 缩短"恢复 → 补齐"延迟。重连后服务端推的首帧
        // (完整快照)会经上面 onmessage 的断档检测触发 resetData, 整段补齐断网期间缺失的 K线+缠论。
        this._onlineHandler = () => {
            clog('[SSE] 网络恢复(online), 标记重连后补齐 + 主动重连 SSE');
            this._needResetOnNextData = true;
            try { this._openSseStream(); } catch (e) { /* ignore */ }
        };
        if (typeof window !== 'undefined') {
            window.addEventListener('online', this._onlineHandler);
        }

        // 墙钟新鲜度看门狗: 独立于 SSE 帧, 每 20s 自查"画布末根 vs 当下"是否在交易时段内严重失速
        // (覆盖 SSE 半开静默/被中间层缓冲转 fallback/不推帧等帧驱动检测的盲区)。失速则触发一次
        // resetData(带防抖退避 + 交易时段门控, 收盘/周末不误闪)。
        this._watchdogTimer = setInterval(() => {
            try {
                if (typeof window === 'undefined' || !window.SseGap || !this.widget) return;
                let si = null;
                try { si = this.widget.symbolInterval(); } catch (e) { return; }
                if (!si || !si.symbol || !si.interval) return;
                const resKey = String(si.symbol).toLowerCase() + String(si.interval).toLowerCase();
                const sym = String(si.symbol || '');
                const market = (sym.indexOf(':') >= 0)
                    ? sym.split(':')[0].toLowerCase()
                    : ((typeof Utils !== 'undefined' && Utils.get_market) ? Utils.get_market() : '');
                const viewLatestSec = this._getViewLatestSec(resKey, si.interval);
                const nowSec = Math.floor(Date.now() / 1000);
                const periodSec = window.SseGap._internal.resolutionToPeriodSeconds(si.interval);
                if (window.SseGap.computeWatchdogReset(market, viewLatestSec, nowSec, periodSec)) {
                    this._doReset(resKey, 'watchdog', viewLatestSec);
                }
            } catch (e) { /* ignore */ }
        }, 20000);

        const self = this;
        const client_id = "chanlun_pro_" + Utils.get_market() + "_" + this.id;
        const user_id = "999";
        const save_load_adapter = {
            getAllCharts: function () {
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data : []);
            },
            removeChart: function (chartId) {
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + "&chart=" + chartId, { method: "DELETE" })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            saveChart: function (chartData) {
                clog("[DEBUG-CHARTS] saveChart called", chartData);
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + (chartData.id ? "&chart=" + chartData.id : ""), {
                    method: "POST",
                    body: new URLSearchParams({
                        name: chartData.name,
                        symbol: chartData.symbol,
                        resolution: chartData.resolution,
                        content: chartData.content
                    })
                })
                    .then(res => res.json())
                    .then(res => {
                        clog("[DEBUG-CHARTS] saveChart response", res);
                        return res.status === 'ok' ? (res.id || chartData.id || "default") : null;
                    })
                    .catch(err => {
                        console.error("[DEBUG-CHARTS] saveChart error", err);
                        return null;
                    });
            },
            getChartContent: function (chartId) {
                clog("[DEBUG-CHARTS] getChartContent called", chartId);
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + "&chart=" + chartId)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data.content : null);
            },
            getAllStudyTemplates: function () {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data : []);
            },
            removeStudyTemplate: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id + "&template=" + templateData.name, { method: "DELETE" })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            saveStudyTemplate: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id, {
                    method: "POST",
                    body: new URLSearchParams({
                        name: templateData.name,
                        content: templateData.content
                    })
                })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            getStudyTemplateContent: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id + "&template=" + templateData.name)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data.content : null);
            },
            saveLineToolsAndGroups: function (layoutId, chartId, state, options = {}) {
                clog("[DEBUG-CHARTS] saveLineToolsAndGroups called", { layoutId, chartId, state, options });
                if (self.shouldSuppressDrawingSave()) {
                    clog("[DEBUG-CHARTS] Skip saveLineToolsAndGroups due to active drawing mutation");
                    return Promise.resolve();
                }
                if (!state || typeof state !== "object") {
                    const error = new TypeError("drawing state must be an object");
                    if (typeof layer !== "undefined" && layer && typeof layer.msg === "function") {
                        layer.msg("画线状态无效，已取消保存");
                    }
                    return Promise.reject(error);
                }                const rawResolution = self.chart ? self.chart.resolution() : Utils.get_local_data(Utils.get_market() + "_interval_" + self.id);
                const resolution = self.cl_independent_drawings ? rawResolution : 'all';
                const symbol = self.chart ? self.chart.symbol() : Utils.get_market() + ":" + Utils.get_code();
                const cacheKey = self.getDrawingsCacheKey(symbol, rawResolution);

                let processedState = { ...state };
                if (state && state.sources) {
                    if (state.sources instanceof Map || typeof state.sources.entries === 'function') {
                        try {
                            processedState.sources = Object.fromEntries(state.sources);
                        } catch (e) {
                            processedState.sources = {};
                            for (let [k, v] of state.sources.entries()) {
                                processedState.sources[k] = v;
                            }
                        }
                    } else if (typeof state.sources === 'object') {
                        try {
                            processedState.sources = JSON.parse(JSON.stringify(state.sources));
                        } catch (e) {
                            processedState.sources = state.sources;
                        }
                    }
                }

                if (state && state.groups) {
                    if (state.groups instanceof Map || typeof state.groups.entries === 'function') {
                        try {
                            processedState.groups = Object.fromEntries(state.groups);
                        } catch (e) {
                            processedState.groups = {};
                            for (let [k, v] of state.groups.entries()) {
                                processedState.groups[k] = v;
                            }
                        }
                    } else if (typeof state.groups === 'object') {
                        try {
                            processedState.groups = JSON.parse(JSON.stringify(state.groups));
                        } catch (e) {
                            processedState.groups = state.groups;
                        }
                    }
                }
                clog("[DEBUG-CHARTS] Queuing drawings save", { symbol, resolution, reason: options.reason });
                const query = new URLSearchParams({
                    client: client_id,
                    user: user_id,
                    chart: String(chartId),
                    layout: String(layoutId),
                    symbol: String(symbol),
                    resolution: String(resolution),
                });
                return self.enqueueLatestDrawingSave(function () {
                    return fetch("/tv/1.1/drawings?" + query.toString(), {
                        method: "POST",
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ state: processedState })
                    }).then(function (response) {
                        if (!response.ok) {
                            throw new Error("Drawing save failed with HTTP " + response.status);
                        }
                        return response.json();
                    }).then(function (payload) {
                        if (!payload || payload.status !== 'ok') {
                            throw new Error((payload && (payload.message || payload.error)) || "Drawing save was rejected");
                        }
                        if (state && state.sources) {
                            self.setDrawingsCache(cacheKey, state);
                        }
                    });
                });
            },            loadLineToolsAndGroups: function (layoutId, chartId, requestType, requestContext = {}) {
                clog("[DEBUG-CHARTS] loadLineToolsAndGroups called", { layoutId, chartId, requestType, requestContext });
                return new Promise((resolve) => {
                    const resolution = requestContext.resolution;
                    const symbol = requestContext.symbol;
                    const token = requestContext.token;

                    if (requestType !== 'mainSeriesLineTools' && requestType !== 'load') {
                        return resolve(null);
                    }
                    const loadSymbol = symbol || (self.chart ? self.chart.symbol() : '');
                    const rawResolution = resolution || (self.chart ? self.chart.resolution() : '');
                    const loadResolution = self.cl_independent_drawings ? rawResolution : 'all';
                    if (!loadSymbol || !loadResolution) {
                        return resolve(null);
                    }

                    fetch("/tv/1.1/drawings?client=" + client_id + "&user=" + user_id + "&chart=" + chartId + "&layout=" + layoutId + "&symbol=" + loadSymbol + "&resolution=" + loadResolution)
                        .then(res => res.json())
                        .then(res => {
                            if (token && !self.isTokenCurrent(token)) {
                                return resolve(null);
                            }
                            if (res.status === 'ok' && res.data && Object.keys(res.data).length > 0) {
                                let loadedState = res.data;
                                const sources = new Map();
                                if (loadedState.sources) {
                                    for (let [key, state] of Object.entries(loadedState.sources)) {
                                        sources.set(key, state);
                                    }
                                }
                                const groups = new Map();
                                if (loadedState.groups) {
                                    for (let [key, state] of Object.entries(loadedState.groups)) {
                                        groups.set(key, state);
                                    }
                                }
                                resolve({ sources, groups });
                            } else {
                                resolve(null);
                            }
                        }).catch(err => {
                            console.error("[DEBUG-CHARTS] loadLineToolsAndGroups error:", err);
                            resolve(null);
                        });
                });
            }
        };
        this.save_load_adapter = save_load_adapter;

        this.widget = window.tvWidget = new TradingView.widget({
            // loading_screen 让 widget 内部加载阶段显示 spinner 而非空白
            loading_screen: (function () {
                var isDark = false;
                try {
                    var t = JSON.parse(localStorage.tv_chart || '{}');
                    isDark = (t.theme === 'dark');
                } catch (e) {}
                return {
                    backgroundColor: isDark ? '#1e1e1e' : '#ffffff',
                    foregroundColor: '#1e9fff',
                };
            })(),
            debug: false, autosize: true, fullscreen: false,
            container: "tv_chart_container_" + this.id,
            symbol: Utils.get_market() + ":" + Utils.get_code(),
            interval: getInitialChartInterval(this.id),
            datafeed: this.udf_datafeed,
            library_path: "static/charting_library/",
            theme: Utils.get_local_data("theme"),
            numeric_formatting: { decimal_sign: "." },
            time_frames: [], timezone: getMarketTimezone(Utils.get_market()), locale: "zh",
            symbol_search_request_delay: 100, auto_save_delay: 5, study_count_limit: 100,
            disabled_features: CHART_DISABLED_FEATURES,
            enabled_features: ["study_templates", "seconds_resolution", "saveload_separate_drawings_storage", "iframe_loading_same_origin"],
            saved_data_meta_info: { uid: 1, name: "default", description: "default" },
            save_load_adapter: save_load_adapter,
            client_id: "chanlun_pro_" + Utils.get_market() + "_" + this.id,
            user_id: "999", load_last_chart: shouldLoadLastChart(),
            custom_indicators_getter: this.getCustomIndicators,
            time_scale: { min_bar_spacing: 0.05, max_bar_spacing: 800 },
        });
        this.setupEventListeners();
        return this;
    }

    getCustomIndicators(PineJS) {
        if (typeof TvIdxMACDBackend === 'undefined') {
            return Promise.resolve([]);
        }
        return Promise.resolve([
            TvIdxMACDBackend.idx(PineJS),
            TvIdxAMA.idx(PineJS), TvIdxATR.idx(PineJS), TvIdxCDBB.idx(PineJS),
            TvIdxCMCM.idx(PineJS), TvIdxDemo.idx(PineJS), TvIdxFCX.idx(PineJS),
            TvIdxHDLY.idx(PineJS), TvIdxHeima.idx(PineJS), TvIdxHLBLW.idx(PineJS),
            TvIdxHLFTX.idx(PineJS), TvIdxKDJ.idx(PineJS), TvIdxLTQS.idx(PineJS),
            TvIdxMA.idx(PineJS), TvIdxMACDBL.idx(PineJS), TvIdxVegasMA.idx(PineJS),
            TvIdxVOL.idx(PineJS),
        ]);
    }

    ensureRequestedDefaultStudies() {
        if (this._defaultStudiesPromise) return this._defaultStudiesPromise;
        if (!this.chart) return Promise.resolve([]);
        if (!(this._defaultStudyIds instanceof Map)) this._defaultStudyIds = new Map();
        const requested = requestedDefaultStudies(
            window.location && typeof window.location.search === "string"
                ? window.location.search
                : "",
        );
        let createFailed = false;
        const pending = (async () => {
            if (!requested.length) return [];
            let existing = [];
            try {
                const studies = typeof this.chart.getAllStudies === "function"
                    ? this.chart.getAllStudies()
                    : [];
                existing = Array.isArray(studies) ? studies : [];
            } catch (e) {
                console.warn("[CHARTS] read default studies failed", e);
            }

            const studyIds = [];
            for (const name of requested) {
                if (this._defaultStudyIds.has(name)) {
                    const knownId = this._defaultStudyIds.get(name);
                    if (knownId !== null && knownId !== undefined) studyIds.push(knownId);
                    continue;
                }
                const current = existing.find((study) => study && study.name === name);
                if (current) {
                    if (current.id !== null && current.id !== undefined) studyIds.push(current.id);
                    this._defaultStudyIds.set(name, current.id);
                    if (name === "MACD_HTF") this.macdStudyId = current.id;
                    continue;
                }
                if (typeof this.chart.createStudy !== "function") {
                    createFailed = true;
                    continue;
                }
                try {
                    const studyId = await this.chart.createStudy(name, false, false);
                    if (studyId !== null && studyId !== undefined) studyIds.push(studyId);
                    this._defaultStudyIds.set(name, studyId);
                    if (name === "MACD_HTF") this.macdStudyId = studyId;
                } catch (e) {
                    createFailed = true;
                    console.warn(`[CHARTS] create default study ${name} failed`, e);
                }
            }
            return studyIds;
        })();
        this._defaultStudiesPromise = pending;
        pending.finally(() => {
            if (createFailed && this._defaultStudiesPromise === pending) {
                this._defaultStudiesPromise = null;
            }
        });
        return pending;
    }

    setupEventListeners() {
        const global_widget = this.widget;
        const self = this;
        this.measureShapeId = null;

        // TV shape 事件携带的对象结构因版本而异，递归深度扫描兼容多种 points 字段位置
        const scanForPoints = (obj, depth = 0) => {
            if (!obj || depth > 3) return null;
            try {
                if (Array.isArray(obj.points) && obj.points.length >= 2 && obj.points[0].time) return obj.points;
                if (Array.isArray(obj._points) && obj._points.length >= 2 && obj._points[0].time) return obj._points;

                const keys = Object.keys(obj);
                for (let k of keys) {
                    const val = obj[k];
                    if (val && typeof val === 'object') {
                        if (Array.isArray(val) && val.length >= 2 && val[0] && val[0].hasOwnProperty('time')) {
                            console.log(`[MACD] 通过深度扫描在属性 [${k}] 中找到坐标!`);
                            return val;
                        }
                        if (!Array.isArray(val) && k !== 'parent' && k !== 'chart') {
                            const found = scanForPoints(val, depth + 1);
                            if (found) return found;
                        }
                    }
                }
            } catch (e) { }
            return null;
        };

        this.widget.headerReady().then(function () {
            var btnDisplay = global_widget.createButton();
            btnDisplay.textContent = "缠论显示设置 ▾";
            btnDisplay.addEventListener("click", function () {
                // 每个图表面板独立一套菜单 DOM，防止多图布局下互相干扰
                const menuId = 'cl_display_menu_' + self.id;
                const backdropId = 'cl_menu_backdrop_' + self.id;
                if ($('#' + menuId).length > 0) {
                    $('#' + menuId).remove();
                    $('#' + backdropId).remove();   // 兼容旧版残留 backdrop
                    return;
                }
                // 兼容旧版可能遗留的 backdrop(刷新前的旧 charts.js 创建过)
                $('#' + backdropId).remove();

                const cfg = self.cl_show_config;
                const cbId = (k) => 'cl_cb_' + k + '_' + self.id;
                const indCbId = 'cl_cb_independent_drawings_' + self.id;

                let _curInterval = "?";
                try { _curInterval = self.widget.symbolInterval().interval; } catch (e) {}
                const _displayLevels = recursiveDisplayLevels(_curInterval);
                const _centerLevels = _displayLevels.map((item) => ({
                    label: `${item.label} 中枢`, key: `center_L${item.level}`, level: item.level,
                }));
                const _trendLevels = _displayLevels.map((item) => ({
                    label: `${item.label} 走势类型`, key: `trend_L${item.level}`, level: item.level,
                }));
                const _divergenceLevels = _displayLevels.flatMap((item) => ([
                    {
                        label: `${item.label} 盘整背驰`,
                        key: `divergence_consolidation_L${item.level}`,
                        level: item.level,
                    },
                    {
                        label: `${item.label} 趋势背驰`,
                        key: `divergence_trend_L${item.level}`,
                        level: item.level,
                    },
                ]));
                const _pointTypes = [
                    { key: 'point_1buy', label: '一买' },
                    { key: 'point_2buy', label: '二买' },
                    { key: 'point_3buy', label: '三买' },
                    { key: 'point_1sell', label: '一卖' },
                    { key: 'point_2sell', label: '二卖' },
                    { key: 'point_3sell', label: '三卖' },
                ];
                const _checked = (key, fallback = true) => (
                    cfg[key] === undefined ? fallback : cfg[key] !== false
                );
                const _cbRow = (key, label, fallback = true) => `
                    <label style="display:block; cursor:pointer; font-size:14px; line-height:24px;">
                        <input type="checkbox" id="${cbId(key)}" ${_checked(key, fallback) ? 'checked' : ''}
                            style="margin-right:6px; vertical-align:middle;">${label}
                    </label>`;
                const _grpTitle = (title, note = '') => `
                    <div style="font-size:14px; color:#2563a6; padding:8px 0 2px; font-weight:700;">
                        ${title}${note ? `<span style="font-size:13px; color:#687386; margin-left:6px; font-weight:400;">${note}</span>` : ''}
                    </div>`;
                const _swatch = (color) => `
                    <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};
                        margin-right:4px;vertical-align:middle;border:1px solid rgba(0,0,0,0.25);"></span>`;

                let html = `
                    <div id="${menuId}" style="position:absolute;z-index:99999999;background:#fff;border:1px solid #cfd6df;
                        box-shadow:0 8px 28px rgba(0,0,0,0.2);border-radius:8px;padding:12px 14px;line-height:24px;
                        font-size:14px;color:#26313d;min-width:340px;max-width:440px;">
                        <div id="${menuId}_lvl_toggle" style="cursor:pointer;font-size:14px;color:#596779;padding:2px 0;user-select:none;">
                            <span id="${menuId}_lvl_arrow">▸</span> 当前周期 <b>${_curInterval}</b> · 严格结构级别
                        </div>
                        <div id="${menuId}_lvl_detail" style="display:none;font-size:13px;color:#687386;line-height:20px;
                            padding:5px 0 5px 14px;border-left:2px solid #dce3eb;margin:3px 0 5px 4px;">
                            笔中枢只作基础观察；下列中枢、走势类型和背驰由当前 K 线递归产生，买卖点仅显示已确认信号。
                        </div>

                        ${_grpTitle('基础结构')}
                        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:14px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('fx')}" ${_checked('fx') ? 'checked' : ''}> 分型</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bi')}" ${_checked('bi') ? 'checked' : ''}> ${_swatch(getDynamicColor(_curInterval, 'bis'))}笔</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('xd')}" ${_checked('xd') ? 'checked' : ''}> ${_swatch(getDynamicColor(_curInterval, 'xds'))}线段</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('center_observation')}" ${_checked('center_observation') ? 'checked' : ''}> ${_swatch(getDynamicColor(_curInterval, 'bi_zss'))}笔中枢</label>
                        </div>

                        ${_grpTitle('中枢', '由当前 K 线递归产生')}
                        ${_cbRow('center_all', '中枢总开关')}
                        <div style="padding-left:14px;display:flex;gap:12px;flex-wrap:wrap;font-size:14px;">
                            ${_centerLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('走势类型', '由当前 K 线递归产生')}
                        ${_cbRow('trend_all', '走势类型总开关', false)}
                        <div style="padding-left:14px;display:flex;gap:12px;flex-wrap:wrap;font-size:14px;">
                            ${_trendLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('买卖点')}
                        ${_cbRow('point_all', '买卖点总开关')}
                        <div style="padding-left:14px;display:grid;grid-template-columns:repeat(3,1fr);gap:4px 10px;font-size:14px;">
                            ${_pointTypes.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}> ${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('背驰', '由当前 K 线递归产生')}
                        ${_cbRow('divergence_all', '背驰总开关')}
                        <div style="padding-left:14px;display:grid;grid-template-columns:repeat(2,minmax(120px,1fr));gap:4px 10px;font-size:14px;">
                            ${_divergenceLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('画线设置')}
                        <label style="display:block;cursor:pointer;font-size:14px;">
                            <input type="checkbox" id="${indCbId}" ${self.cl_independent_drawings ? 'checked' : ''}
                                style="margin-right:6px;vertical-align:middle;">独立周期画线
                        </label>
                        <div style="display:flex;gap:8px;padding-top:8px;">
                            <button id="${menuId}_all" type="button" style="flex:1;font-size:14px;padding:5px 8px;cursor:pointer;
                                border:1px solid #c5ced8;background:#f7f9fb;border-radius:4px;">全选</button>
                            <button id="${menuId}_none" type="button" style="flex:1;font-size:14px;padding:5px 8px;cursor:pointer;
                                border:1px solid #c5ced8;background:#f7f9fb;border-radius:4px;">全清</button>
                        </div>
                    </div>
                `;
                $('body').append(html);

                // 级别映射 折叠/展开
                $('#' + menuId + '_lvl_toggle').on('click', function () {
                    const $d = $('#' + menuId + '_lvl_detail');
                    const $a = $('#' + menuId + '_lvl_arrow');
                    if ($d.is(':visible')) { $d.hide(); $a.text('▸'); }
                    else { $d.show(); $a.text('▾'); }
                });
                // 全选/全清:trigger change 让现有 toggle handler 走完整 cfg 保存 + redraw 流程
                // 排除「独立周期画线」(indCbId): 它是画线存储模式切换非显示项, 误触会切数据源(R1-C8)。
                $('#' + menuId + '_all').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').not('#' + indCbId).each(function () {
                        if (!this.checked) { this.checked = true; $(this).trigger('change'); }
                    });
                });
                $('#' + menuId + '_none').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').not('#' + indCbId).each(function () {
                        if (this.checked) { this.checked = false; $(this).trigger('change'); }
                    });
                });

                // TV createButton() 创建的按钮在 widget 内部 iframe 里，
                // getBoundingClientRect() 返回 iframe 内部坐标，需逐层累加 iframe 偏移
                // 才能正确定位到主文档坐标系（多图表场景下尤其关键）。
                function getElementRectInTopWindow(el) {
                    const rect = el.getBoundingClientRect();
                    let top = rect.top;
                    let left = rect.left;
                    let bottom = rect.bottom;
                    let right = rect.right;
                    let win = el.ownerDocument && el.ownerDocument.defaultView;
                    while (win && win !== window.top) {
                        try {
                            const frameEl = win.frameElement;
                            if (!frameEl) break;
                            const fr = frameEl.getBoundingClientRect();
                            top += fr.top;
                            left += fr.left;
                            bottom += fr.top;
                            right += fr.left;
                            win = frameEl.ownerDocument && frameEl.ownerDocument.defaultView;
                        } catch (e) {
                            // 跨域 iframe 无法访问 frameElement，使用已累加的偏移
                            break;
                        }
                    }
                    return { top, left, bottom, right };
                }

                const btnRect = getElementRectInTopWindow(btnDisplay);

                // 已转换到主文档坐标，补上滚动偏移后定位到按钮正下方
                $('#' + menuId).css({
                    top: (btnRect.bottom + window.scrollY + 5) + 'px',
                    left: (btnRect.left + window.scrollX) + 'px'
                });

                const keys = [
                    'fx', 'bi', 'xd',
                    'center_observation', 'center_all', 'trend_all',
                    'point_all', 'divergence_all',
                    ..._centerLevels.map((item) => item.key),
                    ..._trendLevels.map((item) => item.key),
                    ..._pointTypes.map((item) => item.key),
                    ..._divergenceLevels.map((item) => item.key),
                ];
                keys.forEach(k => {
                    $('#' + cbId(k)).change(function () {
                        const checked = $(this).is(':checked');
                        self.cl_show_config[k] = checked;
                        saveClShowConfig(self.id, self._curResolution, self.cl_show_config);
                        console.log(`[cl_show_config] toggle ${k}=${checked}`);
                        self.debouncedDrawChanlun();
                    });
                });

                $('#' + indCbId).change(function () {
                    self.cl_independent_drawings = $(this).is(':checked');
                    saveClIndependentDrawings(self.id, self.cl_independent_drawings);
                    self._drawingsCache.clear();
                    if (self.chart && self.save_load_adapter) {
                        self.reloadDrawingsForCurrentContext('toggle-drawing-mode');
                    }
                    layer.msg(self.cl_independent_drawings ? '已切换为独立周期画线' : '已切换为共享画线', { time: 1000 });
                });

                // **不要用全屏 backdrop**——TV 按钮在 iframe 内,full-screen backdrop
                // 在 main page 高 z-index 上,会**拦截 iframe 区域所有点击**(包括
                // 「缠论显示设置」按钮再点击),造成用户菜单一旦打开就无法用按钮
                // 关闭、TV 工具栏其它按钮也点不动。
                //
                // 改用 document-level click handler:监听 capture phase,任何 click
                // 落在菜单外都关菜单,**不阻塞 iframe 内交互**。延迟一帧绑定避开
                // 触发本次打开的同一次点击事件。
                setTimeout(() => {
                    // 菜单 append 在主文档,但图表/工具栏在 TV iframe 内——点击图表「空白区」
                    // 的 click 落在 iframe document、不冒泡到主文档,故只绑主文档时收不到、
                    // 菜单关不掉。这里同时给主文档 + 所有同源 iframe 文档绑 capture click,
                    // 点菜单外任意处(含图表空白)即关。跨源 iframe 访问 contentDocument 会抛错,跳过。
                    const docs = [document];
                    document.querySelectorAll('iframe').forEach((f) => {
                        try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) { /* 跨源 iframe 跳过 */ }
                    });
                    const closeHandler = (ev) => {
                        const menuEl = document.getElementById(menuId);
                        const cleanup = () => docs.forEach((d) => d.removeEventListener('click', closeHandler, true));
                        if (!menuEl) { cleanup(); return; }
                        if (menuEl.contains(ev.target)) return;     // 菜单内点击不关(ev.target 在主文档菜单内)
                        menuEl.remove();
                        cleanup();
                    };
                    docs.forEach((d) => d.addEventListener('click', closeHandler, true));
                }, 0);
            });

            var buttonReload = global_widget.createButton();
            buttonReload.textContent = "重新加载数据";
            buttonReload.addEventListener("click", function () { global_widget.resetCache(); global_widget.activeChart().resetData(); });

            var buttonHideMark = global_widget.createButton();
            buttonHideMark.textContent = "隐藏标记";
            buttonHideMark.addEventListener("click", function () { global_widget.activeChart().clearMarks(); });

            var buttonDeleteMark = global_widget.createButton();
            buttonDeleteMark.textContent = "删除标记";
            buttonDeleteMark.addEventListener("click", function () {
                let symbol = global_widget.symbolInterval();
                AppRequest.ajax({ type: "POST", url: "/tv/del_marks", dataType: "json", data: { symbol: symbol.symbol }, success: function (res) { if (res.status == "ok") { global_widget.activeChart().clearMarks(); layer.msg("删除标记成功"); } } });
            });

        });
        this.widget.onChartReady(() => {
            // widget 就绪后移除首屏骨架占位
            var sk = document.getElementById('tv_charts_skeleton');
            if (sk) sk.remove();
            this.chart = this.widget.activeChart();
            if (!this.chart) return;
            this.ensureRequestedDefaultStudies().catch((e) => {
                console.warn("[CHARTS] initialize requested default studies failed", e);
            });
            this._alignResolutionOnReady();   // 按真实周期校正显示配置(构造 _curResolution 仅为猜测)
            this.chart.applyOverrides({ "mainSeriesProperties.candleStyle.upColor": "#ef5350", "mainSeriesProperties.candleStyle.downColor": "#26a69a" });
            const registry = getTVRegistry();
            registry.widgets.set(this.instanceId, this.widget);
            registry.activeManagerId = this.instanceId;
            window.tvWidget = this.widget;
            this.widget._chanlunManagerId = this.instanceId;
            this.udf_datafeed._chanlunManagerId = this.instanceId;

            this.chart.onSymbolChanged().subscribe(null, (s) => this.handleSymbolChange(s));
            this.chart.onIntervalChanged().subscribe(null, (i) => this.handleIntervalChange(i));
            this.chart.onDataLoaded().subscribe(
                null,
                () => this.handleDataReady(
                    this._dataContextVersion || 0,
                    this._currentDataIdentityKey()
                ),
                true
            );
            const initialDataContextVersion = this._dataContextVersion || 0;
            const initialDataContextIdentity = this._currentDataIdentityKey();
            const readyNow = this.chart.dataReady(
                () => this.handleDataReady(initialDataContextVersion, initialDataContextIdentity)
            );
            if (readyNow === true) {
                this.handleDataReady(initialDataContextVersion, initialDataContextIdentity);
            }
            this.widget.subscribe("onTick", () => this.handleTick());
            this.chart.onVisibleRangeChanged().subscribe(null, () => this.handleVisibleRangeChange());

            this.reloadDrawingsForCurrentContext('initial-load');
            this._openSseStream();
            this.updateDrawPalette();   // 画图调色板:优先原生注入 TV 工具栏,失败回退浮层
            this.applyOverscrollGuard();   // 给 TV iframe 注入 overscroll-behavior:none(防 macOS 横滑后退)
            // TV 左侧工具栏异步渲染:重试注入(成功即停,最多 ~4.2s)
            if (this._lvlbtnTimer) clearInterval(this._lvlbtnTimer);
            this._lvlbtnTries = 0;
            this._lvlbtnTimer = setInterval(() => {
                this._lvlbtnTries++;
                this.applyOverscrollGuard();   // iframe 异步加载,随重试一并注入(幂等)
                if (this.injectDrawPaletteIntoTVToolbar() || this._lvlbtnTries >= 12) { clearInterval(this._lvlbtnTimer); this._lvlbtnTimer = null; }
            }, 350);

            // 注入 MACD 区间统计（工具栏按钮 + 右键菜单 + 侧边面板），依赖 chart/widget 已就绪
            try {
                if (window.MacdStats && typeof window.MacdStats.attach === 'function') {
                    window.MacdStats.attach(this);
                }
            } catch (e) {
                console.warn("[DEBUG-CHARTS] MacdStats.attach failed", e);
            }

            this.widget.subscribe('drawing_event', (id, eventType) => {
                // 手画线段/矩形:首次出现(任意事件类型,排除 remove)且当前选了画图色 → 套该色。
                // (实测 chart.applyOverrides 的 linetool 默认色对手画不生效,故用此事件兜底上色。)
                // 记录已上色 id,避免覆盖用户之后手动改的色;add 在 setProperties 前,防 properties_changed 自触发循环。
                if (!this._coloredDrawings) this._coloredDrawings = new Set();
                // ⚠ 只给「用户手画」图形上色,排除缠论自动图形(在 _reconcileOwnedIds 中)——缠论的笔/线段
                // 也是 trend_line,点击会触发 click 事件,否则被误染成当前画图色(用户报:一点就变红)。
                const _isChanlunShape = !!(this._reconcileOwnedIds && this._reconcileOwnedIds.has(id));
                if (this._drawColor && eventType !== 'remove' && !_isChanlunShape && !this._coloredDrawings.has(id)) {
                    try {
                        const sh = this.chart.getShapeById(id);
                        if (sh && sh.setProperties) {
                            const p = sh.getProperties() || {};
                            const ov = {};
                            if ('linecolor' in p) ov.linecolor = this._drawColor;
                            if ('color' in p) ov.color = this._drawColor;
                            if ('backgroundColor' in p) ov.backgroundColor = this._drawColor;
                            if (Object.keys(ov).length) { this._coloredDrawings.add(id); sh.setProperties(ov); }
                        }
                    } catch (e) {}
                }
                if (eventType === 'remove' && this._coloredDrawings) this._coloredDrawings.delete(id);
                if (this.shouldSuppressDrawingSave()) return;
                clog("[DEBUG-CHARTS] drawing_event", id, eventType);
                this.scheduleDrawingsSave('drawing_event');
            });
            this.widget.subscribe('onAutoSaveNeeded', () => {
                if (this.shouldSuppressDrawingSave()) return;
                clog("[DEBUG-CHARTS] onAutoSaveNeeded");
                this.scheduleDrawingsSave('auto_save');
            });
        });
    }

    // ===== 按级别颜色「手动画线段 / 矩形」调色板 =====
    // 需求:用 TV 画线/矩形工具在图上手动作图,可挑各级别(笔/段/1m/5m/30m/日…)颜色来画。
    // 机制:点色块 → setDrawColor 设 TV 趋势线/矩形工具默认色;点「线/框」激活对应工具;
    //       手画完成时 drawing_event=create 再兜底套色。位置优先注入 TV 左侧工具栏,失败回退浮层。

    // 设当前「画图颜色」:applyOverrides 设趋势线/矩形默认色,之后手画的线段/矩形即此色。
    setDrawColor(color) {
        this._drawColor = color;
        try {
            this.chart.applyOverrides({
                "linetooltrendline.linecolor": color,
                "linetooltrendline.linewidth": 2,
                "linetoolrectangle.color": color,
                "linetoolrectangle.backgroundColor": color,
                "linetoolrectangle.linecolor": color,
                "linetoolrectangle.transparency": 80,
            });
        } catch (e) { /* override 失败不致命,create 事件仍会兜底套色 */ }
        try {
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                const g = dd && dd.getElementById('cl_tv_drawpal_' + this.id);
                if (g) this._paintDrawPalette(g);
            }
            const ov = document.getElementById('cl_drawpal_' + this.id);
            if (ov) this._paintDrawPalette(ov);
        } catch (e) {}
    }

    // 高亮调色板中当前画图色对应的色块。
    _paintDrawPalette(grp) {
        try {
            grp.querySelectorAll('.cl-drawcol').forEach(b => {
                const c = b.getAttribute('data-color') || '';
                const active = this._drawColor && c.toUpperCase() === this._drawColor.toUpperCase();
                b.style.background = active ? c : 'transparent';
                b.style.color = active ? '#fff' : c;
                b.style.boxShadow = active ? '0 0 0 1.5px #333' : 'none';
            });
        } catch (e) {}
    }

    // 往容器 grp 构建调色板内容:标题 + 各级色块(点=设画图色) + 线段/矩形工具按钮(点=激活工具)。
    _buildDrawPaletteInto(grp, doc, interval) {
        const { items } = this._levelBarItems(interval);
        const hd = doc.createElement('div');
        hd.textContent = '一键画';
        hd.title = '点对应级别的「线/框」即可直接画(已含选色+激活工具)';
        hd.style.cssText = 'font-size:10px; color:#999; user-select:none; text-align:center;';
        grp.appendChild(hd);
        // 列头:左=线段、右=矩形
        const colhd = doc.createElement('div');
        colhd.style.cssText = 'display:flex; gap:2px; font-size:9px; color:#aaa; user-select:none;';
        ['线', '框'].forEach(t => { const c = doc.createElement('div'); c.textContent = t; c.style.cssText = 'width:23px; text-align:center;'; colhd.appendChild(c); });
        grp.appendChild(colhd);
        // 每级一行:左「线段」按钮(下划线样式) + 右「矩形」按钮(方框样式),均为该级颜色。
        // 点一下 = 设画图色 + 激活对应工具,直接画(用户要的「一键直画」)。
        items.forEach((it) => {
            const row = doc.createElement('div');
            row.style.cssText = 'display:flex; gap:2px;';
            const mk = (tool, isBox) => {
                const b = doc.createElement('div');
                b.className = 'cl-drawbtn';
                b.textContent = it.label;
                b.title = '画 ' + it.label + ' 级别' + (isBox ? '矩形' : '线段') + '(一键:选色+激活工具)';
                b.style.cssText = 'width:23px; height:17px; line-height:14px; text-align:center; font-size:9.5px; font-weight:700; cursor:pointer; box-sizing:border-box; color:' + it.color + '; '
                    + (isBox
                        ? 'border:1.5px solid ' + it.color + '; border-radius:3px;'
                        : 'border-bottom:2.5px solid ' + it.color + ';');
                b.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.setDrawColor(it.color);
                    try { this.widget.selectLineTool(tool); } catch (err) {}
                });
                return b;
            };
            row.appendChild(mk('trend_line', false));
            row.appendChild(mk('rectangle', true));
            grp.appendChild(row);
        });
    }

    // 回退:把调色板做成左侧浮层(TV 工具栏注入失败时)。
    renderDrawPaletteOverlay() {
        try {
            const container = document.getElementById("tv_chart_container_" + this.id);
            if (!container) return;
            if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
            let interval = '?';
            try { interval = this.widget.symbolInterval().interval; } catch (e) {}
            const { sig } = this._levelBarItems(interval);
            const barId = 'cl_drawpal_' + this.id;
            let bar = document.getElementById(barId);
            if (bar && bar.getAttribute('data-sig') === sig) { this._paintDrawPalette(bar); bar.style.display = 'flex'; return; }
            if (bar) bar.remove();
            bar = document.createElement('div');
            bar.id = barId;
            bar.setAttribute('data-sig', sig);
            bar.style.cssText = 'position:absolute; left:6px; top:64px; z-index:42; display:flex; flex-direction:column; align-items:center; gap:3px; background:rgba(255,255,255,0.85); padding:4px 3px; border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,0.18);';
            this._buildDrawPaletteInto(bar, document, interval);
            container.appendChild(bar);
        } catch (e) {
            console.warn('[CHARTS] renderDrawPaletteOverlay failed', e);
        }
    }

    // 当前周期 → 调色板级别颜色项(只各递归级别 1m/5m/30m/日线…,label + 链色;不含笔/段基础)。
    _levelBarItems(interval) {
        const levels = recursiveDisplayLevels(interval);
        const items = levels.map(({ label, level }) => ({
            label,
            color: getRecursiveLevelColor(interval, level),
        }));
        return { items, sig: `${interval}|${levels.map((item) => item.label).join('|')}` };
    }

    // 把「画图调色板」原生注入 TV 左侧画线工具栏列顶部。锚点用几何探测(窄<70+高>400+最左+含多个 group 子)
    // 而非哈希类名,较抗 TV 升级;找不到工具栏返回 false → 调用方回退浮层 renderDrawPaletteOverlay。
    injectDrawPaletteIntoTVToolbar() {
        try {
            let doc = null, inner = null;
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                if (!dd) continue;
                let cand = null;
                dd.querySelectorAll('div').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 70 && r.height > 400 && r.left < 12 && el.children.length >= 3) {
                        if (!cand || r.height > cand.getBoundingClientRect().height) cand = el;
                    }
                });
                if (cand) { doc = dd; inner = cand; break; }
            }
            if (!doc || !inner) return false;
            let interval = '?';
            try { interval = this.widget.symbolInterval().interval; } catch (e) {}
            const { sig } = this._levelBarItems(interval);
            const hideOverlay = () => { const ov = document.getElementById('cl_drawpal_' + this.id); if (ov) ov.style.display = 'none'; };
            const grpId = 'cl_tv_drawpal_' + this.id;
            let grp = doc.getElementById(grpId);
            if (grp && grp.isConnected && grp.getAttribute('data-sig') === sig) {
                this._paintDrawPalette(grp);
                hideOverlay();
                return true;
            }
            if (grp) grp.remove();
            grp = doc.createElement('div');
            grp.id = grpId;
            grp.setAttribute('data-sig', sig);
            grp.style.cssText = 'display:flex; flex-direction:column; align-items:center; gap:3px; padding:6px 0 5px; width:52px; border-bottom:1px solid rgba(120,120,120,0.3);';
            this._buildDrawPaletteInto(grp, doc, interval);
            inner.insertBefore(grp, inner.firstChild);
            hideOverlay();
            return true;
        } catch (e) {
            console.warn('[CHARTS] injectDrawPaletteIntoTVToolbar failed', e);
            return false;
        }
    }

    // macOS 触控板两指横向 swipe 会触发浏览器后退/前进:在图表上想左右平移却跳转到上/下一页。
    // 顶层 app.css 已设 overscroll-behavior:none,但 TV 图表在 iframe 内、iframe 是独立文档,横向
    // overscroll 可能在 iframe 层就被浏览器当成导航手势。这里给同源 TV iframe 的 html/body 也注入
    // overscroll-behavior:none(双保险)。跨源 iframe(contentDocument 抛错)跳过;幂等,可随
    // onChartReady / _lvlbtnTimer 重试多次调用(iframe 异步加载)。
    applyOverscrollGuard() {
        try {
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                if (!dd) continue;
                try {
                    if (dd.documentElement) dd.documentElement.style.overscrollBehavior = 'none';
                    if (dd.body) dd.body.style.overscrollBehavior = 'none';
                } catch (e) { /* 同源但访问受限,跳过 */ }
            }
        } catch (e) { /* querySelectorAll 失败极罕见,静默 */ }
    }

    // 画图调色板统一入口:优先原生注入 TV 工具栏;失败回退左侧浮层。
    updateDrawPalette() {
        if (!this.injectDrawPaletteIntoTVToolbar()) this.renderDrawPaletteOverlay();
    }

    handleSymbolChange(symbol) {
        if (!symbol?.ticker) return;
        const [marketRaw, code] = symbol.ticker.split(":");
        if (!marketRaw || !code) return;
        // ⚠ market 必须归一小写: symbol.ticker 来自 TV 是大写(如 "US:QQQ.US"，因 chart.symbol()
        // 返回大写)，而 Utils.get_market() 存的是小写 "us"。不归一 → "us" !== "US" 恒成立 →
        // 每次切标的都误判 market 变 → location.reload() 整页刷新 → 多标的切换卡死(浏览器实测复现+补丁验证)。
        const market = marketRaw.toLowerCase();
        if (Utils.get_market() !== market) { Utils.set_local_data("market", market); location.assign("/?market=" + encodeURIComponent(market)); return; }
        Utils.set_local_data("market", market); Utils.set_local_data(`${market}_code`, code);
        this._resetDataReadyContext();
        this._latestAppliedBarTime = null;
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('symbol-change');
        this._openSseStream();
        if (typeof ZiXuan.render_zixuan_opts === "function") ZiXuan.render_zixuan_opts();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 同市场切标的:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }
    // 切周期时的显示配置编排:把当前配置存回旧周期 key,解析并应用新周期配置(未配过则继承当前并固化),
    // 更新 _curResolution。纯前端 localStorage,不触发缠论重算;重绘由既有 handleDataReady→重绘链路负责。
    _applyResolutionConfig(newResolution) {
        const oldRes = this._curResolution;
        if (oldRes && oldRes !== newResolution && this.cl_show_config) {
            saveClShowConfig(this.id, oldRes, this.cl_show_config);
        }
        const r = resolveClConfigForResolution(this.id, newResolution, this.cl_show_config);
        this.cl_show_config = r.cfg;
        if (r.persist) {
            saveClShowConfig(this.id, newResolution, r.cfg);
        }
        this._curResolution = newResolution;
    }

    // Finding 1(审计 MED):chart 就绪后按真实周期校正 _curResolution。构造函数的 _curResolution 是猜测
    // (localStorage 或 '1'),可能与 TV 实际显示周期(load_last_chart 存档 / TV 默认日线)不符;就绪后以
    // chart.resolution() 为准,否则首次加载 toggle 会把显示配置存到错误周期 key。
    _alignResolutionOnReady() {
        try {
            const realRes = (this.chart && this.chart.resolution) ? this.chart.resolution() : null;
            if (realRes && realRes !== this._curResolution) {
                this._applyResolutionConfig(realRes);
            }
        } catch (e) { /* resolution() 异常不影响首绘 */ }
    }

    handleIntervalChange(interval) {
        if (!interval) return;
        const market = Utils.get_market(); if (!market) return;

        this._resetDataReadyContext();
        this._drawRetryCount = 0;
        this._latestAppliedBarTime = null;
        const currentSeq = ++this._intervalSwitchSeq;
        this._intervalVersion++;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleIntervalChange → ${interval} (seq=${currentSeq}, ver=${this._intervalVersion}) [_initialLoadDone reset to false]`);
        Utils.set_local_data(`${market}_interval_${this.id}`, interval);
        this._applyResolutionConfig(interval);   // 切周期:存旧周期配置、载入本周期配置(未配过继承当前)
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('interval-change');
        this._openSseStream();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 切周期:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }

    handleDataReady(
        contextVersion = this._dataContextVersion || 0,
        expectedIdentity = this._currentDataIdentityKey()
    ) {
        if (contextVersion !== (this._dataContextVersion || 0)) return false;
        const currentIdentity = this._currentDataIdentityKey();
        if (!currentIdentity || expectedIdentity !== currentIdentity) return false;
        if (!this._chartDataReadyNow()) return false;

        const wasInitialLoad = (
            this._tvDataReadyVersion !== contextVersion ||
            this._tvDataReadyIdentity !== currentIdentity
        );
        const hasPendingDraw = (
            this._pendingChanlunDrawVersion === contextVersion &&
            this._pendingChanlunDrawIdentity === currentIdentity
        );
        this._tvDataReadyVersion = contextVersion;
        this._tvDataReadyIdentity = currentIdentity;
        this._dataReadyProbeVersion = null;
        this._dataReadyProbeIdentity = null;
        this._pendingChanlunDrawVersion = null;
        this._pendingChanlunDrawIdentity = null;
        this._initialLoadDone = true;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleDataReady ✓ context=${contextVersion} initial=${wasInitialLoad} pending=${hasPendingDraw}`);
        this._maybeWidenDefaultView();

        if (hasPendingDraw || wasInitialLoad) {
            if (wasInitialLoad) this.draw_chanlun();
            else this.debouncedDrawChanlun();
        }
        return true;
    }

    // 首次加载某 标的+周期 时,若默认可视窗过窄(实测外汇默认仅 ~4h/~43根 → 只见 1 笔,而数据有 438 笔),
    // 按周期拉宽到一个合理跨度,让图表一打开就显示更多历史与缠论。仅首次、且仅当过窄时设,
    // 不夺已够宽的图(A股默认已宽则跳过)或用户后续缩放。设更宽视窗会触发 TV 按需加载更早的 K 线。
    _maybeWidenDefaultView() {
        try {
            const si = this.widget && this.widget.symbolInterval ? this.widget.symbolInterval() : null;
            if (!si || !si.symbol || !si.interval) return;
            const key = si.symbol + '|' + si.interval;
            if (this._viewSetFor === key) return;
            // 当前 标的+周期 的 K 线必须已加载,否则数据/视窗未就绪 → 不设标记,等下次(handleDataReady/切换)再试。
            const resKey = String(si.symbol).toLowerCase() + String(si.interval).toLowerCase();
            const hp = this.udf_datafeed && this.udf_datafeed._historyProvider;
            const br = hp && hp.bars_result && hp.bars_result.get(resKey);
            if (!br || !br.bars || br.bars.length < 2) return;
            const vr = this.chart && this.chart.getVisibleRange ? this.chart.getVisibleRange() : null;
            if (!vr || !vr.from || !vr.to || vr.to <= vr.from) return;   // 数据/视窗未就绪,下次再试
            const toEpochSeconds = (value) => {
                const numeric = Number(value);
                return Number.isFinite(numeric) && Math.abs(numeric) >= 100000000000
                    ? numeric / 1000
                    : numeric;
            };
            const firstBarSec = toEpochSeconds(br.bars[0]?.time);
            const lastBarSec = toEpochSeconds(br.bars[br.bars.length - 1]?.time);
            if (!Number.isFinite(firstBarSec) || !Number.isFinite(lastBarSec) || firstBarSec > lastBarSec) return;
            const visibleOutsideLoadedBars = vr.to < firstBarSec || vr.from > lastBarSec;
            this._viewSetFor = key;
            // 各周期默认视窗跨度(日历天);跨度按天数,外汇 24h 连续→根数多,A股有夜盘缺口→根数少,均显示充足缠论。
            // 白名单只列分钟~月线;未列出的周期(秒线10S/30S、季线3M、年线12M等)直接跳过不拉宽——
            // 否则 6 天 fallback 对秒线会触发拉数万根、对季/年线又过窄;且这些周期 TV 默认视窗本就够宽。
            const SPAN_DAYS = { '1': 2, '2': 3, '3': 3, '5': 6, '10': 8, '15': 12, '30': 45, '60': 90, '120': 120, '180': 150, '240': 200, '1D': 400, '2D': 700, '1W': 1825, '1M': 5475 };
            const days = SPAN_DAYS[si.interval];
            if (!days) return;   // 周期不在白名单(秒/季/年等)→保持 TV 默认视窗
            const span = days * 86400;
            if (!visibleOutsideLoadedBars && (vr.to - vr.from) >= span * 0.7) return;   // 当前已够宽且与行情相交→不动
            setTimeout(() => {
                try {
                    // 竞态防护:延时期间用户若已切到别的标的/周期,放弃,避免把视窗设成上一周期的范围。
                    const si2 = this.widget && this.widget.symbolInterval ? this.widget.symbolInterval() : null;
                    if (!si2 || (si2.symbol + '|' + si2.interval) !== key) return;
                    const v = this.chart.getVisibleRange();
                    const targetTo = visibleOutsideLoadedBars ? lastBarSec : v?.to;
                    if (!targetTo) return;
                    this.chart.setVisibleRange({
                        from: visibleOutsideLoadedBars
                            ? Math.max(firstBarSec, targetTo - span)
                            : targetTo - span,
                        to: targetTo,
                    });
                } catch (e) {}
            }, 150);
        } catch (e) { /* 视窗调整失败不影响主流程 */ }
    }
    handleTick() {
        const identity = this.getCurrentChartIdentity();
        if (!identity) return;
        const symbolResKey = `${identity.symbol.toString().toLowerCase()}${identity.interval.toString().toLowerCase()}`;
        const barsResult = this.udf_datafeed?._historyProvider?.bars_result?.get(symbolResKey);
        const latestBar = barsResult?.bars?.[barsResult.bars.length - 1];
        if (!latestBar || latestBar.time === this._latestAppliedBarTime) {
            return;
        }
        clog("[DataVerify][Charts] handleTick newBar key=" + symbolResKey, {
            barTime: latestBar.time,
            prevTime: this._latestAppliedBarTime,
            barsCount: barsResult?.bars?.length || 0,
        });
        this._latestAppliedBarTime = latestBar.time;
        this.debouncedDrawChanlun();
    }
    handleVisibleRangeChange() {
        if (this._initialLoadDone) {
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleVisibleRangeChange → debouncedDrawChanlun (will fire 300ms later)`);
            this.debouncedDrawChanlun();
        } else {
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleVisibleRangeChange SKIPPED (_initialLoadDone=false)`);
        }
    }

    safeRemove(entityId) {
        if (!entityId) return Promise.resolve();
        if (typeof entityId.then === 'function') {
            return entityId.then(id => {
                if (id) {
                    let ok = false;
                    try { this.chart.removeEntity(id); ok = true; }
                    catch (e) { console.warn(`[CHANLUN-DIAG][safeRemove] removeEntity 抛错 id=${id}`, e); }
                    if (this._reconcileOwnedIds) this._reconcileOwnedIds.delete(id);
                    if (!ok) console.warn(`[CHANLUN-DIAG][safeRemove] async path 静默失败,id=${id} 可能成为孤儿`);
                }
            }).catch(e => {
                console.warn('[CHANLUN-DIAG][safeRemove] async path promise rejected', e);
            });
        } else {
            let ok = false;
            try { this.chart.removeEntity(entityId); ok = true; }
            catch (e) { console.warn(`[CHANLUN-DIAG][safeRemove] removeEntity 抛错 id=${entityId}`, e); }
            if (this._reconcileOwnedIds) this._reconcileOwnedIds.delete(entityId);
            if (!ok) console.warn(`[CHANLUN-DIAG][safeRemove] sync path 静默失败,id=${entityId} 可能成为孤儿`);
            return Promise.resolve();
        }
    }

    clear_draw_chanlun(clear_type) {
        this.markDrawingMutationStart('chanlun-clear');
        // 全量/末段清除都让 reconcile 走重新创建路径，重置失败重试计数
        // 避免上次首屏未 ready 攒到上限后影响切换/清除后的新一轮重试
        this._resetReconcileRetry();
        const removePromises = [];
        this._clearAllStrictScopes('clear-drawings');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        if (clear_type == "last") {
            for (const symbolKey in this.obj_charts) {
                for (const chartType in this.obj_charts[symbolKey]) {
                    if (this.obj_charts[symbolKey][chartType].length == 0) continue;
                    const maxTime = Math.max(...this.obj_charts[symbolKey][chartType].map((item) => item.time));
                    for (const _i in this.obj_charts[symbolKey][chartType]) {
                        const item = this.obj_charts[symbolKey][chartType][_i];
                        if (item.time == maxTime) {
                            removePromises.push(this.safeRemove(item.id));
                        }
                    }
                    this.obj_charts[symbolKey][chartType] = this.obj_charts[symbolKey][chartType].filter((item) => item.time != maxTime);
                }
            }
        } else {
            Object.values(this.obj_charts).forEach((symbolData) => {
                Object.values(symbolData).forEach((chartItems) => {
                    chartItems.forEach((item) => {
                        removePromises.push(this.safeRemove(item.id));
                    });
                });
            });
            this.obj_charts = {};
        }
        Promise.allSettled(removePromises).then(() => {
            this.markDrawingMutationEnd('chanlun-clear');
        });
    }

    _strictApi() {
        const api = (typeof window !== 'undefined') ? window.ChartStructureReconcile : null;
        if (!api || typeof api.planReconcile !== 'function' || typeof api.ReconcileEpoch !== 'function') {
            throw new Error('strict chart reconcile helper is unavailable');
        }
        return api;
    }

    _ensureStrictReconcileState() {
        if (!(this._strictContainers instanceof Map)) this._strictContainers = new Map();
        if (!(this._strictScopes instanceof Set)) this._strictScopes = new Set();
        if (!(this._strictDesiredByScope instanceof Map)) this._strictDesiredByScope = new Map();
        if (!(this._strictPendingCreates instanceof Map)) this._strictPendingCreates = new Map();
        if (!(this._reconcileOwnedIds instanceof Set)) this._reconcileOwnedIds = new Set();
        if (!this._strictReconcileEpoch) {
            this._strictReconcileEpoch = new (this._strictApi().ReconcileEpoch)();
        }
    }

    _strictFrequencyFromResolution(resolution) {
        const value = String(resolution == null ? '' : resolution).trim();
        const fixed = {
            '10S': '10s', '30S': '30s',
            '1D': 'd', '2D': '2d', '1W': 'w', '1M': 'm',
            '3M': 'q', '12M': 'y',
        };
        if (fixed[value]) return fixed[value];
        if (/^[1-9][0-9]*$/.test(value)) return `${value}m`;
        return value.toLowerCase();
    }

    _strictNormalizeSymbol(symbol) {
        return String(symbol == null ? '' : symbol)
            .replace(/^[^:]+:/, '')
            .trim()
            .toUpperCase();
    }

    _setStrictStructureStatus(state, code = null) {
        this._strictStructureStatus = { state, code };
        try {
            const host = document.getElementById(`tv_chart_container_${this.id}`);
            if (!host) return;
            const statusId = `strict_structure_status_${this.id}`;
            let status = document.getElementById(statusId);
            if (state === 'ready') {
                if (status && typeof status.remove === 'function') status.remove();
                return;
            }
            if (!status) {
                status = document.createElement('div');
                status.id = statusId;
                status.className = 'strict-structure-status';
                status.setAttribute('role', 'status');
                host.appendChild(status);
            }
            status.dataset.state = state;
            status.textContent = state === 'unavailable'
                ? `严格缠论结构暂不可用（${code || 'unknown'}）`
                : '正在同步严格缠论结构…';
        } catch (e) { /* 状态提示失败不能阻断 K 线和严格实体清理 */ }
    }

    _strictLoadedRange(bars) {
        if (!Array.isArray(bars) || bars.length === 0) {
            throw new Error('strict chart requires loaded bars');
        }
        const api = this._strictApi();
        const from = api.barTimeMsToEpochSeconds(bars[0].time);
        const to = api.barTimeMsToEpochSeconds(bars[bars.length - 1].time);
        if (from > to) throw new Error('loaded bars must be ordered');
        return { from, to };
    }

    _strictSourceClosedAt(barsResult) {
        const source = barsResult || {};
        if (Object.prototype.hasOwnProperty.call(source, 'times')) {
            if (!Array.isArray(source.times) || source.times.length === 0) {
                throw new Error('strict structure raw source close is invalid');
            }
            return this._strictApi().barTimeMsToEpochSeconds(
                source.times[source.times.length - 1],
            );
        }
        return this._strictLoadedRange(source.bars).to;
    }

    _validateStrictStructureSnapshot(snapshot, chartData, currentInterval) {
        if (!snapshot || snapshot.schema !== 'chanlun-chart-structure/v4') {
            throw new Error('strict structure schema mismatch');
        }
        const requiredStrings = [
            'symbol', 'source_frequency', 'display_frequency',
            'price_basis_revision', 'structure_price_quantum',
            'strict_config_revision', 'structure_revision',
            'snapshot_revision', 'render_revision',
        ];
        for (const field of requiredStrings) {
            if (typeof snapshot[field] !== 'string' || snapshot[field].length === 0) {
                throw new Error(`strict structure ${field} is required`);
            }
        }
        if (!Number.isInteger(snapshot.source_closed_at)) {
            throw new Error('strict structure source_closed_at must be epoch seconds');
        }
        if (!Number.isFinite(Number(snapshot.structure_price_quantum)) || Number(snapshot.structure_price_quantum) <= 0) {
            throw new Error('strict structure price quantum is invalid');
        }
        if (!Array.isArray(snapshot.stroke_center_observations) || !Array.isArray(snapshot.levels)) {
            throw new Error('strict structure collections are invalid');
        }

        const displayFrequency = this._strictFrequencyFromResolution(currentInterval);
        const chartSymbol = this._strictNormalizeSymbol(
            chartData.chartSymbol || this.widget?.symbolInterval?.()?.symbol,
        );
        if (
            this._strictNormalizeSymbol(snapshot.symbol) !== chartSymbol ||
            snapshot.display_frequency !== displayFrequency ||
            snapshot.source_frequency !== displayFrequency
        ) {
            throw new Error('strict structure chart context mismatch');
        }

        const loadedRange = this._strictLoadedRange(chartData.barsResult?.bars);
        const sourceClosedAt = this._strictSourceClosedAt(chartData.barsResult);
        if (snapshot.source_closed_at !== sourceClosedAt) {
            throw new Error('strict structure source close does not match loaded bars');
        }
        const rawVisible = chartData.visibleRange || { from: chartData.from, to: loadedRange.to };
        const visibleRange = {
            from: Math.floor(Number(rawVisible.from)),
            to: Math.ceil(Number(rawVisible.to)),
        };
        if (!Number.isInteger(visibleRange.from) || !Number.isInteger(visibleRange.to) || visibleRange.from > visibleRange.to) {
            throw new Error('strict structure visible range is invalid');
        }
        const context = {
            chartInstanceId: this.instanceId || `chart-manager-${this.id}`,
            symbol: this._strictNormalizeSymbol(snapshot.symbol),
            interval: displayFrequency,
            price_basis_revision: snapshot.price_basis_revision,
        };
        const contextToken = JSON.stringify([
            context.chartInstanceId,
            context.symbol,
            context.interval,
            context.price_basis_revision,
            snapshot.source_closed_at,
            snapshot.structure_revision,
            snapshot.snapshot_revision,
            snapshot.render_revision,
        ]);
        return { context, contextToken, loadedRange, visibleRange };
    }

    _strictItemEnabled(item) {
        return strictItemEnabled(this.cl_show_config || {}, item);
    }

    _strictRenderGroups(snapshot, context) {
        const api = this._strictApi();
        const groups = new Map();
        const add = (values, levelLabel = null) => {
            for (const rawItem of values || []) {
                const item = (
                    levelLabel && rawItem?.render_kind === 'strict_divergence'
                        ? { ...rawItem, level_label: levelLabel }
                        : rawItem
                );
                if (!item || !Number.isInteger(item.structural_level) || !Array.isArray(item.points)) {
                    throw new Error('strict render item is invalid');
                }
                if (!this._strictItemEnabled(item)) continue;
                const scope = api.scopeKey(context, item);
                if (!groups.has(scope)) groups.set(scope, []);
                groups.get(scope).push(item);
            }
        };
        add(snapshot.stroke_center_observations);
        for (const level of snapshot.levels) {
            if (
                !level || !Number.isInteger(level.structural_level)
                || typeof level.label !== 'string' || !level.label
                || level.origin !== 'current_chart_recursive'
            ) throw new Error('strict level is invalid');
            add(level.centers);
            add(level.center_projections);
            add(level.current_trends);
            // completed_trend_snapshots 是只读审计证据，不创建默认图形。
            add(level.confirmed_points);
            add(level.divergences, level.label);
        }
        return groups;
    }

    _createStrictShape(item, currentInterval, bars) {
        const level = item.structural_level || 0;
        const levelColor = getRecursiveLevelColor(currentInterval, level);
        if (item.render_kind === 'formal_center') {
            return ChartUtils.createZhongshuShape(this.chart, item, {
                color: levelColor,
                linewidth: level === 0 ? 2 : 3,
                overrides: {
                    linestyle: item.state === 'ongoing'
                        ? CHART_CONFIG.LINE_STYLES.DASHED
                        : CHART_CONFIG.LINE_STYLES.SOLID,
                },
            });
        }
        if (item.render_kind === 'center_observation') {
            const linestyle = item.state === 'ongoing'
                ? CHART_CONFIG.LINE_STYLES.DASHED
                : CHART_CONFIG.LINE_STYLES.SOLID;
            return ChartUtils.createZhongshuShape(this.chart, { ...item, linestyle }, {
                color: getDynamicColor(currentInterval, 'bi_zss'),
                linewidth: 1,
                overrides: { transparency: 98, linestyle },
            });
        }
        if (item.render_kind === 'center_projection') {
            return ChartUtils.createZhongshuShape(this.chart, { ...item, linestyle: CHART_CONFIG.LINE_STYLES.DASHED }, {
                color: levelColor,
                linewidth: 1,
                overrides: { transparency: 100, linestyle: CHART_CONFIG.LINE_STYLES.DASHED },
            });
        }
        if (item.render_kind === 'strict_trend') {
            return ChartUtils.createLineShape(this.chart, {
                ...item,
                linestyle: item.state === 'forming' ? CHART_CONFIG.LINE_STYLES.DASHED : CHART_CONFIG.LINE_STYLES.SOLID,
            }, { color: levelColor, linewidth: 2 });
        }
        if (item.render_kind === 'point_confirmed' || item.render_kind === 'point_approaching') {
            const isBuy = String(item.side || item.point_type || '').toLowerCase().includes('buy');
            const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
            const prefix = item.render_kind === 'point_approaching' ? '接近·' : '';
            return ChartUtils.createShape(this.chart, item.points[0], {
                shape: 'text',
                text: `${prefix}L${level}·${item.point_type}`,
                overrides: {
                    color,
                    fontsize: 13,
                    bold: item.render_kind === 'point_confirmed',
                    transparency: item.render_kind === 'point_confirmed' ? 0 : 35,
                    'linetooltext.color': color,
                    'linetooltext.fontsize': 13,
                },
            });
        }
        if (item.render_kind === 'strict_divergence') {
            const isBullish = item.direction === 'down';
            const color = isBullish ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
            const kindLabel = item.kind === 'consolidation' ? '盘整背驰' : '趋势背驰';
            return ChartUtils.createShape(this.chart, item.points[0], {
                shape: 'text',
                text: `${item.level_label || `L${level}`}·${kindLabel}`,
                overrides: {
                    color,
                    fontsize: 13,
                    bold: true,
                    transparency: 0,
                    'linetooltext.color': color,
                    'linetooltext.fontsize': 13,
                },
            });
        }
        throw new Error(`unsupported strict render kind: ${item.render_kind}`);
    }

    _acceptStrictEntity(scope, generation, contextToken, item, realId) {
        const api = this._strictApi();
        const desired = this._strictDesiredByScope.get(scope);
        const isCurrent = (
            realId != null &&
            this._strictReconcileEpoch.current(scope, generation) &&
            this._strictStructureContextToken === contextToken &&
            desired?.get(item.logicalKey) === item.renderKey
        );
        const container = this._strictContainers.get(scope);
        if (!isCurrent || !container || container.some((entry) => entry.logicalKey === item.logicalKey)) {
            if (realId != null) this.safeRemove(realId);
            return false;
        }
        container.push({
            id: realId,
            logicalKey: item.logicalKey,
            renderKey: item.renderKey,
            geometryFingerprint: item.geometryFingerprint,
            time: item.points[0]?.time,
            tailTime: item.points[item.points.length - 1]?.time,
        });
        this._reconcileOwnedIds.add(realId);
        return true;
    }

    _reconcileStrictScope(scope, incoming, loadedRange, visibleRange, createFunc, contextToken) {
        this._ensureStrictReconcileState();
        const api = this._strictApi();
        if (!this._strictContainers.has(scope)) this._strictContainers.set(scope, []);
        this._strictScopes.add(scope);
        const container = this._strictContainers.get(scope);
        const plan = api.planReconcile(container, incoming, loadedRange, visibleRange);
        const generation = this._strictReconcileEpoch.next(scope);
        const removeIds = new Set(plan.removeIds.filter((id) => id != null));
        if (removeIds.size) {
            for (const id of removeIds) this.safeRemove(id);
            const retained = container.filter((entry) => !removeIds.has(entry.id));
            container.length = 0;
            retained.forEach((entry) => container.push(entry));
        }

        const desired = new Map();
        for (const entry of container) desired.set(entry.logicalKey, entry.renderKey);
        for (const item of plan.createItems) desired.set(item.logicalKey, item.renderKey);
        this._strictDesiredByScope.set(scope, desired);

        for (const item of plan.createItems) {
            let result;
            try { result = createFunc(item); }
            catch (error) {
                console.warn('[STRICT-CHART] create strict entity failed', error);
                this._scheduleReconcileRetry('strict-create-throw');
                continue;
            }
            if (result != null && typeof result.then === 'function') {
                const pendingKey = `${scope}|${item.logicalKey}|${generation}`;
                const promise = Promise.resolve(result);
                this._strictPendingCreates.set(pendingKey, { scope, promise });
                promise.then((realId) => {
                    if (realId == null) {
                        this._scheduleReconcileRetry('strict-create-null');
                        return;
                    }
                    this._acceptStrictEntity(scope, generation, contextToken, item, realId);
                }).catch((error) => {
                    console.warn('[STRICT-CHART] async strict entity failed', error);
                    this._scheduleReconcileRetry('strict-create-reject');
                }).finally(() => {
                    if (this._strictPendingCreates.get(pendingKey)?.promise === promise) {
                        this._strictPendingCreates.delete(pendingKey);
                    }
                });
            } else if (result != null) {
                this._acceptStrictEntity(scope, generation, contextToken, item, result);
            } else {
                this._scheduleReconcileRetry('strict-create-null');
            }
        }
    }

    _clearStrictScope(scope, reason = 'clear') {
        if (!(this._strictContainers instanceof Map)) return;
        if (this._strictReconcileEpoch) {
            try { this._strictReconcileEpoch.next(scope); } catch (e) { /* disposed */ }
        }
        const container = this._strictContainers.get(scope) || [];
        for (const entry of container) this.safeRemove(entry.id);
        this._strictContainers.delete(scope);
        this._strictScopes?.delete(scope);
        this._strictDesiredByScope?.delete(scope);
        clog(`[STRICT-CHART] cleared scope reason=${reason} scope=${scope}`);
    }

    _clearAllStrictScopes(reason = 'clear-all') {
        const scopes = new Set();
        if (this._strictScopes instanceof Set) this._strictScopes.forEach((scope) => scopes.add(scope));
        if (this._strictContainers instanceof Map) this._strictContainers.forEach((_, scope) => scopes.add(scope));
        if (this._strictPendingCreates instanceof Map) {
            this._strictPendingCreates.forEach((pending) => { if (pending?.scope) scopes.add(pending.scope); });
        }
        scopes.forEach((scope) => this._clearStrictScope(scope, reason));
    }

    _strictUnavailable(code) {
        this._clearAllStrictScopes(code || 'unavailable');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        this._setStrictStructureStatus('unavailable', code || 'strict_evidence_invalid');
    }

    _drawStrictStructure(chartData, currentInterval) {
        const barsResult = chartData?.barsResult;
        const mode = barsResult?.strict_structure_mode;
        if (mode === 'unavailable') {
            this._strictUnavailable(barsResult.strict_structure_error?.code);
            return;
        }
        let snapshot;
        if (mode === 'replace') snapshot = barsResult.strict_structure;
        else if (mode === 'unchanged') snapshot = this._strictStructureSnapshot;
        else {
            this._strictUnavailable('strict_transport_missing');
            return;
        }
        if (!snapshot) {
            this._strictUnavailable('strict_snapshot_missing');
            return;
        }

        try {
            const validated = this._validateStrictStructureSnapshot(snapshot, chartData, currentInterval);
            const groups = this._strictRenderGroups(snapshot, validated.context);
            this._strictStructureSnapshot = snapshot;
            this._strictStructureContextToken = validated.contextToken;
            this._recMaxLevel = Math.max(
                0,
                ...snapshot.levels.map((level) => level.structural_level),
            );

            const nextScopes = new Set(groups.keys());
            const priorScopes = new Set(this._strictScopes instanceof Set ? this._strictScopes : []);
            for (const [scope, items] of groups.entries()) {
                this._reconcileStrictScope(
                    scope,
                    items,
                    validated.loadedRange,
                    validated.visibleRange,
                    (item) => this._createStrictShape(item, currentInterval, barsResult.bars),
                    validated.contextToken,
                );
            }
            for (const scope of priorScopes) {
                if (!nextScopes.has(scope)) this._clearStrictScope(scope, 'snapshot-replace');
            }
            this._setStrictStructureStatus('ready');
        } catch (error) {
            console.warn('[STRICT-CHART] rejected strict snapshot', error);
            this._strictUnavailable('strict_context_mismatch');
        }
    }

    getChartData() {
        const symbolInterval = this.widget.symbolInterval(); if (!symbolInterval) return null;
        const symbolResKey = `${symbolInterval.symbol.toString().toLowerCase()}${symbolInterval.interval.toString().toLowerCase()}`;
        let barsResult = this.udf_datafeed?._historyProvider?.bars_result?.get(symbolResKey);

        if (!barsResult) {
            // 容错: 写键(getBars 用 symbolInfo.ticker)与读键(symbolInterval().symbol)可能差一个
            // "market:" 前缀。剥前缀直查、或在现有键里找"去前缀后唯一相等"项再试一次。
            const _map = this.udf_datafeed?._historyProvider?.bars_result;
            if (_map) {
                const _interval = symbolInterval.interval.toString().toLowerCase();
                const _bare = symbolResKey.replace(/^[a-z]+:/, '');
                let _alt = _map.get(_bare);
                if (!_alt) {
                    const _cands = Array.from(_map.keys()).filter(k =>
                        k.toLowerCase().endsWith(_interval) &&
                        k.toLowerCase().replace(/^[a-z]+:/, '') === _bare);
                    if (_cands.length === 1) _alt = _map.get(_cands[0]);
                }
                if (_alt) barsResult = _alt;
            }
        }
        if (!barsResult) {
            // 切标的瞬间新标的 getBars 尚未回填 bars_result 属正常过渡态(draw_chanlun 会 retry,
            // 数据到位后 handleBarsReadyEvent 兜底重绘), 用 clog(debug) 而非 warn, 避免 console 刷屏。
            const availableKeys = this.udf_datafeed?._historyProvider?.bars_result ? Array.from(this.udf_datafeed._historyProvider.bars_result.keys()) : [];
            clog(`[DEBUG-CHARTS] getChartData for ${symbolResKey}: NOT FOUND. Available keys:`, availableKeys);
            return null;
        }

        if (!this.chart) {
            clog("[DEBUG-CHARTS] getChartData aborted: this.chart is null.");
            return null;
        }
        const visibleRange = this.chart.getVisibleRange();
        if (!visibleRange || !visibleRange.from || !visibleRange.to) {
            // 切标的后 chart 仍在 loading 时 VisibleRange 短暂无效, 同属正常过渡态, 降级为 clog。
            clog("[DEBUG-CHARTS] getChartData aborted: VisibleRange invalid (chart loading).");
            return null;
        }

        const from = visibleRange.from;
        const symbolKey = `${symbolInterval.symbol}_${symbolInterval.interval}`;
        return {
            symbolKey,
            chartSymbol: symbolInterval.symbol,
            chartInterval: symbolInterval.interval,
            barsResult,
            from,
            visibleRange: { from: visibleRange.from, to: visibleRange.to },
        };
    }



    makeKey(item) {
        // linestyle 不写入 key：末段 pending→done 翻转（linestyle 1→0）不应被视为不同 shape，
        // 否则每次翻转都触发全量 rebuild 导致闪烁。起/终点足以唯一标识多点形态。
        if (Array.isArray(item.points)) {
            return item.points.map(p => `${p.time}_${p.price}`).join('_');
        } else if (item.points?.time !== undefined) {
            return `${item.points.time}_${item.points.price}_${item.text || ''}`;
        }
        return `${item.id || Math.random()}`;
    }

    getUniqueRenderList(sourceList) {
        if (!sourceList || !Array.isArray(sourceList)) return [];
        const finished = [];
        const unfinished = [];
        sourceList.forEach(item => {
            if (item.linestyle == '1' || item.linestyle == 1) {
                unfinished.push(item);
            } else {
                finished.push(item);
            }
        });
        if (unfinished.length > 0) {
            finished.push(unfinished[unfinished.length - 1]);
        }
        return finished;
    }

    _sameReconcileSnapshot(snapshot, from, keys, unfinishedKeys) {
        if (!snapshot || snapshot.from !== from) return false;
        if (!(snapshot.keys instanceof Set) || snapshot.keys.size !== keys.size) return false;
        if (!(snapshot.unfinishedKeys instanceof Set) || snapshot.unfinishedKeys.size !== unfinishedKeys.size) return false;
        for (const key of keys) {
            if (!snapshot.keys.has(key)) return false;
        }
        for (const key of unfinishedKeys) {
            if (!snapshot.unfinishedKeys.has(key)) return false;
        }
        return true;
    }

    reconcile(type, sourceList, from, symbolKey, createFunc, useUnique = true, includeOverlaps = false) {
        const container = this.obj_charts[symbolKey][type];
        const beforeCount = container.length;
        let renderList = sourceList || [];
        const sourceCount = renderList.length;
        if (useUnique) {
            renderList = this.getUniqueRenderList(renderList);
        }
        const afterUniqueCount = renderList.length;

        // 按可视窗口过滤：历史元素可达数百根，全画会造成视觉杂乱。
        // 默认要求 headTime >= from；横跨窗口的多点元素可传
        // includeOverlaps=true，只要 tailTime >= from 就入渲染。
        const newKeys = new Set();
        const itemsToProcess = [];
        renderList.forEach(item => {
            let headTime, tailTime;
            if (Array.isArray(item.points)) {
                headTime = item.points[0]?.time;
                tailTime = item.points[item.points.length - 1]?.time;
            } else {
                headTime = tailTime = item.points?.time;
            }
            // 默认只在头部进入可视窗(必已加载)时创建，避免 createMultipointShape 把画外/未加载
            // 角点 snap 到边缘造成错位。
            // 注:单点形态(bcs/mmds)head=tail,等价;tailTime 仍用于下方 keep 判定。
            const shouldRender = includeOverlaps ? tailTime >= from : headTime >= from;
            if (shouldRender) {
                const key = this.makeKey(item);
                newKeys.add(key);
                itemsToProcess.push({ item, key, time: headTime, tailTime });
            }
        });

        // 精确状态守卫：完整几何 key、from 和未完成状态都相同才跳过。
        // 不再截断字符串，避免最新中枢位于截断范围后时漏掉边界修正。
        const unfinishedKeys = new Set(
            itemsToProcess
                .filter(p => p.item.linestyle == '1' || p.item.linestyle == 1)
                .map(p => p.key)
        );
        const guardKey = `${symbolKey}__${type}`;
        if (this._sameReconcileSnapshot(this._reconcileGuard[guardKey], from, newKeys, unfinishedKeys)) {
            if (window.__chanlunDebug) {
                console.log(
                    `[CHANLUN-DIAG][reconcile.${type}] W1 guard skip ` +
                    `(unchanged: ${newKeys.size} keys, from=${from})`
                );
            }
            return;
        }

        // makeKey 不含 linestyle，pending→done 翻转（虚→实）不触发重建，避免端点漂移
        const beforeContainerLen = container.length;
        // key→新 item 映射,供 toKeep 检测 pending↔done 翻转(makeKey 不含 linestyle,翻转命中同 key)
        const keyToNewItem = new Map();
        itemsToProcess.forEach(p => keyToNewItem.set(p.key, p.item));
        const toKeep = [];
        let removedCount = 0;
        for (const existing of container) {
            const existingTail = existing.tailTime ?? existing.time;
            if (newKeys.has(existing.key) && existingTail >= from) {
                // 端点不变但 linestyle 翻转(笔/线段 pending↔done):同 key 命中 toKeep,但旧 TV shape
                // 仍是旧样式(用户报:笔已完成页面仍显未完成虚线)。就地 setProperties 刷新 linestyle
                // (不重建=无闪烁/无端点漂移),同步 entry.isUnfinished。单点形态(无 linestyle)
                // isUnfinished 恒 false 不触发,天然只作用于笔/线段/走势类型线。
                const newItem = keyToNewItem.get(existing.key);
                if (newItem) {
                    const newUnfinished = (newItem.linestyle == '1' || newItem.linestyle == 1);
                    if (newUnfinished !== existing.isUnfinished && existing.id != null) {
                        try {
                            const sh = this.chart.getShapeById(existing.id);
                            if (sh && sh.setProperties) {
                                sh.setProperties({ linestyle: parseInt(newItem.linestyle) || 0 });
                            }
                        } catch (e) {}
                        existing.isUnfinished = newUnfinished;
                    }
                }
                toKeep.push(existing);
            } else {
                this.safeRemove(existing.id);
                removedCount += 1;
            }
        }
        container.length = 0;
        toKeep.forEach(item => container.push(item));

        const existingKeys = new Set(container.map(item => item.key));
        let createSync = 0, createAsync = 0, createSkip = 0;
        let createFailed = false;   // 本轮有 create→null(chart 未 ready/点超范围)→ 不落守卫、放行重试
        itemsToProcess.forEach(({ item, key, time, tailTime }) => {
            if (existingKeys.has(key)) { createSkip += 1; return; }
            const result = createFunc(item);
            const entry = {
                time: time,
                tailTime: tailTime,
                key: key,
                isUnfinished: (item.linestyle == '1' || item.linestyle == 1),
            };
            if (result != null && typeof result.then === 'function') {
                createAsync += 1;
                result.then(realId => {
                    if (realId == null) {
                        console.warn(`[CHANLUN-DIAG][reconcile.${type}] async create→null key=${(key||'').slice(0,40)}`);
                        delete this._reconcileGuard[guardKey];   // 撤守卫,否则重试同签名被 skip
                        this._scheduleReconcileRetry(`${type}:async-null`);
                        return;
                    }
                    entry.id = realId;
                    container.push(entry);
                    this._reconcileOwnedIds.add(realId);
                }).catch((e) => {
                    console.warn(`[CHANLUN-DIAG][reconcile.${type}] async create→reject key=${(key||'').slice(0,40)}`, e);
                    delete this._reconcileGuard[guardKey];
                    this._scheduleReconcileRetry(`${type}:async-reject`);
                });
            } else if (result != null) {
                entry.id = result;
                container.push(entry);
                this._reconcileOwnedIds.add(result);
                createSync += 1;
            } else {
                console.warn(`[CHANLUN-DIAG][reconcile.${type}] sync create→null key=${(key||'').slice(0,40)}`);
                createFailed = true;
                this._scheduleReconcileRetry(`${type}:sync-null`);
            }
        });

        // 同步路径完成后记录完整状态快照；异步 create 仍在 pending 无妨，resolve 后 push 到
        // container 与快照目标状态一致，下次 guard 时容器已正确。
        // 但若本轮有 sync create→null(chart 未 ready/点超已加载范围),**不落守卫**——否则
        // 状态未变时 _scheduleReconcileRetry 触发的重画会被上面 W1 guard 误 skip,失败的
        // shape 永不重建(表现为「中枢框初次不显、点中枢重勾后才出」)。async→null 在其回调删守卫同理。
        if (!createFailed) {
            this._reconcileGuard[guardKey] = {
                from,
                keys: new Set(newKeys),
                unfinishedKeys: new Set(unfinishedKeys),
            };
        }

        // 仅在 window.__chanlunDebug=true 时输出调试摘要，避免生产 console 刷屏
        // 错误诊断走 console.warn，不受此开关控制
        if (window.__chanlunDebug && (removedCount > 0 || createSync > 0 || createAsync > 0)) {
            console.log(
                `[CHANLUN-DIAG][reconcile.${type}] src=${sourceCount} unique=${afterUniqueCount} ` +
                `inWin=${itemsToProcess.length} containerWas=${beforeContainerLen}→${container.length} ` +
                `removed=${removedCount} createSync=${createSync} createAsync=${createAsync} skip=${createSkip} ` +
                `from=${from} owned=${this._reconcileOwnedIds?.size ?? '?'}`
            );
        }
    }

    // 失败重试调度：createFunc 返回 null（chart 未 ready）时定时重触 draw_chanlun。
    // 首屏数据就绪但布局未完成时极易丢失 shape，导致线段不连续；
    // 通过指数退避并限制次数，避免重试风暴。
    _scheduleReconcileRetry(reason) {
        if (!this._reconcileRetry) {
            this._reconcileRetry = { count: 0, timer: null };
        }
        const state = this._reconcileRetry;
        if (state.count >= 7) return;          // 最多 7 次：100→200→400→800→1600→3200→6400ms ≈ 12.7s
        if (state.timer) return;               // 已排队，避免叠加
        const delayMs = Math.min(100 * Math.pow(2, state.count), 6400);
        state.count += 1;
        state.timer = setTimeout(() => {
            state.timer = null;
            clog(`[CHANLUN-TIMING] reconcile retry #${state.count} (${reason}) after ${delayMs}ms`);
            // 绕过防抖直接调用：持续缩放时 visibleRangeChange 会不停 reset 300ms 防抖，
            // retry 经 debounced 路径会被无限期延后
            this.draw_chanlun();
        }, delayMs);
    }

    _resetReconcileRetry() {
        if (this._reconcileRetry && this._reconcileRetry.timer) {
            clearTimeout(this._reconcileRetry.timer);
        }
        this._reconcileRetry = { count: 0, timer: null };
        // 同步清掉守卫缓存，否则下次源数据相同时会被 W1 guard 误 skip
        this._reconcileGuard = {};
        if (this._verifyRebuildTimer) {
            clearTimeout(this._verifyRebuildTimer);
            this._verifyRebuildTimer = null;
        }
        this._verifyingUntil = null;
    }

    // 补绘调度：full rebuild 后 500ms 触发一次 verify-rebuild，用于补齐布局抖动遗漏的 shape。
    // 守门用 timestamp（_verifyingUntil）而非微任务标志：draw_chanlun 是 async，
    // microtask 重置会在首个 await 之前跑掉，导致 verify 自身又排队 → 500ms 自循环。
    _scheduleVerifyRebuild() {
        if (this._verifyRebuildTimer) {
            clearTimeout(this._verifyRebuildTimer);
        }
        this._verifyRebuildTimer = setTimeout(() => {
            this._verifyRebuildTimer = null;
            this._verifyingUntil = performance.now() + 1500;
            // 清掉守卫缓存，确保 reconcile 走全量 rebuild 路径，基于稳定布局重新落位 shape
            this._reconcileGuard = {};
            clog(`[CHANLUN-TIMING] verify-rebuild firing`);
            this.draw_chanlun();
        }, 500);
    }

    _isVerifyingNow() {
        return this._verifyingUntil != null && performance.now() < this._verifyingUntil;
    }

    initChartContainer(symbolKey) {
        if (!this.obj_charts[symbolKey]) {
            this.obj_charts[symbolKey] = {};
            CHART_CONFIG.CHART_TYPES.forEach((type) => { this.obj_charts[symbolKey][type] = []; });
        }
        return this.obj_charts[symbolKey];
    }

    getMACDStudyId() {
        if (this.macdStudyId) return this.macdStudyId;
        const studies = this.chart.getAllStudies();
        const macdStudy = studies.find(s => s.name === 'MACD_HTF');
        if (macdStudy) { this.macdStudyId = macdStudy.id; return macdStudy.id; }
        return null;
    }

    drawChartElements(chartData, currentInterval) {
        const { symbolKey, barsResult, from } = chartData;
        if (!barsResult) return;
        this.initChartContainer(symbolKey);

        clog("[DataVerify][Charts] drawChartElements interval=" + currentInterval, {
            fxs: barsResult.fxs?.length || 0,
            bis: barsResult.bis?.length || 0,
            xds: barsResult.xds?.length || 0,
            strict_mode: barsResult.strict_structure_mode || 'missing',
            strict_revision: barsResult.strict_structure?.render_revision || null,
        });

        const safeCreate = (promise, type) => {
            if (promise && typeof promise.then === 'function') {
                return promise.catch(e => {
                    console.error(`[DEBUG-CHARTS] Error creating shape (${type}):`, e);
                    return null;
                });
            }
            return promise;
        };

        // 使用本图表实例独立的显示配置，多图布局下互不影响
        const cfg = this.cl_show_config;
        this.reconcile('fxs', cfg.fx ? barsResult.fxs : [], from, symbolKey, (item) => safeCreate(ChartUtils.createFxShape(this.chart, item), 'fx'), false);
        // 笔细(1)、线段粗(2):缠论惯例,笔数量多取细线、线段更高级取粗线;粗细差再叠加颜色差,提升可辨识。
        this.reconcile('bis', cfg.bi ? barsResult.bis : [], from, symbolKey, (item) => safeCreate(ChartUtils.createLineShape(this.chart, item, { color: getDynamicColor(currentInterval, "bis"), linewidth: 1 }), 'bi'));
        this.reconcile('xds', cfg.xd ? barsResult.xds : [], from, symbolKey, (item) => safeCreate(ChartUtils.createLineShape(this.chart, item, { color: getDynamicColor(currentInterval, "xds"), linewidth: 2 }), 'xd'));
        // 中枢、走势类型和买卖点只消费一个严格原子快照；旧字段即使仍滞留在浏览器
        // cache 中也不会进入绘制路径。可视窗口只决定是否创建，几何裁剪只依据已加载 K 线。
        this._drawStrictStructure(chartData, currentInterval);
        this.updateDrawPalette();
        if (this._sweepOrphanTimer) clearTimeout(this._sweepOrphanTimer);
        this._sweepOrphanTimer = setTimeout(() => {
            this._sweepOrphanTimer = null;
            this.sweepOrphanShapes();
        }, 100);
        return;
    }

    /**
     * 扫描并清理孤儿 shape:存在于 _reconcileOwnedIds 但所有 container 都已不引用的 entity。
     *
     * 触发场景:
     *   1. safeRemove 在 TV 内部静默失败(removeEntity 抛错被 try/catch 吞)
     *   2. reconcile 之间的 race:旧 container 已清零,async safeRemove 还未真的删
     *   3. 同一 key 因端点漂移走两次 create 路径,产生重复 shape
     *
     * 不会误删用户手画的 line tool:它们从未通过 reconcile 创建,不在 _reconcileOwnedIds 中。
     */
    sweepOrphanShapes() {
        if (!this.chart) {
            console.log('[CHANLUN-DIAG][sweep] skipped: this.chart is null');
            return;
        }
        if (!this._reconcileOwnedIds) {
            console.log('[CHANLUN-DIAG][sweep] skipped: _reconcileOwnedIds is null');
            return;
        }
        const inUseIds = new Set();
        Object.values(this.obj_charts || {}).forEach(symbolData => {
            Object.values(symbolData || {}).forEach(items => {
                (items || []).forEach(item => {
                    if (item && item.id != null && typeof item.id !== 'object') {
                        inUseIds.add(item.id);
                    }
                });
            });
        });
        if (this._strictContainers instanceof Map) {
            this._strictContainers.forEach((items) => {
                (items || []).forEach((item) => {
                    if (item && item.id != null && typeof item.id !== 'object') inUseIds.add(item.id);
                });
            });
        }
        let tvShapes = [];
        try { tvShapes = this.chart.getAllShapes() || []; }
        catch (e) { console.warn('[CHANLUN-DIAG][sweep] getAllShapes 抛错', e); }
        const tvIds = new Set(tvShapes.map(s => s.id));

        // ownedOrphans：owned 有但 container 已无引用 → 应删除的孤儿
        const ownedOrphans = [];
        this._reconcileOwnedIds.forEach(id => {
            if (!inUseIds.has(id)) ownedOrphans.push(id);
        });

        // trulyForeign：TV 里有但 owned/inUse 都无记录 → 理论上是用户手画，race 时可能漏 add
        const trulyForeign = tvShapes.filter(s => !inUseIds.has(s.id) && !this._reconcileOwnedIds.has(s.id));

        // sweep 总结仅在 window.__chanlunDebug 时输出，避免生产 console 刷屏
        if (window.__chanlunDebug) {
            console.log(
                `[CHANLUN-DIAG][sweep] tvShapes=${tvShapes.length} owned=${this._reconcileOwnedIds.size} ` +
                `inUse=${inUseIds.size} ownedOrphans=${ownedOrphans.length} trulyForeign=${trulyForeign.length}`
            );
        }

        // 打头 5 个真正"我们没跟踪但 TV 里有"的 shape — 诊断用,看是不是用户手画 vs 漏跟踪
        if (trulyForeign.length > 0) {
            console.log('[CHANLUN-DIAG][sweep] trulyForeign sample:', trulyForeign.slice(0, 5).map(s => ({
                id: s.id, name: s.name,
                // EntityInfo 含 zorder/visible 等;具体 points 需用 getShapeById 取
            })));
            // 进一步:对前 3 个尝试 getShapeById 拿 points,确认是 trend_line 错位
            trulyForeign.slice(0, 3).forEach(s => {
                try {
                    const api = this.chart.getShapeById(s.id);
                    const points = api?.getPoints?.();
                    console.log(`[CHANLUN-DIAG][sweep] foreign detail id=${s.id} name=${s.name} points=`, points);
                } catch (e) {
                    console.log(`[CHANLUN-DIAG][sweep] foreign detail id=${s.id} getShapeById 抛错`, e);
                }
            });
        }

        if (ownedOrphans.length > 0) {
            let removed = 0;
            ownedOrphans.forEach(id => {
                try {
                    this.chart.removeEntity(id);
                    removed += 1;
                } catch (e) {
                    console.warn(`[CHANLUN-DIAG][sweep] removeEntity 抛错 id=${id}`, e);
                }
                this._reconcileOwnedIds.delete(id);
            });
            if (window.__chanlunDebug) {
                console.log(`[CHANLUN-DIAG][sweep] orphan removed=${removed} owned-after=${this._reconcileOwnedIds.size}`);
            }
        }

        // reconcile 入口已用 headTime >= from 过滤，shape 起点严格在可见窗内，
        // 不会被 TV snap 到边缘，无需 snap-check + remove+recreate，仅保留孤儿扫描即可。
    }

    async draw_chanlun() {
        const currentVersion = this._intervalVersion;
        const capturedSeq = this._intervalSwitchSeq;

        if (!this.chart) {
            try {
                this.chart = this.widget.activeChart();
            } catch (e) {
                console.warn("[DEBUG-CHARTS] draw_chanlun: activeChart not available");
                return;
            }
        }

        const dataContextVersion = this._dataContextVersion || 0;
        const dataContextIdentity = this._currentDataIdentityKey();
        if (
            this._tvDataReadyVersion !== dataContextVersion ||
            this._tvDataReadyIdentity !== dataContextIdentity ||
            !this._chartDataReadyNow()
        ) {
            this._requestChanlunDrawWhenReady();
            return;
        }

        await new Promise(resolve => setTimeout(resolve, 0));

        if (this._intervalVersion !== currentVersion || capturedSeq !== this._intervalSwitchSeq) {
            console.warn("周期已切换，丢弃过期的缠论渲染任务");
            return;
        }

        const chartData = this.getChartData();
        if (!chartData) {
            if (!this._drawRetryCount) this._drawRetryCount = 0;
            // 切标的后新标的 getBars 的发起+回填+chart 渲染就绪可能 >5s(多标的并发/长桥 QPS
            // 排队时尤甚), retry 上限拉长到 30×500ms≈15s 覆盖; 数据到位后 handleBarsReadyEvent
            // 也会兜底主动重绘。retry 期间「数据未就绪」是切标的的正常过渡态, 用 clog(debug) 而非
            // console.warn, 避免切标的瞬间 console 被 NOT FOUND/exhausted 刷屏(被误读成功能故障)。
            if (this._drawRetryCount < 30) {
                this._drawRetryCount++;
                clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry#${this._drawRetryCount}/30 in 500ms`);
                setTimeout(() => this.debouncedDrawChanlun(), 500);
            } else {
                clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry exhausted (handleBarsReadyEvent 仍会在数据到位后兜底重绘)`);
            }
            return;
        }
        this._drawRetryCount = 0;

        const symbolInterval = this.widget.symbolInterval();
        if (!symbolInterval) {
            console.warn("[DEBUG-CHARTS] draw_chanlun aborted: symbolInterval is null");
            return;
        }

        // 计算可视区与 bars 范围对比，分析"可视区比 bars 窄多少"
        const vr = this.chart?.getVisibleRange?.();
        const firstBarSec = (chartData.barsResult.bars?.[0]?.time || 0) / 1000;
        const lastBarSec = (chartData.barsResult.bars?.[chartData.barsResult.bars.length - 1]?.time || 0) / 1000;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun executing interval=${symbolInterval.interval}`);
        clog(`[CHANLUN-TIMING]   from=${chartData.from} (visibleRange.from), barsRange=[${firstBarSec.toFixed(0)}, ${lastBarSec.toFixed(0)}], visibleRange=[${vr?.from?.toFixed(0)}, ${vr?.to?.toFixed(0)}]`);
        clog(`[CHANLUN-TIMING]   barsResult: bars=${chartData.barsResult.bars?.length || 0} fxs=${chartData.barsResult.fxs?.length || 0} bis=${chartData.barsResult.bis?.length || 0} xds=${chartData.barsResult.xds?.length || 0} strict=${chartData.barsResult.strict_structure_mode || 'missing'}`);

        const bisInside = (chartData.barsResult.bis || []).filter(b => (b.points?.[0]?.time ?? 0) >= chartData.from).length;
        const xdsInside = (chartData.barsResult.xds || []).filter(x => (x.points?.[0]?.time ?? 0) >= chartData.from).length;
        const totalBis = chartData.barsResult.bis?.length || 0;
        const totalXds = chartData.barsResult.xds?.length || 0;
        clog(`[CHANLUN-TIMING]   FILTER bis: ${bisInside}/${totalBis} pass (filtered out ${totalBis - bisInside}), xds: ${xdsInside}/${totalXds} pass (filtered out ${totalXds - xdsInside})`);
        this.markDrawingMutationStart('chanlun-redraw');
        const drawStartTs = performance.now();
        try {
            this.drawChartElements(chartData, symbolInterval.interval);
            // 切回已缓存周期时 TV 不再触发 onDataLoaded，handleDataReady 不来，
            // _initialLoadDone 永远 false，导致后续缩放平移无法补绘左侧形态。
            // 兜底：draw_chanlun 成功执行说明 chart 已有数据，强制置 true 恢复响应能力。
            this._initialLoadDone = true;
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun DONE (took ${(performance.now() - drawStartTs).toFixed(0)}ms) [_initialLoadDone=true]`);
        } finally {
            this.markDrawingMutationEnd('chanlun-redraw');
        }
    }

    _closeSseStream() {
        if (this._sse) {
            try { this._sse.close(); } catch (e) { /* ignore */ }
            this._sse = null;
        }
        this._clearSseFallback();
    }

    // SSE 被中间层(frp http vhost / nginx)缓冲时的兜底：手动驱动 datafeed 的
    // DataPulseProvider 轮询(getBars 增量, 拉后端最新 K线+缠论), 频率远快于默认 30s。
    _startSseFallback() {
        if (this._sseFallbackInterval) return;
        const dpp = this.udf_datafeed && this.udf_datafeed._dataPulseProvider;
        if (!dpp || typeof dpp._updateData !== 'function') return;
        this._sseFallbackInterval = setInterval(() => {
            try { dpp._updateData(); } catch (e) { /* ignore */ }
        }, 6000);
    }

    // 清理 SSE 健康哨兵 + fallback 快轮询定时器。
    _clearSseFallback() {
        if (this._sseHealthTimer) { clearTimeout(this._sseHealthTimer); this._sseHealthTimer = null; }
        if (this._sseFallbackInterval) { clearInterval(this._sseFallbackInterval); this._sseFallbackInterval = null; }
    }

    // 取 TV 画布当前渲染到的末根秒数(从 bars_result 末根, 与 onmessage/看门狗同源)。
    // 用于断档/看门狗判定的"画布末根"参照, 以及退避"上次 reset 是否生效"判定。无数据返回 null。
    _getViewLatestSec(resKey, interval) {
        try {
            const map = this.udf_datafeed && this.udf_datafeed._historyProvider && this.udf_datafeed._historyProvider.bars_result;
            if (!map) return null;
            let prev = map.get(resKey);
            if (!prev) {
                // 与 getChartData 同口径(M-1): 写键(getBars 用 symbolInfo.ticker)与读键
                // (symbolInterval().symbol)可能差一个 "market:" 前缀。剥前缀直查 / 去前缀唯一匹配再试。
                // 否则 G3(SSE 半开从无帧, 只有轮询写的 ticker 键)下看门狗 plain get 命中不到 → 静默失效。
                const bare = String(resKey).replace(/^[a-z]+:/, '');
                prev = map.get(bare);
                if (!prev && interval) {
                    const iv = String(interval).toLowerCase();
                    const cands = Array.from(map.keys()).filter(k =>
                        k.toLowerCase().endsWith(iv) &&
                        k.toLowerCase().replace(/^[a-z]+:/, '') === bare);
                    if (cands.length === 1) prev = map.get(cands[0]);
                }
            }
            const bars = prev && prev.bars;
            const lastBar = (bars && bars.length) ? bars[bars.length - 1] : null;
            return (lastBar && lastBar.time) ? Math.round(lastBar.time / 1000) : null;
        } catch (e) { return null; }
    }

    // 统一 resetData 入口: 防抖 + 指数退避(避免冷缓存/数据源停滞下每 8s reset 风暴) +
    // resetCache()(TV 文档契约, 清内部 datafeed 缓存/合并基准, 与重载按钮一致) + 退避记账。
    // 返回是否真的执行了 reset(false=被防抖跳过或无图表, 调用方据此决定是否继续走增量)。
    _doReset(resKey, reason, viewLatestSec) {
        try {
            if (typeof window === 'undefined' || !window.SseGap) return false;
            const nowSec = Math.floor(Date.now() / 1000);
            // 防抖+退避决策走纯函数(M-2): 被拒不消耗退避, 放行才基于"画布较上次 reset 是否前进"更新退避。
            const gate = window.SseGap.computeResetGate(this._resetState[resKey], nowSec, viewLatestSec, 30);
            if (!gate.allow) return false;
            const ch = (this.widget && typeof this.widget.activeChart === 'function') ? this.widget.activeChart() : null;
            // 无图表: 不持久化 gate.state(等同未 reset, 下次重新判), 避免空记账污染退避。
            if (!ch || typeof ch.resetData !== 'function') return false;
            // 契约: resetData 前先 resetCache(TV 文档要求)。容错: 老版无 resetCache 时降级裸 resetData。
            try { if (this.widget && typeof this.widget.resetCache === 'function') this.widget.resetCache(); } catch (e) { /* ignore */ }
            // H1(阶段E): 置一次性 force_refresh 标志 → 下次 firstDataRequest(即将由 resetData 触发)绕过后端
            // 缓存重算,补齐断档(datafeed getBars 读此标志注入 force_refresh=1,用后即清)。同步置位,不改时序。
            try {
                const _hp = this.udf_datafeed && this.udf_datafeed._historyProvider;
                if (_hp) _hp._forceRefreshOnce = true;
            } catch (e) { /* ignore */ }
            ch.resetData();
            this._resetState[resKey] = gate.state;  // 仅真执行 reset 后落记账
            clog(`[SSE] resetData 全量补齐 reason=${reason} resKey=${resKey} backoff=${gate.state.backoffLevel}`);
            return true;
        } catch (e) { clog('[SSE] _doReset 异常', e); return false; }
    }

    _openSseStream() {
        // feature flag 关闭 → 完全不建连, 退回纯轮询(现状)。
        if (typeof window !== 'undefined' && window.__CHANLUN_SSE_ENABLED === false) return;
        if (typeof EventSource === 'undefined' || !this.widget) return;
        this._closeSseStream();
        let si = null;
        try { si = this.widget.symbolInterval(); } catch (e) { si = null; }
        if (!si || !si.symbol || !si.interval) return;
        // 与 getChartData 读 bars_result 的 key 同源(widget.symbolInterval())，
        // 保证 SSE 更新写入的 res_key 与缠论重绘读取的一致。
        const symbol = si.symbol;
        const resolution = si.interval;
        const url = `/tv/stream?symbol=${encodeURIComponent(symbol)}&resolution=${encodeURIComponent(resolution)}`;
        let es = null;
        try { es = new EventSource(url); } catch (e) { console.warn('[SSE] 创建失败', e); return; }
        this._sse = es;
        this._sseGotData = false;
        es.addEventListener('chanlun', (ev) => {
            // 首个数据帧到达 → SSE 通道确实通(未被中间层缓冲) → 取消 fallback 快轮询。
            if (!this._sseGotData) {
                this._sseGotData = true;
                this._clearSseFallback();
                clog('[SSE] 数据帧到达, 通道健康, 取消 fallback 快轮询');
            }
            try {
                // 断流时长门槛(M-3): es.onerror 记录的断开若 ≥30s 视为真断档 → 置位重连补齐;
                // 弱网瞬断秒回则忽略, 避免频繁无条件 resetData 闪烁。navigator 'online' 另路直接置位。
                if (this._disconnectedSinceMs != null) {
                    const _downMs = Date.now() - this._disconnectedSinceMs;
                    this._disconnectedSinceMs = null;
                    if (_downMs >= 30000) this._needResetOnNextData = true;
                }
                const data = JSON.parse(ev.data);
                const resKey = String(symbol).toLowerCase() + String(resolution).toLowerCase();
                const hp = this.udf_datafeed && this.udf_datafeed._historyProvider;

                // reconnect 后首帧: 无条件全量补齐, 不去猜中间缺几根。只要发生过真断档重连(online 事件
                // 或 es.onerror 断流≥30s 置位 _needResetOnNextData), 这一帧就整段重拉——治"30s 轮询抢在
                // SSE 首帧前推进 bars_result、污染断档参照系致漏判"的竞态, 以及服务端重启后的整段补齐。
                if (this._needResetOnNextData) {
                    this._needResetOnNextData = false;
                    if (this._doReset(resKey, 'reconnect', this._getViewLatestSec(resKey, resolution))) return;
                }

                // 常规断档检测(SSE 持续连通但数据跳变/长时间无更新恢复): data.t 完整快照里
                // (画布末根, SSE 末根) 之间真有缺根才补。_doReset 内含防抖+退避+resetCache。
                try {
                    if (hp && hp.bars_result && typeof window !== 'undefined' && window.SseGap) {
                        const _tvLatestSec = this._getViewLatestSec(resKey, resolution);
                        if (window.SseGap.shouldResetForGap(data.t, _tvLatestSec, resolution)) {
                            if (this._doReset(resKey, 'gap-detect', _tvLatestSec)) return;
                        }
                    }
                } catch (_gapErr) { /* gap 检测失败不阻断正常推送 */ }

                // 复用 getBars 同一合并逻辑(applyChanlunUpdate=_processHistoryResponse):
                // 更新 bars_result + 派发 chanlun-bars-ready → draw_chanlun 重绘缠论。
                if (hp && typeof hp.applyChanlunUpdate === 'function') {
                    hp.applyChanlunUpdate(data, { symbol, resolution });
                }
                // K线也随 SSE 实时刷新：把推送里的最新 bar 喂给 TV(绕过轮询/节流/bars<2 抛错)。
                if (this.udf_datafeed && typeof this.udf_datafeed.feedRealtimeBar === 'function') {
                    this.udf_datafeed.feedRealtimeBar(resKey, data);
                }
            } catch (e) { console.warn('[SSE] 处理推送失败', e); }
        });
        es.onerror = () => {
            // 记录首次断开时刻(不重复刷新)。下一帧到达时按"断开时长"决定是否需要全量补齐(M-3):
            // 弱网瞬断秒回(<阈值)忽略, 避免频繁无条件 resetData 闪烁; 真断档(≥阈值)才置位 reconnect-reset。
            // navigator 'online'(可靠的真断网恢复信号)另路直接置位, 不受此门槛限制。
            if (this._disconnectedSinceMs == null) this._disconnectedSinceMs = Date.now();
            clog('[SSE] 连接错误, 浏览器将自动重连');
        };
        // SSE 健康哨兵：连上后 12s 仍未收到任何数据帧 → 判定被中间层 vhost 缓冲 →
        // 启动 6s fallback 快轮询兜底实时；后续收到帧会自动取消(见 addEventListener)。
        this._sseHealthTimer = setTimeout(() => {
            if (!this._sseGotData) {
                clog('[SSE] 12s 未收到数据帧(疑被中间层缓冲), 启动 6s fallback 快轮询');
                this._startSseFallback();
            }
        }, 12000);
        clog(`[SSE] opened symbol=${symbol} res=${resolution}`);
    }

    dispose() {
        this._clearAllStrictScopes('dispose');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        if (this._strictReconcileEpoch) {
            try { this._strictReconcileEpoch.dispose(); } catch (e) { /* already disposed */ }
            this._strictReconcileEpoch = null;
        }
        this._closeSseStream();
        // 工具栏注入重试定时器:正常 ≤4.2s 自停,但实例若在窗口内被 dispose 需显式清,
        // 避免定时器多跑几拍对已 dispose 实例操作(与 _sweepOrphanTimer 等清理对齐,审查 L-3)。
        if (this._lvlbtnTimer) { clearInterval(this._lvlbtnTimer); this._lvlbtnTimer = null; }
        if (this._watchdogTimer) { clearInterval(this._watchdogTimer); this._watchdogTimer = null; }
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
            this._visibilityHandler = null;
        }
        if (this._onlineHandler) {
            if (typeof window !== 'undefined') window.removeEventListener('online', this._onlineHandler);
            this._onlineHandler = null;
        }
        if (this._barReadyHandler) {
            window.removeEventListener('chanlun-bars-ready', this._barReadyHandler);
            this._barReadyHandler = null;
        }
        const registry = getTVRegistry();
        registry.chartManagers.delete(this.instanceId);
        registry.datafeeds.delete(this.instanceId);
        registry.widgets.delete(this.instanceId);
        if (registry.activeManagerId === this.instanceId) {
            registry.activeManagerId = null;
        }
    }
}

var Charts = (function () {
    return {
        show_tv_chart: function (id) {
            const chartManager = new ChartManager(id).init();
            // 调试钩子:把每个 ChartManager 实例存到 window.__cm[id],
            // 便于 Playwright/DevTools 直接 inspect cl_show_config / obj_charts。
            // 生产无害——挂在 window 不影响业务逻辑;若担心暴露面,可改为
            // 仅当 ``window.__chanlunDebug=true`` 时挂。
            try {
                if (!window.__cm) window.__cm = {};
                window.__cm[id] = chartManager;
            } catch (e) { /* ignore */ }
            return chartManager.widget;
        },
    };
})();
