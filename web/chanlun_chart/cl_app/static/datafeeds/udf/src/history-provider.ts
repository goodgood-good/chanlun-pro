import {
  Bar,
  HistoryMetadata,
  LibrarySymbolInfo,
  PeriodParams,
} from "../../../charting_library/datafeed-api";

import {
  getErrorMessage,
  RequestParams,
  UdfErrorResponse,
  UdfOkResponse,
  UdfResponse,
} from "./helpers";

import { IRequester } from "./irequester";
import { chartBarTimeSeconds } from "./bar-time";

type StrictStructureMode = "replace" | "unchanged" | "unavailable";

// 冷态 1m/5m 历史需要分页取数并计算结构，服务端实测可超过通用请求的 15 秒上限。
// 只放宽周期切换触发的首个完整快照；配置、报价与实时增量仍由 Requester 的
// 15 秒默认值约束，避免故障时所有请求长时间悬挂。
const DEFAULT_INITIAL_HISTORY_TIMEOUT_MS = 45_000;

interface StrictStructureError {
  code: string;
}

// tslint:disable: no-any
interface HistoryPartialDataResponse extends UdfOkResponse {
  t: number[];
  c: number[];
  o?: never;
  h?: never;
  l?: never;
  v?: never;
  update?: boolean;
}

interface HistoryFullDataResponse extends UdfOkResponse {
  macd_dif?: number[];
  macd_dea?: number[];
  macd_hist?: number[];
  macd_area?: number[];
  higher_macd_dif?: number[];
  higher_macd_dea?: number[];
  higher_macd_hist?: number[];
  t: number[];
  c: number[];
  o: number[];
  h: number[];
  l: number[];
  v: number[];
  fxs: TextPoint[];
  bis: LineSegment[];
  xds: LineSegment[];
  bi_zss: LineSegment[];
  xd_zss: LineSegment[];
  bcs: TextPoint[];
  mmds: TextPoint[];
  bi_mmds?: TextPoint[];
  xd_mmds?: TextPoint[];
  bi_bcs?: TextPoint[];
  xd_bcs?: TextPoint[];
  xd_zslx?: LineSegment[];
  xd_zslx_lines?: LineSegment[];
  recursive_levels?: unknown[];
  higher_zs?: unknown[];
  interval_nest?: unknown;
  update: boolean;
  full_snapshot?: boolean;
  strict_structure_mode?: StrictStructureMode;
  strict_structure?: Record<string, unknown>;
  strict_structure_error?: StrictStructureError;
  chart_color?: Map<string, string>;
}

// tslint:enable: no-any
interface HistoryNoDataResponse extends UdfResponse {
  s: "no_data";
  nextTime?: number;
}

type HistoryResponse =
  | HistoryFullDataResponse
  | HistoryPartialDataResponse
  | HistoryNoDataResponse;

export type PeriodParamsWithOptionalCountback = Omit<
  PeriodParams,
  "countBack"
> & { countBack?: number };

// 定义点位接口
interface Point {
  price: number;
  time: number;
}

// 定义带线型的线段接口
interface LineSegment {
  linestyle: string;
  points: Point[];
}

// 定义带文本的点位接口
interface TextPoint {
  points: Point | Point[];
  text: string;
}

function currentCenterPeriod(resolution: unknown): string {
  const value = String(resolution || "").trim();
  if (/^[1-9][0-9]*$/.test(value)) return `${value}m`;
  if (/^(1?D)$/i.test(value)) return "d";
  return value.toLowerCase();
}

/**
 * `higher_zs` 为跨周期中枢载体，但当前周期也会在其中重复一份 `xd_zss`。
 * 两份数据经过增量合并后可能处于不同版本，页面若读取重复副本就会出现中枢
 * 边界落后、刷新后才恢复。这里把当前周期组强制绑定到已合并的 `xd_zss`；
 * 其他真实周期仍保留各自独立计算结果。
 */
function syncCurrentCenterGroup(
  groups: unknown[] | undefined,
  resolution: unknown,
  currentCenters: LineSegment[]
): unknown[] {
  const period = currentCenterPeriod(resolution);
  const supported = ["1m", "5m", "30m", "d"];
  let found = false;
  const result = (Array.isArray(groups) ? groups : []).map((group) => {
    if (!group || typeof group !== "object" || Array.isArray(group)) return group;
    const record = group as Record<string, unknown>;
    if (String(record.period || "").toLowerCase() !== period) return group;
    found = true;
    return { ...record, zss: currentCenters };
  });
  if (!found && supported.includes(period)) {
    const labels: Record<string, string> = {
      "1m": "1m 中枢",
      "5m": "5m 中枢",
      "30m": "30m 中枢",
      d: "日线 中枢",
    };
    result.push({ period, level_name: labels[period], zss: currentCenters });
  }
  return result;
}

