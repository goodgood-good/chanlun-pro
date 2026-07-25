(function (global, factory) {
    typeof exports === 'object' && typeof module !== 'undefined' ? factory(exports) :
    typeof define === 'function' && define.amd ? define(['exports'], factory) :
    (global = typeof globalThis !== 'undefined' ? globalThis : global || self, factory(global.Datafeeds = {}));
})(this, (function (exports) { 'use strict';

    /**
     * If you want to enable logs from datafeed set it to `true`
     */
    function logMessage(message) {
    }
    function getErrorMessage(error) {
        if (error === undefined) {
            return '';
        }
        else if (typeof error === 'string') {
            return error;
        }
        return error.message;
    }

    class QuotesProvider {
        constructor(datafeedUrl, requester) {
            this._datafeedUrl = datafeedUrl;
            this._requester = requester;
        }
        getQuotes(symbols) {
            return new Promise((resolve, reject) => {
                this._requester.sendRequest(this._datafeedUrl, 'quotes', { symbols: symbols })
                    .then((response) => {
                    if (response.s === 'ok') {
                        resolve(response.d);
                    }
                    else {
                        reject(response.errmsg);
                    }
                })
                    .catch((error) => {
                    const errorMessage = getErrorMessage(error);
                    reject(`network error: ${errorMessage}`);
                });
            });
        }
    }

    class Requester {
        constructor(headers, timeoutMs = 15_000) {
            if (headers) {
                this._headers = headers;
            }
            this._timeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 15_000;
        }
        sendRequest(datafeedUrl, urlPath, params) {
            if (params !== undefined) {
                const paramKeys = Object.keys(params);
                if (paramKeys.length !== 0) {
                    urlPath += '?';
                }
                urlPath += paramKeys.map((key) => {
                    return `${encodeURIComponent(key)}=${encodeURIComponent(params[key].toString())}`;
                }).join('&');
            }
            // Send user cookies if the URL is on the same origin as the calling script.
            const controller = typeof AbortController === 'undefined' ? undefined : new AbortController();
            const options = { credentials: 'same-origin' };
            if (controller !== undefined) {
                options.signal = controller.signal;
            }
            let timeoutId;
            const timeout = new Promise((_resolve, reject) => {
                timeoutId = setTimeout(() => {
                    controller?.abort();
                    reject(new Error(`Request timed out after ${this._timeoutMs}ms`));
                }, this._timeoutMs);
            });
            if (this._headers !== undefined) {
                options.headers = this._headers;
            }
            // eslint-disable-next-line no-restricted-globals
            return Promise.race([
                fetch(`${datafeedUrl}/${urlPath}`, options)
                    .then((response) => {
                    if (response.ok === false) {
                        throw new Error(`Request failed with HTTP ${response.status}`);
                    }
                    return response.text();
                })
                    .then((responseText) => JSON.parse(responseText)),
                timeout,
            ])
                .finally(() => clearTimeout(timeoutId));
        }
    }

    function calendarResolution(resolution) {
        const value = String(resolution || "");
        if (/^(?:D|1D|2D)$/i.test(value)) {
            return "d";
        }
        if (/^(?:W|1W)$/i.test(value)) {
            return "w";
        }
        if (value === "M" || value === "1M") {
            return "m";
        }
        if (value === "3M") {
            return "q";
        }
        if (value === "12M") {
            return "y";
        }
        return null;
    }
    /**
     * Convert a raw market-close timestamp to the coordinate TradingView expects.
     *
     * The raw timestamp remains available separately for strict-snapshot identity
     * and MACD alignment.  Only the Bar passed to the chart uses this coordinate.
     */
    function chartBarTimeSeconds(sourceTime, resolution) {
        if (!Number.isInteger(sourceTime)) {
            throw new Error("history bar time must be epoch seconds");
        }
        const calendar = calendarResolution(resolution);
        if (calendar === null) {
            return sourceTime;
        }
        const source = new Date(sourceTime * 1000);
        let year = source.getUTCFullYear();
        let month = source.getUTCMonth();
        let day = source.getUTCDate();
        if (calendar === "w") {
            const daysSinceMonday = (source.getUTCDay() + 6) % 7;
            day -= daysSinceMonday;
        }
        else if (calendar === "m") {
            day = 1;
        }
        else if (calendar === "q") {
            month = Math.floor(month / 3) * 3;
            day = 1;
        }
        else if (calendar === "y") {
            month = 0;
            day = 1;
        }
        return Date.UTC(year, month, day) / 1000;
    }

    class HistoryProvider {
        constructor(datafeedUrl, requester, limitedServerResponse, options = {}) {
            this._fullRequestSerial = 0;
            this._latestFullRequestByKey = new Map();
            // H1(阶段E): charts.js 断档 gap-reset 前置此一次性标志; getBars(firstDataRequest) 读到即注入
            // force_refresh=1(用后即清),让后端绕过缓存重算补齐断档。public 供 charts.js 外部置位。
            this._forceRefreshOnce = false;
            this._datafeedUrl = datafeedUrl;
            this._requester = requester;
            this._limitedServerResponse = limitedServerResponse;
            this._options = options;
            this._barsResultMaxSize = options.barsResultMaxSize || 100;
            this.bars_result = new Map();
        }
        /** 把 bars_result 裁到 _barsResultMaxSize 以内，按插入顺序淘汰最老条目。 */
        _pruneBarsResult() {
            while (this.bars_result.size > this._barsResultMaxSize) {
                const oldestKey = this.bars_result.keys().next().value;
                if (oldestKey === undefined) {
                    break;
                }
                this.bars_result.delete(oldestKey);
                this._latestFullRequestByKey.delete(oldestKey);
            }
        }
        _resultKey(symbol, resolution) {
            return String(symbol || "").toLowerCase() + String(resolution || "").toLowerCase();
        }
        _beginFullRequest(requestParams) {
            const resKey = this._resultKey(requestParams["symbol"], requestParams["resolution"]);
            const requestSerial = ++this._fullRequestSerial;
            this._latestFullRequestByKey.set(resKey, requestSerial);
            return requestSerial;
        }
        _fullRequestIsCurrent(requestParams, requestGeneration) {
            if (requestGeneration === undefined) {
                return true;
            }
            const resKey = this._resultKey(requestParams["symbol"], requestParams["resolution"]);
            return this._latestFullRequestByKey.get(resKey) === requestGeneration;
        }
        _resultForCompletedRequest(result, requestParams, requestGeneration) {
            if (this._fullRequestIsCurrent(requestParams, requestGeneration)) {
                return result;
            }
            const resKey = this._resultKey(requestParams["symbol"], requestParams["resolution"]);
            const current = this.bars_result.get(resKey);
            if (current === undefined) {
                return result;
            }
            // A later first request already won.  Return its Bar snapshot to the
            // stale callback too, otherwise TradingView itself can regress even though
            // bars_result was protected from the old response.
            return {
                ...current,
                bars: current.bars.map((bar) => ({ ...bar })),
                meta: { ...current.meta },
            };
        }
        /**
         * TV onResetCacheNeeded 触发时清掉对应 symbol+resolution 的缓存。
         * 由 UDFCompatibleDatafeedBase.subscribeBars 注册的 reset 回调调用。
         */
        _clearBarsResultForSymbolResolution(symbol, resolution) {
            if (!symbol || !resolution) {
                return;
            }
            const resKey = this._resultKey(symbol, resolution);
            this.bars_result.delete(resKey);
            // Deleting the generation invalidates every response that was already in
            // flight.  A later request receives a globally unique serial.
            this._latestFullRequestByKey.delete(resKey);
        }
        /** 通知前端：bars_result[resKey] 已就绪，可以读出来画缠论了。 */
        _emitBarsReady(resKey, requestParams) {
            try {
                window.dispatchEvent(new CustomEvent('chanlun-bars-ready', {
                    detail: {
                        key: resKey,
                        symbol: String(requestParams["symbol"] || '').toLowerCase(),
                        resolution: String(requestParams["resolution"] || '').toLowerCase(),
                        managerId: this._options.managerId || null,
                    }
                }));
            }
            catch (e) { /* SSR 或测试环境无 window 时静默忽略 */ }
        }
        getBars(symbolInfo, resolution, periodParams) {
            const requestParams = {
                symbol: symbolInfo.ticker || "",
                resolution: resolution,
                from: periodParams.from,
                to: periodParams.to,
            };
            if (periodParams.countBack !== undefined) {
                requestParams.countback = periodParams.countBack;
            }
            if (periodParams.firstDataRequest !== undefined) {
                requestParams.firstDataRequest = periodParams.firstDataRequest;
            }
            // H1(阶段E): charts.js 断档 gap-reset 前置 _forceRefreshOnce, 这里(仅 firstDataRequest)注入
            // force_refresh=1 让后端绕过缓存重算补齐断档; 一次性,用后即清。
            if (periodParams.firstDataRequest && this._forceRefreshOnce) {
                requestParams.force_refresh = 1;
                this._forceRefreshOnce = false;
            }
            if (symbolInfo.currency_code !== undefined) {
                requestParams.currencyCode = symbolInfo.currency_code;
            }
            if (symbolInfo.unit_id !== undefined) {
                requestParams.unitId = symbolInfo.unit_id;
            }
            const requestGeneration = periodParams.firstDataRequest
                ? this._beginFullRequest(requestParams)
                : undefined;
            return new Promise(async (resolve, reject) => {
                try {
                    const initialResponse = await this._requester.sendRequest(this._datafeedUrl, "history", requestParams);
                    const result = this._processHistoryResponse(initialResponse, requestParams, requestGeneration);
                    if (this._limitedServerResponse &&
                        this._fullRequestIsCurrent(requestParams, requestGeneration)) {
                        await this._processTruncatedResponse(result, requestParams, requestGeneration);
                    }
                    resolve(this._resultForCompletedRequest(result, requestParams, requestGeneration));
                }
                catch (e) {
                    if (e instanceof Error || typeof e === "string") {
                        const reasonString = getErrorMessage(e);
                        // tslint:disable-next-line:no-console
                        console.warn(`HistoryProvider: getBars() failed, error=${reasonString}`);
                        reject(reasonString);
                    }
                }
            });
        }
        async _processTruncatedResponse(result, requestParams, requestGeneration) {
            let lastResultLength = result.bars.length;
            try {
                while (this._limitedServerResponse &&
                    this._limitedServerResponse.maxResponseLength > 0 &&
                    this._limitedServerResponse.maxResponseLength === lastResultLength &&
                    requestParams.from < requestParams.to) {
                    // adjust request parameters for follow-up request
                    if (requestParams.countback) {
                        requestParams.countback =
                            requestParams.countback - lastResultLength;
                    }
                    if (this._limitedServerResponse.expectedOrder === "earliestFirst") {
                        requestParams.from = Math.round(result.bars[result.bars.length - 1].time / 1000);
                    }
                    else {
                        requestParams.to = Math.round(result.bars[0].time / 1000);
                    }
                    const followupResponse = await this._requester.sendRequest(this._datafeedUrl, "history", requestParams);
                    const followupResult = this._processHistoryResponse(followupResponse, requestParams, requestGeneration);
                    lastResultLength = followupResult.bars.length;
                    // merge result with results collected so far
                    if (this._limitedServerResponse.expectedOrder === "earliestFirst") {
                        if (followupResult.bars[0].time ===
                            result.bars[result.bars.length - 1].time) {
                            // Datafeed shouldn't include a value exactly matching the `to` timestamp but in case it does
                            // we will remove the duplicate.
                            followupResult.bars.shift();
                        }
                        result.bars.push(...followupResult.bars);
                    }
                    else {
                        if (followupResult.bars[followupResult.bars.length - 1].time ===
                            result.bars[0].time) {
                            // Datafeed shouldn't include a value exactly matching the `to` timestamp but in case it does
                            // we will remove the duplicate.
                            followupResult.bars.pop();
                        }
                        result.bars.unshift(...followupResult.bars);
                    }
                }
            }
            catch (e) {
                /**
                 * Error occurred during followup request. We won't reject the original promise
                 * because the initial response was valid so we will return what we've got so far.
                 */
                if (e instanceof Error || typeof e === "string") {
                    const reasonString = getErrorMessage(e);
                    // tslint:disable-next-line:no-console
                    console.warn(`HistoryProvider: getBars() warning during followup request, error=${reasonString}`);
                }
            }
        }
        /**
         * SSE 推送复用入口：与 getBars 走同一份 response→bars_result 合并逻辑
         * (_processHistoryResponse)，保证轮询与推送两条路径行为一致、不漂移。
         * 入参 response 为 /tv/history 同构对象(含 s/update/t/o/h/l/c/缠论字段)。
         */
        applyChanlunUpdate(response, requestParams) {
            return this._processHistoryResponse(response, requestParams);
        }
        _processHistoryResponse(response, requestParams, requestGeneration) {
            if (response.s !== "ok" && response.s !== "no_data") {
                throw new Error(response.errmsg);
            }
            const bars = [];
            const meta = {
                noData: false,
            };
            if (response.s === "no_data") {
                meta.noData = true;
                meta.nextTime = response.nextTime;
            }
            else {
                const volumePresent = response.v !== undefined;
                const ohlPresent = response.o !== undefined;
                const resolution = String(requestParams["resolution"] || "");
                for (let i = 0; i < response.t.length; ++i) {
                    const barValue = {
                        time: chartBarTimeSeconds(response.t[i], resolution) * 1000,
                        close: response.c[i],
                        open: response.c[i],
                        high: response.c[i],
                        low: response.c[i],
                    };
                    if (ohlPresent) {
                        barValue.open = response.o[i];
                        barValue.high = response.h[i];
                        barValue.low = response.l[i];
                    }
                    if (volumePresent) {
                        barValue.volume = response.v[i];
                    }
                    bars.push(barValue);
                }
                // 设置保存的key
                const res_key = this._resultKey(requestParams["symbol"], requestParams["resolution"]);
                // 保存数据
                let obj_res = this.bars_result.get(res_key);
                // 2026-07 修复(前端幽灵形态)：判断本次响应是否为"右侧最新窗口"的权威回答。
                // TradingView 对同一 res_key 的非首次请求(update!=false)只有两种来源：
                //   - 30s 轮询(data-pulse-provider 请求 to≈now，覆盖最新窗口)；
                //   - 向左滚动(backward 请求 to 早于已知最旧/最新K线，补更早历史)。
                // 用请求的 to 是否达到"已知最新K线时间"区分二者——只有前者才能安全地对
                // [from,to] 窗口内、新响应未再提及的已完成形态做"权威删除"(它们已被后端证伪)；
                // 后者若同样删除，会把仍然正确、只是这次响应没提到的右侧现有形态误删。
                //
                // 独立代码审查复核发现两处需要显式收口的边界(均属"信息不足时默认不删"更保守)：
                // ① requestParams.from/to 并非所有调用方都提供——生产唯一的 applyChanlunUpdate
                //    调用点(charts.js 的 SSE onmessage)只传 { symbol, resolution }，from/to 缺失时
                //    Number(undefined)=NaN。若仅用 `=== undefined` 判空，NaN 会绕过短路检查、让
                //    后续比较"恰好"因 NaN 比较恒假而意外安全——这是脆弱的隐式行为，显式用
                //    Number.isFinite 收口，让"信息不足→不删"成为明确意图而非侥幸。
                // ② obj_res 存在但 bars 为空(理论上不应出现的退化态，如异常写入)时，旧逻辑默认
                //    当作权威窗口处理；没有K线证据时更保守的默认是不做窗口删除。
                const existingMaxBarMs = obj_res && obj_res.bars && obj_res.bars.length > 0
                    ? obj_res.bars[obj_res.bars.length - 1].time
                    : undefined;
                const requestTo = Number(requestParams.to);
                const requestFrom = Number(requestParams.from);
                const isRecentWindowAuthoritative = Number.isFinite(requestTo) &&
                    Number.isFinite(requestFrom) &&
                    existingMaxBarMs !== undefined &&
                    requestTo * 1000 >= existingMaxBarMs;
                // 形态的 points[].time 与 requestParams.from/to 同为"秒"单位(后端 fun.datetime_to_int
                // 与 UDF from/to 一致)，此处窗口边界无需 *1000。
                const windowFrom = isRecentWindowAuthoritative ? requestFrom : undefined;
                const windowTo = isRecentWindowAuthoritative ? requestTo : undefined;
                const isSourceTimeInAuthoritativeWindow = (sourceTime) => {
                    if (windowFrom === undefined ||
                        windowTo === undefined ||
                        !Number.isInteger(sourceTime))
                        return false;
                    const chartTime = chartBarTimeSeconds(sourceTime, resolution);
                    return chartTime >= windowFrom && chartTime <= windowTo;
                };
                const raw_times = (response.t || []).map((t) => t * 1000);
                const macd_dif = response.macd_dif || [];
                const macd_dea = response.macd_dea || [];
                const macd_hist = response.macd_hist || [];
                const macd_area = response.macd_area || [];
                const higher_macd_dif = response.higher_macd_dif || [];
                const higher_macd_dea = response.higher_macd_dea || [];
                const higher_macd_hist = response.higher_macd_hist || [];
                const mergeAlignedArrays = (existingTimes = [], existingArr = [], newTimes = [], newArr = []) => {
                    const map = new Map();
                    existingTimes.forEach((t, i) => {
                        let val = existingArr[i];
                        if (val === null || val === undefined)
                            val = NaN;
                        map.set(t, val);
                    });
                    newTimes.forEach((t, i) => {
                        let val = newArr[i];
                        if (val === null || val === undefined)
                            val = NaN;
                        map.set(t, val);
                    });
                    const allTimes = Array.from(new Set([...existingTimes, ...newTimes])).sort((a, b) => a - b);
                    return {
                        times: allTimes,
                        values: allTimes.map(t => {
                            const v = map.get(t);
                            return (v === undefined || v === null) ? NaN : v;
                        })
                    };
                };
                const generationIsCurrent = requestGeneration === undefined ||
                    this._latestFullRequestByKey.get(res_key) === requestGeneration;
                const incomingLastRawMs = raw_times.length > 0
                    ? raw_times[raw_times.length - 1]
                    : undefined;
                const existingRawTimes = obj_res?.times || [];
                const existingLastRawMs = existingRawTimes.length > 0
                    ? existingRawTimes[existingRawTimes.length - 1]
                    : undefined;
                const isRegressiveFullSnapshot = response.update === false &&
                    incomingLastRawMs !== undefined &&
                    existingLastRawMs !== undefined &&
                    incomingLastRawMs < existingLastRawMs;
                const canWriteCache = generationIsCurrent && !isRegressiveFullSnapshot;
                if (canWriteCache && (response.update == false || obj_res == undefined)) {
                    const difObj = mergeAlignedArrays([], [], raw_times, macd_dif);
                    const deaObj = mergeAlignedArrays([], [], raw_times, macd_dea);
                    const histObj = mergeAlignedArrays([], [], raw_times, macd_hist);
                    const areaObj = mergeAlignedArrays([], [], raw_times, macd_area);
                    const hDifObj = mergeAlignedArrays([], [], raw_times, higher_macd_dif);
                    const hDeaObj = mergeAlignedArrays([], [], raw_times, higher_macd_dea);
                    const hHistObj = mergeAlignedArrays([], [], raw_times, higher_macd_hist);
                    this.bars_result.set(res_key, {
                        // TradingView mutates calendar Bar objects after getBars returns.
                        // Keep an independent cache graph so chart-only normalization cannot
                        // corrupt merge keys or strict loaded-range coordinates.
                        bars: bars.map((bar) => ({ ...bar })),
                        meta: meta,
                        times: difObj.times,
                        macd_dif: difObj.values,
                        macd_dea: deaObj.values,
                        macd_hist: histObj.values,
                        macd_area: areaObj.values,
                        higher_macd_dif: hDifObj.values,
                        higher_macd_dea: hDeaObj.values,
                        higher_macd_hist: hHistObj.values,
                        fxs: response.fxs,
                        bis: response.bis,
                        xds: response.xds,
                        bi_zss: response.bi_zss,
                        xd_zss: response.xd_zss,
                        bcs: response.bcs,
                        mmds: response.mmds,
                        bi_mmds: response.bi_mmds || [],
                        xd_mmds: response.xd_mmds || [],
                        bi_bcs: response.bi_bcs || [],
                        xd_bcs: response.xd_bcs || [],
                        xd_zslx: response.xd_zslx || [],
                        xd_zslx_lines: response.xd_zslx_lines || [],
                        recursive_levels: response.recursive_levels || [],
                        higher_zs: response.higher_zs || [],
                        interval_nest: response.interval_nest,
                        strict_structure_mode: response.strict_structure_mode,
                        strict_structure: response.strict_structure,
                        strict_structure_error: response.strict_structure_error,
                        chart_color: response.chart_color,
                    });
                    this._pruneBarsResult();
                    this._emitBarsReady(res_key, requestParams);
                }
                else if (canWriteCache && obj_res !== undefined) {
                    // 更新存在的数据
                    // 更新逻辑，找到大于等于返回的第一个时间的所有数据；
                    // 保留小于返回的第一个时间的所有数据；
                    // 然后添加返回的数据；
                    // 最后按时间排序；
                    // 1. 更新其他数据结构（如分型、笔、线段等）
                    // 处理TextPoint类型数据（fxs, bcs, mmds）
                    // 2026-07 修复(前端幽灵形态，对称补 updateLineSegments 同款窗口删除): 新响应为空
                    // 时原逻辑无条件保留全部旧点位——权威窗口内被证伪撤销的买卖点/背驰/分型会同样
                    // "幽灵"残留(与线段类型此前的漏洞同源)。windowFrom/windowTo 权威时按窗口过滤。
                    const updateTextPoints = (existingPoints, newPoints) => {
                        // 获取点位时间的辅助函数，处理points可能是对象或数组的情况
                        const getPointTime = (point) => {
                            if (Array.isArray(point.points)) {
                                // 如果是数组，取第一个元素的time
                                return point.points[0].time;
                            }
                            else {
                                // 如果是单个对象，直接取time
                                return point.points.time;
                            }
                        };
                        if (!newPoints || newPoints.length === 0) {
                            if (!existingPoints || existingPoints.length === 0)
                                return [];
                            if (windowFrom === undefined || windowTo === undefined)
                                return existingPoints;
                            return existingPoints.filter((p) => {
                                const t = getPointTime(p);
                                return !isSourceTimeInAuthoritativeWindow(t);
                            });
                        }
                        if (!existingPoints || existingPoints.length === 0)
                            return newPoints;
                        // Round11 BUG2 修复(对称 updateLineSegments 的 key-upsert): 原按 minResponseTime 时间切割只保留
                        // 早于新响应最早点的旧点——向左滚动时新响应是更旧窗口(min 小), 右侧最近的分型/买卖点/背驰
                        // (time 大)全不 < min → 被丢弃(与已硬化线段不对称)。改 time+price+text 身份 upsert:
                        // 权威窗口内(右侧最新窗)未被新响应提及的旧点剔除(去幽灵), 向左滚动(window undefined)只增不删。
                        const isTextInAuthWindow = (p) => {
                            if (windowFrom === undefined || windowTo === undefined)
                                return false;
                            const tt = getPointTime(p);
                            return isSourceTimeInAuthoritativeWindow(tt);
                        };
                        const textPointKey = (p) => {
                            const tt = getPointTime(p);
                            const pp = Array.isArray(p.points) ? p.points[0].price : p.points.price;
                            return `${tt}_${pp}_${p.text || ''}`;
                        };
                        const mergedTextByKey = new Map();
                        for (const point of existingPoints) {
                            if (!isTextInAuthWindow(point)) {
                                mergedTextByKey.set(textPointKey(point), point);
                            }
                        }
                        for (const point of newPoints) {
                            mergedTextByKey.set(textPointKey(point), point);
                        }
                        return Array.from(mergedTextByKey.values()).sort((a, b) => getPointTime(a) - getPointTime(b));
                    };
                    // 处理LineSegment类型数据（bis, xds, bi_zss, xd_zss）
                    // 用 key 合并去重：避免按 minResponseTime 切割导致「起点在窗口内、
                    // 终点在窗口外」的跨界线段在 backward 历史响应处理中被永久丢弃。
                    // 未完成线段（linestyle=1）以 起点+linestyle 为 key，让新版本覆盖旧版本。
                    //
                    // 2026-07 修复(前端幽灵形态)：仅靠 key upsert 只增不删——已完成段一旦起点
                    // 被新行情证伪、新响应里不再提及，旧版本会永久残留(后端刚修的同类 bug，
                    // 只是从后端搬到前端)。windowFrom/windowTo 非 undefined 时(右侧最新窗口的
                    // 权威响应，见调用处 isRecentWindowAuthoritative)，落在该窗口内的已有段一律
                    // 先剔除，只有仍被新响应提及(同 key)才会重新加入——真正体现"这段窗口内，
                    // 新响应就是权威真相"。向左滚动(windowFrom/To 为 undefined)时不做此剔除，
                    // 保留原有"只增不删"以免误删右侧现有形态。
                    //
                    // 已知残留风险(独立代码审查复核，概率极低未特意处理): "权威窗口内的响应即真相"
                    // 这一假设要求响应确为基于完整历史的权威重算。后端 tv.py 对绝大多数轮询缺失
                    // (cache_head_gap/cache_tail_gap)都改走 prepend_klines_and_replace_cache 的
                    // 全量重算(见 chart_compute.py 对应注释)，满足此假设；仅当缓存条目存在但完全
                    // 无覆盖(cache_no_coverage，需要一条先前写入的"零K线"退化 entry 才可能出现)时，
                    // 才会退化到窄窗口独立计算，理论上可能因缺少足够上下文而漏掉一根仍然有效的
                    // 已完成形态、被本逻辑误判为已作废并删除。触发条件本身就是不该出现的后端退化态，
                    // 且 SSE 全量快照会在下个周期内自愈，暂不针对性处理。
                    // windowFrom/windowTo 取自外层闭包(同一 else 分支顶部算好的 const，见上方注释)，
                    // 无需作为参数重复传递——6 处调用点共享同一对值。
                    const updateLineSegments = (existingSegments, newSegments) => {
                        const isInAuthoritativeWindow = (s) => {
                            if (windowFrom === undefined || windowTo === undefined)
                                return false;
                            const t = s.points[0] && s.points[0].time;
                            return isSourceTimeInAuthoritativeWindow(t);
                        };
                        if (!newSegments || newSegments.length === 0) {
                            if (!existingSegments || existingSegments.length === 0)
                                return [];
                            if (windowFrom === undefined || windowTo === undefined)
                                return existingSegments;
                            // 权威窗口内、新响应却一根都没提——该窗口内已有段全部作废。
                            return existingSegments.filter((s) => !isInAuthoritativeWindow(s));
                        }
                        if (!existingSegments || existingSegments.length === 0)
                            return newSegments;
                        // 身份 key：仅用起点 (head.time, head.price)。
                        // 一根线段从某个起点出发，任意时刻只该有一个版本——
                        // 端点(tail)随 K 线包含合并会漂移（CLKline.k.date 会变），
                        // linestyle 也会从 1(未完成) 翻成 0(完成)，
                        // 把它们写进 key 会让"同一根段的新旧两版"都被保留 → 视觉上线段重叠/断裂。
                        // 用 head 作为身份，Map.set 让最新版本覆盖旧版本即可。
                        const segmentKey = (s) => {
                            const head = s.points[0];
                            return `${head.time}_${head.price}`;
                        };
                        // 新响应是否带来"未完成段"(linestyle=1)。SSE 全量快照含当前唯一的未完成段;
                        // 向左滚动的历史区间响应则不含。
                        const newHasPending = newSegments.some((s) => s.points && s.points.length > 0 && Number(s.linestyle) === 1);
                        const merged = new Map();
                        for (const segment of existingSegments) {
                            // 丢弃条件(任一成立即丢，交由下面 newSegments 决定是否以新版本重新加入)：
                            // ① 新响应带来当前未完成段时，旧的未完成段一律丢——未完成段起点会随重算
                            //    漂移，旧 head key 不会被新版本覆盖，否则累积"多个未完成笔"；
                            // ② 该段起点落在本次权威窗口内——右侧最新窗口的响应即权威真相，窗口内
                            //    未被新响应提及的已完成段已被证伪，不能只增不删地累积幽灵形态。
                            if (segment.points.length > 0 &&
                                !(newHasPending && Number(segment.linestyle) === 1) &&
                                !isInAuthoritativeWindow(segment)) {
                                merged.set(segmentKey(segment), segment);
                            }
                        }
                        for (const segment of newSegments) {
                            if (segment.points.length > 0) {
                                merged.set(segmentKey(segment), segment);
                            }
                        }
                        return Array.from(merged.values()).sort((a, b) => {
                            if (a.points.length === 0 && b.points.length === 0)
                                return 0;
                            if (a.points.length === 0)
                                return -1;
                            if (b.points.length === 0)
                                return 1;
                            return a.points[0].time - b.points[0].time;
                        });
                    };
                    // 更新所有数据
                    obj_res.fxs = updateTextPoints(obj_res.fxs, response.fxs);
                    obj_res.bis = updateLineSegments(obj_res.bis, response.bis);
                    obj_res.xds = updateLineSegments(obj_res.xds, response.xds);
                    obj_res.bi_zss = updateLineSegments(obj_res.bi_zss, response.bi_zss);
                    obj_res.xd_zss = updateLineSegments(obj_res.xd_zss, response.xd_zss);
                    obj_res.bcs = updateTextPoints(obj_res.bcs, response.bcs);
                    obj_res.mmds = updateTextPoints(obj_res.mmds, response.mmds);
                    obj_res.bi_mmds = updateTextPoints(obj_res.bi_mmds || [], response.bi_mmds || []);
                    obj_res.xd_mmds = updateTextPoints(obj_res.xd_mmds || [], response.xd_mmds || []);
                    obj_res.bi_bcs = updateTextPoints(obj_res.bi_bcs || [], response.bi_bcs || []);
                    obj_res.xd_bcs = updateTextPoints(obj_res.xd_bcs || [], response.xd_bcs || []);
                    obj_res.xd_zslx = updateLineSegments(obj_res.xd_zslx || [], response.xd_zslx || []);
                    obj_res.xd_zslx_lines = updateLineSegments(obj_res.xd_zslx_lines || [], response.xd_zslx_lines || []);
                    // SSE 全量快照(prepend 产出完整 chart_data):形态列表直接整体替换,绕过上面为"部分响应
                    // (向左滚动)"设计的 updateXxx 合并 → 从根上杜绝任何形态(笔/线段/中枢/走势类型/分型/
                    // 买卖点/背驰)的"只增不删"陈旧累积(如多个未完成笔)。K线 bars/MACD 仍走下面增量合并
                    // 保持视图不重置、随末根推进。scroll 等部分响应不带 full_snapshot, 仍走上面的合并(兜底)。
                    if (response.full_snapshot) {
                        obj_res.fxs = response.fxs || [];
                        obj_res.bis = response.bis || [];
                        obj_res.xds = response.xds || [];
                        obj_res.bi_zss = response.bi_zss || [];
                        obj_res.xd_zss = response.xd_zss || [];
                        obj_res.bcs = response.bcs || [];
                        obj_res.mmds = response.mmds || [];
                        obj_res.bi_mmds = response.bi_mmds || [];
                        obj_res.xd_mmds = response.xd_mmds || [];
                        obj_res.bi_bcs = response.bi_bcs || [];
                        obj_res.xd_bcs = response.xd_bcs || [];
                        obj_res.xd_zslx = response.xd_zslx || [];
                        obj_res.xd_zslx_lines = response.xd_zslx_lines || [];
                    }
                    // R1-C7: recursive_levels 的嵌套 mmds/bcs 与顶层同为「按窗口裁切的单点形态」,
                    // 无条件整体替换会让 backward(向左滚动)响应用老窗口(通常为空)clobber 右侧
                    // 最新高级别买卖点/背驰(R11 BUG2 只修顶层 mmds/bcs 的兄弟盲区)。full_snapshot
                    // 仍整体替换; 非快照按 level 键对齐, 嵌套 mmds/bcs 走 updateTextPoints 同口径
                    // 合并(backward 只增不删/右侧权威窗内被证伪剔除), zss/zslx_lines 等后端全局
                    // 透传字段以新响应为准。
                    {
                        const newLevels = response.recursive_levels || [];
                        if (response.full_snapshot) {
                            obj_res.recursive_levels = newLevels;
                        }
                        else {
                            const oldByLevel = new Map();
                            (obj_res.recursive_levels || []).forEach((lv, i) => {
                                oldByLevel.set(lv && lv.level !== undefined ? lv.level : i, lv);
                            });
                            obj_res.recursive_levels = newLevels.map((lv, i) => {
                                if (!lv || typeof lv !== 'object')
                                    return lv;
                                const key = lv.level !== undefined ? lv.level : i;
                                const old = oldByLevel.get(key);
                                if (!old || typeof old !== 'object')
                                    return lv;
                                const merged = Object.assign({}, lv);
                                merged.mmds = updateTextPoints(old.mmds || [], lv.mmds || []);
                                merged.bcs = updateTextPoints(old.bcs || [], lv.bcs || []);
                                return merged;
                            });
                        }
                    }
                    obj_res.higher_zs = response.higher_zs || [];
                    obj_res.interval_nest = response.interval_nest;
                    obj_res.chart_color = response.chart_color;
                    // ⚠ 增量更新 K线 bars：原 else 分支只更新缠论形态+MACD，漏了 obj_res.bars，
                    // 导致 SSE 推送(update:true)缠论更新而 K线 lastBar 不动。保留旧 bars 中早于新数据
                    // 首根的，追加本次 bars，让 K线随 SSE 实时推进(与 dist/bundle.js 同步)。
                    if (bars.length > 0) {
                        // Round11 BUG1 修复: 原"保留早于新首根的旧bars + concat"假设新bars恒为最近后缀; 向左滚动时新bars
                        // 是更旧窗口(newFirstTime小)→ keptBars空 → 最近K线被clobber → _getViewLatestSec读到陈旧末根
                        // → 下一SSE帧/看门狗误判巨隙 → resetData视图弹回最新, 毁盘中回看。改按 time 并集(新覆盖同time)。
                        const barByTime = new Map();
                        for (const bar of (obj_res.bars || []))
                            barByTime.set(bar.time, bar);
                        for (const bar of bars)
                            barByTime.set(bar.time, { ...bar });
                        obj_res.bars = Array.from(barByTime.values()).sort((a, b) => a.time - b.time);
                    }
                    const oldTimes = obj_res.times || [];
                    const difObj = mergeAlignedArrays(oldTimes, obj_res.macd_dif, raw_times, macd_dif);
                    const deaObj = mergeAlignedArrays(oldTimes, obj_res.macd_dea, raw_times, macd_dea);
                    const histObj = mergeAlignedArrays(oldTimes, obj_res.macd_hist, raw_times, macd_hist);
                    const areaObj = mergeAlignedArrays(oldTimes, obj_res.macd_area, raw_times, macd_area);
                    const hDifObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_dif, raw_times, higher_macd_dif);
                    const hDeaObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_dea, raw_times, higher_macd_dea);
                    const hHistObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_hist, raw_times, higher_macd_hist);
                    obj_res.times = difObj.times;
                    obj_res.macd_dif = difObj.values;
                    obj_res.macd_dea = deaObj.values;
                    obj_res.macd_hist = histObj.values;
                    obj_res.macd_area = areaObj.values;
                    obj_res.higher_macd_dif = hDifObj.values;
                    obj_res.higher_macd_dea = hDeaObj.values;
                    obj_res.higher_macd_hist = hHistObj.values;
                    const strictMode = response.strict_structure_mode;
                    if (strictMode === "replace") {
                        const strictStructure = response.strict_structure;
                        if (strictStructure &&
                            strictStructure.schema === "chanlun-chart-structure/v5") {
                            obj_res.strict_structure_mode = "replace";
                            obj_res.strict_structure = strictStructure;
                            delete obj_res.strict_structure_error;
                        }
                        else {
                            obj_res.strict_structure_mode = "unavailable";
                            delete obj_res.strict_structure;
                            obj_res.strict_structure_error = {
                                code: "strict_transport_invalid",
                            };
                        }
                    }
                    else if (strictMode === "unavailable") {
                        obj_res.strict_structure_mode = "unavailable";
                        delete obj_res.strict_structure;
                        obj_res.strict_structure_error =
                            response.strict_structure_error || {
                                code: "strict_evidence_invalid",
                            };
                    }
                    else if (strictMode === "unchanged") {
                        // Preserve the last atomic snapshot as cached evidence, but expose
                        // the transport mode verbatim.  Leaving the old "replace" mode in
                        // place makes pagination/realtime merges re-validate a stale payload
                        // as though it arrived with the newly merged bars.
                        obj_res.strict_structure_mode = "unchanged";
                    }
                    else if (strictMode !== "unchanged" && strictMode !== undefined) {
                        obj_res.strict_structure_mode = "unavailable";
                        delete obj_res.strict_structure;
                        obj_res.strict_structure_error = {
                            code: "strict_transport_invalid",
                        };
                    }
                    this.bars_result.set(res_key, obj_res);
                    this._pruneBarsResult();
                    this._emitBarsReady(res_key, requestParams);
                }
            }
            const result = {
                bars: bars,
                meta: meta,
                fxs: response.fxs,
                bis: response.bis,
                xds: response.xds,
                bi_zss: response.bi_zss,
                xd_zss: response.xd_zss,
                bcs: response.bcs,
                mmds: response.mmds,
                bi_mmds: response.bi_mmds || [],
                xd_mmds: response.xd_mmds || [],
                bi_bcs: response.bi_bcs || [],
                xd_bcs: response.xd_bcs || [],
                xd_zslx: response.xd_zslx || [],
                xd_zslx_lines: response.xd_zslx_lines || [],
                recursive_levels: response.recursive_levels || [],
                higher_zs: response.higher_zs || [],
                interval_nest: response.interval_nest,
                strict_structure_mode: response.strict_structure_mode,
                strict_structure: response.strict_structure,
                strict_structure_error: response.strict_structure_error,
                chart_color: response.chart_color,
            };
            return result;
        }
    }

    const REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS = 60;
    class DataPulseProvider {
        constructor(historyProvider, updateFrequency) {
            this._subscribers = {};
            this._requestsPending = new Set();
            this._historyProvider = historyProvider;
            this._requestTimeoutMs = Math.max(10_000, Number.isFinite(updateFrequency) ? updateFrequency * 2 : 10_000);
            setInterval(this._updateData.bind(this), updateFrequency);
        }
        subscribeBars(symbolInfo, resolution, newDataCallback, listenerGuid) {
            if (this._subscribers.hasOwnProperty(listenerGuid)) {
                return;
            }
            this._subscribers[listenerGuid] = {
                lastBarTime: null,
                listener: newDataCallback,
                resolution: resolution,
                symbolInfo: symbolInfo,
            };
            logMessage(`DataPulseProvider: subscribed for #${listenerGuid} - {${symbolInfo.name}, ${resolution}}`);
        }
        unsubscribeBars(listenerGuid) {
            delete this._subscribers[listenerGuid];
        }
        /**
         * SSE 推送驱动：把最新 bar 直接喂给匹配的订阅者(绕过轮询/浏览器节流)。
         * symbolResKey = (ticker||name).toLowerCase() + resolution.toLowerCase()。
         */
        feedBar(symbolResKey, bar, requireInitialized = false) {
            for (const guid in this._subscribers) {
                const sub = this._subscribers[guid];
                const si = sub.symbolInfo || {};
                const key = String(si.ticker || si.name || '').toLowerCase()
                    + String(sub.resolution).toLowerCase();
                if (key !== symbolResKey) {
                    continue;
                }
                if (requireInitialized && sub.lastBarTime === null) {
                    continue;
                }
                if (sub.lastBarTime !== null && bar.time < sub.lastBarTime) {
                    continue;
                }
                sub.lastBarTime = bar.time;
                try {
                    sub.listener(bar);
                }
                catch (e) {
                    /* ignore listener errors */
                }
            }
        }
        _updateData() {
            // A stalled symbol must not block refreshes for every other subscriber.
            // eslint-disable-next-line guard-for-in
            for (const listenerGuid in this._subscribers) {
                if (this._requestsPending.has(listenerGuid)) {
                    continue;
                }
                this._requestsPending.add(listenerGuid);
                this._withTimeout(Promise.resolve().then(() => this._updateDataForSubscriber(listenerGuid)), listenerGuid)
                    .then(() => {
                })
                    .catch((reason) => {
                    logMessage(`DataPulseProvider: data for #${listenerGuid} updated with error=${getErrorMessage(reason)}`);
                })
                    .finally(() => {
                    this._requestsPending.delete(listenerGuid);
                });
            }
        }
        _withTimeout(request, listenerGuid) {
            let timeoutId;
            const timeout = new Promise((_resolve, reject) => {
                timeoutId = setTimeout(() => {
                    reject(new Error(`Data refresh timed out for #${listenerGuid}`));
                }, this._requestTimeoutMs);
            });
            return Promise.race([request, timeout]).finally(() => {
                if (timeoutId !== undefined) {
                    clearTimeout(timeoutId);
                }
            });
        }
        _updateDataForSubscriber(listenerGuid) {
            const subscriptionRecord = this._subscribers[listenerGuid];
            const rangeEndTime = parseInt((Date.now() / 1000).toString())
                + REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS;
            // BEWARE: please note we really need 2 bars, not the only last one
            // see the explanation below. `10` is the `large enough` value to work around holidays
            const rangeStartTime = rangeEndTime - periodLengthSeconds(subscriptionRecord.resolution, 10);
            return this._historyProvider.getBars(subscriptionRecord.symbolInfo, subscriptionRecord.resolution, {
                from: rangeStartTime,
                to: rangeEndTime,
                countBack: 2,
                firstDataRequest: false,
            })
                .then((result) => {
                this._onSubscriberDataReceived(listenerGuid, result);
            });
        }
        _onSubscriberDataReceived(listenerGuid, result) {
            // means the subscription was cancelled while waiting for data
            if (!this._subscribers.hasOwnProperty(listenerGuid)) {
                return;
            }
            const bars = result.bars;
            if (bars.length === 0) {
                return;
            }
            const lastBar = bars[bars.length - 1];
            const subscriptionRecord = this._subscribers[listenerGuid];
            if (subscriptionRecord.lastBarTime !== null && lastBar.time < subscriptionRecord.lastBarTime) {
                return;
            }
            const isNewBar = subscriptionRecord.lastBarTime !== null && lastBar.time > subscriptionRecord.lastBarTime;
            // Pulse updating may miss some trades data (ie, if pulse period = 10 secods and new bar is started 5 seconds later after the last update, the
            // old bar's last 5 seconds trades will be lost). Thus, at fist we should broadcast old bar updates when it's ready.
            if (isNewBar && bars.length >= 2) {
                // 仅当确有前一根时补发它；bars<2(数据源延迟/窗口仅 1 根)时跳过补发、
                // 不再抛错，避免“数据延迟后新 bar 到达”令 K 线更新整条中断。
                const previousBar = bars[bars.length - 2];
                subscriptionRecord.listener(previousBar);
            }
            subscriptionRecord.lastBarTime = lastBar.time;
            subscriptionRecord.listener(lastBar);
        }
    }
    function periodLengthSeconds(resolution, requiredPeriodsCount) {
        let daysCount = 0;
        if (resolution === 'D' || resolution === '1D') {
            daysCount = requiredPeriodsCount;
        }
        else if (resolution === 'M' || resolution === '1M') {
            daysCount = 31 * requiredPeriodsCount;
        }
        else if (resolution === 'W' || resolution === '1W') {
            daysCount = 7 * requiredPeriodsCount;
        }
        else {
            daysCount = requiredPeriodsCount * parseInt(resolution) / (24 * 60);
        }
        return daysCount * 24 * 60 * 60;
    }

    class QuotesPulseProvider {
        constructor(quotesProvider) {
            this._subscribers = {};
            this._requestsPending = 0;
            this._timers = null;
            this._quotesProvider = quotesProvider;
        }
        subscribeQuotes(symbols, fastSymbols, onRealtimeCallback, listenerGuid) {
            this._subscribers[listenerGuid] = {
                symbols: symbols,
                fastSymbols: fastSymbols,
                listener: onRealtimeCallback,
            };
            this._createTimersIfRequired();
        }
        unsubscribeQuotes(listenerGuid) {
            delete this._subscribers[listenerGuid];
            if (Object.keys(this._subscribers).length === 0) {
                this._destroyTimers();
            }
        }
        _createTimersIfRequired() {
            if (this._timers === null) {
                const fastTimer = window.setInterval(this._updateQuotes.bind(this, 1 /* SymbolsType.Fast */), 10000 /* UpdateTimeouts.Fast */);
                const generalTimer = window.setInterval(this._updateQuotes.bind(this, 0 /* SymbolsType.General */), 60000 /* UpdateTimeouts.General */);
                this._timers = { fastTimer, generalTimer };
            }
        }
        _destroyTimers() {
            if (this._timers !== null) {
                clearInterval(this._timers.fastTimer);
                clearInterval(this._timers.generalTimer);
                this._timers = null;
            }
        }
        _updateQuotes(updateType) {
            if (this._requestsPending > 0) {
                return;
            }
            // eslint-disable-next-line guard-for-in
            for (const listenerGuid in this._subscribers) {
                this._requestsPending++;
                const subscriptionRecord = this._subscribers[listenerGuid];
                this._quotesProvider.getQuotes(updateType === 1 /* SymbolsType.Fast */ ? subscriptionRecord.fastSymbols : subscriptionRecord.symbols)
                    .then((data) => {
                    this._requestsPending--;
                    if (!this._subscribers.hasOwnProperty(listenerGuid)) {
                        return;
                    }
                    subscriptionRecord.listener(data);
                    logMessage(`QuotesPulseProvider: data for #${listenerGuid} (${updateType}) updated successfully, pending=${this._requestsPending}`);
                })
                    .catch((reason) => {
                    this._requestsPending--;
                    logMessage(`QuotesPulseProvider: data for #${listenerGuid} (${updateType}) updated with error=${getErrorMessage(reason)}, pending=${this._requestsPending}`);
                });
            }
        }
    }

    function extractField$1(data, field, arrayIndex, valueIsArray) {
        if (!(field in data)) {
            // eslint-disable-next-line no-console
            console.warn(`Field "${String(field)}" not present in response`);
            return undefined;
        }
        const value = data[field];
        if (Array.isArray(value) && (!valueIsArray || Array.isArray(value[0]))) {
            return value[arrayIndex];
        }
        return value;
    }
    function symbolKey(symbol, currency, unit) {
        // here we're using a separator that quite possible shouldn't be in a real symbol name
        return symbol + (currency !== undefined ? '_%|#|%_' + currency : '') + (unit !== undefined ? '_%|#|%_' + unit : '');
    }
    class SymbolsStorage {
        constructor(datafeedUrl, datafeedSupportedResolutions, requester) {
            this._exchangesList = ['NYSE', 'FOREX', 'AMEX'];
            this._symbolsInfo = {};
            this._symbolsList = [];
            this._datafeedUrl = datafeedUrl;
            this._datafeedSupportedResolutions = datafeedSupportedResolutions;
            this._requester = requester;
            this._readyPromise = this._init();
            this._readyPromise.catch((error) => {
                // seems it is impossible
                // eslint-disable-next-line no-console
                console.error(`SymbolsStorage: Cannot init, error=${error.toString()}`);
            });
        }
        // BEWARE: this function does not consider symbol's exchange
        resolveSymbol(symbolName, currencyCode, unitId) {
            return this._readyPromise.then(() => {
                const symbolInfo = this._symbolsInfo[symbolKey(symbolName, currencyCode, unitId)];
                if (symbolInfo === undefined) {
                    return Promise.reject('invalid symbol');
                }
                return Promise.resolve(symbolInfo);
            });
        }
        searchSymbols(searchString, exchange, symbolType, maxSearchResults) {
            return this._readyPromise.then(() => {
                const weightedResult = [];
                const queryIsEmpty = searchString.length === 0;
                searchString = searchString.toUpperCase();
                for (const symbolName of this._symbolsList) {
                    const symbolInfo = this._symbolsInfo[symbolName];
                    if (symbolInfo === undefined) {
                        continue;
                    }
                    if (symbolType.length > 0 && symbolInfo.type !== symbolType) {
                        continue;
                    }
                    if (exchange && exchange.length > 0 && symbolInfo.exchange !== exchange) {
                        continue;
                    }
                    const positionInName = symbolInfo.name.toUpperCase().indexOf(searchString);
                    const positionInDescription = symbolInfo.description.toUpperCase().indexOf(searchString);
                    if (queryIsEmpty || positionInName >= 0 || positionInDescription >= 0) {
                        const alreadyExists = weightedResult.some((item) => item.symbolInfo === symbolInfo);
                        if (!alreadyExists) {
                            const weight = positionInName >= 0 ? positionInName : 8000 + positionInDescription;
                            weightedResult.push({ symbolInfo: symbolInfo, weight: weight });
                        }
                    }
                }
                const result = weightedResult
                    .sort((item1, item2) => item1.weight - item2.weight)
                    .slice(0, maxSearchResults)
                    .map((item) => {
                    const symbolInfo = item.symbolInfo;
                    return {
                        symbol: symbolInfo.name,
                        full_name: `${symbolInfo.exchange}:${symbolInfo.name}`,
                        description: symbolInfo.description,
                        exchange: symbolInfo.exchange,
                        params: [],
                        type: symbolInfo.type,
                        ticker: symbolInfo.name,
                    };
                });
                return Promise.resolve(result);
            });
        }
        _init() {
            const promises = [];
            const alreadyRequestedExchanges = {};
            for (const exchange of this._exchangesList) {
                if (alreadyRequestedExchanges[exchange]) {
                    continue;
                }
                alreadyRequestedExchanges[exchange] = true;
                promises.push(this._requestExchangeData(exchange));
            }
            return Promise.all(promises)
                .then(() => {
                this._symbolsList.sort();
            });
        }
        _requestExchangeData(exchange) {
            return new Promise((resolve, reject) => {
                this._requester.sendRequest(this._datafeedUrl, 'symbol_info', { group: exchange })
                    .then((response) => {
                    try {
                        this._onExchangeDataReceived(exchange, response);
                    }
                    catch (error) {
                        reject(error instanceof Error ? error : new Error(`SymbolsStorage: Unexpected exception ${error}`));
                        return;
                    }
                    resolve();
                })
                    .catch((reason) => {
                    logMessage(`SymbolsStorage: Request data for exchange '${exchange}' failed, reason=${getErrorMessage(reason)}`);
                    resolve();
                });
            });
        }
        _onExchangeDataReceived(exchange, data) {
            let symbolIndex = 0;
            let fullName;
            try {
                const symbolsCount = data.symbol.length;
                const tickerPresent = data.ticker !== undefined;
                for (; symbolIndex < symbolsCount; ++symbolIndex) {
                    const symbolName = data.symbol[symbolIndex];
                    const listedExchange = extractField$1(data, 'exchange-listed', symbolIndex);
                    const tradedExchange = extractField$1(data, 'exchange-traded', symbolIndex);
                    if (listedExchange !== undefined || tradedExchange !== undefined) {
                        // eslint-disable-next-line no-console
                        console.warn('Starting from v30, both "exchange-listed" and "exchange-traded" fields are deprecated. Please use "exchange_listed_name" instead.');
                        fullName = tradedExchange + ':' + symbolName;
                    }
                    const exchangeListedName = extractField$1(data, 'exchange_listed_name', symbolIndex);
                    if (exchangeListedName === undefined) {
                        // eslint-disable-next-line no-console
                        console.warn('Starting from v30, both "exchange-listed" and "exchange-traded" fields are deprecated. Please use "exchange_listed_name" instead.');
                    }
                    else {
                        fullName = exchangeListedName + ':' + symbolName;
                    }
                    const currencyCode = extractField$1(data, 'currency-code', symbolIndex);
                    const unitId = extractField$1(data, 'unit-id', symbolIndex);
                    const ticker = tickerPresent ? extractField$1(data, 'ticker', symbolIndex) : symbolName;
                    const symbolInfo = {
                        ticker: ticker,
                        name: symbolName,
                        base_name: [listedExchange + ':' + symbolName],
                        listed_exchange: listedExchange,
                        exchange: exchangeListedName || listedExchange,
                        currency_code: currencyCode,
                        original_currency_code: extractField$1(data, 'original-currency-code', symbolIndex),
                        unit_id: unitId,
                        original_unit_id: extractField$1(data, 'original-unit-id', symbolIndex),
                        unit_conversion_types: extractField$1(data, 'unit-conversion-types', symbolIndex, true),
                        description: extractField$1(data, 'description', symbolIndex),
                        has_intraday: definedValueOrDefault(extractField$1(data, 'has-intraday', symbolIndex), false),
                        visible_plots_set: definedValueOrDefault(extractField$1(data, 'visible-plots-set', symbolIndex), undefined),
                        minmov: extractField$1(data, 'minmovement', symbolIndex) || extractField$1(data, 'minmov', symbolIndex) || 0,
                        minmove2: extractField$1(data, 'minmove2', symbolIndex) || extractField$1(data, 'minmov2', symbolIndex),
                        fractional: extractField$1(data, 'fractional', symbolIndex),
                        pricescale: extractField$1(data, 'pricescale', symbolIndex),
                        type: extractField$1(data, 'type', symbolIndex),
                        session: extractField$1(data, 'session-regular', symbolIndex),
                        session_holidays: extractField$1(data, 'session-holidays', symbolIndex),
                        corrections: extractField$1(data, 'corrections', symbolIndex),
                        timezone: extractField$1(data, 'timezone', symbolIndex),
                        supported_resolutions: definedValueOrDefault(extractField$1(data, 'supported-resolutions', symbolIndex, true), this._datafeedSupportedResolutions),
                        has_daily: definedValueOrDefault(extractField$1(data, 'has-daily', symbolIndex), true),
                        intraday_multipliers: definedValueOrDefault(extractField$1(data, 'intraday-multipliers', symbolIndex, true), ['1', '5', '15', '30', '60']),
                        has_weekly_and_monthly: extractField$1(data, 'has-weekly-and-monthly', symbolIndex),
                        has_empty_bars: extractField$1(data, 'has-empty-bars', symbolIndex),
                        volume_precision: definedValueOrDefault(extractField$1(data, 'volume-precision', symbolIndex), 0),
                        format: 'price',
                    };
                    this._symbolsInfo[ticker] = symbolInfo;
                    this._symbolsInfo[symbolName] = symbolInfo;
                    if (fullName !== undefined) {
                        this._symbolsInfo[fullName] = symbolInfo;
                    }
                    if (currencyCode !== undefined || unitId !== undefined) {
                        this._symbolsInfo[symbolKey(ticker, currencyCode, unitId)] = symbolInfo;
                        this._symbolsInfo[symbolKey(symbolName, currencyCode, unitId)] = symbolInfo;
                        if (fullName !== undefined) {
                            this._symbolsInfo[symbolKey(fullName, currencyCode, unitId)] = symbolInfo;
                        }
                    }
                    this._symbolsList.push(symbolName);
                }
            }
            catch (error) {
                throw new Error(`SymbolsStorage: API error when processing exchange ${exchange} symbol #${symbolIndex} (${data.symbol[symbolIndex]}): ${Object(error).message}`);
            }
        }
    }
    function definedValueOrDefault(value, defaultValue) {
        return value !== undefined ? value : defaultValue;
    }

    function extractField(data, field, arrayIndex) {
        const value = data[field];
        return Array.isArray(value) ? value[arrayIndex] : value;
    }
    /**
     * This class implements interaction with UDF-compatible datafeed.
     * See [UDF protocol reference](@docs/connecting_data/UDF.md)
     */
    class UDFCompatibleDatafeedBase {
        constructor(datafeedURL, quotesProvider, requester, updateFrequency = 10 * 1000, limitedServerResponse, options = {}) {
            this._configuration = defaultConfiguration();
            this._symbolsStorage = null;
            this._subscribersResetCallbacks = {};
            this._datafeedURL = datafeedURL;
            this._requester = requester;
            this._historyProvider = new HistoryProvider(datafeedURL, this._requester, limitedServerResponse, options);
            this._quotesProvider = quotesProvider;
            this._dataPulseProvider = new DataPulseProvider(this._historyProvider, updateFrequency);
            this._quotesPulseProvider = new QuotesPulseProvider(this._quotesProvider);
            this._configurationReadyPromise = this._requestConfiguration()
                .then((configuration) => {
                if (configuration === null) {
                    configuration = defaultConfiguration();
                }
                this._setupWithConfiguration(configuration);
            });
        }
        onReady(callback) {
            this._configurationReadyPromise.then(() => {
                callback(this._configuration);
            });
        }
        getQuotes(symbols, onDataCallback, onErrorCallback) {
            this._quotesProvider.getQuotes(symbols).then(onDataCallback).catch(onErrorCallback);
        }
        subscribeQuotes(symbols, fastSymbols, onRealtimeCallback, listenerGuid) {
            this._quotesPulseProvider.subscribeQuotes(symbols, fastSymbols, onRealtimeCallback, listenerGuid);
        }
        unsubscribeQuotes(listenerGuid) {
            this._quotesPulseProvider.unsubscribeQuotes(listenerGuid);
        }
        getMarks(symbolInfo, from, to, onDataCallback, resolution) {
            if (!this._configuration.supports_marks) {
                return;
            }
            const requestParams = {
                symbol: symbolInfo.ticker || '',
                from: from,
                to: to,
                resolution: resolution,
            };
            this._send('marks', requestParams)
                .then((response) => {
                if (!Array.isArray(response)) {
                    const result = [];
                    for (let i = 0; i < response.id.length; ++i) {
                        result.push({
                            id: extractField(response, 'id', i),
                            time: extractField(response, 'time', i),
                            color: extractField(response, 'color', i),
                            text: extractField(response, 'text', i),
                            label: extractField(response, 'label', i),
                            labelFontColor: extractField(response, 'labelFontColor', i),
                            minSize: extractField(response, 'minSize', i),
                            borderWidth: extractField(response, 'borderWidth', i),
                            hoveredBorderWidth: extractField(response, 'hoveredBorderWidth', i),
                            imageUrl: extractField(response, 'imageUrl', i),
                            showLabelWhenImageLoaded: extractField(response, 'showLabelWhenImageLoaded', i),
                        });
                    }
                    response = result;
                }
                onDataCallback(response);
            })
                .catch((error) => {
                logMessage(`UdfCompatibleDatafeed: Request marks failed: ${getErrorMessage(error)}`);
                onDataCallback([]);
            });
        }
        getTimescaleMarks(symbolInfo, from, to, onDataCallback, resolution) {
            if (!this._configuration.supports_timescale_marks) {
                return;
            }
            const requestParams = {
                symbol: symbolInfo.ticker || '',
                from: from,
                to: to,
                resolution: resolution,
            };
            this._send('timescale_marks', requestParams)
                .then((response) => {
                if (!Array.isArray(response)) {
                    const result = [];
                    for (let i = 0; i < response.id.length; ++i) {
                        result.push({
                            id: extractField(response, 'id', i),
                            time: extractField(response, 'time', i),
                            color: extractField(response, 'color', i),
                            label: extractField(response, 'label', i),
                            tooltip: extractField(response, 'tooltip', i),
                            imageUrl: extractField(response, 'imageUrl', i),
                            showLabelWhenImageLoaded: extractField(response, 'showLabelWhenImageLoaded', i),
                        });
                    }
                    response = result;
                }
                onDataCallback(response);
            })
                .catch((error) => {
                logMessage(`UdfCompatibleDatafeed: Request timescale marks failed: ${getErrorMessage(error)}`);
                onDataCallback([]);
            });
        }
        getServerTime(callback) {
            if (!this._configuration.supports_time) {
                return;
            }
            this._send('time')
                .then((response) => {
                const time = parseInt(response);
                if (!isNaN(time)) {
                    callback(time);
                }
            })
                .catch((error) => {
                logMessage(`UdfCompatibleDatafeed: Fail to load server time, error=${getErrorMessage(error)}`);
            });
        }
        searchSymbols(userInput, exchange, symbolType, onResult) {
            if (this._configuration.supports_search) {
                const params = {
                    limit: 30 /* Constants.SearchItemsLimit */,
                    query: userInput.toUpperCase(),
                    type: symbolType,
                    exchange: exchange,
                };
                this._send('search', params)
                    .then((response) => {
                    if (response.s !== undefined) {
                        logMessage(`UdfCompatibleDatafeed: search symbols error=${response.errmsg}`);
                        onResult([]);
                        return;
                    }
                    onResult(response);
                })
                    .catch((reason) => {
                    logMessage(`UdfCompatibleDatafeed: Search symbols for '${userInput}' failed. Error=${getErrorMessage(reason)}`);
                    onResult([]);
                });
            }
            else {
                if (this._symbolsStorage === null) {
                    throw new Error('UdfCompatibleDatafeed: inconsistent configuration (symbols storage)');
                }
                this._symbolsStorage.searchSymbols(userInput, exchange, symbolType, 30 /* Constants.SearchItemsLimit */)
                    .then(onResult)
                    .catch(onResult.bind(null, []));
            }
        }
        resolveSymbol(symbolName, onResolve, onError, extension) {
            const currencyCode = extension && extension.currencyCode;
            const unitId = extension && extension.unitId;
            function onResultReady(symbolInfo) {
                onResolve(symbolInfo);
            }
            if (!this._configuration.supports_group_request) {
                const params = {
                    symbol: symbolName,
                };
                if (currencyCode !== undefined) {
                    params.currencyCode = currencyCode;
                }
                if (unitId !== undefined) {
                    params.unitId = unitId;
                }
                this._send('symbols', params)
                    .then((response) => {
                    if (response.s !== undefined) {
                        onError('unknown_symbol');
                    }
                    else {
                        const symbol = response.name;
                        const listedExchange = response.listed_exchange ?? response['exchange-listed'];
                        const tradedExchange = response.exchange ?? response['exchange-traded'];
                        const result = {
                            ...response,
                            name: symbol,
                            base_name: [listedExchange + ':' + symbol],
                            listed_exchange: listedExchange,
                            exchange: tradedExchange,
                            ticker: response.ticker,
                            currency_code: response.currency_code ?? response['currency-code'],
                            original_currency_code: response.original_currency_code ?? response['original-currency-code'],
                            unit_id: response.unit_id ?? response['unit-id'],
                            original_unit_id: response.original_unit_id ?? response['original-unit-id'],
                            unit_conversion_types: response.unit_conversion_types ?? response['unit-conversion-types'],
                            has_intraday: response.has_intraday ?? response['has-intraday'] ?? false,
                            visible_plots_set: response.visible_plots_set ?? response['visible-plots-set'],
                            minmov: response.minmovement ?? response.minmov ?? 0,
                            minmove2: response.minmovement2 ?? response.minmove2,
                            session: response.session ?? response['session-regular'],
                            session_holidays: response.session_holidays ?? response['session-holidays'],
                            supported_resolutions: response.supported_resolutions ?? response['supported-resolutions'] ?? this._configuration.supported_resolutions ?? [],
                            has_daily: response.has_daily ?? response['has-daily'] ?? true,
                            intraday_multipliers: response.intraday_multipliers ?? response['intraday-multipliers'] ?? ['1', '5', '15', '30', '60'],
                            has_weekly_and_monthly: response.has_weekly_and_monthly ?? response['has-weekly-and-monthly'],
                            has_empty_bars: response.has_empty_bars ?? response['has-empty-bars'],
                            volume_precision: response.volume_precision ?? response['volume-precision'],
                            format: response.format ?? 'price',
                        };
                        onResultReady(result);
                    }
                })
                    .catch((reason) => {
                    logMessage(`UdfCompatibleDatafeed: Error resolving symbol: ${getErrorMessage(reason)}`);
                    onError('unknown_symbol');
                });
            }
            else {
                if (this._symbolsStorage === null) {
                    throw new Error('UdfCompatibleDatafeed: inconsistent configuration (symbols storage)');
                }
                this._symbolsStorage.resolveSymbol(symbolName, currencyCode, unitId).then(onResultReady).catch(onError);
            }
        }
        getBars(symbolInfo, resolution, periodParams, onResult, onError) {
            this._historyProvider.getBars(symbolInfo, resolution, periodParams)
                .then((result) => {
                onResult(result.bars, result.meta);
            })
                .catch(onError);
        }
        /**
         * SSE 推送驱动 K 线：从 /tv/history 同构 response 取最新一根 bar，喂给
         * DataPulseProvider 的订阅者，让 K 线随 SSE 实时刷新(不依赖轮询)。
         */
        feedRealtimeBar(symbolResKey, response, resolution = '') {
            const t = response.t;
            const c = response.c;
            if (!response || !t || t.length === 0 || !c) {
                return;
            }
            const o = response.o;
            const h = response.h;
            const l = response.l;
            const v = response.v;
            const makeBar = (idx) => {
                const closeVal = c[idx];
                if (closeVal === undefined || closeVal === null) {
                    return null;
                }
                const bar = {
                    time: chartBarTimeSeconds(t[idx], resolution) * 1000,
                    open: o ? o[idx] : closeVal,
                    high: h ? h[idx] : closeVal,
                    low: l ? l[idx] : closeVal,
                    close: closeVal,
                };
                if (v && v[idx] !== undefined && v[idx] !== null) {
                    bar.volume = v[idx];
                }
                return bar;
            };
            const i = t.length - 1;
            // 新根出现先补喂倒数第二根(刚收盘那根)最终 OHLC, 再喂末根: 复刻 DataPulseProvider
            // 轮询的 previousBar 补发。否则该根蜡烛永久停在收盘前 <=8s 旧值(SSE 已把 sub.lastBarTime
            // 推进到末根, 废掉轮询里 isNewBar 的 previousBar 分支)。feedBar 的 bar.time<lastBarTime
            // 校验令稳态逐帧重复喂前一根无副作用；订阅尚未收到首根实时 bar 时跳过
            // 前一根，避免 TV 已从 getBars 渲染末根后收到更早时间而触发 time violation。
            // t.length<2 时保持单根容错。
            if (t.length >= 2) {
                const prevBar = makeBar(i - 1);
                if (prevBar !== null) {
                    this._dataPulseProvider.feedBar(symbolResKey, prevBar, true);
                }
            }
            const lastBar = makeBar(i);
            if (lastBar === null) {
                return;
            }
            this._dataPulseProvider.feedBar(symbolResKey, lastBar);
        }
        subscribeBars(symbolInfo, resolution, onTick, listenerGuid, _onResetCacheNeededCallback) {
            this._dataPulseProvider.subscribeBars(symbolInfo, resolution, onTick, listenerGuid);
            // TV 通过 onResetCacheNeeded 通知 datafeed 当前 symbol+resolution 的缓存需要清空
            // （比如盘后数据修正、合约换月等）。我们在原回调外再清掉 bars_result，避免缠论形态
            // 还基于已失效的 bars 渲染。
            if (_onResetCacheNeededCallback) {
                const originalCallback = _onResetCacheNeededCallback;
                this._subscribersResetCallbacks[listenerGuid] = () => {
                    this._historyProvider._clearBarsResultForSymbolResolution(symbolInfo.ticker || symbolInfo.name, resolution);
                    originalCallback();
                };
                _onResetCacheNeededCallback = this._subscribersResetCallbacks[listenerGuid];
            }
        }
        unsubscribeBars(listenerGuid) {
            this._dataPulseProvider.unsubscribeBars(listenerGuid);
            delete this._subscribersResetCallbacks[listenerGuid];
        }
        _requestConfiguration() {
            return this._send('config')
                .catch((reason) => {
                logMessage(`UdfCompatibleDatafeed: Cannot get datafeed configuration - use default, error=${getErrorMessage(reason)}`);
                return null;
            });
        }
        _send(urlPath, params) {
            return this._requester.sendRequest(this._datafeedURL, urlPath, params);
        }
        _setupWithConfiguration(configurationData) {
            this._configuration = configurationData;
            if (configurationData.exchanges === undefined) {
                configurationData.exchanges = [];
            }
            if (!configurationData.supports_search && !configurationData.supports_group_request) {
                throw new Error('Unsupported datafeed configuration. Must either support search, or support group request');
            }
            if (configurationData.supports_group_request || !configurationData.supports_search) {
                this._symbolsStorage = new SymbolsStorage(this._datafeedURL, configurationData.supported_resolutions || [], this._requester);
            }
            logMessage(`UdfCompatibleDatafeed: Initialized with ${JSON.stringify(configurationData)}`);
        }
    }
    function defaultConfiguration() {
        return {
            supports_search: false,
            supports_group_request: true,
            supported_resolutions: [
                '1',
                '5',
                '15',
                '30',
                '60',
                '1D',
                '1W',
                '1M',
            ],
            supports_marks: false,
            supports_timescale_marks: false,
        };
    }

    class UDFCompatibleDatafeed extends UDFCompatibleDatafeedBase {
        constructor(datafeedURL, updateFrequency = 10 * 1000, limitedServerResponse, options = {}) {
            const requester = new Requester();
            const quotesProvider = new QuotesProvider(datafeedURL, requester);
            super(datafeedURL, quotesProvider, requester, updateFrequency, limitedServerResponse, options);
        }
    }

    exports.UDFCompatibleDatafeed = UDFCompatibleDatafeed;

}));
