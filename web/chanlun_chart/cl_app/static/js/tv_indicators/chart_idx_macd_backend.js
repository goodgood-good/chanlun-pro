// MACD_HTF：从 datafeed 缓存中读取后端已算好的跨周期 MACD 数据并在 TV 图上渲染。
// 支持 1m→5m、5m→30m、30m→d 等降频显示。

var TvIdxMACDBackend = (function () {

  function getTVRegistry() {
    return window.ChanlunTVRegistry || null;
  }

  function getPreferredChartContext(rawTicker, rawInterval) {
    const registry = getTVRegistry();
    const preferredDatafeeds = [];
    let preferredWidget = null;
    const normalizedTicker = String(rawTicker || '').toLowerCase();

    if (registry) {
      if (normalizedTicker && registry.widgets instanceof Map) {
        // 收集所有「同标的」widget; 多级别下同标的会有多个不同周期 widget。
        const tickerMatches = [];
        for (const widget of registry.widgets.values()) {
          try {
            if (widget && widget.symbolInterval) {
              const symbolInterval = widget.symbolInterval();
              if (symbolInterval && String(symbolInterval.symbol || '').toLowerCase() === normalizedTicker) {
                tickerMatches.push({ widget: widget, interval: String(symbolInterval.interval == null ? '' : symbolInterval.interval) });
              }
            }
          } catch (e) { }
        }
        // 多级别: 按周期等价挑出本指标 study 自己的那个 widget(选不出/单图→首个,
        // 保持旧行为与 current system 对 context 数字周期的修正); 避免误用别周期 widget 覆盖 interval。
        const ownIdx = pickPreferredWidgetIndex(
          tickerMatches.map(function (m) { return m.interval; }), rawInterval);
        if (ownIdx >= 0) preferredWidget = tickerMatches[ownIdx].widget;
      }

      if (!preferredWidget && registry.activeManagerId && registry.widgets instanceof Map) {
        preferredWidget = registry.widgets.get(registry.activeManagerId) || null;
      }
      if (!preferredWidget && registry.widgets instanceof Map && registry.widgets.size > 0) {
        preferredWidget = registry.widgets.values().next().value || null;
      }

      if (registry.datafeeds instanceof Map) {
        if (preferredWidget && preferredWidget._chanlunManagerId && registry.datafeeds.has(preferredWidget._chanlunManagerId)) {
          preferredDatafeeds.push(registry.datafeeds.get(preferredWidget._chanlunManagerId));
        }
        for (const datafeed of registry.datafeeds.values()) {
          if (datafeed && !preferredDatafeeds.includes(datafeed)) {
            preferredDatafeeds.push(datafeed);
          }
        }
      }
    }

    return { widget: preferredWidget, datafeeds: preferredDatafeeds };
  }

  // 二分查找最近 bar，按周期自适应容差（小时级1h，日级2d，周级5d）
  function smartSearch(times, target, intervalStr) {
    if (target === undefined || target === null || isNaN(target)) return -1;
    const isSeconds = target < 10000000000;
    let tolerance = isSeconds ? 3600 : 3600000;

    if (intervalStr.includes('w')) tolerance = isSeconds ? 432000 : 432000000;
    else if (intervalStr.includes('d') || intervalStr === '1440') tolerance = isSeconds ? 172800 : 172800000;

    let left = 0;
    let right = times.length - 1;
    let idx = -1;

    while (left <= right) {
      const mid = Math.floor((left + right) / 2);
      if (times[mid] >= target) {
        idx = mid;
        right = mid - 1;
      } else {
        left = mid + 1;
      }
    }

    let bestIdx = -1;
    let minDiff = Infinity;

    if (idx !== -1) {
      const diff = Math.abs(times[idx] - target);
      if (diff <= tolerance && diff < minDiff) {
        minDiff = diff;
        bestIdx = idx;
      }
    }

    let prevIdx = (idx === -1) ? times.length - 1 : idx - 1;
    if (prevIdx >= 0) {
      const diff = Math.abs(times[prevIdx] - target);
      if (diff <= tolerance && diff < minDiff) {
        minDiff = diff;
        bestIdx = prevIdx;
      }
    }
    return bestIdx;
  }

  // 判断两个 TV resolution 是否指同一周期(用于多级别下识别本指标自己的 widget)。
  function _resEquiv(a, b) {
    a = String(a == null ? '' : a).toLowerCase();
    b = String(b == null ? '' : b).toLowerCase();
    if (a === b) return true;
    const GROUPS = [
      ['1d', 'd', '1440'],
      ['1w', 'w'],
      ['1m', 'm'],
      ['3m', 'q'],
      ['12m', 'y'],
      ['180', '3h'],
      ['240', '4h'],
      ['360', '6h'],
      ['480', '8h'],
      ['720', '12h'],
    ];
    for (let g = 0; g < GROUPS.length; g++) {
      if (GROUPS[g].indexOf(a) !== -1 && GROUPS[g].indexOf(b) !== -1) return true;
    }
    return false;
  }

  // 从「同标的的多个 widget 周期」里挑出本指标 study 自己那个的下标。
  // 单图/选不出→首个(index 0, 与旧逻辑一致); 空→ -1。
  function pickPreferredWidgetIndex(intervals, rawInterval) {
    if (!intervals || intervals.length === 0) return -1;
    if (intervals.length === 1) return 0;
    for (let i = 0; i < intervals.length; i++) {
      if (_resEquiv(intervals[i], rawInterval)) return i;
    }
    return 0;
  }

  return {
    _internal: { pickPreferredWidgetIndex: pickPreferredWidgetIndex, _resEquiv: _resEquiv },
    idx: function (PineJS) {
      return {
        name: "MACD_HTF",
        metainfo: {
          _metainfoVersion: 54,
          id: "MACD_HTF@tv-basicstudies-1",
          name: "MACD_HTF",
          description: "MACD_HTF",
          shortDescription: "MACD_HTF",
          is_price_study: false,
          isCustomIndicator: true,
          plots: [
            { id: "plot_hist", type: "line", target: "plot_macd_pane" },
            { id: "plot_hist_color", type: "colorer", target: "plot_hist", palette: "paletteHist" },
            { id: "plot_dif", type: "line", target: "plot_macd_pane" },
            { id: "plot_dea", type: "line", target: "plot_macd_pane" },
          ],
          palettes: {
            paletteHist: {
              colors: {
                0: { name: "Color 0" },
                1: { name: "Color 1" },
                2: { name: "Color 2" },
                3: { name: "Color 3" },
              }
            },
          },
          defaults: {
            styles: {
              plot_hist: { linestyle: 0, linewidth: 1, plottype: 5, trackPrice: false, transparency: 0, visible: true },
              plot_dif: { linestyle: 0, linewidth: 1, plottype: 0, trackPrice: false, transparency: 0, visible: true, color: "#2962FF" },
              plot_dea: { linestyle: 0, linewidth: 1, plottype: 0, trackPrice: false, transparency: 0, visible: true, color: "#FF6D00" },
            },
            palettes: {
              paletteHist: {
                colors: {
                  0: { color: "#ef5350", width: 1, style: 1 },
                  1: { color: "#ffcdd2", width: 1, style: 1 },
                  2: { color: "#b2dfdb", width: 1, style: 1 },
                  3: { color: "#26a69a", width: 1, style: 1 },
                }
              }
            },
            inputs: {},
          },
          styles: {
            plot_hist: { title: "Histogram", histogramBase: 0 },
            plot_dif: { title: "MACD", histogramBase: 0 },
            plot_dea: { title: "Signal", histogramBase: 0 },
          },
          inputs: [],
          format: { type: "price", precision: 4 },
        },
        constructor: function () {
          this.init = function (context, inputCallback) { };
          this.main = function (context, inputCallback) {
            this._context = context;
            this._input = inputCallback;

            let v_dif = NaN, v_dea = NaN, v_hist = NaN, prev_hist = 0;

            try {
              const currentTime = context.symbol.time;
              if (currentTime === undefined || currentTime === null || isNaN(currentTime)) {
                return [NaN, 0, NaN, NaN];
              }

              let rawTicker = String(context.symbol.ticker || "").toLowerCase();
              let rawInterval = String(context.symbol.interval || "").toLowerCase();

              const preferredContext = getPreferredChartContext(rawTicker, rawInterval);
              const preferredWidget = preferredContext.widget;

              // TV 自定义指标拿到的 interval 有时是数字字符串，需从 widget.symbolInterval() 修正
              if (preferredWidget) {
                try {
                  if (preferredWidget.symbolInterval) {
                    const symbolInterval = preferredWidget.symbolInterval();
                    const realRes = symbolInterval.interval.toString().toLowerCase();
                    const realSymbol = symbolInterval.symbol.toString().toLowerCase();
                    if (!rawTicker || rawTicker === "") {
                      rawTicker = realSymbol;
                    }
                    if (realRes && realRes !== rawInterval) {
                      if (/^\d+$/.test(rawInterval) && !/^\d+$/.test(realRes)) {
                        rawInterval = realRes;
                      } else if (realRes.includes('d') || realRes.includes('w')) {
                        rawInterval = realRes;
                      }
                    }
                  }
                } catch (e) {
                }
              }

              let datafeeds = preferredContext.datafeeds || [];

              // targetCode 使用完整 ticker（如 a:sh.000001）进行 fuzzy 匹配
              let targetCode = rawTicker;
              let targetInterval = rawInterval;
              const mappings = {
                'd': '1d', '1d': 'd', 'w': '1w', '1w': 'w', 'm': '1m', '1m': 'm',
                '1440': '1d', '180': '3h', '3h': '180', '240': '4h', '4h': '240',
                '360': '6h', '6h': '360', '480': '8h', '8h': '480',
                '720': '12h', '12h': '720', '3d': '3D',
              };
              if (mappings[rawInterval]) targetInterval = mappings[rawInterval];

              let barsResult = null;

              for (const df of datafeeds) {
                if (df._historyProvider && df._historyProvider.bars_result) {
                  const barsMap = df._historyProvider.bars_result;
                  for (const key of barsMap.keys()) {
                    const k = String(key);

                    if (!k.includes(targetCode)) continue;

                    // C14: key = ticker + resolution 无分隔, 原 endsWith 会让 5m 图("...5")
                    // 命中 15m 条目("...15".endsWith("5")=true). 改为提取 targetCode 之后的
                    // resolution 段做全等比较(全等失败宁可 miss 也不错配错周期)。
                    let intervalMatch = false;
                    const suffix = k.slice(k.indexOf(targetCode) + targetCode.length);
                    if (suffix === targetInterval) intervalMatch = true;
                    else if (mappings[targetInterval] && suffix === mappings[targetInterval]) intervalMatch = true;
                    else if (/^\d+$/.test(targetInterval) && suffix === targetInterval + 'm') intervalMatch = true;

                    if (intervalMatch) {
                      barsResult = barsMap.get(k);
                      break;
                    }
                  }
                }
                if (barsResult) break;
              }

              if (barsResult && barsResult.times) {
                // 判断是否有跨周期 MACD 数据
                const hasHigherMacd = barsResult.higher_macd_dif &&
                  barsResult.higher_macd_dif.length > 0 &&
                  !barsResult.higher_macd_dif.every(function (v) { return isNaN(v) || v === null; });

                var src_dif = hasHigherMacd ? barsResult.higher_macd_dif : barsResult.macd_dif;
                var src_dea = hasHigherMacd ? barsResult.higher_macd_dea : barsResult.macd_dea;
                var src_hist = hasHigherMacd ? barsResult.higher_macd_hist : barsResult.macd_hist;

                if (src_dif) {
                  const dataTime = barsResult.times[barsResult.times.length - 1];
                  let searchTime = currentTime;
                  if (dataTime < 10000000000 && searchTime > 10000000000) {
                    searchTime = Math.floor(searchTime / 1000);
                  }

                  const alignedIndex = smartSearch(barsResult.times, searchTime, rawInterval);

                  if (alignedIndex !== -1) {
                    v_dif = Number(src_dif[alignedIndex]);
                    v_dea = Number(src_dea[alignedIndex]);
                    v_hist = Number(src_hist[alignedIndex]);
                    if (alignedIndex > 0) prev_hist = Number(src_hist[alignedIndex - 1]);
                  }
                }
              }

            } catch (e) {
              console.error("[MACD Crash]", e);
            }

            let colorIndex = 0;
            if (!isNaN(v_hist)) {
              if (v_hist >= 0) colorIndex = (v_hist >= prev_hist) ? 0 : 1;
              else colorIndex = (v_hist > prev_hist) ? 2 : 3;
            }
            return [v_hist, colorIndex, v_dif, v_dea];
          };
        },
      };
    },
  };
})();

// Node 单测入口(浏览器下 module 未定义,守卫跳过)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = TvIdxMACDBackend;
}