export interface GetBarsResult {
  bars: Bar[];
  meta: HistoryMetadata;
  times?: number[];
  macd_dif?: number[];
  macd_dea?: number[];
  macd_hist?: number[];
  macd_area?: number[];
  higher_macd_dif?: number[];
  higher_macd_dea?: number[];
  higher_macd_hist?: number[];
  fxs: TextPoint[];
  bis: LineSegment[];
  xds: LineSegment[];
  bi_zss: LineSegment[];
  xd_zss: LineSegment[];
  bcs: TextPoint[];
  mmds: TextPoint[];
  bi_mmds?: TextPoint[];
  xd_mmds?: TextPoint[];
  bi_bcs?: TextPoint[];
  xd_bcs?: TextPoint[];
  xd_zslx?: LineSegment[];
  xd_zslx_lines?: LineSegment[];
  recursive_levels?: unknown[];
  higher_zs?: unknown[];
  interval_nest?: unknown;
  strict_structure_mode?: StrictStructureMode;
  strict_structure?: Record<string, unknown>;
  strict_structure_error?: StrictStructureError;
  chart_color?: Map<string, string>;
}

export interface LimitedResponseConfiguration {
  /**
   * Set this value to the maximum number of bars which
   * the data backend server can supply in a single response.
   * This doesn't affect or change the library behavior regarding
   * how many bars it will request. It just allows this Datafeed
   * implementation to correctly handle this situation.
   */
  maxResponseLength: number;
  /**
   * If the server can't return all the required bars in a single
   * response then `expectedOrder` specifies whether the server
   * will send the latest (newest) or earliest (older) data first.
   */
  expectedOrder: "latestFirst" | "earliestFirst";
}

export interface HistoryProviderOptions {
  /**
   * bars_result 的 LRU 上限。默认 100。
   * 用户长时间在多标的多周期间反复切换，bars_result 会无限增长 → 内存泄漏。
   * 超过上限时按插入顺序淘汰最老条目（Map 的迭代顺序即插入顺序）。
   */
  barsResultMaxSize?: number;
  /**
   * 多图表时用于事件分发：每个 datafeed 实例绑定一个 managerId，
   * dispatch 'chanlun-bars-ready' 时回填到 detail，前端 ChartManager 据此过滤
   * 只响应自己 datafeed 触发的事件。
   */
  managerId?: string | null;
  /**
   * 每次 /history 请求都携带的不可变上下文（例如人工复核因果锁）。
   * 这些参数先写入，随后由 UDF 自己的 symbol/resolution/from/to 覆盖，调用方
   * 因而不能借固定参数篡改实际行情请求范围。
   */
  historyParams?: Readonly<RequestParams>;
  /**
   * 周期切换/首次打开时完整历史快照的有界等待时间。默认 45 秒。
   * 该值不会影响配置、报价或 firstDataRequest=false 的实时增量请求。
   */
  initialHistoryTimeoutMs?: number;
}

export class HistoryProvider {
  private _datafeedUrl: string;
  private readonly _requester: IRequester;
  private readonly _limitedServerResponse?: LimitedResponseConfiguration;
  private readonly _options: HistoryProviderOptions;
  private readonly _historyParams: Readonly<RequestParams>;
  private readonly _initialHistoryTimeoutMs: number;
  private readonly _barsResultMaxSize: number;
  private _fullRequestSerial: number = 0;
  private readonly _latestFullRequestByKey: Map<string, number> = new Map();
  public bars_result: Map<string, GetBarsResult>;
  // H1(阶段E): charts.js 断档 gap-reset 前置此一次性标志; getBars(firstDataRequest) 读到即注入
  // force_refresh=1(用后即清),让后端绕过缓存重算补齐断档。public 供 charts.js 外部置位。
  public _forceRefreshOnce: boolean = false;

