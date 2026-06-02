// 缠论显示配置（cl_show_config）与独立周期画线开关（cl_independent_drawings）
// 按 ChartManager 实例独立维护，key 形如 cl_show_config_<chartId>；
// 旧全局 key 仅在新 key 不存在时作为默认值迁移，老用户设置不丢失。

// 默认的缠论显示项配置
const CL_SHOW_DEFAULT = {
    fx: true, bi: true, xd: true, bc: true, mmd: true,
    // 中枢按级别独立 toggle(笔中枢 / 线段中枢 / 递归层级中枢),平级独立控制:
    zs_bi: true, zs_xd: true, zs_recursive: true,
    higher_zs: true,
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

// 新核心递归层级中枢按级别配色(L0 笔中枢→L1→L2…);级别越高框越粗(见 drawChartElements)。
// 与旧 bi_zss/xd_zss(灰/蓝)区分,用暖色系突出"重做后的多级中枢"。超出长度按取模循环。
const RECURSIVE_LEVEL_COLORS = ["#26A69A", "#EF5350", "#AB47BC", "#FF9800", "#42A5F5", "#EC407A"];

// 多周期叠加中枢按"第几个高周期"配色(5min级别→[0]、30min级别→[1]…),冷色系区分递归中枢。
const HIGHER_ZS_COLORS = ["#5C6BC0", "#00897B", "#7E57C2", "#3949AB"];

// 着色规则(按「中枢的构成单元在当前周期的颜色」着色,直观区分笔/段):
//   bis    = 当前周期笔色;
//   xds    = 当前周期线段色 = **下一级周期的 bis 色**(原文「线段 = 高一级笔」);
//   bi_zss = 当前周期笔中枢 = **当前周期 bis 色**(笔中枢由笔构成 → 同笔色);
//   xd_zss = 当前周期线段中枢 = **当前周期 xds 色**(线段中枢由线段构成 → 同线段色)。
const DYNAMIC_CHART_COLORS = {
    "1": { ...DEFAULT_COLORS, bis: "#DF8344", xds: "#9C27B0", bi_zss: "#DF8344", xd_zss: "#9C27B0" },
    "5": { ...DEFAULT_COLORS, bis: "#9C27B0", xds: "#4FADEA", bi_zss: "#9C27B0", xd_zss: "#4FADEA" },
    "30": { ...DEFAULT_COLORS, bis: "#4FADEA", xds: "#EA3323", bi_zss: "#4FADEA", xd_zss: "#EA3323" },
    "1D": { ...DEFAULT_COLORS, bis: "#EA3323", xds: "#9FCE63", bi_zss: "#EA3323", xd_zss: "#9FCE63" },
    "1W": { ...DEFAULT_COLORS, bis: "#9FCE63", xds: "#4274B1", bi_zss: "#9FCE63", xd_zss: "#4274B1" },
    "1M": { ...DEFAULT_COLORS, bis: "#4274B1", xds: "#C638DD", bi_zss: "#4274B1", xd_zss: "#C638DD" },
};

function getDynamicColor(interval, elementType) {
    if (DYNAMIC_CHART_COLORS[interval] && DYNAMIC_CHART_COLORS[interval][elementType]) {
        return DYNAMIC_CHART_COLORS[interval][elementType];
    }
    return DEFAULT_COLORS[elementType] || "#FFFFFF";
}

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
        const isBuy = mmd.text.includes("B");
        const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
        const isSplit = !!mmd.level;
        const isXd = isSplit && mmd.level === "xd";
        const size = isSplit ? (isXd ? MMD_ICON_SIZE.xd : MMD_ICON_SIZE.bi) : MMD_ICON_SIZE.default;
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
        const isBuy = mmd.text.includes("B");
        const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
        const isSplit = !!mmd.level;
        const isXd = isSplit && mmd.level === "xd";
        const fontsize = isSplit ? (isXd ? MMD_LABEL_FONTSIZE.xd : MMD_LABEL_FONTSIZE.bi) : MMD_LABEL_FONTSIZE.default;
        const levelPrefix = isSplit ? (isXd ? "段" : "笔") : "";
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
        return this.createShape(chart, bc.points, { shape: "balloon", text: bc.text, overrides: { markerColor: CHART_CONFIG.COLORS.BCS, backgroundColor: CHART_CONFIG.COLORS.BCS, textColor: CHART_CONFIG.COLORS.BC_TEXT, transparency: 70, backgroundTransparency: 70, fontsize: 12, ...options.overrides }, ...options });
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
        if (detail.symbol && detail.symbol !== identity.symbol.toLowerCase()) {
            return;
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
        this.udf_datafeed = new Datafeeds.UDFCompatibleDatafeed("/tv", 3000, undefined, {
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
                console.log("[DEBUG-CHARTS] saveChart called", chartData);
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
                        console.log("[DEBUG-CHARTS] saveChart response", res);
                        return res.status === 'ok' ? (res.id || chartData.id || "default") : null;
                    })
                    .catch(err => {
                        console.error("[DEBUG-CHARTS] saveChart error", err);
                        return null;
                    });
            },
            getChartContent: function (chartId) {
                console.log("[DEBUG-CHARTS] getChartContent called", chartId);
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
                console.log("[DEBUG-CHARTS] saveLineToolsAndGroups called", { layoutId, chartId, state, options });
                return new Promise((resolve) => {
                    if (self.shouldSuppressDrawingSave()) {
                        console.log("[DEBUG-CHARTS] Skip saveLineToolsAndGroups due to active drawing mutation");
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

                    console.log("[DEBUG-CHARTS] Saving drawings for", { symbol, resolution, sourcesCount: Object.keys(processedState.sources || {}).length, rawSources: state.sources, reason: options.reason });

                    fetch("/tv/1.1/drawings?client=" + client_id + "&user=" + user_id + "&chart=" + chartId + "&layout=" + layoutId + "&symbol=" + symbol + "&resolution=" + resolution, {
                        method: "POST",
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({ state: processedState })
                    }).then(res => res.json()).then(res => {
                        console.log("[DEBUG-CHARTS] saveLineToolsAndGroups response", res);
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
                console.log("[DEBUG-CHARTS] loadLineToolsAndGroups called", { layoutId, chartId, requestType, requestContext });
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
                const _chain = FREQ_CHAIN[_curInterval] || [_curInterval, "高一级", "高二级", "高三级"];
                const _lbl = (i) => _chain[i] || `L+${i}`;

                // 重组后的菜单:按「功能分组」组织 + 顶部级别映射默认折叠 +
                // 底部「全选/全清」一键操作。比起原始扁平 14 项更易扫读,
                // 减少新用户「原文化新增」等晦涩术语的认知负担。
                const _cbRow = (k, label, indent) => `<label style="display:block; cursor:pointer; ${indent ? 'padding-left:14px; font-size:12px;' : ''}"><input type="checkbox" id="${cbId(k)}" ${cfg[k] ? 'checked' : ''} style="margin-right:6px; vertical-align:middle;">${label}</label>`;
                const _grpTitle = (t) => `<div style="font-size:11px; color:#4a90e2; padding:5px 0 1px; font-weight:bold;">${t}</div>`;

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
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bi')}" ${cfg.bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('xd')}" ${cfg.xd ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">线段</label>
                        </div>

                        ${_grpTitle('中枢')}
                        <div style="display:flex; gap:14px; font-size:12px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('zs_bi')}" ${cfg.zs_bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔中枢</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('zs_xd')}" ${cfg.zs_xd ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">线段中枢</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('zs_recursive')}" ${cfg.zs_recursive !== false ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">递归中枢</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('higher_zs')}" ${cfg.higher_zs !== false ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">高周期中枢</label>
                        </div>

                        ${_grpTitle('买卖点')}
                        ${_cbRow('mmd', '总开关', false)}
                        <div style="padding-left:14px; font-size:12px; display:flex; gap:12px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('mmd_bi')}" ${cfg.mmd_bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔层(小)</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('mmd_xd')}" ${cfg.mmd_xd ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">段层(大)</label>
                        </div>

                        ${_grpTitle('背驰')}
                        ${_cbRow('bc', '总开关', false)}
                        <div style="padding-left:14px; font-size:12px; display:flex; gap:12px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bc_bi')}" ${cfg.bc_bi ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">笔背驰</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bc_xd')}" ${cfg.bc_xd ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">段背驰</label>
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
                    // 笔/段独立级别开关(中枢 / 买卖点 / 背驰)
                    'zs_bi', 'zs_xd', 'mmd_bi', 'mmd_xd', 'bc_bi', 'bc_xd',
                    // 新核心递归层级中枢开关
                    'zs_recursive',
                    // 多周期叠加中枢开关
                    'higher_zs',
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
                    const closeHandler = (ev) => {
                        const menuEl = document.getElementById(menuId);
                        if (!menuEl) {
                            document.removeEventListener('click', closeHandler, true);
                            return;
                        }
                        if (menuEl.contains(ev.target)) return;     // 菜单内点击不关
                        menuEl.remove();
                        document.removeEventListener('click', closeHandler, true);
                    };
                    document.addEventListener('click', closeHandler, true);
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

            // 注入 MACD 区间统计（工具栏按钮 + 右键菜单 + 侧边面板），依赖 chart/widget 已就绪
            try {
                if (window.MacdStats && typeof window.MacdStats.attach === 'function') {
                    window.MacdStats.attach(this);
                }
            } catch (e) {
                console.warn("[DEBUG-CHARTS] MacdStats.attach failed", e);
            }

            this.widget.subscribe('drawing_event', (id, eventType) => {
                if (this.shouldSuppressDrawingSave()) return;
                console.log("[DEBUG-CHARTS] drawing_event", id, eventType);
                this.scheduleDrawingsSave('drawing_event');
            });
            this.widget.subscribe('onAutoSaveNeeded', () => {
                if (this.shouldSuppressDrawingSave()) return;
                console.log("[DEBUG-CHARTS] onAutoSaveNeeded");
                this.scheduleDrawingsSave('auto_save');
            });
        });
    }

    handleSymbolChange(symbol) {
        if (!symbol?.ticker) return;
        const [market, code] = symbol.ticker.split(":");
        if (!market || !code) return;
        if (Utils.get_market() !== market) { Utils.set_local_data("market", market); location.reload(); return; }
        Utils.set_local_data("market", market); Utils.set_local_data(`${market}_code`, code);
        this._initialLoadDone = false;
        this._latestAppliedBarTime = null;
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('symbol-change');
        if (typeof ZiXuan.render_zixuan_opts === "function") ZiXuan.render_zixuan_opts();
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
    }

    handleDataReady() {
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleDataReady fired [_initialLoadDone=true]`);
        this._initialLoadDone = true;
        this.debouncedDrawChanlun();
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
        const barsResult = this.udf_datafeed?._historyProvider?.bars_result?.get(symbolResKey);

        if (!barsResult) {
            const availableKeys = this.udf_datafeed?._historyProvider?.bars_result ? Array.from(this.udf_datafeed._historyProvider.bars_result.keys()) : [];
            console.warn(`[DEBUG-CHARTS] getChartData for ${symbolResKey}: NOT FOUND. Available keys:`, availableKeys);
            return null;
        }

        if (!this.chart) {
            console.warn("[DEBUG-CHARTS] getChartData aborted: this.chart is null.");
            return null;
        }
        const visibleRange = this.chart.getVisibleRange();
        if (!visibleRange || !visibleRange.from || !visibleRange.to) {
            console.warn("[DEBUG-CHARTS] getChartData aborted: VisibleRange invalid (chart loading).");
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
        const signature = `${newKeys.size}|${from}|${sortedKeys.join(',').slice(0, 256)}`;
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
        const toKeep = [];
        let removedCount = 0;
        for (const existing of container) {
            const existingTail = existing.tailTime ?? existing.time;
            if (newKeys.has(existing.key) && existingTail >= from) {
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
                        this._scheduleReconcileRetry(`${type}:async-null`);
                        return;
                    }
                    entry.id = realId;
                    container.push(entry);
                    this._reconcileOwnedIds.add(realId);
                }).catch((e) => {
                    console.warn(`[CHANLUN-DIAG][reconcile.${type}] async create→reject key=${(key||'').slice(0,40)}`, e);
                    this._scheduleReconcileRetry(`${type}:async-reject`);
                });
            } else if (result != null) {
                entry.id = result;
                container.push(entry);
                this._reconcileOwnedIds.add(result);
                createSync += 1;
            } else {
                console.warn(`[CHANLUN-DIAG][reconcile.${type}] sync create→null key=${(key||'').slice(0,40)}`);
                this._scheduleReconcileRetry(`${type}:sync-null`);
            }
        });

        // 同步路径完成后记录签名；异步 create 仍在 pending 无妨，resolve 后 push 到
        // container 与签名目标状态一致，下次 guard 时容器已正确
        this._reconcileGuard[guardKey] = signature;

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
        this.reconcile('bis', cfg.bi ? barsResult.bis : [], from, symbolKey, (item) => safeCreate(ChartUtils.createLineShape(this.chart, item, { color: getDynamicColor(currentInterval, "bis"), linewidth: 2 }), 'bi'));
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
        this.reconcile('bi_zss', cfg.zs_bi ? barsResult.bi_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "bi_zss"), 1)(item), 'bi_zs'), false);
        this.reconcile('xd_zss', cfg.zs_xd ? barsResult.xd_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "xd_zss"), 2)(item), 'xd_zs'), false);
        // 新核心递归层级中枢(recursive_levels):后端按级别给 [{level, zss, zslxs, ...}]。
        // 旧 bi_zss/xd_zss 后端默认关闭(2026-05 清场态),重做后的多级中枢全在此画出。
        // 各级 zss 扁平化(附 _level)后用单 reconcile —— 单 key 增量天然正确;
        // 按级别选色/线宽:L0(笔中枢)细框,L1+(高级别中枢)粗框。
        // 递归中枢按级别绑到对应开关:L0=笔中枢(zs_bi)、L1=线段中枢(zs_xd)、L2+=递归中枢(zs_recursive)。
        // 让「笔中枢/线段中枢」按钮分别控制新核心最低两级(旧 bi_zss/xd_zss 后端清场已产空、是死开关)。
        const recZss = [];
        for (const lvObj of (barsResult.recursive_levels || [])) {
            if (!lvObj || !Array.isArray(lvObj.zss)) continue;
            const lvl = lvObj.level || 0;
            const showLvl = lvl === 0 ? (cfg.zs_bi !== false)
                          : lvl === 1 ? (cfg.zs_xd !== false)
                          : (cfg.zs_recursive !== false);
            if (!showLvl) continue;
            for (const zs of lvObj.zss) recZss.push({ ...zs, _level: lvl });
        }
        this.reconcile('recursive_zss', recZss, from, symbolKey, (item) => {
            const lvl = item._level || 0;
            const color = RECURSIVE_LEVEL_COLORS[lvl % RECURSIVE_LEVEL_COLORS.length];
            return safeCreate(wrapZs(color, lvl === 0 ? 1 : 2)(item), 'rec_zs');
        }, false);
        // 多周期中枢叠加(higher_zs):后端按高周期给 [{period, level_name, zss}]。
        // 低周期图(1m/5m)叠加真实高周期(5m/30m)的 L1 线段中枢。扁平化(附 _gi 组序)后
        // 单 reconcile,按高周期序选色(冷色系,与递归中枢区分)。
        const higherZss = [];
        (barsResult.higher_zs || []).forEach((grp, gi) => {
            if (!grp || !Array.isArray(grp.zss)) return;
            grp.zss.forEach(zs => higherZss.push({ ...zs, _gi: gi }));
        });
        // includeOverlaps=true:高周期中枢跨度大,起点常在可视窗口左侧外,需"终点在
        // 窗口内即画"(全局视角),否则滚到右侧时高级别中枢会被窗口过滤掉、看不到。
        this.reconcile('higher_zss', (cfg.higher_zs !== false) ? higherZss : [], from, symbolKey, (item) => {
            const color = HIGHER_ZS_COLORS[(item._gi || 0) % HIGHER_ZS_COLORS.length];
            return safeCreate(wrapZs(color, 2)(item), 'higher_zs');
        }, false, true);
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
            if (this._drawRetryCount < 10) {
                this._drawRetryCount++;
                clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry#${this._drawRetryCount}/10 in 500ms`);
                setTimeout(() => this.debouncedDrawChanlun(), 500);
            } else {
                console.warn(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry exhausted`);
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

    dispose() {
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
