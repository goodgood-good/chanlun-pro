// MACD_HTF：从 datafeed 缓存中读取后端已算好的跨周期 MACD 数据并在 TV 图上渲染。
// 支持 1m→5m、5m→30m、30m→d 等降频显示。

var TvIdxMACDBackend = (function () {

  function getTVRegistry() {
    return window.ChanlunTVRegistry || null;
  }

  function getPreferredChartContext(rawTicker) {
    const registry = getTVRegistry();
    const preferredDatafeeds = [];
    let preferredWidget = null;
    const normalizedTicker = String(rawTicker || '').toLowerCase();

    if (registry) {
      if (normalizedTicker && registry.widgets instanceof Map) {
        for (const widget of registry.widgets.values()) {
          try {
            if (widget && widget.symbolInterval) {
              const symbolInterval = widget.symbolInterval();
              if (symbolInterval && String(symbolInterval.symbol || '').toLowerCase() === normalizedTicker) {
                preferredWidget = widget;
                break;
              }
            }
          } catch (e) { }
        }
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

    if (!preferredWidget && window.tvWidget) {
      preferredWidget = window.tvWidget;
    }
    if (window.tvDatafeed && !preferredDatafeeds.includes(window.tvDatafeed)) {
      preferredDatafeeds.push(window.tvDatafeed);
    }
    if (window.GlobalTVDatafeeds && window.GlobalTVDatafeeds.length > 0) {
      for (const datafeed of window.GlobalTVDatafeeds) {
        if (datafeed && !preferredDatafeeds.includes(datafeed)) {
          preferredDatafeeds.push(datafeed);
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

  return {
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

              const preferredContext = getPreferredChartContext(rawTicker);
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
              const mappings = { 'd': '1d', '1d': 'd', 'w': '1w', '1w': 'w', 'm': '1m', '1m': 'm', '1440': '1d', '240': '4h' };
              if (mappings[rawInterval]) targetInterval = mappings[rawInterval];

              let barsResult = null;

              for (const df of datafeeds) {
                if (df._historyProvider && df._historyProvider.bars_result) {
                  const barsMap = df._historyProvider.bars_result;
                  for (const key of barsMap.keys()) {
                    const k = String(key);

                    if (!k.includes(targetCode)) continue;

                    let intervalMatch = false;
                    // 严格后缀
                    if (k.endsWith(targetInterval)) intervalMatch = true;
                    // 映射后缀
                    else if (mappings[targetInterval] && k.endsWith(mappings[targetInterval])) intervalMatch = true;
                    // 补m后缀 (仅当目标是纯数字时)
                    else if (/^\d+$/.test(targetInterval) && k.endsWith(targetInterval + 'm')) intervalMatch = true;

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