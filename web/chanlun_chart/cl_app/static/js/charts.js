// 缠论显示配置（cl_show_config）与独立周期画线开关（cl_independent_drawings）
// 按 ChartManager 实例独立维护，key 形如 cl_show_config_<chartId>；
// 旧全局 key 仅在新 key 不存在时作为默认值迁移，老用户设置不丢失。

// 默认的缠论显示项配置
const CL_SHOW_DEFAULT = {
    fx: true, bi: true, xd: true, zs: true, bc: true, mmd: true,
    // 买卖点/背驰按级别独立 toggle(笔层数量远多于段层、用户常需只看段层):
    mmd_bi: true, mmd_xd: true, bc_bi: true, bc_xd: true,
    // 原文化新增独立开关(默认全开,用户可在控制面板单独 toggle):
    zs_direction: true,        // 中枢按 zs.type 着色(up/down/zd)
    zs_expanded: true,         // ⑤ 扩展中枢加粗框
    xd_zslx: true,             // ③ 线段级走势类型区间(半透明矩形)
    recursive_levels: true,    // ④ 递归 L1+ 高级中枢与走势类型
    interval_nest: true,       // 区间套链 flags + 精确转折点
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

function loadClShowConfig(chartId) {
    try {
        const raw = localStorage.getItem('cl_show_config_' + chartId);
        if (raw) {
            return Object.assign({}, CL_SHOW_DEFAULT, JSON.parse(raw));
        }
        // 兼容旧版全局 key 作为首次默认值，不写回旧 key
        const legacy = localStorage.getItem('cl_show_config');
        if (legacy) {
            return Object.assign({}, CL_SHOW_DEFAULT, JSON.parse(legacy));
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
        // 中枢方向着色(③ ZslxCalculator 回填):上涨中枢=暖红、下跌中枢=冷蓝、震荡=灰
        ZS_UP: "#E57373", ZS_DOWN: "#64B5F6", ZS_ZD: "#90A4AE",
        // 走势类型(③ xd_zslx)半透明背景色
        ZSLX_UP: "#FFCCBC", ZSLX_DOWN: "#BBDEFB", ZSLX_ZHENGLI: "#CFD8DC",
        // 递归层级 L1+ 高级中枢(④):级别越高颜色越深
        RECURSIVE_L1: "#7E57C2", RECURSIVE_L2: "#4A148C", RECURSIVE_L3: "#311B92",
        // 区间套(原文第三章·第六节):链路灰、转折点高亮
        INTERVAL_NEST_LINK: "#9E9E9E", INTERVAL_NEST_TP: "#FF6F00",
    },
    LINE_STYLES: { SOLID: 0, DOTTED: 1, DASHED: 2 },
    CHART_TYPES: [
        "fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds",
        // 拆分版买卖点/背驰(笔层 vs 段层),独立 reconcile
        "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs",
        // 原文化新增:③ 走势类型 / ④ 递归层级 / 区间套
        "xd_zslx", "recursive_zss_L1", "recursive_zss_L2", "recursive_zss_L3",
        "recursive_zslxs_L1", "recursive_zslxs_L2", "recursive_zslxs_L3",
        "interval_nest_links", "interval_nest_tp",
    ],
};

const DEFAULT_COLORS = {
    bis: CHART_CONFIG.COLORS.BI, xds: CHART_CONFIG.COLORS.XD,
    bi_zss: CHART_CONFIG.COLORS.BI_ZSS, xd_zss: CHART_CONFIG.COLORS.XD_ZSS,
};

const DYNAMIC_CHART_COLORS = {
    "1": { ...DEFAULT_COLORS, bis: "#DF8344", xds: "#9C27B0", xd_zss: "#4FADEA", bi_zss: "#FFFF55" },
    "5": { ...DEFAULT_COLORS, bis: "#9C27B0", xds: "#4FADEA", xd_zss: "#EA3323", bi_zss: "#4FADEA" },
    "30": { ...DEFAULT_COLORS, bis: "#4FADEA", xds: "#EA3323", xd_zss: "#9FCE63", bi_zss: "#EA3323" },
    "1D": { ...DEFAULT_COLORS, bis: "#EA3323", xds: "#9FCE63", xd_zss: "#4274B1", bi_zss: "#9FCE63" },
    "1W": { ...DEFAULT_COLORS, bis: "#9FCE63", xds: "#4274B1", xd_zss: "#C638DD", bi_zss: "#4274B1" },
    "1M": { ...DEFAULT_COLORS, bis: "#4274B1", xds: "#C638DD", xd_zss: "#5E813F", bi_zss: "#C638DD" },
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
        // 中枢方向着色(§3.6 回填):options.useDirectionColor=true 时按 zs.type 选色,
        // 否则用 options.color(老路径,通常是 getDynamicColor 给的级别色)。
        // ⑤ 扩展中枢(is_expanded):自动加粗+不透明边框,与普通中枢区分。
        let color = options.color || CHART_CONFIG.COLORS.BI;
        if (options.useDirectionColor && zs.type) {
            if (zs.type === "up") color = CHART_CONFIG.COLORS.ZS_UP;
            else if (zs.type === "down") color = CHART_CONFIG.COLORS.ZS_DOWN;
            else if (zs.type === "zd") color = CHART_CONFIG.COLORS.ZS_ZD;
        }
        let linewidth = options.linewidth || 1;
        let transparency = 95;
        if (zs.is_expanded) {
            linewidth = Math.max(linewidth + 2, 3);
            transparency = 80;          // 扩展中枢更显眼
        }
        return this.createShape(chart, zs.points, { shape: "rectangle", overrides: { linestyle: parseInt(zs.linestyle) || 0, linewidth, linecolor: color, backgroundColor: color, transparency, color, "trendline.linecolor": color, fillBackground: true, filled: true, ...options.overrides }, ...options });
    },
    createZslxShape(chart, zslx, options = {}) {
        // 走势类型区间(③ xd_zslx) —— 用 **粗虚线空心矩形** 表「上涨/下跌/盘整」
        // 区间。与中枢矩形区分:
        //   - 中枢矩形是半透明填充,会盖在 K 线上;
        //   - 走势类型只画边框、不填背景,避免与中枢/笔/段视觉冲突;
        //   - 边框用 dashed/dotted 虚线 + 粗线宽,远看就能识别区间范围。
        let color = CHART_CONFIG.COLORS.ZSLX_ZHENGLI;
        if (zslx.direction === "up") color = CHART_CONFIG.COLORS.ZSLX_UP;
        else if (zslx.direction === "down") color = CHART_CONFIG.COLORS.ZSLX_DOWN;
        return this.createShape(chart, zslx.points, { shape: "rectangle", overrides: { linestyle: 2, linewidth: 3, linecolor: color, backgroundColor: color, transparency: 100, fillBackground: false, filled: false, color, "trendline.linecolor": color, ...options.overrides }, ...options });
    },
    createRecursiveZsShape(chart, zs, options = {}) {
        // 递归层级中枢(④ L1+) —— 用包络区间(GG/DD)矩形,与 L0 ZS/ZD 核心区
        // 在几何上视觉分层。颜色按级别:L1=紫、L2=深紫、L3=深紫几乎黑。
        // **粗实线边框 + 极淡半透明填充** —— 既能看到边界又不遮挡 L0 中枢。
        const levelColors = {
            1: CHART_CONFIG.COLORS.RECURSIVE_L1,
            2: CHART_CONFIG.COLORS.RECURSIVE_L2,
            3: CHART_CONFIG.COLORS.RECURSIVE_L3,
        };
        const color = levelColors[options.level] || CHART_CONFIG.COLORS.RECURSIVE_L1;
        const linewidth = 2 + (options.level || 1);     // 级别越高线越粗
        const transparency = zs.is_expanded ? 70 : 92;
        return this.createShape(chart, zs.points, { shape: "rectangle", overrides: { linestyle: parseInt(zs.linestyle) || 0, linewidth, linecolor: color, backgroundColor: color, transparency, color, "trendline.linecolor": color, fillBackground: true, filled: true, ...options.overrides }, ...options });
    },
    createIntervalNestLinkShape(chart, link, options = {}) {
        // 区间套·一重:在该重背驰段终点处画 flag,标注 ``L{level}`` 与是否真背驰。
        // link 形态为 ``{points:{time,price}, level, is_beichi, direction, ...}``。
        const color = link.is_beichi ? CHART_CONFIG.COLORS.INTERVAL_NEST_LINK : CHART_CONFIG.COLORS.BCS;
        const label = `L${link.level}${link.is_beichi ? "✓" : "?"}`;
        return this.createShape(chart, link.points, { shape: "flag", text: label, overrides: { markerColor: color, color, backgroundColor: color, transparency: 80, fontsize: 10, ...options.overrides }, ...options });
    },
    createIntervalNestTurningPointShape(chart, tp, options = {}) {
        // 区间套·转折点:最低重背驰段终分型,用大号高亮 icon 表「精确转折」。
        // tp 形态为 ``{points:{time,price}, direction}``。
        const color = CHART_CONFIG.COLORS.INTERVAL_NEST_TP;
        const shape = options.direction === "up" ? "arrow_down" : "arrow_up";
        return this.createShape(chart, tp.points, { shape, text: "区间套转折", overrides: { arrowColor: color, color, fontsize: 14, bold: true, ...options.overrides }, ...options });
    },
    createMmdShape(chart, mmd, options = {}) {
        const isBuy = mmd.text.includes("B");
        const color = isBuy ? CHART_CONFIG.COLORS.MMD_UP : CHART_CONFIG.COLORS.MMD_DOWN;
        const shape = isBuy ? "arrow_up" : "arrow_down";
        // 按级别分样式(buy/sell point 数量笔层 >> 段层):
        //   - 笔层(``options.levelHint === 'bi'``):小字号 + 高透明,「次要级别」
        //   - 段层(``options.levelHint === 'xd'``):大字号 + 加粗 + 不透明,「主要级别」
        //   - 缺省(老 mmds 合并版):中等字号
        // 标签:拆分版 ``mmd.level`` 已带级别字段,前缀「笔/段」直接显示;合并版
        // 保留原 ``笔:3B/段:1B`` 全文。
        const isSplit = !!mmd.level;
        const isXd = isSplit && mmd.level === "xd";
        const fontsize = isSplit ? (isXd ? 14 : 10) : 12;
        const transparency = isSplit ? (isXd ? 0 : 40) : 0;
        const labelPrefix = isSplit ? (isXd ? "段·" : "笔·") : "";
        const label = labelPrefix + mmd.text.replace(/[笔段]:/g, "");
        return this.createShape(chart, mmd.points, { shape, text: label, overrides: { arrowColor: color, color, fontsize, bold: isXd, transparency, ...options.overrides }, ...options });
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
                    $('#' + backdropId).remove();
                    return;
                }

                const cfg = self.cl_show_config;
                const cbId = (k) => 'cl_cb_' + k + '_' + self.id;
                const indCbId = 'cl_cb_independent_drawings_' + self.id;

                let html = `
                    <div id="${menuId}" style="position: absolute; z-index: 99999999; background: #fff; border: 1px solid #ccc; box-shadow: 0 2px 10px rgba(0,0,0,0.2); border-radius: 4px; padding: 10px; line-height: 28px; font-size: 14px; color: #333;">
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('fx')}" ${cfg.fx ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 分型</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('bi')}" ${cfg.bi ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 笔</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('xd')}" ${cfg.xd ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 线段</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('zs')}" ${cfg.zs ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 中枢</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('bc')}" ${cfg.bc ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 背驰</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('mmd')}" ${cfg.mmd ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 买卖点(总开关)</label>
                        <div style="padding-left: 16px; font-size: 12px;">
                            <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('mmd_bi')}" ${cfg.mmd_bi ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 笔层(小)</label>
                            <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('mmd_xd')}" ${cfg.mmd_xd ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 段层(大)</label>
                            <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('bc_bi')}" ${cfg.bc_bi ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 笔背驰</label>
                            <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('bc_xd')}" ${cfg.bc_xd ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 段背驰</label>
                        </div>
                        <hr style="margin: 5px 0;">
                        <div style="font-size: 12px; color: #666; margin-bottom: 4px;">原文化新增</div>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('zs_direction')}" ${cfg.zs_direction ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 中枢方向着色</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('zs_expanded')}" ${cfg.zs_expanded ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 扩展中枢加粗</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('xd_zslx')}" ${cfg.xd_zslx ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 走势类型区间</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('recursive_levels')}" ${cfg.recursive_levels ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 递归层级 L1+</label>
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${cbId('interval_nest')}" ${cfg.interval_nest ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 区间套</label>
                        <hr style="margin: 5px 0;">
                        <label style="display:block; cursor:pointer;"><input type="checkbox" id="${indCbId}" ${self.cl_independent_drawings ? 'checked' : ''} style="margin-right: 8px; vertical-align: middle;"> 独立周期画线</label>
                    </div>
                `;
                $('body').append(html);

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
                    'fx', 'bi', 'xd', 'zs', 'bc', 'mmd',
                    // 笔/段独立级别开关
                    'mmd_bi', 'mmd_xd', 'bc_bi', 'bc_xd',
                    'zs_direction', 'zs_expanded', 'xd_zslx',
                    'recursive_levels', 'interval_nest',
                ];
                keys.forEach(k => {
                    $('#' + cbId(k)).change(function () {
                        self.cl_show_config[k] = $(this).is(':checked');
                        saveClShowConfig(self.id, self.cl_show_config);
                        // 清 reconcile 守卫:样式型 toggle(如 zs_direction/zs_expanded)
                        // 不改变 makeKey 的 time/price 签名,_reconcileGuard 会误判
                        // "数据未变"早期 return,toggle 不生效。清守卫让 reconcile
                        // 走完整 add/remove 流程、按新 cfg 重建所有 shape。
                        self._reconcileGuard = {};
                        // 同时清掉所有 owned shape,让 reconcile 完全从空容器重建。
                        // 不这样的话 makeKey 命中的旧 shape 会被 toKeep 保留,
                        // 样式 cfg 变了但 shape 没重创建——看起来 toggle 不生效。
                        if (self.chart) {
                            CHART_CONFIG.CHART_TYPES.forEach((type) => {
                                Object.keys(self.obj_charts || {}).forEach((sk) => {
                                    const container = self.obj_charts[sk] && self.obj_charts[sk][type];
                                    if (!container || container.length === 0) return;
                                    container.forEach((item) => self.safeRemove(item.id));
                                    container.length = 0;
                                });
                            });
                        }
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

                // 透明遮罩覆盖全屏，确保点击弹框外任意区域都能可靠关闭菜单
                const backdrop = $('<div id="' + backdropId + '" style="position:fixed;top:0;left:0;width:100%;height:100%;z-index:99999998;background:transparent;"></div>');
                $('body').append(backdrop);
                backdrop.on('click', function () {
                    $('#' + menuId).remove();
                    $(this).remove();
                });
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

    reconcile(type, sourceList, from, symbolKey, createFunc, useUnique = true) {
        const container = this.obj_charts[symbolKey][type];
        const beforeCount = container.length;
        let renderList = sourceList || [];
        const sourceCount = renderList.length;
        if (useUnique) {
            renderList = this.getUniqueRenderList(renderList);
        }
        const afterUniqueCount = renderList.length;

        // 按可视窗口过滤：历史 bis 可达数百根全画视觉杂乱；
        // headTime >= from 才入渲染，画外起点定格在创建时位置（可接受的 trade-off）
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
            // headTime >= from：避免 createMultipointShape snap 把画外起点吸附到可见区边缘造成错位长斜线；
            // 代价是缩放级别低时跨可见窗的 XD 不显示，收益是零错位
            if (headTime >= from) {
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
        // 中枢方向着色由 cfg.zs_direction 开关(默认 true)控制——开启时按 zs.type
        // 选 ZS_UP/ZS_DOWN/ZS_ZD,关闭时回退按级别色(老行为)。⑤ 扩展中枢标记
        // (is_expanded)由 cfg.zs_expanded 控制是否加粗框。两者数据均来自后端。
        const useDirColor = cfg.zs_direction !== false;
        const showExpanded = cfg.zs_expanded !== false;
        const wrapZs = (lvlColor) => (item) => {
            const zsItem = showExpanded ? item : { ...item, is_expanded: false };
            return ChartUtils.createZhongshuShape(this.chart, zsItem, {
                color: lvlColor, linewidth: lvlColor === getDynamicColor(currentInterval, "bi_zss") ? 1 : 2,
                useDirectionColor: useDirColor,
            });
        };
        this.reconcile('bi_zss', cfg.zs ? barsResult.bi_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "bi_zss"))(item), 'bi_zs'));
        this.reconcile('xd_zss', cfg.zs ? barsResult.xd_zss : [], from, symbolKey, (item) => safeCreate(wrapZs(getDynamicColor(currentInterval, "xd_zss"))(item), 'xd_zs'));
        // 背驰/买卖点 —— 拆分版优先(笔/段独立 reconcile + 不同样式 + 独立 toggle);
        // 后端 ``bi_mmds``/``xd_mmds``/``bi_bcs``/``xd_bcs`` 拿不到时,fallback
        // 到合并版 ``bcs``/``mmds``(向后兼容旧 web/老 cache 命中场景)。
        const hasSplitMmds = barsResult.bi_mmds || barsResult.xd_mmds;
        const hasSplitBcs = barsResult.bi_bcs || barsResult.xd_bcs;
        const showBiMmd = cfg.mmd_bi !== false;
        const showXdMmd = cfg.mmd_xd !== false;
        const showBiBc = cfg.bc_bi !== false;
        const showXdBc = cfg.bc_xd !== false;
        if (hasSplitMmds) {
            this.reconcile('bi_mmds', (cfg.mmd && showBiMmd) ? (barsResult.bi_mmds || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item), 'mmd_bi'), false);
            this.reconcile('xd_mmds', (cfg.mmd && showXdMmd) ? (barsResult.xd_mmds || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item), 'mmd_xd'), false);
            this.reconcile('mmds', [], from, symbolKey, () => null, false);   // 清掉旧合并版
        } else {
            this.reconcile('mmds', cfg.mmd ? barsResult.mmds : [], from, symbolKey, (item) => safeCreate(ChartUtils.createMmdShape(this.chart, item), 'mmd'), false);
        }
        if (hasSplitBcs) {
            this.reconcile('bi_bcs', (cfg.bc && showBiBc) ? (barsResult.bi_bcs || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc_bi'), false);
            this.reconcile('xd_bcs', (cfg.bc && showXdBc) ? (barsResult.xd_bcs || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc_xd'), false);
            this.reconcile('bcs', [], from, symbolKey, () => null, false);
        } else {
            this.reconcile('bcs', cfg.bc ? barsResult.bcs : [], from, symbolKey, (item) => safeCreate(ChartUtils.createBcShape(this.chart, item), 'bc'), false);
        }

        // ③ 走势类型(xd_zslx) —— 半透明矩形区间标记上涨/下跌/盘整
        this.reconcile('xd_zslx', cfg.xd_zslx !== false ? (barsResult.xd_zslx || []) : [], from, symbolKey, (item) => safeCreate(ChartUtils.createZslxShape(this.chart, item), 'xd_zslx'), false);

        // ④ 递归层级树 —— L1/L2/L3 高级中枢与走势类型(L0 已在 xd_zss / xd_zslx)
        const recLevels = (cfg.recursive_levels !== false ? (barsResult.recursive_levels || []) : []);
        // 收集到 by-level container(reconcile key 已预先分配 recursive_zss_L1..L3 / _zslxs_L1..L3)
        for (let L = 1; L <= 3; L++) {
            const lv = recLevels.find((x) => x.level === L);
            this.reconcile(`recursive_zss_L${L}`, lv ? lv.zss : [], from, symbolKey, (item) => safeCreate(ChartUtils.createRecursiveZsShape(this.chart, item, { level: L }), `rec_zs_L${L}`));
            this.reconcile(`recursive_zslxs_L${L}`, lv ? lv.zslxs : [], from, symbolKey, (item) => safeCreate(ChartUtils.createZslxShape(this.chart, item, { overrides: { transparency: 88 } }), `rec_zslx_L${L}`), false);
        }

        // 区间套 —— 链路 flags + 精确转折点 marker。reconcile.makeKey 依赖
        // item.points(单点形态用 ``{points:{time,price}}``,多点用 ``{points:[...]}``);
        // 后端 inest.links / turning_point 是裸 {time,price},包装成 ``points`` 形态。
        const inest = (cfg.interval_nest !== false ? barsResult.interval_nest : null);
        const linksWrapped = (inest && inest.links ? inest.links : []).map(l => ({
            ...l, points: { time: l.time, price: l.price }, text: `L${l.level}`,
        }));
        this.reconcile('interval_nest_links', linksWrapped, from, symbolKey, (item) => safeCreate(ChartUtils.createIntervalNestLinkShape(this.chart, item), 'inest_link'), false);
        const tpItems = (inest && inest.turning_point) ? [{
            points: { time: inest.turning_point.time, price: inest.turning_point.price },
            direction: inest.direction,
            text: "转折",
        }] : [];
        this.reconcile('interval_nest_tp', tpItems, from, symbolKey, (item) => safeCreate(ChartUtils.createIntervalNestTurningPointShape(this.chart, item, { direction: item.direction }), 'inest_tp'), false);

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
            return chartManager.widget;
        },
    };
})();