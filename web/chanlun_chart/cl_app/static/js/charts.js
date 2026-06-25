// 缠论显示配置（cl_show_config）与独立周期画线开关（cl_independent_drawings）
// 按 ChartManager 实例独立维护，key 形如 cl_show_config_<chartId>；
// 旧全局 key 仅在新 key 不存在时作为默认值迁移，老用户设置不丢失。

// 默认的缠论显示项配置
const CL_SHOW_DEFAULT = {
    fx: true, bi: true, xd: true, bc: true, mmd: true,
    // 中枢按级别独立 toggle(笔中枢 zs_bi / 线段中枢=L0 zs_xd),平级独立控制;
    // 扩展高级别 zs_L1/zs_L2/zs_L3 在菜单按 recursive_levels 实际级别动态生成、默认开。
    zs_bi: true, zs_xd: true,
    // 买卖点/背驰按级别独立 toggle(笔层数量远多于段层、用户常需只看段层):
    mmd_bi: true, mmd_xd: true, bc_bi: true, bc_xd: true,
};

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

// 旧配置只有单个 ``zs`` 总开关;现已拆成 ``zs_bi``(笔中枢)/``zs_xd``(线段中枢)
// 两个独立开关。迁移:旧 ``zs`` 的值作为两者初值,保留用户「曾把中枢关掉」的意图。
function _migrateZsToggle(merged, parsed) {
    if (parsed && parsed.zs !== undefined) {
        if (parsed.zs_bi === undefined) merged.zs_bi = parsed.zs;
        if (parsed.zs_xd === undefined) merged.zs_xd = parsed.zs;
    }
    return merged;
}

function loadClShowConfig(chartId) {
    try {
        const raw = localStorage.getItem('cl_show_config_' + chartId);
        if (raw) {
            const parsed = JSON.parse(raw);
            return _migrateZsToggle(Object.assign({}, CL_SHOW_DEFAULT, parsed), parsed);
        }
        // 兼容旧版全局 key 作为首次默认值，不写回旧 key
        const legacy = localStorage.getItem('cl_show_config');
        if (legacy) {
            const parsed = JSON.parse(legacy);
            return _migrateZsToggle(Object.assign({}, CL_SHOW_DEFAULT, parsed), parsed);
        }
    } catch (e) {
        console.warn('[CHARTS] loadClShowConfig parse failed', e);
    }
    return Object.assign({}, CL_SHOW_DEFAULT);
}

