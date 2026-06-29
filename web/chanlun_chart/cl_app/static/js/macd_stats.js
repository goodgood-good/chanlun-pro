// MACD 区间统计：在 TradingView 图表上选区间统计 MACD/MACD_HTF 红绿柱面积与峰谷，
// 提供工具栏按钮 + 右键菜单选点 + 可拖拽侧边面板。
// 依赖: charts.js (ChartManager / ChanlunTVRegistry / GlobalTVDatafeeds)

var MacdStats = (function () {

    const PANEL_ID = 'macd-stats-panel';
    const RANGE_SHAPE_TAG = 'macd-stats-range';
    const MARKER_SHAPE_TAG = 'macd-stats-marker';

    const COLOR_POS = '#ef5350'; // 红柱
    const COLOR_NEG = '#26a69a'; // 绿柱
    const COLOR_RANGE_BG = '#FFD54F';

    // 复刻后端 HIGHER_FREQ_MAP(chart_compute.py:85);仅取本工具会用到的层级。
    const HIGHER_FREQ_MAP = { '1m': '5m', '5m': '30m', '30m': 'd', 'd': 'w', 'w': 'm', 'm': 'y' };
    const DEFAULT_MARKET_OFFSET_H = 8; // 日级以上分桶的通用偏移(近似)

    // TV resolution → 项目 frequency(复刻 constants.py resolution_maps,大小写敏感)
    const RES_TO_FREQ = {
        '10S': '10s', '30S': '30s', '1': '1m', '2': '2m', '3': '3m', '5': '5m',
        '10': '10m', '15': '15m', '30': '30m', '60': '60m', '120': '120m',
        '180': '3h', '240': '4h', '1D': 'd', '2D': '2d', '1W': 'w', '1M': 'm',
        '3M': 'q', '12M': 'y',
    };

    // 复制自 chart_idx_macd_backend.js 的 smartSearch，避免跨文件耦合
    function smartSearch(times, target, intervalStr) {
        if (target === undefined || target === null || isNaN(target)) return -1;
        if (!times || times.length === 0) return -1;

        const isSeconds = target < 10000000000;
        let tolerance = isSeconds ? 3600 : 3600000;
        const ivs = String(intervalStr || '').toLowerCase();
        if (ivs.includes('w')) tolerance = isSeconds ? 432000 : 432000000;
        else if (ivs.includes('d') || ivs === '1440') tolerance = isSeconds ? 172800 : 172800000;

        let left = 0, right = times.length - 1, idx = -1;
        while (left <= right) {
            const mid = Math.floor((left + right) / 2);
            if (times[mid] >= target) { idx = mid; right = mid - 1; }
            else { left = mid + 1; }
        }

        let bestIdx = -1, minDiff = Infinity;
        if (idx !== -1) {
            const diff = Math.abs(times[idx] - target);
            if (diff <= tolerance && diff < minDiff) { minDiff = diff; bestIdx = idx; }
        }
        const prevIdx = (idx === -1) ? times.length - 1 : idx - 1;
        if (prevIdx >= 0) {
            const diff = Math.abs(times[prevIdx] - target);
            if (diff <= tolerance && diff < minDiff) { minDiff = diff; bestIdx = prevIdx; }
        }
        return bestIdx;
    }

    function findBarsResult(targetCode, targetInterval) {
        const datafeeds = [];
        if (window.GlobalTVDatafeeds && window.GlobalTVDatafeeds.length > 0) {
            for (const df of window.GlobalTVDatafeeds) datafeeds.push(df);
        }
        if (window.tvDatafeed && !datafeeds.includes(window.tvDatafeed)) datafeeds.push(window.tvDatafeed);

        const code = String(targetCode || '').toLowerCase();
        const itv = String(targetInterval || '').toLowerCase();
        const mappings = { 'd': '1d', '1d': 'd', 'w': '1w', '1w': 'w', 'm': '1m', '1m': 'm', '1440': '1d', '240': '4h' };

        for (const df of datafeeds) {
            if (!df || !df._historyProvider || !df._historyProvider.bars_result) continue;
            const barsMap = df._historyProvider.bars_result;
            for (const key of barsMap.keys()) {
                const k = String(key);
                if (!k.toLowerCase().includes(code)) continue;
                let match = false;
                if (k.endsWith(itv)) match = true;
                else if (mappings[itv] && k.endsWith(mappings[itv])) match = true;
                else if (/^\d+$/.test(itv) && k.endsWith(itv + 'm')) match = true;
                if (match) return barsMap.get(key);
            }
        }
        return null;
    }

    /**
     * 在指定区间内统计 MACD 红绿柱信息。
     * @param {number[]} times - bar 时间戳数组
     * @param {number[]} hist  - 对应的 hist 数组 (macd_hist 或 higher_macd_hist)
     * @param {number} startIdx - 区间起始索引（含）
     * @param {number} endIdx   - 区间结束索引（含）
     * @param {object} opts
     *   - excludeLast: boolean，true 时排除区间末尾未收盘的最后一根
     * @returns {object} 统计结果
     */
    function computeStats(times, hist, startIdx, endIdx, opts) {
        opts = opts || {};
        const difArr = opts.difArr || null;
        const deaArr = opts.deaArr || null;
        const result = {
            startIdx, endIdx,
            barCount: 0,
            posArea: 0, negArea: 0, netArea: 0,
            posAreaX2: 0, negAreaX2: 0, netAreaX2: 0,
            difMax: null, difMin: null, deaMax: null, deaMin: null,
            posMax: 0, posMaxTime: null, posMaxIdx: -1,
            negMin: 0, negMinTime: null, negMinIdx: -1,
            segmentCount: 0,
            posSegments: [], // [{startIdx,endIdx,area,peak,peakIdx}]
            negSegments: [],
            excludedLast: false,
        };
        if (!times || !hist || startIdx < 0 || endIdx < startIdx) return result;
        if (endIdx >= times.length) endIdx = times.length - 1;

        let realEnd = endIdx;
        if (opts.excludeLast && realEnd > startIdx) {
            realEnd = realEnd - 1;
            result.excludedLast = true;
            result.endIdx = realEnd;
        }

        // 每根 hist 独立累加面积；同号连续柱归并为段(posSegments/negSegments)
        let curSeg = null; // {startIdx, endIdx, area, peak, peakIdx, sign}

        const flushSeg = () => {
            if (!curSeg) return;
            if (curSeg.sign > 0) result.posSegments.push(curSeg);
            else if (curSeg.sign < 0) result.negSegments.push(curSeg);
            curSeg = null;
        };

        for (let i = startIdx; i <= realEnd; i++) {
            const v = Number(hist[i]);
            if (!isFinite(v)) continue;

            if (difArr) {
                const dv = Number(difArr[i]);
                if (isFinite(dv)) {
                    if (result.difMax === null || dv > result.difMax) result.difMax = dv;
                    if (result.difMin === null || dv < result.difMin) result.difMin = dv;
                }
            }
            if (deaArr) {
                const ev = Number(deaArr[i]);
                if (isFinite(ev)) {
                    if (result.deaMax === null || ev > result.deaMax) result.deaMax = ev;
                    if (result.deaMin === null || ev < result.deaMin) result.deaMin = ev;
                }
            }

            const sign = v > 0 ? 1 : (v < 0 ? -1 : 0);

            // 段切换检测
            if (!curSeg || curSeg.sign !== sign) {
                flushSeg();
                if (sign !== 0) {
                    curSeg = {
                        startIdx: i, endIdx: i,
                        area: 0, peak: v, peakIdx: i, sign: sign,
                    };
                }
            } else {
                curSeg.endIdx = i;
            }

            result.barCount++;
            if (v > 0) {
                result.posArea += v;
                if (v > result.posMax) {
                    result.posMax = v;
                    result.posMaxTime = times[i];
                    result.posMaxIdx = i;
                }
            } else if (v < 0) {
                result.negArea += Math.abs(v);
                if (v < result.negMin) {
                    result.negMin = v;
                    result.negMinTime = times[i];
                    result.negMinIdx = i;
                }
            }

            if (curSeg) {
                curSeg.area += Math.abs(v);
                if (sign > 0 && v > curSeg.peak) { curSeg.peak = v; curSeg.peakIdx = i; }
                if (sign < 0 && v < curSeg.peak) { curSeg.peak = v; curSeg.peakIdx = i; }
            }
        }
        flushSeg();

        result.netArea = result.posArea - result.negArea;
        result.posAreaX2 = result.posArea * 2;
        result.negAreaX2 = result.negArea * 2;
        result.netAreaX2 = result.netArea * 2;
        result.segmentCount = result.posSegments.length + result.negSegments.length;
        return result;
    }

    // 同向峰值绝对值:|max| 与 |min| 取较大者;两者皆无数据返回 null。
    function peakAbs(mx, mn) {
        const a = (mx === null || mx === undefined || isNaN(mx)) ? null : Math.abs(mx);
        const b = (mn === null || mn === undefined || isNaN(mn)) ? null : Math.abs(mn);
        if (a === null && b === null) return null;
        return Math.max(a === null ? 0 : a, b === null ? 0 : b);
    }

    // 由 TV resolution(或已是 frequency)解析「高一级周期」frequency,无对照返回 null。
    // 先 RES_TO_FREQ 把 resolution 转 frequency(大小写敏感),再查 HIGHER_FREQ_MAP。
    function resolveHigherFreq(rawInterval) {
        const r = String(rawInterval || '');
        const freq = RES_TO_FREQ[r] || r; // 已是 frequency 时原样
        return HIGHER_FREQ_MAP[freq] || null;
    }

    // 单根时间戳 → 高一级周期的桶 key。分钟级与后端 _higher_bucket_keys 逐字一致;
    // 日级以上用通用偏移近似(offset 默认 8h)。
    function bucketKeyOf(t, higherFreq, marketOffsetH) {
        if (t === undefined || t === null || isNaN(t)) return null;
        const ts = t < 1e10 ? Math.floor(t) : Math.floor(t / 1000); // 归一到秒
        if (higherFreq === '5m') return Math.floor(ts / 300);
        if (higherFreq === '30m') return Math.floor(ts / 1800);
        const offset = (marketOffsetH == null ? DEFAULT_MARKET_OFFSET_H : marketOffsetH) * 3600;
        const days = Math.floor((ts + offset) / 86400);
        if (higherFreq === 'd') return days;
        if (higherFreq === 'w') return Math.floor((days + 3) / 7); // 1970-01-01 是周四
        const d = new Date(ts * 1000);
        if (higherFreq === 'm') return d.getUTCFullYear() * 12 + d.getUTCMonth();
        if (higherFreq === 'y') return d.getUTCFullYear();
        return null;
    }

    // 把 [startIdx,endIdx] 的插值序列按桶 key 归并,每桶取桶末根(同 key 最后一根)的真值。
    function reduceToBuckets(times, arr, startIdx, endIdx, higherFreq, marketOffsetH) {
        const out = [];
        if (!times || !arr) return out;
        let curKey = null, lastIdx = -1;
        for (let i = startIdx; i <= endIdx; i++) {
            const k = bucketKeyOf(times[i], higherFreq, marketOffsetH);
            if (k === null) continue;
            if (curKey === null) { curKey = k; lastIdx = i; continue; }
            if (k !== curKey) {
                out.push({ idx: lastIdx, value: Number(arr[lastIdx]) });
                curKey = k;
            }
            lastIdx = i;
        }
        if (lastIdx >= 0) out.push({ idx: lastIdx, value: Number(arr[lastIdx]) });
        return out;
    }

    // HTF 逐桶统计:先把 hist/dif/dea 还原回高周期桶末真值,再在桶粒度上算面积/柱高/黄白线。
    function computeStatsHTF(times, hHist, hDif, hDea, startIdx, endIdx, higherFreq, marketOffsetH, opts) {
        opts = opts || {};
        const result = {
            bucketCount: 0,
            posArea: 0, negArea: 0, netArea: 0,
            posAreaX2: 0, negAreaX2: 0, netAreaX2: 0,
            posMax: 0, posMaxIdx: -1, negMin: 0, negMinIdx: -1,
            difMax: null, difMin: null, deaMax: null, deaMin: null,
            excludedLast: false,
        };
        if (!times || !hHist) return result;
        if (endIdx >= times.length) endIdx = times.length - 1;

        let histBuckets = reduceToBuckets(times, hHist, startIdx, endIdx, higherFreq, marketOffsetH);
        const difBuckets = hDif ? reduceToBuckets(times, hDif, startIdx, endIdx, higherFreq, marketOffsetH) : [];
        const deaBuckets = hDea ? reduceToBuckets(times, hDea, startIdx, endIdx, higherFreq, marketOffsetH) : [];

        // 排除末桶(未收盘)
        let cut = histBuckets.length;
        if (opts.excludeLast && histBuckets.length > 1) { cut = histBuckets.length - 1; result.excludedLast = true; }

        for (let b = 0; b < cut; b++) {
            const v = histBuckets[b].value;
            if (!isFinite(v)) continue;
            result.bucketCount++;
            if (v > 0) { result.posArea += v; if (v > result.posMax) { result.posMax = v; result.posMaxIdx = histBuckets[b].idx; } }
            else if (v < 0) { result.negArea += Math.abs(v); if (v < result.negMin) { result.negMin = v; result.negMinIdx = histBuckets[b].idx; } }
        }
        for (let b = 0; b < Math.min(cut, difBuckets.length); b++) {
            const dv = difBuckets[b].value;
            if (!isFinite(dv)) continue;
            if (result.difMax === null || dv > result.difMax) result.difMax = dv;
            if (result.difMin === null || dv < result.difMin) result.difMin = dv;
        }
        for (let b = 0; b < Math.min(cut, deaBuckets.length); b++) {
            const ev = deaBuckets[b].value;
            if (!isFinite(ev)) continue;
            if (result.deaMax === null || ev > result.deaMax) result.deaMax = ev;
            if (result.deaMin === null || ev < result.deaMin) result.deaMin = ev;
        }
        result.netArea = result.posArea - result.negArea;
        result.posAreaX2 = result.posArea * 2;
        result.negAreaX2 = result.negArea * 2;
        result.netAreaX2 = result.netArea * 2;
        return result;
    }

    // 区间内每根缠论线段(xds)的斜率。time 经 smartSearch 对齐到 bar 索引,
    // 斜率 =(终点price−起点price)/(终点idx−起点idx)。只返回与 [startIdx,endIdx] 重叠的线段。
    function computeSegmentSlopes(times, xds, startIdx, endIdx) {
        const out = [];
        if (!times || !Array.isArray(xds)) return out;
        for (const seg of xds) {
            const pts = seg && seg.points;
            if (!pts || pts.length < 2) continue;
            const inSec = times.length > 0 && times[times.length - 1] < 1e10;
            const norm = (t) => inSec ? (t > 1e10 ? Math.floor(t / 1000) : t) : (t < 1e10 ? t * 1000 : t);
            const sIdx = smartSearch(times, norm(pts[0].time), '');
            const eIdx = smartSearch(times, norm(pts[1].time), '');
            if (sIdx < 0 || eIdx < 0 || eIdx === sIdx) continue;
            const lo = Math.min(sIdx, eIdx), hi = Math.max(sIdx, eIdx);
            if (hi < startIdx || lo > endIdx) continue; // 与区间无重叠
            const slope = (pts[1].price - pts[0].price) / (eIdx - sIdx);
            out.push({
                startTime: pts[0].time, endTime: pts[1].time,
                startPrice: pts[0].price, endPrice: pts[1].price,
                startIdx: sIdx, endIdx: eIdx,
                slope, dir: slope >= 0 ? 'up' : 'down',
            });
        }
        return out;
    }

    // 每个 ChartManager 实例独立持有一个控制器
    class MacdStatsController {
        constructor(chartManager) {
            this.cm = chartManager;
            this.startTime = null;
            this.endTime = null;
            this.startTimeRaw = null; // 原始单位（秒/毫秒），用于 createShape
            this.endTimeRaw = null;
            this.pickMode = null; // null | 'start' | 'end'
            this.shapeIds = []; // 当前绘制的标记 ids
            this.contextMenuUnsub = null;
            this._toolbarBtn = null;
            this._snapshots = []; // 历史区间快照
            this._dragMoveHandler = null; // document mousemove handler，dispose 时移除
            this._dragUpHandler = null;   // document mouseup handler，dispose 时移除
        }

        // 注入工具栏按钮（点击进入"取点模式"）
        attachToolbarButton() {
            if (!this.cm.widget || typeof this.cm.widget.headerReady !== 'function') return;
            this.cm.widget.headerReady().then(() => {
                try {
                    const btn = this.cm.widget.createButton({ align: 'right', useTradingViewStyle: false });
                    if (!btn) return;
                    btn.setAttribute('title', 'MACD 区间统计：点击后依次单击两根 K 线');
                    btn.style.cssText = 'cursor:pointer;padding:0 10px;color:#FFD54F;font-weight:bold;';
                    btn.innerHTML = '📊 MACD 区间';
                    btn.addEventListener('click', () => this.beginPickRange());
                    this._toolbarBtn = btn;
                } catch (e) {
                    console.warn('[MacdStats] createButton failed', e);
                }
            }).catch(e => console.warn('[MacdStats] headerReady failed', e));
        }

        // 注入右键菜单
        attachContextMenu() {
            if (!this.cm.chart || typeof this.cm.chart.onContextMenu !== 'function') return;
            try {
                this.cm.chart.onContextMenu((unixTime, price) => {
                    return [
                        { position: 'top', text: '-' },
                        {
                            position: 'top',
                            text: '📊 MACD: 设为统计起点',
                            click: () => this.setStartTime(unixTime),
                        },
                        {
                            position: 'top',
                            text: '📊 MACD: 设为统计终点并计算',
                            click: () => this.setEndTime(unixTime, true),
                        },
                        {
                            position: 'top',
                            text: '📊 MACD: 清除区间',
                            click: () => this.clearRange(),
                        },
                    ];
                });
            } catch (e) {
                console.warn('[MacdStats] onContextMenu failed', e);
            }
        }

        beginPickRange() {
            this.startTime = null;
            this.endTime = null;
            this.startTimeRaw = null;
            this.endTimeRaw = null;
            this.pickMode = 'start';
            this._showToast('请单击图表第一根 K 线作为【起点】(可按 ESC 取消)');
            this._installCrosshairPicker();
        }

        _installCrosshairPicker() {
            if (!this.cm.chart || !this.cm.widget) {
                console.warn('[MacdStats] chart/widget not ready');
                return;
            }

            // 清理上一次未完成的 picker
            if (this._pickerCleanup) {
                try { this._pickerCleanup(); } catch (e) { /* ignore */ }
                this._pickerCleanup = null;
            }

            let lastTime = null;
            const installedAt = Date.now();

            // 订阅 crosshair 移动，获取鼠标悬停的 K 线时间
            let crosshairSub = null;
            const crosshairHandler = (params) => {
                if (params && params.time !== undefined && params.time !== null) {
                    lastTime = params.time;
                }
            };
            try {
                if (typeof this.cm.chart.crossHairMoved === 'function') {
                    crosshairSub = this.cm.chart.crossHairMoved();
                    if (crosshairSub && typeof crosshairSub.subscribe === 'function') {
                        crosshairSub.subscribe(null, crosshairHandler);
                    }
                }
            } catch (e) {
                console.warn('[MacdStats] subscribe crossHairMoved failed', e);
            }

            // 用 TV 官方的 widget.subscribe('mouse_down') 事件
            // 这是 TV 提供的标准 API，专门用于监听图表内的鼠标按下，不会被 canvas 吞掉
            const onMouseDown = (params) => {
                // 防止按钮点击的同一次事件被立即捕获
                if (Date.now() - installedAt < 250) return;

                let t = lastTime;
                // 兜底：取可见区间右端
                if (t === null || t === undefined) {
                    try {
                        const range = this.cm.chart.getVisibleRange && this.cm.chart.getVisibleRange();
                        if (range && range.to) t = range.to;
                    } catch (e) { /* ignore */ }
                }
                if (t === null || t === undefined) {
                    this._showToast('未能识别 K 线位置，请先在 K 线上方移动鼠标');
                    return;
                }

                if (this.pickMode === 'start') {
                    this.setStartTime(t);
                    this.pickMode = 'end';
                    this._showToast('已设起点，请单击【终点】K 线');
                } else if (this.pickMode === 'end') {
                    this.setEndTime(t, true);
                    this.pickMode = null;
                    cleanup();
                }
            };

            const onKey = (ev) => {
                if (ev.key === 'Escape') {
                    this._showToast('已取消区间选择');
                    this.pickMode = null;
                    cleanup();
                }
            };

            // 注意：TV widget.subscribe 返回 undefined，不返回订阅对象，
            // 取消订阅要用 widget.unsubscribe(eventName, handler)
            try {
                this.cm.widget.subscribe('mouse_down', onMouseDown);
            } catch (e) {
                console.warn('[MacdStats] widget.subscribe(mouse_down) failed', e);
                this._showToast('当前 TV 版本不支持 mouse_down 事件，请改用右键菜单选区间');
                return;
            }

            const cleanup = () => {
                try { this.cm.widget.unsubscribe('mouse_down', onMouseDown); } catch (e) { /* ignore */ }
                document.removeEventListener('keydown', onKey, true);
                if (crosshairSub && typeof crosshairSub.unsubscribe === 'function') {
                    try { crosshairSub.unsubscribe(null, crosshairHandler); } catch (e) { /* ignore */ }
                }
                this._pickerCleanup = null;
            };

            this._pickerCleanup = cleanup;
            document.addEventListener('keydown', onKey, true);
        }

        setStartTime(t) {
            this.startTimeRaw = t;
            this.startTime = t;
            this._showToast(`起点已设：${this._fmtTime(t)}`);
        }

        setEndTime(t, autoCompute) {
            this.endTimeRaw = t;
            this.endTime = t;
            this._showToast(`终点已设：${this._fmtTime(t)}`);
            if (autoCompute) this.computeAndRender();
        }

        clearRange() {
            this.startTime = this.endTime = null;
            this.startTimeRaw = this.endTimeRaw = null;
            this.pickMode = null;
            this._removeShapes();
            this._hidePanel();
        }

        // 核心：计算并渲染
        computeAndRender() {
            if (this.startTime === null || this.endTime === null) {
                this._showToast('请先选择起点和终点');
                return;
            }
            const symbolInterval = this.cm.widget && this.cm.widget.symbolInterval && this.cm.widget.symbolInterval();
            if (!symbolInterval) return;

            const code = String(symbolInterval.symbol || '').toLowerCase();
            const interval = String(symbolInterval.interval || '').toLowerCase();
            const barsResult = findBarsResult(code, interval);
            if (!barsResult || !barsResult.times || barsResult.times.length === 0) {
                console.warn('[MacdStats] findBarsResult failed', { code, interval });
                this._showToast('未找到当前图表数据，请稍后重试');
                return;
            }

            // 时间单位对齐
            const times = barsResult.times;
            const dataInSec = times[times.length - 1] < 10000000000;
            let s = this.startTime, e = this.endTime;
            if (dataInSec && s > 10000000000) s = Math.floor(s / 1000);
            if (dataInSec && e > 10000000000) e = Math.floor(e / 1000);
            // 如果 startTime 是毫秒、数据是秒，反之亦然，统一对齐
            if (!dataInSec && s < 10000000000) s = s * 1000;
            if (!dataInSec && e < 10000000000) e = e * 1000;
            if (s > e) { const tmp = s; s = e; e = tmp; }

            const firstT = times[0];
            const lastT = times[times.length - 1];

            // 夹紧到 K 线数据范围内：用户可能点在右侧未来空白区或左侧
            let startIdx = smartSearch(times, s, interval);
            let endIdx = smartSearch(times, e, interval);

            // 兜底：超出右边界 → 夹到最后一根
            if (startIdx === -1) {
                if (s > lastT) startIdx = times.length - 1;
                else if (s < firstT) startIdx = 0;
            }
            if (endIdx === -1) {
                if (e > lastT) endIdx = times.length - 1;
                else if (e < firstT) endIdx = 0;
            }

            if (startIdx === -1 || endIdx === -1) {
                console.warn('[MacdStats] smartSearch failed even after clamp', { s, e, firstT, lastT });
                this._showToast('时间无法对齐到 K 线，请重新选择');
                return;
            }
            if (startIdx > endIdx) { const tmp = startIdx; startIdx = endIdx; endIdx = tmp; }
            if (startIdx === endIdx) {
                this._showToast('起点和终点是同一根 K 线，请重新选择');
                return;
            }

            const hasHigher = barsResult.higher_macd_hist
                && barsResult.higher_macd_hist.length > 0
                && !barsResult.higher_macd_hist.every(v => isNaN(v) || v === null);

            const rawInterval = String(symbolInterval.interval || ''); // 不 lowercase,保 1D/1W/1M
            const higherFreq = resolveHigherFreq(rawInterval);

            // 本周期:逐根,带黄白线极值
            const statsLocal = computeStats(times, barsResult.macd_hist || [], startIdx, endIdx, {
                excludeLast: true,
                difArr: barsResult.macd_dif || null,
                deaArr: barsResult.macd_dea || null,
            });
            // 跨周期:还原高周期桶取真值(取代失效的 htfDedup)
            const statsHtf = (hasHigher && higherFreq) ? computeStatsHTF(
                times,
                barsResult.higher_macd_hist,
                barsResult.higher_macd_dif || null,
                barsResult.higher_macd_dea || null,
                startIdx, endIdx, higherFreq, DEFAULT_MARKET_OFFSET_H,
                { excludeLast: true },
            ) : null;
            // 区间内缠论线段斜率
            const slopes = computeSegmentSlopes(times, barsResult.xds || [], startIdx, endIdx);

            // 绘制区间背景
            this.cm.markDrawingMutationStart('macd-stats');
            try {
                this._removeShapes();
            } finally {
                this.cm.markDrawingMutationEnd('macd-stats');
            }

            // 渲染面板
            this._renderPanel({
                code, interval,
                startTime: times[startIdx], endTime: times[endIdx],
                barCount: endIdx - startIdx + 1,
                statsLocal, statsHtf, hasHigher, slopes, higherFreq,
            });
        }

        // -------------- 绘图 --------------
        _drawRangeRect(times, startIdx, endIdx) {
            try {
                const t1 = times[startIdx];
                const t2 = times[endIdx];
                // 用 hline + vline 模拟区间高亮（rectangle 需要价格坐标，MACD 面板没有可靠的价格范围）
                // 改为在主图上画两条垂直线
                if (typeof this.cm.chart.createMultipointShape === 'function') {
                    const id1 = this.cm.chart.createMultipointShape(
                        [{ time: t1 }],
                        {
                            shape: 'vertical_line', lock: true, disableSelection: true,
                            disableSave: true, disableUndo: true, showInObjectsTree: false,
                            overrides: { linecolor: COLOR_RANGE_BG, linewidth: 2, linestyle: 2 },
                        }
                    );
                    const id2 = this.cm.chart.createMultipointShape(
                        [{ time: t2 }],
                        {
                            shape: 'vertical_line', lock: true, disableSelection: true,
                            disableSave: true, disableUndo: true, showInObjectsTree: false,
                            overrides: { linecolor: COLOR_RANGE_BG, linewidth: 2, linestyle: 2 },
                        }
                    );
                    if (id1) this.shapeIds.push(id1);
                    if (id2) this.shapeIds.push(id2);
                }
            } catch (e) { console.warn('[MacdStats] draw range failed', e); }
        }

        _drawMarkers(times, statsLocal, statsHtf) {
            const place = (idx, text, up) => {
                if (idx < 0 || idx >= times.length) return;
                try {
                    const id = this.cm.chart.createShape(
                        { time: times[idx] },
                        {
                            shape: up ? 'arrow_down' : 'arrow_up',
                            text: text,
                            lock: true, disableSelection: true, disableSave: true, disableUndo: true,
                            showInObjectsTree: false,
                            overrides: {
                                color: up ? COLOR_POS : COLOR_NEG,
                                backgroundColor: up ? COLOR_POS : COLOR_NEG,
                                fontsize: 11, transparency: 30,
                            },
                        }
                    );
                    if (id) this.shapeIds.push(id);
                } catch (e) { /* ignore */ }
            };
            // 仅在主图标记，副图（MACD pane）不易通过公开 API 精准定位
            if (statsLocal.posMaxIdx >= 0) place(statsLocal.posMaxIdx, `MACD红峰 ${statsLocal.posMax.toFixed(4)}`, true);
            if (statsLocal.negMinIdx >= 0) place(statsLocal.negMinIdx, `MACD绿谷 ${statsLocal.negMin.toFixed(4)}`, false);
            if (statsHtf) {
                if (statsHtf.posMaxIdx >= 0) place(statsHtf.posMaxIdx, `HTF红峰 ${statsHtf.posMax.toFixed(4)}`, true);
                if (statsHtf.negMinIdx >= 0) place(statsHtf.negMinIdx, `HTF绿谷 ${statsHtf.negMin.toFixed(4)}`, false);
            }
        }

        _removeShapes() {
            if (!this.cm.chart) return;
            for (const id of this.shapeIds) {
                try { this.cm.chart.removeEntity(id); } catch (e) { /* ignore */ }
            }
            this.shapeIds = [];
        }

        // -------------- 面板 UI --------------
        _ensurePanel() {
            let panel = document.getElementById(PANEL_ID);
            if (panel) return panel;
            panel = document.createElement('div');
            panel.id = PANEL_ID;
            panel.style.cssText = [
                'position:fixed', 'top:80px', 'right:20px', 'width:340px', 'max-height:80vh',
                'overflow-y:auto', 'background:#1e222d', 'color:#d1d4dc',
                'border:1px solid #363c4e', 'border-radius:6px',
                'box-shadow:0 4px 16px rgba(0,0,0,0.4)', 'z-index:9999',
                'font-size:12px', 'font-family:Consolas,Monaco,monospace',
                'padding:12px', 'display:none',
            ].join(';');
            document.body.appendChild(panel);
            // 拖拽支持
            this._makeDraggable(panel);
            return panel;
        }

        _makeDraggable(panel) {
            let isDown = false, ox = 0, oy = 0;
            panel.addEventListener('mousedown', (ev) => {
                if (ev.target.closest('.macd-stats-no-drag')) return;
                isDown = true;
                ox = ev.clientX - panel.offsetLeft;
                oy = ev.clientY - panel.offsetTop;
            });
            // mousemove/mouseup 挂在 document 上：handler 存到 this，dispose 时移除。
            // 否则 panel 每次创建/销毁都会让旧 handler 堆积（连带泄漏旧 panel 元素）。
            this._dragMoveHandler = (ev) => {
                if (!isDown) return;
                panel.style.left = (ev.clientX - ox) + 'px';
                panel.style.top = (ev.clientY - oy) + 'px';
                panel.style.right = 'auto';
            };
            this._dragUpHandler = () => { isDown = false; };
            document.addEventListener('mousemove', this._dragMoveHandler);
            document.addEventListener('mouseup', this._dragUpHandler);
        }

        _hidePanel() {
            const p = document.getElementById(PANEL_ID);
            if (p) p.style.display = 'none';
        }

        _renderPanel(payload) {
            const panel = this._ensurePanel();
            panel.style.display = 'block';

            const fmt = (n) => (n === null || n === undefined || isNaN(n)) ? '-' : Number(n).toFixed(4);
            const fmtPeak = (mx, mn) => fmt(peakAbs(mx, mn)); // 同向峰值绝对值,无数据 → "-"
            const renderBlock = (title, s, note) => {
                if (!s) return `<div style="opacity:.6;margin:6px 0;">[${title}] 无数据</div>`;
                const cnt = (s.bucketCount !== undefined) ? `桶数 ${s.bucketCount}` : `柱数 ${s.barCount}`;
                return `
                <div style="margin:8px 0;padding:8px;background:#262b3a;border-radius:4px;">
                  <div style="font-weight:bold;color:#FFD54F;margin-bottom:6px;">${title}<span style="font-weight:normal;color:#9aa3b8;font-size:11px;"> ${note || ''}</span></div>
                  <div>📊 ${cnt} ${s.excludedLast ? '<span style="color:#888">(已排除末根)</span>' : ''}</div>
                  <div style="color:${COLOR_POS}">🔴 红柱面积 <b>${fmt(s.posArea)}</b> (×2 <b>${fmt(s.posAreaX2)}</b>) | 峰 <b>${fmt(s.posMax)}</b></div>
                  <div style="color:${COLOR_NEG}">🟢 绿柱面积 <b>${fmt(s.negArea)}</b> (×2 <b>${fmt(s.negAreaX2)}</b>) | 谷 <b>${fmt(s.negMin)}</b></div>
                  <div>⚖️ 净面积 <b style="color:${s.netArea >= 0 ? COLOR_POS : COLOR_NEG}">${fmt(s.netArea)}</b> (×2 <b>${fmt(s.netAreaX2)}</b>)</div>
                  <div>📈 黄白线 DIF峰 <b>${fmtPeak(s.difMax, s.difMin)}</b> | DEA峰 <b>${fmtPeak(s.deaMax, s.deaMin)}</b></div>
                </div>`;
            };
            const renderSlopes = (slopes) => {
                if (!slopes || slopes.length === 0) return '<div style="opacity:.5;font-size:11px;">区间内无完整线段</div>';
                const rows = slopes.map(sl => `
                  <div style="color:${sl.dir === 'up' ? COLOR_POS : COLOR_NEG};font-size:11px;">
                    ${sl.dir === 'up' ? '↑' : '↓'} ${this._fmtTime(sl.startTime)} → ${this._fmtTime(sl.endTime)} 斜率 <b>${fmt(sl.slope)}</b>
                  </div>`).join('');
                return `<div style="margin:8px 0;padding:8px;background:#262b3a;border-radius:4px;">
                  <div style="font-weight:bold;color:#FFD54F;margin-bottom:6px;">线段斜率 (xds)</div>${rows}</div>`;
            };

            panel.innerHTML = `
              <div class="macd-stats-no-drag" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;cursor:default;">
                <div style="font-weight:bold;color:#fff;font-size:13px;">📊 MACD 区间统计</div>
                <div>
                  <button class="macd-stats-no-drag" data-act="snapshot" style="background:#2962FF;border:0;color:#fff;padding:2px 8px;border-radius:3px;cursor:pointer;margin-right:4px;">快照</button>
                  <button class="macd-stats-no-drag" data-act="close" style="background:#444;border:0;color:#fff;padding:2px 8px;border-radius:3px;cursor:pointer;">×</button>
                </div>
              </div>
              <div style="font-size:11px;color:#9aa3b8;margin-bottom:6px;">
                ${payload.code} · ${payload.interval} · ${payload.barCount} 根 K 线<br>
                ${this._fmtTime(payload.startTime)} → ${this._fmtTime(payload.endTime)}
              </div>
              ${renderBlock('MACD (本周期)', payload.statsLocal, '×2 口径')}
              ${(payload.hasHigher && payload.higherFreq) ? renderBlock('MACD_HTF (跨周期)', payload.statsHtf, (payload.higherFreq && (payload.higherFreq === '5m' || payload.higherFreq === '30m')) ? '×1 口径 · 桶粒度' : '×1 口径 · 桶粒度(日级近似)') : '<div style="opacity:.5;font-size:11px;">未启用 MACD_HTF 跨周期数据</div>'}
              ${renderSlopes(payload.slopes)}
              ${this._renderSnapshots()}
            `;

            panel.querySelector('[data-act="close"]').addEventListener('click', () => this.clearRange());
            panel.querySelector('[data-act="snapshot"]').addEventListener('click', () => {
                // 快照保留本级别+高级别的面积/柱高/黄白线极值,供跨区间对比
                const pick = (s) => s ? {
                    posArea: s.posArea, negArea: s.negArea, netArea: s.netArea,
                    posMax: s.posMax, negMin: s.negMin,
                    difMax: s.difMax, difMin: s.difMin, deaMax: s.deaMax, deaMin: s.deaMin,
                } : null;
                this._snapshots.unshift({
                    label: `${payload.interval} ${this._fmtTime(payload.startTime)} ~ ${this._fmtTime(payload.endTime)}`,
                    local: pick(payload.statsLocal),
                    htf: (payload.hasHigher && payload.higherFreq) ? pick(payload.statsHtf) : null,
                });
                if (this._snapshots.length > 5) this._snapshots.length = 5;
                this._renderPanel(payload);
            });
        }

        _renderSnapshots() {
            if (!this._snapshots || this._snapshots.length === 0) return '';
            const fmt = (n) => (n === null || n === undefined || isNaN(n)) ? '-' : Number(n).toFixed(4);
            // 一行展示某级别(本级/高级)的 面积红/绿/净 · 柱高红/绿 · 黄白线DIF/DEA
            const line = (tag, st) => {
                if (!st) return `<div style="opacity:.5;">${tag}: 无数据</div>`;
                const net = st.netArea >= 0 ? COLOR_POS : COLOR_NEG;
                return `<div>${tag} 面积 `
                    + `<span style="color:${COLOR_POS}">${fmt(st.posArea)}</span>/`
                    + `<span style="color:${COLOR_NEG}">${fmt(st.negArea)}</span>/`
                    + `<span style="color:${net}">${fmt(st.netArea)}</span>`
                    + ` · 柱 <span style="color:${COLOR_POS}">${fmt(st.posMax)}</span>/`
                    + `<span style="color:${COLOR_NEG}">${fmt(st.negMin)}</span>`
                    + ` · 线 ${fmt(peakAbs(st.difMax, st.difMin))}/${fmt(peakAbs(st.deaMax, st.deaMin))}</div>`;
            };
            const blocks = this._snapshots.map((s, i) => `
              <div style="margin:4px 0;padding:6px;background:#2a2f3e;border-radius:4px;font-size:11px;">
                <div style="color:#9aa3b8;margin-bottom:3px;"><b>#${i + 1}</b> ${s.label}</div>
                ${line('本级', s.local)}
                ${line('高级', s.htf)}
              </div>
            `).join('');
            return `
              <div style="margin-top:10px;">
                <div style="font-weight:bold;color:#FFD54F;margin-bottom:4px;">历史快照</div>
                <div style="font-size:10px;color:#9aa3b8;margin-bottom:4px;">每条:面积 红/绿/净 · 柱高 红/绿 · 黄白线 DIF/DEA</div>
                ${blocks}
              </div>`;
        }

        // -------------- 工具 --------------
        _fmtTime(t) {
            if (t === null || t === undefined || isNaN(t)) return '-';
            const ms = t < 10000000000 ? t * 1000 : t;
            const d = new Date(ms);
            const pad = (n) => String(n).padStart(2, '0');
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }

        _showToast(msg) {
            let toast = document.getElementById('macd-stats-toast');
            if (!toast) {
                toast = document.createElement('div');
                toast.id = 'macd-stats-toast';
                toast.style.cssText = [
                    'position:fixed', 'top:60px', 'left:50%', 'transform:translateX(-50%)',
                    'background:rgba(41,98,255,0.95)', 'color:#fff', 'padding:8px 16px',
                    'border-radius:4px', 'z-index:10000', 'font-size:12px',
                    'transition:opacity .3s', 'pointer-events:none',
                ].join(';');
                document.body.appendChild(toast);
            }
            toast.textContent = msg;
            toast.style.opacity = '1';
            clearTimeout(this._toastTimer);
            this._toastTimer = setTimeout(() => { toast.style.opacity = '0'; }, 2200);
        }

        dispose() {
            this._removeShapes();
            this._hidePanel();
            if (this._dragMoveHandler) {
                document.removeEventListener('mousemove', this._dragMoveHandler);
                this._dragMoveHandler = null;
            }
            if (this._dragUpHandler) {
                document.removeEventListener('mouseup', this._dragUpHandler);
                this._dragUpHandler = null;
            }
            const panel = document.getElementById(PANEL_ID);
            if (panel) panel.remove();
        }
    }

    return {
        attach(chartManager) {
            if (!chartManager) return null;
            if (chartManager._macdStats) return chartManager._macdStats;
            const ctrl = new MacdStatsController(chartManager);
            chartManager._macdStats = ctrl;
            ctrl.attachToolbarButton();
            ctrl.attachContextMenu();
            return ctrl;
        },
        // 便于调试
        _internal: { computeStats, smartSearch, findBarsResult, bucketKeyOf, reduceToBuckets, computeStatsHTF, computeSegmentSlopes, resolveHigherFreq, peakAbs },
    };
})();

if (typeof window !== 'undefined') {
    window.MacdStats = MacdStats;
}
// Node 单测入口(浏览器下 module 未定义,守卫跳过)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = MacdStats;
}