  public constructor(
    datafeedUrl: string,
    requester: IRequester,
    limitedServerResponse?: LimitedResponseConfiguration,
    options: HistoryProviderOptions = {}
  ) {
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
  private _pruneBarsResult(): void {
    while (this.bars_result.size > this._barsResultMaxSize) {
      const oldestKey = this.bars_result.keys().next().value;
      if (oldestKey === undefined) {
        break;
      }
      this.bars_result.delete(oldestKey);
      this._latestFullRequestByKey.delete(oldestKey);
    }
  }

  private _resultKey(symbol: unknown, resolution: unknown): string {
    return String(symbol || "").toLowerCase() + String(resolution || "").toLowerCase();
  }

  private _beginFullRequest(requestParams: RequestParams): number {
    const resKey = this._resultKey(
      requestParams["symbol"],
      requestParams["resolution"]
    );
    const requestSerial = ++this._fullRequestSerial;
    this._latestFullRequestByKey.set(resKey, requestSerial);
    return requestSerial;
  }

  private _fullRequestIsCurrent(
    requestParams: RequestParams,
    requestGeneration?: number
  ): boolean {
    if (requestGeneration === undefined) {
      return true;
    }
    const resKey = this._resultKey(
      requestParams["symbol"],
      requestParams["resolution"]
    );
    return this._latestFullRequestByKey.get(resKey) === requestGeneration;
  }

  private _resultForCompletedRequest(
    result: GetBarsResult,
    requestParams: RequestParams,
    requestGeneration?: number
  ): GetBarsResult {
    if (this._fullRequestIsCurrent(requestParams, requestGeneration)) {
      return result;
    }
    const resKey = this._resultKey(
      requestParams["symbol"],
      requestParams["resolution"]
    );
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
  public _clearBarsResultForSymbolResolution(symbol: string, resolution: string): void {
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
  private _emitBarsReady(resKey: string, requestParams: RequestParams): void {
    try {
      window.dispatchEvent(new CustomEvent('chanlun-bars-ready', {
        detail: {
          key: resKey,
          symbol: String(requestParams["symbol"] || '').toLowerCase(),
          resolution: String(requestParams["resolution"] || '').toLowerCase(),
          managerId: this._options.managerId || null,
        }
      }));
    } catch (e) { /* SSR 或测试环境无 window 时静默忽略 */ }
  }

  public getBars(
    symbolInfo: LibrarySymbolInfo,
    resolution: string,
    periodParams: PeriodParamsWithOptionalCountback
  ): Promise<GetBarsResult> {
    const requestParams: RequestParams = {
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

    return new Promise(
      async (
        resolve: (result: GetBarsResult) => void,
        reject: (reason: string) => void
      ) => {
        try {
          const initialResponse =
            await this._requester.sendRequest<HistoryResponse>(
              this._datafeedUrl,
              "history",
              requestParams,
              requestTimeoutMs
            );
          const result = this._processHistoryResponse(
            initialResponse,
            requestParams,
            requestGeneration
          );

          if (
            this._limitedServerResponse &&
            this._fullRequestIsCurrent(requestParams, requestGeneration)
          ) {
            await this._processTruncatedResponse(
              result,
              requestParams,
              requestGeneration
            );
          }
          resolve(this._resultForCompletedRequest(
            result,
            requestParams,
            requestGeneration
          ));
        } catch (e: unknown) {
          if (e instanceof Error || typeof e === "string") {
            const reasonString = getErrorMessage(e);
            // tslint:disable-next-line:no-console
            console.warn(
              `HistoryProvider: getBars() failed, error=${reasonString}`
            );
            reject(reasonString);
          }
        }
      }
    );
  }

  private async _processTruncatedResponse(
    result: GetBarsResult,
    requestParams: RequestParams,
    requestGeneration?: number
  ) {
    let lastResultLength = result.bars.length;
    try {
      while (
        this._limitedServerResponse &&
        this._limitedServerResponse.maxResponseLength > 0 &&
        this._limitedServerResponse.maxResponseLength === lastResultLength &&
        requestParams.from < requestParams.to
      ) {
        // adjust request parameters for follow-up request
        if (requestParams.countback) {
          requestParams.countback =
            (requestParams.countback as number) - lastResultLength;
        }
        if (this._limitedServerResponse.expectedOrder === "earliestFirst") {
          requestParams.from = Math.round(
            result.bars[result.bars.length - 1].time / 1000
          );
        } else {
          requestParams.to = Math.round(result.bars[0].time / 1000);
        }

        const followupResponse =
          await this._requester.sendRequest<HistoryResponse>(
            this._datafeedUrl,
            "history",
            requestParams,
            requestGeneration === undefined
              ? undefined
              : this._initialHistoryTimeoutMs
          );
        const followupResult = this._processHistoryResponse(
          followupResponse,
          requestParams,
          requestGeneration
        );
        lastResultLength = followupResult.bars.length;
        // merge result with results collected so far
        if (this._limitedServerResponse.expectedOrder === "earliestFirst") {
          if (
            followupResult.bars[0].time ===
            result.bars[result.bars.length - 1].time
          ) {
            // Datafeed shouldn't include a value exactly matching the `to` timestamp but in case it does
            // we will remove the duplicate.
            followupResult.bars.shift();
          }
          result.bars.push(...followupResult.bars);
        } else {
          if (
            followupResult.bars[followupResult.bars.length - 1].time ===
            result.bars[0].time
          ) {
            // Datafeed shouldn't include a value exactly matching the `to` timestamp but in case it does
            // we will remove the duplicate.
            followupResult.bars.pop();
          }
          result.bars.unshift(...followupResult.bars);
        }
      }
    } catch (e: unknown) {
      /**
       * Error occurred during followup request. We won't reject the original promise
       * because the initial response was valid so we will return what we've got so far.
       */
      if (e instanceof Error || typeof e === "string") {
        const reasonString = getErrorMessage(e);
        // tslint:disable-next-line:no-console
        console.warn(
          `HistoryProvider: getBars() warning during followup request, error=${reasonString}`
        );
      }
    }
  }

  /**
   * SSE 推送复用入口：与 getBars 走同一份 response→bars_result 合并逻辑
   * (_processHistoryResponse)，保证轮询与推送两条路径行为一致、不漂移。
   * 入参 response 为 /tv/history 同构对象(含 s/update/t/o/h/l/c/缠论字段)。
   */
  public applyChanlunUpdate(
    response: HistoryResponse | UdfErrorResponse,
    requestParams: RequestParams
  ): GetBarsResult {
    return this._processHistoryResponse(response, requestParams);
  }

  private _processHistoryResponse(
    response: HistoryResponse | UdfErrorResponse,
    requestParams: RequestParams,
    requestGeneration?: number
  ) {
    if (response.s !== "ok" && response.s !== "no_data") {
      throw new Error(response.errmsg);
    }

    const bars: Bar[] = [];
    const meta: HistoryMetadata = {
      noData: false,
    };

    if (response.s === "no_data") {
      meta.noData = true;
      meta.nextTime = response.nextTime;
    } else {
      const volumePresent = response.v !== undefined;
      const ohlPresent = response.o !== undefined;
      const resolution = String(requestParams["resolution"] || "");

      for (let i = 0; i < response.t.length; ++i) {
        const barValue: Bar = {
          time: chartBarTimeSeconds(response.t[i], resolution) * 1000,
          close: response.c[i],
          open: response.c[i],
          high: response.c[i],
          low: response.c[i],
        };

        if (ohlPresent) {
          barValue.open = (response as HistoryFullDataResponse).o[i];
          barValue.high = (response as HistoryFullDataResponse).h[i];
          barValue.low = (response as HistoryFullDataResponse).l[i];
        }

        if (volumePresent) {
          barValue.volume = (response as HistoryFullDataResponse).v[i];
        }

        bars.push(barValue);
      }

      // 设置保存的key
      const res_key = this._resultKey(
        requestParams["symbol"],
        requestParams["resolution"]
      );

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
      const existingMaxBarMs =
        obj_res && obj_res.bars && obj_res.bars.length > 0
          ? obj_res.bars[obj_res.bars.length - 1].time
          : undefined;
      const requestTo = Number(requestParams.to);
      const requestFrom = Number(requestParams.from);
      const isRecentWindowAuthoritative =
        Number.isFinite(requestTo) &&
        Number.isFinite(requestFrom) &&
        existingMaxBarMs !== undefined &&
        requestTo * 1000 >= existingMaxBarMs;
      // 形态的 points[].time 与 requestParams.from/to 同为"秒"单位(后端 fun.datetime_to_int
      // 与 UDF from/to 一致)，此处窗口边界无需 *1000。
      const windowFrom: number | undefined = isRecentWindowAuthoritative ? requestFrom : undefined;
      const windowTo: number | undefined = isRecentWindowAuthoritative ? requestTo : undefined;
      const isSourceTimeInAuthoritativeWindow = (sourceTime: unknown): boolean => {
        if (
          windowFrom === undefined ||
          windowTo === undefined ||
          !Number.isInteger(sourceTime)
        ) return false;
        const chartTime = chartBarTimeSeconds(sourceTime as number, resolution);
        return chartTime >= windowFrom && chartTime <= windowTo;
      };

      const raw_times = (response.t || []).map((t: number) => t * 1000);
      const macd_dif = (response as HistoryFullDataResponse).macd_dif || [];
      const macd_dea = (response as HistoryFullDataResponse).macd_dea || [];
      const macd_hist = (response as HistoryFullDataResponse).macd_hist || [];
      const macd_area = (response as HistoryFullDataResponse).macd_area || [];
      const higher_macd_dif = (response as HistoryFullDataResponse).higher_macd_dif || [];
      const higher_macd_dea = (response as HistoryFullDataResponse).higher_macd_dea || [];
      const higher_macd_hist = (response as HistoryFullDataResponse).higher_macd_hist || [];

      const mergeAlignedArrays = (existingTimes: number[] = [], existingArr: number[] = [], newTimes: number[] = [], newArr: number[] = []) => {
          const map = new Map<number, number>();
          existingTimes.forEach((t, i) => {
              let val = existingArr[i];
              if (val === null || val === undefined) val = NaN;
              map.set(t, val);
          });
          newTimes.forEach((t, i) => {
              let val = newArr[i];
              if (val === null || val === undefined) val = NaN;
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

      const generationIsCurrent =
        requestGeneration === undefined ||
        this._latestFullRequestByKey.get(res_key) === requestGeneration;
      const incomingLastRawMs = raw_times.length > 0
        ? raw_times[raw_times.length - 1]
        : undefined;
      const existingRawTimes = obj_res?.times || [];
      const existingLastRawMs = existingRawTimes.length > 0
        ? existingRawTimes[existingRawTimes.length - 1]
        : undefined;
      const isAuthoritativeSnapshot =
        response.update === false ||
        (response as HistoryFullDataResponse).full_snapshot === true;
      const isRegressiveFullSnapshot =
        isAuthoritativeSnapshot &&
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
          fxs: (response as HistoryFullDataResponse).fxs,
          bis: (response as HistoryFullDataResponse).bis,
          xds: (response as HistoryFullDataResponse).xds,
          bi_zss: (response as HistoryFullDataResponse).bi_zss,
          xd_zss: (response as HistoryFullDataResponse).xd_zss,
          bcs: (response as HistoryFullDataResponse).bcs,
          mmds: (response as HistoryFullDataResponse).mmds,
          bi_mmds: (response as HistoryFullDataResponse).bi_mmds || [],
          xd_mmds: (response as HistoryFullDataResponse).xd_mmds || [],
          bi_bcs: (response as HistoryFullDataResponse).bi_bcs || [],
          xd_bcs: (response as HistoryFullDataResponse).xd_bcs || [],
          xd_zslx: (response as HistoryFullDataResponse).xd_zslx || [],
          xd_zslx_lines: (response as HistoryFullDataResponse).xd_zslx_lines || [],
          recursive_levels: (response as HistoryFullDataResponse).recursive_levels || [],
          higher_zs: syncCurrentCenterGroup(
            (response as HistoryFullDataResponse).higher_zs,
            resolution,
            (response as HistoryFullDataResponse).xd_zss || []
          ),
          interval_nest: (response as HistoryFullDataResponse).interval_nest,
          strict_structure_mode: (response as HistoryFullDataResponse).strict_structure_mode,
          strict_structure: (response as HistoryFullDataResponse).strict_structure,
          strict_structure_error: (response as HistoryFullDataResponse).strict_structure_error,
          chart_color: (response as HistoryFullDataResponse).chart_color,
        });
        this._pruneBarsResult();
        this._emitBarsReady(res_key, requestParams);
      } else if (canWriteCache && obj_res !== undefined) {
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
        const updateTextPoints = (
          existingPoints: TextPoint[],
          newPoints: TextPoint[]
        ): TextPoint[] => {
          // 获取点位时间的辅助函数，处理points可能是对象或数组的情况
          const getPointTime = (point: TextPoint): number => {
            if (Array.isArray(point.points)) {
              // 如果是数组，取第一个元素的time
              return point.points[0].time;
            } else {
              // 如果是单个对象，直接取time
              return point.points.time;
            }
          };

          if (!newPoints || newPoints.length === 0) {
            if (!existingPoints || existingPoints.length === 0) return [];
            if (windowFrom === undefined || windowTo === undefined) return existingPoints;
            return existingPoints.filter((p) => {
              const t = getPointTime(p);
              return !isSourceTimeInAuthoritativeWindow(t);
            });
          }
          if (!existingPoints || existingPoints.length === 0) return newPoints;

          // Round11 BUG2 修复(对称 updateLineSegments 的 key-upsert): 原按 minResponseTime 时间切割只保留
          // 早于新响应最早点的旧点——向左滚动时新响应是更旧窗口(min 小), 右侧最近的分型/买卖点/背驰
          // (time 大)全不 < min → 被丢弃(与已硬化线段不对称)。改 time+price+text 身份 upsert:
          // 权威窗口内(右侧最新窗)未被新响应提及的旧点剔除(去幽灵), 向左滚动(window undefined)只增不删。
          const isTextInAuthWindow = (p: TextPoint): boolean => {
            if (windowFrom === undefined || windowTo === undefined) return false;
            const tt = getPointTime(p);
            return isSourceTimeInAuthoritativeWindow(tt);
          };
          const textPointKey = (p: TextPoint): string => {
            const tt = getPointTime(p);
            const pp = Array.isArray(p.points) ? p.points[0].price : p.points.price;
            return `${tt}_${pp}_${p.text || ''}`;
          };
          const mergedTextByKey = new Map<string, TextPoint>();
          for (const point of existingPoints) {
            if (!isTextInAuthWindow(point)) {
              mergedTextByKey.set(textPointKey(point), point);
            }
          }
          for (const point of newPoints) {
            mergedTextByKey.set(textPointKey(point), point);
          }
          return Array.from(mergedTextByKey.values()).sort(
            (a, b) => getPointTime(a) - getPointTime(b)
          );
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
        const updateLineSegments = (
          existingSegments: LineSegment[],
          newSegments: LineSegment[]
        ): LineSegment[] => {
          const isInAuthoritativeWindow = (s: LineSegment): boolean => {
            if (windowFrom === undefined || windowTo === undefined) return false;
            const t = s.points[0] && s.points[0].time;
            return isSourceTimeInAuthoritativeWindow(t);
          };

          if (!newSegments || newSegments.length === 0) {
            if (!existingSegments || existingSegments.length === 0) return [];
            if (windowFrom === undefined || windowTo === undefined) return existingSegments;
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
          const segmentKey = (s: LineSegment): string => {
            const head = s.points[0];
            return `${head.time}_${head.price}`;
          };

          // 新响应是否带来"未完成段"(linestyle=1)。SSE 全量快照含当前唯一的未完成段;
          // 向左滚动的历史区间响应则不含。
          const newHasPending = newSegments.some(
            (s) => s.points && s.points.length > 0 && Number(s.linestyle) === 1
          );

          const merged = new Map<string, LineSegment>();
          for (const segment of existingSegments) {
            // 丢弃条件(任一成立即丢，交由下面 newSegments 决定是否以新版本重新加入)：
            // ① 新响应带来当前未完成段时，旧的未完成段一律丢——未完成段起点会随重算
            //    漂移，旧 head key 不会被新版本覆盖，否则累积"多个未完成笔"；
            // ② 该段起点落在本次权威窗口内——右侧最新窗口的响应即权威真相，窗口内
            //    未被新响应提及的已完成段已被证伪，不能只增不删地累积幽灵形态。
            if (
              segment.points.length > 0 &&
              !(newHasPending && Number(segment.linestyle) === 1) &&
              !isInAuthoritativeWindow(segment)
            ) {
              merged.set(segmentKey(segment), segment);
            }
          }
          for (const segment of newSegments) {
            if (segment.points.length > 0) {
              merged.set(segmentKey(segment), segment);
            }
          }

          return Array.from(merged.values()).sort((a, b) => {
            if (a.points.length === 0 && b.points.length === 0) return 0;
            if (a.points.length === 0) return -1;
            if (b.points.length === 0) return 1;
            return a.points[0].time - b.points[0].time;
          });
        };

        // 更新所有数据
        obj_res.fxs = updateTextPoints(
          obj_res.fxs,
          (response as HistoryFullDataResponse).fxs
        );
        obj_res.bis = updateLineSegments(
          obj_res.bis,
          (response as HistoryFullDataResponse).bis
        );
        obj_res.xds = updateLineSegments(
          obj_res.xds,
          (response as HistoryFullDataResponse).xds
        );
        obj_res.bi_zss = updateLineSegments(
          obj_res.bi_zss,
          (response as HistoryFullDataResponse).bi_zss
        );
        obj_res.xd_zss = updateLineSegments(
          obj_res.xd_zss,
          (response as HistoryFullDataResponse).xd_zss
        );
        obj_res.bcs = updateTextPoints(
          obj_res.bcs,
          (response as HistoryFullDataResponse).bcs
        );
        obj_res.mmds = updateTextPoints(
          obj_res.mmds,
          (response as HistoryFullDataResponse).mmds
        );
        obj_res.bi_mmds = updateTextPoints(
          obj_res.bi_mmds || [],
          (response as HistoryFullDataResponse).bi_mmds || []
        );
        obj_res.xd_mmds = updateTextPoints(
          obj_res.xd_mmds || [],
          (response as HistoryFullDataResponse).xd_mmds || []
        );
        obj_res.bi_bcs = updateTextPoints(
          obj_res.bi_bcs || [],
          (response as HistoryFullDataResponse).bi_bcs || []
        );
        obj_res.xd_bcs = updateTextPoints(
          obj_res.xd_bcs || [],
          (response as HistoryFullDataResponse).xd_bcs || []
        );
        obj_res.xd_zslx = updateLineSegments(
          obj_res.xd_zslx || [],
          (response as HistoryFullDataResponse).xd_zslx || []
        );
        obj_res.xd_zslx_lines = updateLineSegments(
          obj_res.xd_zslx_lines || [],
          (response as HistoryFullDataResponse).xd_zslx_lines || []
        );
        // SSE 全量快照(prepend 产出完整 chart_data):形态列表直接整体替换,绕过上面为"部分响应
        // (向左滚动)"设计的 updateXxx 合并 → 从根上杜绝任何形态(笔/线段/中枢/走势类型/分型/
        // 买卖点/背驰)的"只增不删"陈旧累积(如多个未完成笔)。K线 bars/MACD 仍走下面增量合并
        // 保持视图不重置、随末根推进。scroll 等部分响应不带 full_snapshot, 仍走上面的合并(兜底)。
        if ((response as HistoryFullDataResponse).full_snapshot) {
          obj_res.fxs = (response as HistoryFullDataResponse).fxs || [];
          obj_res.bis = (response as HistoryFullDataResponse).bis || [];
          obj_res.xds = (response as HistoryFullDataResponse).xds || [];
          obj_res.bi_zss = (response as HistoryFullDataResponse).bi_zss || [];
          obj_res.xd_zss = (response as HistoryFullDataResponse).xd_zss || [];
          obj_res.bcs = (response as HistoryFullDataResponse).bcs || [];
          obj_res.mmds = (response as HistoryFullDataResponse).mmds || [];
          obj_res.bi_mmds = (response as HistoryFullDataResponse).bi_mmds || [];
          obj_res.xd_mmds = (response as HistoryFullDataResponse).xd_mmds || [];
          obj_res.bi_bcs = (response as HistoryFullDataResponse).bi_bcs || [];
          obj_res.xd_bcs = (response as HistoryFullDataResponse).xd_bcs || [];
          obj_res.xd_zslx = (response as HistoryFullDataResponse).xd_zslx || [];
          obj_res.xd_zslx_lines = (response as HistoryFullDataResponse).xd_zslx_lines || [];
        }
        // R1-C7: recursive_levels 的嵌套 mmds/bcs 与顶层同为「按窗口裁切的单点形态」,
        // 无条件整体替换会让 backward(向左滚动)响应用老窗口(通常为空)clobber 右侧
        // 最新高级别买卖点/背驰(R11 BUG2 只修顶层 mmds/bcs 的兄弟盲区)。full_snapshot
        // 仍整体替换; 非快照按 level 键对齐, 嵌套 mmds/bcs 走 updateTextPoints 同口径
        // 合并(backward 只增不删/右侧权威窗内被证伪剔除), zss/zslx_lines 等后端全局
        // 透传字段以新响应为准。
        {
          const newLevels =
            (response as HistoryFullDataResponse).recursive_levels || [];
          if ((response as HistoryFullDataResponse).full_snapshot) {
            obj_res.recursive_levels = newLevels;
          } else {
            const oldByLevel = new Map<any, any>();
            (obj_res.recursive_levels || []).forEach((lv: any, i: number) => {
              oldByLevel.set(lv && lv.level !== undefined ? lv.level : i, lv);
            });
            obj_res.recursive_levels = newLevels.map((lv: any, i: number) => {
              if (!lv || typeof lv !== 'object') return lv;
              const key = lv.level !== undefined ? lv.level : i;
              const old = oldByLevel.get(key);
              if (!old || typeof old !== 'object') return lv;
              const merged: any = Object.assign({}, lv);
              merged.mmds = updateTextPoints(old.mmds || [], lv.mmds || []);
              merged.bcs = updateTextPoints(old.bcs || [], lv.bcs || []);
              return merged;
            });
          }
        }
        // higher_zs 是全局四周期快照，向左翻历史的窄窗口响应不得把右侧当前
        // 快照覆盖掉。权威最新窗口才整体替换；随后无条件把当前周期组同步到
        // 已经完成合并/全量替换的 xd_zss，消除同一中枢的双版本来源。
        if (
          (response as HistoryFullDataResponse).full_snapshot ||
          isRecentWindowAuthoritative
        ) {
          obj_res.higher_zs =
            (response as HistoryFullDataResponse).higher_zs || [];
        }
        obj_res.higher_zs = syncCurrentCenterGroup(
          obj_res.higher_zs,
          resolution,
          obj_res.xd_zss || []
        );
        obj_res.interval_nest = (response as HistoryFullDataResponse).interval_nest;
        obj_res.chart_color = (response as HistoryFullDataResponse).chart_color;

        // ⚠ 增量更新 K线 bars：原 else 分支只更新缠论形态+MACD，漏了 obj_res.bars，
        // 导致 SSE 推送(update:true)缠论更新而 K线 lastBar 不动。保留旧 bars 中早于新数据
        // 首根的，追加本次 bars，让 K线随 SSE 实时推进(与 dist/bundle.js 同步)。
        if (bars.length > 0) {
          // Round11 BUG1 修复: 原"保留早于新首根的旧bars + concat"假设新bars恒为最近后缀; 向左滚动时新bars
          // 是更旧窗口(newFirstTime小)→ keptBars空 → 最近K线被clobber → _getViewLatestSec读到陈旧末根
          // → 下一SSE帧/看门狗误判巨隙 → resetData视图弹回最新, 毁盘中回看。改按 time 并集(新覆盖同time)。
          const barByTime = new Map<number, Bar>();
          for (const bar of (obj_res.bars || [])) barByTime.set(bar.time, bar);
          for (const bar of bars) barByTime.set(bar.time, { ...bar });
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

        const strictMode = (response as HistoryFullDataResponse).strict_structure_mode;
        if (strictMode === "replace") {
          const strictStructure = (response as HistoryFullDataResponse).strict_structure;
          if (
            strictStructure &&
            strictStructure.schema === "chanlun-chart-structure/v5"
          ) {
            obj_res.strict_structure_mode = "replace";
            obj_res.strict_structure = strictStructure;
            delete obj_res.strict_structure_error;
          } else {
            obj_res.strict_structure_mode = "unavailable";
            delete obj_res.strict_structure;
            obj_res.strict_structure_error = {
              code: "strict_transport_invalid",
            };
          }
        } else if (strictMode === "unavailable") {
          obj_res.strict_structure_mode = "unavailable";
          delete obj_res.strict_structure;
          obj_res.strict_structure_error =
            (response as HistoryFullDataResponse).strict_structure_error || {
              code: "strict_evidence_invalid",
            };
        } else if (strictMode === "unchanged") {
          // Preserve the last atomic snapshot as cached evidence, but expose
          // the transport mode verbatim.  Leaving the old "replace" mode in
          // place makes pagination/realtime merges re-validate a stale payload
          // as though it arrived with the newly merged bars.
          obj_res.strict_structure_mode = "unchanged";
        } else if (strictMode !== "unchanged" && strictMode !== undefined) {
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
      fxs: (response as HistoryFullDataResponse).fxs,
      bis: (response as HistoryFullDataResponse).bis,
      xds: (response as HistoryFullDataResponse).xds,
      bi_zss: (response as HistoryFullDataResponse).bi_zss,
      xd_zss: (response as HistoryFullDataResponse).xd_zss,
      bcs: (response as HistoryFullDataResponse).bcs,
      mmds: (response as HistoryFullDataResponse).mmds,
      bi_mmds: (response as HistoryFullDataResponse).bi_mmds || [],
      xd_mmds: (response as HistoryFullDataResponse).xd_mmds || [],
      bi_bcs: (response as HistoryFullDataResponse).bi_bcs || [],
      xd_bcs: (response as HistoryFullDataResponse).xd_bcs || [],
      xd_zslx: (response as HistoryFullDataResponse).xd_zslx || [],
      xd_zslx_lines: (response as HistoryFullDataResponse).xd_zslx_lines || [],
      recursive_levels: (response as HistoryFullDataResponse).recursive_levels || [],
      higher_zs: syncCurrentCenterGroup(
        (response as HistoryFullDataResponse).higher_zs,
        requestParams["resolution"],
        (response as HistoryFullDataResponse).xd_zss || []
      ),
      interval_nest: (response as HistoryFullDataResponse).interval_nest,
      strict_structure_mode: (response as HistoryFullDataResponse).strict_structure_mode,
      strict_structure: (response as HistoryFullDataResponse).strict_structure,
      strict_structure_error: (response as HistoryFullDataResponse).strict_structure_error,
      chart_color: (response as HistoryFullDataResponse).chart_color,
    };

    return result;
  }
}