function saveClShowConfig(chartId, cfg) {
    try {
        localStorage.setItem('cl_show_config_' + chartId, JSON.stringify(cfg));
    } catch (e) {
        console.warn('[CHARTS] saveClShowConfig failed', e);
    }
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
    CHART_TYPES: [
        "fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds",
        // 拆分版买卖点/背驰(笔层 vs 段层),独立 reconcile
        "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs",
        // 买卖点文字标签(与 icon 箭头分离的第二个 shape,各自独立 reconcile)
        "mmd_labels", "bi_mmd_labels", "xd_mmd_labels",
        // 新核心递归层级中枢:recursive_levels 各级 zss 扁平化为单容器
        "recursive_zss",
        // 新核心递归层级走势类型「线段」线条:recursive_levels 各级 zslx_lines 扁平化为单容器;
        // 各级按绝对级别取链色(与该级中枢同色、形状区分线/框),各级独立 toggle(xd_L0/xd_L1…)。
        "recursive_zslx_lines",
        // 新核心各级(5m/30m…)买卖点/背驰:recursive_levels[].mmds/bcs(箭头/标签/背驰各独立容器)。
        // ⚠必须注册:reconcile 取 obj_charts[symbolKey][type],未注册→undefined→container.length 抛错→
        // 5m/30m 买卖点/背驰永不渲染(中枢 recursive_zss 已注册故能显示,买卖点不显示=此遗漏所致)。
        "recursive_mmds", "recursive_mmd_labels", "recursive_bcs",
        // 多周期中枢叠加(低周期图叠加的高周期线段中枢)
        "higher_zss",
    ],
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
        const color = options.color || CHART_CONFIG.COLORS.BI;
        const linewidth = options.linewidth || 1;
        const transparency = 95;
        return this.createShape(chart, zs.points, { shape: "rectangle", overrides: { linestyle: parseInt(zs.linestyle) || 0, linewidth, linecolor: color, backgroundColor: color, transparency, color, "trendline.linecolor": color, fillBackground: true, filled: true, ...options.overrides }, ...options });
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
    // 偏移点:优先用 ATR 基准(offsetBase × atrRatio);基准不可用时回退价格百分比。
    mmdOffsetPoint(mmd, atrRatio, priceRatioFallback, offsetBase = 0, fromPoint = null) {
        const src = fromPoint || mmd.points || {};
        const base = mmd.points || {};
        if (typeof src.price !== "number" || typeof base.price !== "number") return src;
        const off = offsetBase > 0
            ? offsetBase * atrRatio
            : Math.abs(base.price) * priceRatioFallback;
        return { ...src, price: mmd.text.includes("B") ? src.price - off : src.price + off };
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
        const isBuy = mmd.text.toLowerCase().includes("b");   // buy/1B/3buy… 含 b;sell/S 不含
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
        const isBuy = mmd.text.toLowerCase().includes("b");
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
        // 每个图表面板独立维护缠论显示配置与独立周期画线开关
        this.cl_show_config = loadClShowConfig(this.id);
        this.cl_independent_drawings = loadClIndependentDrawings(this.id);
        this._initialLoadDone = false; // 首次数据就绪前屏蔽 visibleRangeChange 重绘
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
        this._barReadyHandler = null;
        this._sse = null;  // SSE 实时推送连接(每图表一个; flag 关/不支持时为 null)
        this._visibilityHandler = null;  // 页面可见性兜底监听句柄
        // reconcile 失败自动重试状态：count 累计失败次数，timer 已排队的句柄（防重复）
        this._reconcileRetry = { count: 0, timer: null };
        // reconcile 创建过的全部 entity id 集合，用于 sweep 时识别孤儿 shape。
        // safeRemove 静默失败或同一 key 两次 create 时 container 与 TV 会脱钩，
        // 孤儿 shape 残留为长斜线；sweep 强制 removeEntity 清除。
        // 用户手画的 shape 从未进入此 set，不会被误删。
        this._reconcileOwnedIds = new Set();
        // reconcile 签名守卫：{ 'symbolKey__type': signature }
        // signature = `size|from|sortedKeys.slice(0,256)`
        // 同 (symbolKey, type) 下 newKeys+from 未变时直接 return，
        // 跳过 O(N+M) 遍历，防止 zoom/pan 触发的 TV 冗余事件造成无效开销。
        this._reconcileGuard = {};
        // full rebuild 后 500ms 补一次 verify-rebuild，让 TV 在稳定布局上重新落位 shape
        this._verifyRebuildTimer = null;
        this._verifyingUntil = null;  // performance.now() 时间戳，在此之前的 reconcile 属于 verify 内部
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
        const wasInitialLoad = !this._initialLoadDone;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleBarsReadyEvent ✓ symbol=${detail.symbol} res=${detail.resolution} bars=${detail.bars || '?'} fxs=${detail.fxs || '?'} bis=${detail.bis || '?'} xds=${detail.xds || '?'} wasInitialLoad=${wasInitialLoad}`);
        this._initialLoadDone = true;
        // 首次 bars 到达直接重绘，跳过 debounce 缩短感知延迟；后续事件仍走防抖路径
        if (wasInitialLoad) {
            this.draw_chanlun();
        } else {
            this.debouncedDrawChanlun();
        }
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
                return new Promise((resolve) => {
                    if (self.shouldSuppressDrawingSave()) {
                        clog("[DEBUG-CHARTS] Skip saveLineToolsAndGroups due to active drawing mutation");
                        return resolve();
                    }
                    const rawResolution = self.chart ? self.chart.resolution() : Utils.get_local_data(Utils.get_market() + "_interval_" + self.id);
                    const resolution = self.cl_independent_drawings ? rawResolution : 'all';
                    const symbol = self.chart ? self.chart.symbol() : Utils.get_market() + ":" + Utils.get_code();
                    const cacheKey = self.getDrawingsCacheKey(symbol, rawResolution);

                    let processedState = { ...state };
                    if (state.sources) {
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

                    clog("[DEBUG-CHARTS] Saving drawings for", { symbol, resolution, sourcesCount: Object.keys(processedState.sources || {}).length, rawSources: state.sources, reason: options.reason });

                    fetch("/tv/1.1/drawings?client=" + client_id + "&user=" + user_id + "&chart=" + chartId + "&layout=" + layoutId + "&symbol=" + symbol + "&resolution=" + resolution, {
                        method: "POST",
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ state: processedState })
                    }).then(res => res.json()).then(res => {
                        clog("[DEBUG-CHARTS] saveLineToolsAndGroups response", res);
                        if (state && state.sources) {
                            self.setDrawingsCache(cacheKey, state);
                        }
                        resolve();
                    }).catch(err => {
                        console.error("[DEBUG-CHARTS] saveLineToolsAndGroups error", err);
                        resolve();
                    });
                });
            },
            loadLineToolsAndGroups: function (layoutId, chartId, requestType, requestContext = {}) {
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
            interval: Utils.get_local_data(Utils.get_market() + "_interval_" + this.id),
            datafeed: this.udf_datafeed,
            library_path: "static/charting_library/",
            theme: Utils.get_local_data("theme"),
            numeric_formatting: { decimal_sign: "." },
            time_frames: [], timezone: getMarketTimezone(Utils.get_market()), locale: "zh",
            symbol_search_request_delay: 100, auto_save_delay: 5, study_count_limit: 100,
            disabled_features: ["go_to_date"],
            enabled_features: ["study_templates", "seconds_resolution", "saveload_separate_drawings_storage"],
            saved_data_meta_info: { uid: 1, name: "default", description: "default" },
            save_load_adapter: save_load_adapter,
            client_id: "chanlun_pro_" + Utils.get_market() + "_" + this.id,
            user_id: "999", load_last_chart: true,
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

                // 当前周期 → 各项级别映射:
                //   笔中枢 = 最低级别中枢;
                //   线段 = 最低级别走势类型;
                //   线段中枢 / 走势类型 = 当前周期中枢 / 当前周期走势类型;
                //   当前周期走势类型再作为高一级中枢的构件。
                let _curInterval = "?";
                try { _curInterval = self.widget.symbolInterval().interval; } catch (e) {}
                // FREQ_CHAIN 已提升为模块级常量(菜单与左侧级别快捷开关浮条共用)
                const _chain = FREQ_CHAIN[_curInterval] || [_curInterval, "高一级", "高二级", "高三级"];
                const _lbl = (i) => _chain[i] || `L+${i}`;

                // 中枢级别列表(P8):由 recursive_levels 实际层级驱动。
                // L0 = 本周期线段中枢(保留 zs_xd 键,兼容旧用户配置);
                // L1/L2/L3 = 扩展高级别中枢,键 zs_L1/zs_L2/zs_L3,标签从 FREQ_CHAIN 取。
                // 菜单作用域无 barsResult；读 drawChartElements 缓存到实例的最大级别(默认 0=仅 L0,数据未到时)。
                const _recMaxLevel = self._recMaxLevel || 0;
                const _zsLevels = [{ label: _chain[0] + '级别', key: 'zs_xd' }];
                for (let i = 1; i <= _recMaxLevel; i++) {
                    _zsLevels.push({ label: (_chain[i] || ('L' + i)) + '级别', key: 'zs_L' + i });
                }
                // 买卖点/背驰也按周期级别(与中枢平行):1m级别=本周期线段(mmd_xd/bc_xd),
                // 5m/30m级别=recursive_levels L1/L2(mmd_L1.../bc_L1...);笔层单列。
                const _mmdLevels = [{ label: _chain[0] + '级别', key: 'mmd_xd' }];
                const _bcLevels = [{ label: _chain[0] + '级别', key: 'bc_xd' }];
                for (let i = 1; i <= _recMaxLevel; i++) {
                    const _lab = (_chain[i] || ('L' + i)) + '级别';
                    _mmdLevels.push({ label: _lab, key: 'mmd_L' + i });
                    _bcLevels.push({ label: _lab, key: 'bc_L' + i });
                }
                // 各级走势类型「线段」线条(与中枢平行,key=xd_L0/xd_L1…):在低周期图上叠加
                // 显示 5m/30m… 各级线段。**默认关**(高级别线条会增加视觉密度,按需开启);
                // 颜色与该级中枢同(同绝对级别),形状用线条区分。
                const _zslxLevels = [];
                for (let i = 0; i <= _recMaxLevel; i++) {
                    _zslxLevels.push({ label: (_chain[i] || ('L' + i)) + '级别', key: 'xd_L' + i });
                }

                // 重组后的菜单:按「功能分组」组织 + 顶部级别映射默认折叠 +
                // 底部「全选/全清」一键操作。比起原始扁平 14 项更易扫读,
                // 减少新用户「原文化新增」等晦涩术语的认知负担。
                const _cbRow = (k, label, indent) => `<label style="display:block; cursor:pointer; ${indent ? 'padding-left:14px; font-size:12px;' : ''}"><input type="checkbox" id="${cbId(k)}" ${cfg[k] ? 'checked' : ''} style="margin-right:6px; vertical-align:middle;">${label}</label>`;
                const _grpTitle = (t) => `<div style="font-size:11px; color:#4a90e2; padding:5px 0 1px; font-weight:bold;">${t}</div>`;
                // 级别色块:让「按周期级别」的选项自带颜色图例,直观对应图上各级线段/中枢的网站递归色。
                const _sw = (c) => `<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${c};margin-right:3px;vertical-align:middle;border:1px solid rgba(0,0,0,0.25);"></span>`;

                let html = `
                    <div id="${menuId}" style="position: absolute; z-index: 99999999; background: #fff; border: 1px solid #ccc; box-shadow: 0 2px 10px rgba(0,0,0,0.2); border-radius: 4px; padding: 10px; line-height: 22px; font-size: 13px; color: #333; min-width: 220px;">
                        <div id="${menuId}_lvl_toggle" style="cursor:pointer; font-size:11px; color:#888; padding:1px 0; user-select:none;">
                            <span id="${menuId}_lvl_arrow">▸</span> 级别映射 (当前周期: <b>${_curInterval}</b>)
                        </div>
                        <div id="${menuId}_lvl_detail" style="display:none; font-size:11px; color:#888; line-height:1.5em; padding:2px 0 4px 14px; border-left:2px solid #eee; margin:2px 0 4px 4px;">
                            笔中枢 → 最低级别中枢<br>
                            线段 → 最低级别走势类型<br>
                            线段中枢 → <b>${_lbl(0)}</b>中枢
                        </div>

                        ${_grpTitle('基础')}
                        <div style="display:flex; gap:14px; font-size:12px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('fx')}" ${cfg.fx ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">分型</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bi')}" ${cfg.bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${_sw(getDynamicColor(_curInterval, 'bis'))}笔</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('xd')}" ${cfg.xd ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${_sw(getDynamicColor(_curInterval, 'xds'))}线段</label>
                        </div>

                        ${_grpTitle('中枢 (按周期级别)')}
                        <label style="display:block; cursor:pointer; font-size:12px;"><input type="checkbox" id="${cbId('zs_all')}" ${cfg.zs_all !== false ? 'checked' : ''} style="margin-right:6px; vertical-align:middle;">总开关</label>
                        <div style="padding-left:14px; display:flex; gap:12px; font-size:12px; flex-wrap:wrap;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('zs_bi')}" ${(cfg.zs_all !== false && cfg.zs_bi) ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${_sw(getDynamicColor(_curInterval, 'bi_zss'))}笔中枢</label>
                            ${_zsLevels.map((L, ai) => `<label style="cursor:pointer;"><input type="checkbox" id="${cbId(L.key)}" ${(cfg.zs_all !== false && cfg[L.key] !== false) ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${_sw(getRecursiveLevelColor(_curInterval, ai))}${L.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('走势类型线段 (按周期级别)')}
                        ${_cbRow('zslx_all', '总开关', false)}
                        <div style="padding-left:14px; display:flex; gap:12px; font-size:12px; flex-wrap:wrap;">
                            ${_zslxLevels.map((L, ai) => `<label style="cursor:pointer;"><input type="checkbox" id="${cbId(L.key)}" ${(cfg.zslx_all === true && cfg[L.key] !== false) ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${_sw(getRecursiveLevelColor(_curInterval, ai))}${L.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('买卖点 (按周期级别)')}
                        ${_cbRow('mmd', '总开关', false)}
                        <div style="padding-left:14px; font-size:12px; display:flex; gap:12px; flex-wrap:wrap;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('mmd_bi')}" ${cfg.mmd_bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔层</label>
                            ${_mmdLevels.map((L) => `<label style="cursor:pointer;"><input type="checkbox" id="${cbId(L.key)}" ${cfg[L.key] !== false ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${L.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('背驰 (按周期级别)')}
                        ${_cbRow('bc', '总开关', false)}
                        <div style="padding-left:14px; font-size:12px; display:flex; gap:12px; flex-wrap:wrap;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bc_bi')}" ${cfg.bc_bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔背驰</label>
                            ${_bcLevels.map((L) => `<label style="cursor:pointer;"><input type="checkbox" id="${cbId(L.key)}" ${cfg[L.key] !== false ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">${L.label}</label>`).join('')}
                        </div>

                        <hr style="margin:6px 0 4px;">
                        <label style="display:block; cursor:pointer; font-size:12px;"><input type="checkbox" id="${indCbId}" ${self.cl_independent_drawings ? 'checked' : ''} style="margin-right:6px; vertical-align:middle;">独立周期画线</label>
                        <div style="display:flex; gap:6px; padding-top:6px;">
                            <button id="${menuId}_all" type="button" style="flex:1; font-size:11px; padding:3px 4px; cursor:pointer; border:1px solid #ccc; background:#f7f7f7; border-radius:3px;">全选</button>
                            <button id="${menuId}_none" type="button" style="flex:1; font-size:11px; padding:3px 4px; cursor:pointer; border:1px solid #ccc; background:#f7f7f7; border-radius:3px;">全清</button>
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
                $('#' + menuId + '_all').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').each(function () {
                        if (!this.checked) { this.checked = true; $(this).trigger('change'); }
                    });
                });
                $('#' + menuId + '_none').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').each(function () {
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
                    'fx', 'bi', 'xd', 'bc', 'mmd',
                    // 中枢总开关 zs_all(默认开) / 走势类型线段总开关 zslx_all(默认关);新key避开遗留 zs/xd_zslx
                    'zs_all', 'zslx_all',
                    // 买卖点/背驰 笔段独立开关 + 笔中枢(观察)
                    'zs_bi', 'mmd_bi', 'mmd_xd', 'bc_bi', 'bc_xd',
                    // 中枢按周期级别:zs_xd(L0本周期线段中枢) + zs_L1/zs_L2/zs_L3(P8扩展高级别),随数据动态
                    ..._zsLevels.map((L) => L.key),
                    // 各级走势类型线段:xd_L0/xd_L1/…(默认关),同样需 handler 才能保存配置+触发重绘
                    ..._zslxLevels.map((L) => L.key),
                    // Recursive higher-level signal toggles are generated dynamically.
                    // Without these handlers, mmd_L*/bc_L* checkboxes render but do
                    // not save config or trigger a redraw.
                    ..._mmdLevels.slice(1).map((L) => L.key),
                    ..._bcLevels.slice(1).map((L) => L.key),
                ];
                keys.forEach(k => {
                    $('#' + cbId(k)).change(function () {
                        const checked = $(this).is(':checked');
                        self.cl_show_config[k] = checked;
                        saveClShowConfig(self.id, self.cl_show_config);
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
                $.post({ type: "POST", url: "/tv/del_marks", dataType: "json", data: { symbol: symbol.symbol }, success: function (res) { if (res.status == "ok") { global_widget.activeChart().clearMarks(); layer.msg("删除标记成功"); } } });
            });

        });
        this.widget.onChartReady(() => {
            // widget 就绪后移除首屏骨架占位
            var sk = document.getElementById('tv_charts_skeleton');
            if (sk) sk.remove();
            this.chart = this.widget.activeChart();
            if (!this.chart) return;
            this.chart.applyOverrides({ "mainSeriesProperties.candleStyle.upColor": "#ef5350", "mainSeriesProperties.candleStyle.downColor": "#26a69a" });
            const registry = getTVRegistry();
            registry.widgets.set(this.instanceId, this.widget);
            registry.activeManagerId = this.instanceId;
            window.tvWidget = this.widget;
            this.widget._chanlunManagerId = this.instanceId;
            this.udf_datafeed._chanlunManagerId = this.instanceId;

            this.chart.onSymbolChanged().subscribe(null, (s) => this.handleSymbolChange(s));
            this.chart.onIntervalChanged().subscribe(null, (i) => this.handleIntervalChange(i));
            this.chart.onDataLoaded().subscribe(null, () => this.handleDataReady(), true);
            this.chart.dataReady(() => this.handleDataReady());
            this.widget.subscribe("onTick", () => this.handleTick());
            this.chart.onVisibleRangeChanged().subscribe(null, () => this.handleVisibleRangeChange());

            this.reloadDrawingsForCurrentContext('initial-load');
            this._openSseStream();
            this.updateDrawPalette();   // 画图调色板:优先原生注入 TV 工具栏,失败回退浮层
            // TV 左侧工具栏异步渲染:重试注入(成功即停,最多 ~4.2s)
            if (this._lvlbtnTimer) clearInterval(this._lvlbtnTimer);
            this._lvlbtnTries = 0;
            this._lvlbtnTimer = setInterval(() => {
                this._lvlbtnTries++;
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
        const chain = FREQ_CHAIN[interval] || [interval, '高一级', '高二级', '高三级'];
        const maxLevel = Math.max(this._recMaxLevel || 0, Math.min(chain.length - 1, 3));
        const items = [];
        for (let k = 0; k <= maxLevel; k++) {
            items.push({ label: (chain[k] || ('L' + k)), color: getRecursiveLevelColor(interval, k) });
        }
        return { items, sig: interval + '|' + maxLevel };
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
        if (Utils.get_market() !== market) { Utils.set_local_data("market", market); location.reload(); return; }
        Utils.set_local_data("market", market); Utils.set_local_data(`${market}_code`, code);
        this._initialLoadDone = false;
        this._latestAppliedBarTime = null;
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('symbol-change');
        this._openSseStream();
        if (typeof ZiXuan.render_zixuan_opts === "function") ZiXuan.render_zixuan_opts();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 同市场切标的:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }
    handleIntervalChange(interval) {
        if (!interval) return;
        const market = Utils.get_market(); if (!market) return;

        this._initialLoadDone = false;
        this._drawRetryCount = 0;
        this._latestAppliedBarTime = null;
        const currentSeq = ++this._intervalSwitchSeq;
        this._intervalVersion++;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleIntervalChange → ${interval} (seq=${currentSeq}, ver=${this._intervalVersion}) [_initialLoadDone reset to false]`);
        Utils.set_local_data(`${market}_interval_${this.id}`, interval);
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('interval-change');
        this._openSseStream();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 切周期:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }

    handleDataReady() {
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleDataReady fired [_initialLoadDone=true]`);
        this._initialLoadDone = true;
        this._maybeWidenDefaultView();
        this.debouncedDrawChanlun();
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
            this._viewSetFor = key;
            // 各周期默认视窗跨度(日历天);跨度按天数,外汇 24h 连续→根数多,A股有夜盘缺口→根数少,均显示充足缠论。
            // 白名单只列分钟~月线;未列出的周期(秒线10S/30S、季线3M、年线12M等)直接跳过不拉宽——
            // 否则 6 天 fallback 对秒线会触发拉数万根、对季/年线又过窄;且这些周期 TV 默认视窗本就够宽。
            const SPAN_DAYS = { '1': 2, '2': 3, '3': 3, '5': 6, '10': 8, '15': 12, '30': 45, '60': 90, '120': 120, '180': 150, '240': 200, '1D': 400, '2D': 700, '1W': 1825, '1M': 5475 };
            const days = SPAN_DAYS[si.interval];
            if (!days) return;   // 周期不在白名单(秒/季/年等)→保持 TV 默认视窗
            const span = days * 86400;
            if ((vr.to - vr.from) >= span * 0.7) return;   // 当前已够宽(如A股默认)→不动
            setTimeout(() => {
                try {
                    // 竞态防护:延时期间用户若已切到别的标的/周期,放弃,避免把视窗设成上一周期的范围。
                    const si2 = this.widget && this.widget.symbolInterval ? this.widget.symbolInterval() : null;
                    if (!si2 || (si2.symbol + '|' + si2.interval) !== key) return;
                    const v = this.chart.getVisibleRange();
                    if (v && v.to) this.chart.setVisibleRange({ from: v.to - span, to: v.to });
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
        return { symbolKey, barsResult, from };
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

        // 签名守卫：newKeys+from 都未变时直接 return，省掉 O(N+M) 容器遍历
        // 必须在 newKeys 计算完成后、容器遍历之前执行
        const sortedKeys = [...newKeys].sort();
        // 签名纳入「未完成(pending)项的 key 集合」:makeKey 不含 linestyle,纯 pending→done 翻转
        // (端点不变)不改 keys → 原签名不变 → 守卫 skip 整个 reconcile → 笔已变完成笔但页面仍显
        // 未完成(用户报)。把 unfinished key 集折进签名,翻转即改签名 → 不再被 skip。
        const unfinishedSig = itemsToProcess
            .filter(p => p.item.linestyle == '1' || p.item.linestyle == 1)
            .map(p => p.key).sort().join(',').slice(0, 256);
        const signature = `${newKeys.size}|${from}|${sortedKeys.join(',').slice(0, 256)}|U${unfinishedSig}`;
        const guardKey = `${symbolKey}__${type}`;
        if (this._reconcileGuard[guardKey] === signature) {
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

        // 同步路径完成后记录签名；异步 create 仍在 pending 无妨，resolve 后 push 到
        // container 与签名目标状态一致，下次 guard 时容器已正确。
        // 但若本轮有 sync create→null(chart 未 ready/点超已加载范围),**不落守卫**——否则
        // 签名未变时 _scheduleReconcileRetry 触发的重画会被上面 W1 guard 误 skip,失败的
        // shape 永不重建(表现为「中枢框初次不显、点中枢重勾后才出」)。async→null 在其回调删守卫同理。
        if (!createFailed) {
            this._reconcileGuard[guardKey] = signature;
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
        // 缓存递归层级最大 level，供「缠论显示设置」菜单动态生成级别开关(菜单 click 作用域无 barsResult)
        this._recMaxLevel = Math.max(0, ...((barsResult.recursive_levels) || []).map(lv => (lv && lv.level != null) ? lv.level : 0));
        this.initChartContainer(symbolKey);

        clog("[DataVerify][Charts] drawChartElements interval=" + currentInterval, {
            fxs: barsResult.fxs?.length || 0,
            bis: barsResult.bis?.length || 0,
            xds: barsResult.xds?.length || 0,
            bi_zss: barsResult.bi_zss?.length || 0,
            xd_zss: barsResult.xd_zss?.length || 0,
            bcs: barsResult.bcs?.length || 0,
            mmds: barsResult.mmds?.length || 0,
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
        const wrapZs = (lvlColor, linewidth) => (item) => {
            return ChartUtils.createZhongshuShape(this.chart, item, {
                color: lvlColor,
                linewidth,
            });
        };
        // 中枢矩形不应走 getUniqueRenderList(那是给 bi/xd 末段 pending 闪烁
        // 去重用的:它把所有 linestyle=1 的项压成 1 个,但 zss 阵列中**多个**
        // pending 中枢正常并存,被压成 1 个会让大部分中枢消失。useUnique=false。
        // 笔中枢 / 线段中枢现按独立开关(zs_bi / zs_xd)分别控制显隐。
        const _zsOn = cfg.zs_all !== false;   // 中枢总开关(默认开,新key避开遗留 zs):门控 笔中枢/线段中枢/各级递归中枢
        this.reconcile('bi_zss', (_zsOn && cfg.zs_bi) ? barsResult.bi_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "bi_zss"), 1)(item), 'bi_zs'), false);
        this.reconcile('xd_zss', (_zsOn && cfg.zs_xd) ? barsResult.xd_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "xd_zss"), 2)(item), 'xd_zs'), false);
        // 新核心递归层级中枢(recursive_levels, P8):后端按级别给 [{level, zss, zslxs, ...}]。
        // L0=本周期线段中枢(开关 zs_xd), L1/L2/L3=扩展高级别中枢(开关 zs_L1/zs_L2/zs_L3)。
        // 各级 zss 扁平化(附 _level)后用单 reconcile;按级别选色/线宽。
        const recZss = [];
        for (const lvObj of (barsResult.recursive_levels || [])) {
            if (!lvObj || !Array.isArray(lvObj.zss)) continue;
            const lvl = lvObj.level || 0;
            const toggleKey = lvl === 0 ? 'zs_xd' : 'zs_L' + lvl;
            if (cfg.zs_all === false || cfg[toggleKey] === false) continue;   // 中枢总开关 + 各级
            for (const zs of lvObj.zss) recZss.push({ ...zs, _level: lvl });
        }
        // includeOverlaps=true + 左沿 clamp:高级别(5m/30m)中枢框跨度数周~数月,其左沿
        // 几乎恒在可视窗之前——曾用 false(只渲染左沿入窗的框)导致大级别中枢**永不显示**
        // (2026-06-11 浏览器实测 recursive_zss 容器恒 0,「图上看不到 30m 中枢」前端真凶)。
        // 历史教训(为何当初改 false):TV createMultipointShape 把**未加载范围外**的角点
        // snap 到边缘 → 框塌成方块/错位。两全:渲染前把窗外左沿 clamp 到 from(已加载窗口
        // 左缘,必已加载)→ 框显示右段、角点不 snap 不错位;滚动使真实左沿入窗后自动恢复
        // (from 变化 → 签名守卫失效 → 重建,makeKey 基于原始 item、key 稳定)。
        const clampHeadToFrom = (item) => {
            if (!Array.isArray(item.points)) return item;
            let needClamp = false;
            const pts = item.points.map(p => {
                if (p && p.time < from) { needClamp = true; return { ...p, time: from }; }
                return p;
            });
            return needClamp ? { ...item, points: pts } : item;
        };
        this.reconcile('recursive_zss', recZss, from, symbolKey, (item) => {
            const lvl = item._level || 0;
            // 递归中枢按绝对级别取网站链色:1m图 L0青/L1红/L2绿/L3蓝… 级别越高框越粗。
            const color = getRecursiveLevelColor(currentInterval, lvl);
            return safeCreate(wrapZs(color, lvl === 0 ? 1 : 2)(clampHeadToFrom(item)), 'rec_zs');
        }, false, true);
        // 新核心递归层级走势类型「线段」线条:各级 zslx_lines 扁平化(附 _level),用于在低周期图上
        // 叠加显示 5m/30m… 各级线段。每级独立开关 xd_L{level},**默认关**(cfg 未显式 true 不渲染);
        // 颜色与该级中枢同(getRecursiveLevelColor,同绝对级别),用线条与中枢框区分。
        const recLines = [];
        for (const lvObj of (barsResult.recursive_levels || [])) {
            if (!lvObj || !Array.isArray(lvObj.zslx_lines)) continue;
            const lvl = lvObj.level || 0;
            // 走势类型总开关 zslx_all(默认关,新key避开遗留 xd_zslx);开后各级默认显示、可单独关。
            if (!(cfg.zslx_all === true && cfg['xd_L' + lvl] !== false)) continue;
            for (const ln of lvObj.zslx_lines) recLines.push({ ...ln, _level: lvl });
        }
        this.reconcile('recursive_zslx_lines', recLines, from, symbolKey, (item) => {
            const lvl = item._level || 0;
            const color = getRecursiveLevelColor(currentInterval, lvl);
            return safeCreate(ChartUtils.createLineShape(this.chart, clampHeadToFrom(item), { color, linewidth: 2 }), 'rec_xdl');
        }, false, true);
        // P7 higher_zs 已停用(后端不再产出)。保留空路径防旧 cache 残留数据报错;
        // 内存缓存可能短暂命中旧 higher_zs(此时已无 per-period 开关可关),重启服务/清缓存后消失。
        if (barsResult.higher_zs && barsResult.higher_zs.length) {
            const higherZss = [];
            (barsResult.higher_zs || []).forEach((grp, gi) => {
                if (!grp || !Array.isArray(grp.zss)) return;
                grp.zss.forEach(zs => higherZss.push({ ...zs, _gi: gi }));
            });
            this.reconcile('higher_zss', higherZss, from, symbolKey, (item) => {
                const color = HIGHER_ZS_COLORS[(item._gi || 0) % HIGHER_ZS_COLORS.length];
                return safeCreate(wrapZs(color, 2)(item), 'higher_zs');
            }, false, true);
        } else {
            this.reconcile('higher_zss', [], from, symbolKey, (item) => item, false, true);
        }
        // 背驰/买卖点 —— 拆分版优先(笔/段独立 reconcile + 不同样式 + 独立 toggle);
        // 后端 ``bi_mmds``/``xd_mmds``/``bi_bcs``/``xd_bcs`` 拿不到时,fallback
        // 到合并版 ``bcs``/``mmds``(向后兼容旧 web/老 cache 命中场景)。
        const hasSplitMmds = barsResult.bi_mmds || barsResult.xd_mmds;
        const hasSplitBcs = barsResult.bi_bcs || barsResult.xd_bcs;
        const showBiMmd = cfg.mmd_bi !== false;
        const showXdMmd = cfg.mmd_xd !== false;
        const showBiBc = cfg.bc_bi !== false;
        const showXdBc = cfg.bc_xd !== false;
        // 买卖点 = icon 箭头(定位) + text 标签(类型),两套 shape 各自独立 reconcile;
        // 用同一份(已按 toggle 门控的)数据源,保证箭头与标签一一对应。
        // 买卖点偏移基准:近 N 根 K 线平均振幅(ATR 式),让箭头/标签自适应贴近 K 线,
        // 不再因绝对价格高低而浮空(尤其修复港美股高价 / 低波动标的)。
        const mmdOpt = { offsetBase: ChartUtils.computeMmdOffsetBase(barsResult.bars) };
        if (hasSplitMmds) {
            const biMmds = (cfg.mmd && showBiMmd) ? (barsResult.bi_mmds || []) : [];
            const xdMmds = (cfg.mmd && showXdMmd) ? (barsResult.xd_mmds || []) : [];
            this.reconcile('bi_mmds', biMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item, mmdOpt), 'mmd_bi'), false);
            this.reconcile('xd_mmds', xdMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item, mmdOpt), 'mmd_xd'), false);
            this.reconcile('bi_mmd_labels', biMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdLabelShape(this.chart, item, mmdOpt), 'mmd_bi_label'), false);
            this.reconcile('xd_mmd_labels', xdMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdLabelShape(this.chart, item, mmdOpt), 'mmd_xd_label'), false);
            this.reconcile('mmds', [], from, symbolKey, () => null, false);          // 清掉旧合并版
            this.reconcile('mmd_labels', [], from, symbolKey, () => null, false);
        } else {
            const mmds = cfg.mmd ? barsResult.mmds : [];
            this.reconcile('mmds', mmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item, mmdOpt), 'mmd'), false);
            this.reconcile('mmd_labels', mmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdLabelShape(this.chart, item, mmdOpt), 'mmd_label'), false);
        }
        if (hasSplitBcs) {
            this.reconcile('bi_bcs', (cfg.bc && showBiBc) ? (barsResult.bi_bcs || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc_bi'), false);
            this.reconcile('xd_bcs', (cfg.bc && showXdBc) ? (barsResult.xd_bcs || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc_xd'), false);
            this.reconcile('bcs', [], from, symbolKey, () => null, false);
        } else {
            this.reconcile('bcs', cfg.bc ? barsResult.bcs : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc'), false);
        }
        // 新核心各级(5m/30m/…)买卖点/背驰:recursive_levels[].mmds/bcs,与该级中枢**同 toggle**
        // (zs_L1/zs_L2/…);受买卖点/背驰主开关(mmd/bc)门控。L0 走 bi/xd_mmds、此处只高级别。
        const recMmds = [];
        const recBcs = [];
        for (const lvObj of (barsResult.recursive_levels || [])) {
            const _lv = (lvObj && lvObj.level) || 0;
            if (_lv === 0) continue;     // L0 走 bi/xd_mmds;此处高级别(5m/30m)
            // 买卖点按各级自己的开关(mmd_L1=5m/mmd_L2=30m)+主开关 mmd;背驰同理 bc_L*。
            if (cfg.mmd !== false && cfg['mmd_L' + _lv] !== false) for (const m of (lvObj.mmds || [])) recMmds.push(m);
            if (cfg.bc !== false && cfg['bc_L' + _lv] !== false) for (const b of (lvObj.bcs || [])) recBcs.push(b);
        }
        this.reconcile('recursive_mmds', recMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item, mmdOpt), 'rec_mmd'), false);
        this.reconcile('recursive_mmd_labels', recMmds, from, symbolKey, (item) => safeCreate(ChartUtils.createMmdLabelShape(this.chart, item, mmdOpt), 'rec_mmd_label'), false);
        this.reconcile('recursive_bcs', recBcs, from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'rec_bc'), false);

        // 刷新画图调色板(周期/数据变化后级别颜色更新;优先 TV 工具栏注入,失败回退浮层)。
        this.updateDrawPalette();

        // 一轮 reconcile 完后扫一次孤儿,清理 race 残留(safeRemove 静默失败 / container 提前清零)。
        // 因为 reconcile 内 create 是异步的,延后到下一帧执行,等所有 promise resolve 后再扫。
        if (this._sweepOrphanTimer) clearTimeout(this._sweepOrphanTimer);
        this._sweepOrphanTimer = setTimeout(() => {
            this._sweepOrphanTimer = null;
            this.sweepOrphanShapes();
        }, 100);
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
        clog(`[CHANLUN-TIMING]   barsResult: bars=${chartData.barsResult.bars?.length || 0} fxs=${chartData.barsResult.fxs?.length || 0} bis=${chartData.barsResult.bis?.length || 0} xds=${chartData.barsResult.xds?.length || 0} bi_zss=${chartData.barsResult.bi_zss?.length || 0} mmds=${chartData.barsResult.mmds?.length || 0}`);

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
                const data = JSON.parse(ev.data);
                const hp = this.udf_datafeed && this.udf_datafeed._historyProvider;
                // 复用 getBars 同一合并逻辑(applyChanlunUpdate=_processHistoryResponse):
                // 更新 bars_result + 派发 chanlun-bars-ready → draw_chanlun 重绘缠论。
                if (hp && typeof hp.applyChanlunUpdate === 'function') {
                    hp.applyChanlunUpdate(data, { symbol, resolution });
                }
                // K线也随 SSE 实时刷新：把推送里的最新 bar 喂给 TV(绕过轮询/节流/bars<2 抛错)。
                if (this.udf_datafeed && typeof this.udf_datafeed.feedRealtimeBar === 'function') {
                    const resKey = String(symbol).toLowerCase() + String(resolution).toLowerCase();
                    this.udf_datafeed.feedRealtimeBar(resKey, data);
                }
            } catch (e) { console.warn('[SSE] 处理推送失败', e); }
        });
        es.onerror = () => { clog('[SSE] 连接错误, 浏览器将自动重连; 轮询继续兜底'); };
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
        this._closeSseStream();
        // 工具栏注入重试定时器:正常 ≤4.2s 自停,但实例若在窗口内被 dispose 需显式清,
        // 避免定时器多跑几拍对已 dispose 实例操作(与 _sweepOrphanTimer 等清理对齐,审查 L-3)。
        if (this._lvlbtnTimer) { clearInterval(this._lvlbtnTimer); this._lvlbtnTimer = null; }
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
            this._visibilityHandler = null;
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
