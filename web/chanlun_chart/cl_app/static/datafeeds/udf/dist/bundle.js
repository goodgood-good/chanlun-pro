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
        sendRequest(datafeedUrl, urlPath, params, timeoutMs) {
            if (params !== undefined) {
                const paramKeys = Object.keys(params);
                if (paramKeys.length !== 0) {
                    urlPath += '?';
                }
                urlPath += paramKeys.map((key) => {
                    return `${encodeURIComponent(key)}=${encodeURIComponent(params[key].toString())}`;
                }).join('&');
            }
            const effectiveTimeoutMs = Number.isFinite(timeoutMs) && Number(timeoutMs) > 0
                ? Number(timeoutMs)
                : this._timeoutMs;
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
                    reject(new Error(`Request timed out after ${effectiveTimeoutMs}ms`));
                }, effectiveTimeoutMs);
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

    // 冷态 1m/5m 历史需要分页取数并计算结构，服务端实测可超过通用请求的 15 秒上限。
    // 只放宽周期切换触发的首个完整快照；配置、报价与实时增量仍由 Requester 的
    // 15 秒默认值约束，避免故障时所有请求长时间悬挂。
    const DEFAULT_INITIAL_HISTORY_TIMEOUT_MS = 45_000;
    // 首次冷态请求若刚好撞上服务启动或缓存落盘，服务端可能在客户端超时后不久完成。
    // 自动重试一次即可命中刚生成的缓存，避免图表永久停在“这里没有数据”等待手动刷新。
    const INITIAL_HISTORY_RETRY_DELAY_MS = 750;
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
            this._historyParams = Object.freeze({ ...(options.historyParams || {}) });
            this._initialHistoryTimeoutMs =
                Number.isFinite(options.initialHistoryTimeoutMs) &&
                    Number(options.initialHistoryTimeoutMs) > 0
                    ? Number(options.initialHistoryTimeoutMs)
                    : DEFAULT_INITIAL_HISTORY_TIMEOUT_MS;
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
        async _requestHistoryWithStartupRetry(requestParams, requestTimeoutMs, requestGeneration) {
            try {
                return await this._requester.sendRequest(this._datafeedUrl, "history", requestParams, requestTimeoutMs);
            }
            catch (error) {
                const reasonString = error instanceof Error || typeof error === "string"
                    ? getErrorMessage(error)
                    : "";
                const retryableStartupTimeout = requestGeneration !== undefined &&
                    reasonString.startsWith("Request timed out after ") &&
                    this._fullRequestIsCurrent(requestParams, requestGeneration);
                if (!retryableStartupTimeout) {
                    throw error;
                }
                // tslint:disable-next-line:no-console
                console.warn(`HistoryProvider: initial history timed out; retrying once after ${INITIAL_HISTORY_RETRY_DELAY_MS}ms`);
                await new Promise((resolve) => {
                    setTimeout(resolve, INITIAL_HISTORY_RETRY_DELAY_MS);
                });
                // 用户可能在退避期间切换标的/周期。旧请求不得再制造额外后端负载。
                if (!this._fullRequestIsCurrent(requestParams, requestGeneration)) {
                    throw error;
                }
                return this._requester.sendRequest(this._datafeedUrl, "history", requestParams, requestTimeoutMs);
            }
        }
        getBars(symbolInfo, resolution, periodParams) {
            const requestParams = {
                ...this._historyParams,
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
            const requestTimeoutMs = requestGeneration === undefined
                ? undefined
                : this._initialHistoryTimeoutMs;
            return new Promise(async (resolve, reject) => {
                try {
                    const initialResponse = await this._requestHistoryWithStartupRetry(requestParams, requestTimeoutMs, requestGeneration);
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
                    const followupResponse = await this._requester.sendRequest(this._datafeedUrl, "history", requestParams, requestGeneration === undefined
                        ? undefined
                        : this._initialHistoryTimeoutMs);
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
                const resolution = String(requestParams["resolution"] || "");
                for (let i = 0; i < response.t.length; ++i) {
                    const barValue = {
                        time: chartBarTimeSeconds(response.t[i], resolution) * 1000,
                        close: response.c[i],
                        open: response.o[i],
                        high: response.h[i],
                        low: response.l[i],
                        volume: response.v[i],
                    };
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
                const isAuthoritativeSnapshot = response.update === false ||
                    response.full_snapshot === true;
                const isRegressiveFullSnapshot = isAuthoritativeSnapshot &&
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
                        strict_structure_mode: response.strict_structure_mode,
                        strict_structure: response.strict_structure,
                        strict_structure_error: response.strict_structure_error,
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
                    // 分型是单点基础图元；权威窗口内未再次出现的点应被删除。
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
                        // 权威窗口内按身份替换；向左分页只合并，不删除右侧图元。
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
                    // 笔与线段按起点身份合并。
                    // 用 key 合并去重：避免按 minResponseTime 切割导致「起点在窗口内、
                    // 终点在窗口外」的跨界线段在 backward 历史响应处理中被永久丢弃。
                    // 三态线段以起点为稳定身份，让本次响应覆盖先前状态。
                    //
                    // 2026-07 修复(前端幽灵形态)：仅靠 key upsert 只增不删——已完成段一旦起点
                    // 被新行情证伪、新响应里不再提及，先前状态会永久残留(后端刚修的同类 bug，
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
                        // 一根线段从某个起点出发，任意时刻只该有一个状态——
                        // 端点(tail)随 K 线包含合并会漂移（CLKline.k.date 会变），
                        // 状态也会从 forming 翻成 formed，再翻成 locked，
                        // 把它们写进 key 会同时保留同一根段的前后状态 → 视觉上线段重叠/断裂。
                        // 用 head 作为身份，Map.set 让本次响应覆盖先前状态即可。
                        const segmentKey = (s) => {
                            const head = s.points[0];
                            return `${head.time}_${head.price}`;
                        };
                        const isForming = (segment) => {
                            const state = String(segment.state || "").toLowerCase();
                            return state ? state === "forming" : Number(segment.linestyle) === 1;
                        };
                        // 新响应是否带来形成中尾段。SSE 全量快照含当前唯一的形成中尾段；
                        // 向左滚动的历史区间响应则不含。
                        const newHasPending = newSegments.some((s) => s.points && s.points.length > 0 && isForming(s));
                        const merged = new Map();
                        for (const segment of existingSegments) {
                            // 丢弃条件(任一成立即丢，交由下面 newSegments 决定是否以新版本重新加入)：
                            // ① 新响应带来当前未完成段时，旧的未完成段一律丢——未完成段起点会随重算
                            //    漂移，旧 head key 不会被新版本覆盖，否则累积"多个未完成笔"；
                            // ② 该段起点落在本次权威窗口内——右侧最新窗口的响应即权威真相，窗口内
                            //    未被新响应提及的已完成段已被证伪，不能只增不删地累积幽灵形态。
                            if (segment.points.length > 0 &&
                                !(newHasPending && isForming(segment)) &&
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
                    // SSE 全量快照直接整体替换基础图元。K线 bars/MACD 仍走下面增量合并
                    // 保持视图不重置、随末根推进。scroll 等部分响应不带 full_snapshot, 仍走上面的合并(兜底)。
                    if (response.full_snapshot) {
                        obj_res.fxs = response.fxs || [];
                        obj_res.bis = response.bis || [];
                        obj_res.xds = response.xds || [];
                    }
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
                            strictStructure.schema === "chanlun-chart-structure") {
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
                strict_structure_mode: response.strict_structure_mode,
                strict_structure: response.strict_structure,
                strict_structure_error: response.strict_structure_error,
            };
            return result;
        }
    }

    const REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS = 60;
    const A_SHARE_UTC_OFFSET_MS = 8 * 60 * 60 * 1000;
    const A_SHARE_REALTIME_WINDOWS = [
        // Poll through a short publication grace after each close so the final bar
        // can arrive without keeping every open chart busy throughout lunch/night.
        [9 * 60 + 30, 11 * 60 + 40],
        [13 * 60, 15 * 60 + 10],
    ];
    function _symbolMarket(symbolInfo) {
        const exchange = String(symbolInfo.exchange || '').trim().toLowerCase();
        if (exchange) {
            return exchange;
        }
        const ticker = String(symbolInfo.ticker || symbolInfo.name || '');
        const separator = ticker.indexOf(':');
        return separator > 0 ? ticker.slice(0, separator).trim().toLowerCase() : '';
    }
    function _realtimePollingDue(symbolInfo, nowMs) {
        // Only suppress the market whose exact civil-time contract is proven here.
        // Unknown/cross-market symbols fail open so an optimization can never make
        // their live charts stale.
        if (_symbolMarket(symbolInfo) !== 'a') {
            return true;
        }
        const shanghai = new Date(nowMs + A_SHARE_UTC_OFFSET_MS);
        const weekday = shanghai.getUTCDay();
        if (weekday === 0 || weekday === 6) {
            return false;
        }
        const minuteOfDay = shanghai.getUTCHours() * 60 + shanghai.getUTCMinutes();
        return A_SHARE_REALTIME_WINDOWS.some(([start, end]) => (minuteOfDay >= start && minuteOfDay <= end));
    }
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
            if (!subscriptionRecord || !_realtimePollingDue(subscriptionRecord.symbolInfo, Date.now())) {
                return Promise.resolve();
            }
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

    /**
     * This class implements interaction with UDF-compatible datafeed.
     * See [UDF protocol reference](@docs/connecting_data/UDF.md)
     */
    class UDFCompatibleDatafeedBase {
        constructor(datafeedURL, quotesProvider, requester, updateFrequency = 10 * 1000, limitedServerResponse, options = {}) {
            this._configuration = defaultConfiguration();
            this._subscribersResetCallbacks = {};
            this._datafeedURL = datafeedURL;
            this._requester = requester;
            const reviewResolveParams = {};
            const suppliedParams = options.historyParams || {};
            for (const key of [
                'review_candidate_id',
                'review_source_sha256',
                'review_as_of',
            ]) {
                const value = suppliedParams[key];
                if (value !== undefined && value !== null && value !== '') {
                    reviewResolveParams[key] = value;
                }
            }
            this._reviewResolveParams = Object.freeze(reviewResolveParams);
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
        resolveSymbol(symbolName, onResolve, onError, extension) {
            const currencyCode = extension && extension.currencyCode;
            const unitId = extension && extension.unitId;
            function onResultReady(symbolInfo) {
                onResolve(symbolInfo);
            }
            const params = {
                ...this._reviewResolveParams,
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
                    return;
                }
                const symbol = response.name;
                const result = {
                    ...response,
                    name: symbol,
                    base_name: [response.listed_exchange + ':' + symbol],
                    listed_exchange: response.listed_exchange,
                    exchange: response.exchange,
                    ticker: response.ticker,
                    currency_code: response.currency_code,
                    original_currency_code: response.original_currency_code,
                    unit_id: response.unit_id,
                    original_unit_id: response.original_unit_id,
                    unit_conversion_types: response.unit_conversion_types,
                    has_intraday: response.has_intraday ?? false,
                    visible_plots_set: response.visible_plots_set,
                    minmov: response.minmov ?? 0,
                    minmove2: response.minmove2,
                    session: response.session,
                    session_holidays: response.session_holidays,
                    supported_resolutions: response.supported_resolutions ?? this._configuration.supported_resolutions ?? [],
                    has_daily: response.has_daily ?? true,
                    intraday_multipliers: response.intraday_multipliers ?? ['1', '5', '15', '30', '60'],
                    has_weekly_and_monthly: response.has_weekly_and_monthly,
                    has_empty_bars: response.has_empty_bars,
                    volume_precision: response.volume_precision,
                    format: response.format ?? 'price',
                };
                onResultReady(result);
            })
                .catch((reason) => {
                logMessage(`UdfCompatibleDatafeed: Error resolving symbol: ${getErrorMessage(reason)}`);
                onError('unknown_symbol');
            });
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
            if (!configurationData.supports_search) {
                throw new Error('Unsupported datafeed configuration. Search is required');
            }
            logMessage(`UdfCompatibleDatafeed: Initialized with ${JSON.stringify(configurationData)}`);
        }
    }
    function defaultConfiguration() {
        return {
            supports_search: true,
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
