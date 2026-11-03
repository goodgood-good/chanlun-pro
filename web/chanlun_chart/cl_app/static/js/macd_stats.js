// MACD 区间统计入口。交互与图中指标数值统计由 macd_interval_analysis.js 提供。
// 下方保留已有纯函数调用接口，显示统计不再按近似时区反推 HTF 桶。
// 依赖: charts.js (ChartManager / ChanlunTVRegistry)

var MacdStats = (function () {

    const PANEL_ID = 'macd-stats-panel';
    const RANGE_SHAPE_TAG = 'macd-stats-range';
    const MARKER_SHAPE_TAG = 'macd-stats-marker';

    const COLOR_POS = globalThis.ChartColors?.palette.up || '#ef5350'; // 红柱
    const COLOR_NEG = globalThis.ChartColors?.palette.down || '#26a69a'; // 绿柱
    const COLOR_RANGE_BG = '#FFD54F';

    // 复刻后端 HIGHER_FREQ_MAP(chart_compute.py:85);仅取本工具会用到的层级。
    const HIGHER_FREQ_MAP = { '1m': '5m', '5m': '30m', '30m': 'd', 'd': 'w', 'w': 'm', 'm': 'y' };
    const DEFAULT_MARKET_OFFSET_H = 8; // 日级以上分桶的通用偏移(近似)

    // TV resolution → 项目 frequency(复刻 constants.py resolution_maps,大小写敏感)
    const RES_TO_FREQ = {
        '10S': '10s', '30S': '30s', '1': '1m', '2': '2m', '3': '3m', '5': '5m',
        '10': '10m', '15': '15m', '30': '30m', '60': '60m', '120': '120m',
        '180': '3h', '240': '4h', '360': '6h', '480': '8h', '720': '12h',
        '1D': 'd', '2D': '2d', '3D': '3d', '1W': 'w', '1M': 'm',
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

    function intervalKeyMatch(k, code, itv, mappings) {
        // C14: bars_result key = ticker + resolution 无分隔。原 endsWith 会让 5m 图
        // ("...5") 命中 15m 条目("...15".endsWith("5")=true), 显示错周期 MACD。改为提取
        // ticker(code) 之后的 resolution 段做全等比较(全等失败宁可 miss 也不错配)。
        const kl = String(k).toLowerCase();
        const idx = kl.indexOf(code);
        if (idx < 0) return false;
        const suffix = kl.slice(idx + code.length);
        if (suffix === itv) return true;
        if (mappings[itv] && suffix === mappings[itv]) return true;
        if (/^\d+$/.test(itv) && suffix === itv + 'm') return true;
        return false;
    }

    function findBarsResult(targetCode, targetInterval) {
        const datafeeds = [];
        const registry = window.ChanlunTVRegistry;
        if (registry && registry.datafeeds instanceof Map) {
            for (const df of registry.datafeeds.values()) datafeeds.push(df);
        }

        const code = String(targetCode || '').toLowerCase();
        const itv = String(targetInterval || '').toLowerCase();
        const mappings = {
            'd': '1d', '1d': 'd', 'w': '1w', '1w': 'w', 'm': '1m', '1m': 'm',
            '1440': '1d', '180': '3h', '3h': '180', '240': '4h', '4h': '240',
            '360': '6h', '6h': '360', '480': '8h', '8h': '480',
            '720': '12h', '12h': '720', '3d': '3D',
        };

        for (const df of datafeeds) {
            if (!df || !df._historyProvider || !df._historyProvider.bars_result) continue;
            const barsMap = df._historyProvider.bars_result;
            for (const key of barsMap.keys()) {
                const k = String(key);
                if (!k.toLowerCase().includes(code)) continue;
                if (intervalKeyMatch(k, code, itv, mappings)) return barsMap.get(key);
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
    const intervalAnalysis = (typeof module !== 'undefined' && module.exports)
        ? require('./macd_interval_analysis.js') : globalThis.MacdIntervalAnalysis;
    const computeStats = intervalAnalysis.computeStats;

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

    return {
        attach: intervalAnalysis.attach,
        _internal: { computeStats, smartSearch, findBarsResult, intervalKeyMatch, bucketKeyOf,
            reduceToBuckets, computeStatsHTF, computeSegmentSlopes, resolveHigherFreq, peakAbs,
            parseExport: intervalAnalysis.parseExport, rangeIndices: intervalAnalysis.rangeIndices },
    };
})();

if (typeof window !== 'undefined') {
    window.MacdStats = MacdStats;
}
// Node 单测入口(浏览器下 module 未定义,守卫跳过)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = MacdStats;
}
