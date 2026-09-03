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
// 首次冷态请求若刚好撞上服务启动或缓存落盘，服务端可能在客户端超时后不久完成。
// 自动重试一次即可命中刚生成的缓存，避免图表永久停在“这里没有数据”等待手动刷新。
const INITIAL_HISTORY_RETRY_DELAY_MS = 750;

function decodeNumericDeltaColumn(
  values: number[] = [],
  scale: number | undefined,
  label: string,
  allowNull: boolean = true
): number[] {
  if (scale === undefined) return values;
  if (!Number.isSafeInteger(scale) || scale <= 0) {
    throw new Error(`${label} delta scale is invalid`);
  }
  let previous = 0;
  return values.map((delta) => {
    if (delta === null || delta === undefined) {
      if (allowNull) return NaN;
      throw new Error(`${label} delta contains a null value`);
    }
    if (!Number.isSafeInteger(delta)) {
      throw new Error(`${label} delta contains an unsafe integer`);
    }
    const current = previous + delta;
    if (!Number.isSafeInteger(current)) {
      throw new Error(`${label} delta accumulator is unsafe`);
    }
    previous = current;
    return current / scale;
  });
}

interface StrictStructureError {
  code: string;
}

interface HistoryFullDataResponse extends UdfOkResponse {
  history_floor?: number;
  macd_delta_scale?: number;
  time_delta?: boolean;
  macd_dif?: number[];
  macd_dea?: number[];
  macd_hist?: number[];
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
  update: boolean;
  full_snapshot?: boolean;
  strict_structure_mode?: StrictStructureMode;
  strict_structure?: Record<string, unknown>;
  strict_structure_error?: StrictStructureError;
}
interface HistoryNoDataResponse extends UdfResponse {
  s: "no_data";
  nextTime?: number;
}

type HistoryResponse =
  | HistoryFullDataResponse
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
  state?: "forming" | "formed" | "locked";
  locked?: boolean;
  points: Point[];
}

// 定义带文本的点位接口
interface TextPoint {
  points: Point | Point[];
  text: string;
}

export interface GetBarsResult {
  bars: Bar[];
  meta: HistoryMetadata;
  times?: number[];
  macd_dif?: number[];
  macd_dea?: number[];
  macd_hist?: number[];
  higher_macd_dif?: number[];
  higher_macd_dea?: number[];
  higher_macd_hist?: number[];
  fxs: TextPoint[];
  bis: LineSegment[];
  xds: LineSegment[];
  strict_structure_mode?: StrictStructureMode;
  strict_structure?: Record<string, unknown>;
  strict_structure_error?: StrictStructureError;
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
  private readonly _completeHistoryFloorByKey: Map<string, number> = new Map();
  private _activeHistoryRequests: number = 0;
  private _lastHistorySettledAt: number = 0;
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

  /**
   * 图表切换的原子展示闸门使用此状态，避免 TradingView 首帧完成后仍有
   * 向左补历史请求在运行，导致用户先看到少量 K 线、随后又整批扩展。
   */
  public hasPendingHistoryWork(quietPeriodMs: number = 0): boolean {
    if (this._activeHistoryRequests > 0) {
      return true;
    }
    const quietMs = Math.max(0, Number(quietPeriodMs) || 0);
    return quietMs > 0 &&
      this._lastHistorySettledAt > 0 &&
      Date.now() - this._lastHistorySettledAt < quietMs;
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
      this._completeHistoryFloorByKey.delete(oldestKey);
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
    return this._fullRequestSerial === requestGeneration &&
      this._latestFullRequestByKey.get(resKey) === requestGeneration;
  }

  private _resultForCompletedRequest(
    result: GetBarsResult,
    requestParams: RequestParams,
    requestGeneration?: number
  ): GetBarsResult {
    const resKey = this._resultKey(
      requestParams["symbol"],
      requestParams["resolution"]
    );
    const current = this.bars_result.get(resKey);
    const requestIsCurrent = this._fullRequestIsCurrent(
      requestParams,
      requestGeneration
    );
    if (requestIsCurrent) {
      const resultLastRaw = result.times?.[result.times.length - 1];
      const currentLastRaw = current?.times?.[current.times.length - 1];
      const resultLast = result.bars[result.bars.length - 1]?.time;
      const currentLast = current?.bars[current.bars.length - 1]?.time;
      // A current-generation request can still finish with an older durable
      // snapshot than the SSE/polling aggregate already held in memory.  The
      // cache write is rejected below; return that same non-regressive cache to
      // TradingView as well so the visible chart cannot jump backwards.
      const currentIsAhead =
        resultLastRaw !== undefined && currentLastRaw !== undefined
          ? currentLastRaw > resultLastRaw
          : resultLast !== undefined && currentLast !== undefined
            ? currentLast > resultLast
            : false;
      if (requestGeneration === undefined || current === undefined || !currentIsAhead) {
        return result;
      }
    }
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
    this._completeHistoryFloorByKey.delete(resKey);
    // Deleting the generation invalidates every response that was already in
    // flight.  A later request receives a globally unique serial.
    const invalidatedGeneration = this._latestFullRequestByKey.get(resKey);
    this._latestFullRequestByKey.delete(resKey);
    if (invalidatedGeneration === this._fullRequestSerial) {
      this._fullRequestSerial += 1;
    }
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

  private async _requestHistoryWithStartupRetry(
    requestParams: RequestParams,
    requestTimeoutMs: number | undefined,
    requestGeneration: number | undefined
  ): Promise<HistoryResponse | UdfErrorResponse> {
    try {
      return await this._requester.sendRequest<HistoryResponse>(
        this._datafeedUrl,
        "history",
        requestParams,
        requestTimeoutMs,
        requestGeneration === undefined ? undefined : "history-initial"
      );
    } catch (error: unknown) {
      const reasonString =
        error instanceof Error || typeof error === "string"
          ? getErrorMessage(error)
          : "";
      const retryableStartupTimeout =
        requestGeneration !== undefined &&
        reasonString.startsWith("Request timed out after ") &&
        this._fullRequestIsCurrent(requestParams, requestGeneration);
      if (!retryableStartupTimeout) {
        throw error;
      }

      // tslint:disable-next-line:no-console
      console.warn(
        `HistoryProvider: initial history timed out; retrying once after ${INITIAL_HISTORY_RETRY_DELAY_MS}ms`
      );
      await new Promise<void>((resolve) => {
        setTimeout(resolve, INITIAL_HISTORY_RETRY_DELAY_MS);
      });
      // 用户可能在退避期间切换标的/周期。旧请求不得再制造额外后端负载。
      if (!this._fullRequestIsCurrent(requestParams, requestGeneration)) {
        throw error;
      }
      return this._requester.sendRequest<HistoryResponse>(
        this._datafeedUrl,
        "history",
        requestParams,
        requestTimeoutMs,
        requestGeneration === undefined ? undefined : "history-initial"
      );
    }
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
    if (String(this._historyParams.embedded || "") === "1") {
      requestParams.numeric_delta = 1;
    }
    if (periodParams.countBack !== undefined) {
      const atomicFullInitialHistory = (
        periodParams.firstDataRequest === true
        && (
          String(this._historyParams.embedded || "") === "1"
          || String(this._historyParams.atomic_initial || "") === "1"
        )
      );
      // 原子展示页面不能先接收 countback 短帧，再于遮罩解除后补齐历史。
      // 首次请求不传 countback，使 /tv/history 在同一响应里返回当前完整缓存；
      // 其他通用 datafeed 调用方仍保留 TradingView 原生 countback 语义。
      if (!atomicFullInitialHistory) {
        requestParams.countback = periodParams.countBack;
      }
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

    const resKey = this._resultKey(
      requestParams["symbol"],
      requestParams["resolution"]
    );
    const completeHistoryFloor = this._completeHistoryFloorByKey.get(resKey);
    if (
      periodParams.firstDataRequest !== true &&
      String(this._historyParams.embedded || "") === "1" &&
      completeHistoryFloor !== undefined &&
      Number.isFinite(Number(periodParams.from)) &&
      Number.isFinite(Number(periodParams.to)) &&
      Number(periodParams.from) <= Number(periodParams.to) &&
      Number(periodParams.to) <= completeHistoryFloor
    ) {
      return Promise.resolve({
        bars: [],
        meta: { noData: true },
        fxs: [],
        bis: [],
        xds: [],
      });
    }

    const requestGeneration = periodParams.firstDataRequest
      ? this._beginFullRequest(requestParams)
      : undefined;
    const requestTimeoutMs = requestGeneration === undefined
      ? undefined
      : this._initialHistoryTimeoutMs;

    this._activeHistoryRequests += 1;
    return new Promise(
      async (
        resolve: (result: GetBarsResult) => void,
        reject: (reason: string) => void
      ) => {
        try {
          const initialResponse = await this._requestHistoryWithStartupRetry(
            requestParams,
            requestTimeoutMs,
            requestGeneration
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
          const reasonString =
            e instanceof Error || typeof e === "string"
              ? getErrorMessage(e)
              : "Unknown history request failure";
          const supersededAbort =
            requestGeneration !== undefined &&
            !this._fullRequestIsCurrent(requestParams, requestGeneration) &&
            /abort/i.test(reasonString);
          if (!supersededAbort) {
            // tslint:disable-next-line:no-console
            console.warn(
              `HistoryProvider: getBars() failed, error=${reasonString}`
            );
          }
          reject(reasonString);
        }
      }
    ).finally(() => {
      this._activeHistoryRequests = Math.max(0, this._activeHistoryRequests - 1);
      this._lastHistorySettledAt = Date.now();
    });
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
              : this._initialHistoryTimeoutMs,
            requestGeneration === undefined ? undefined : "history-initial"
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
    let resultTimes: number[] | undefined;
    const meta: HistoryMetadata = {
      noData: false,
    };

    if (response.s === "no_data") {
      meta.noData = true;
      meta.nextTime = response.nextTime;
    } else {
      const resolution = String(requestParams["resolution"] || "");
	  const symbol = String(requestParams["symbol"] || "");

      const fullResponse = response as HistoryFullDataResponse;
      const responseTimes = decodeNumericDeltaColumn(
        fullResponse.t,
        fullResponse.time_delta === true ? 1 : undefined,
        "history time",
        false
      );

      const res_key = this._resultKey(
        requestParams["symbol"],
        requestParams["resolution"]
      );
      const historyFloor = Number(fullResponse.history_floor);

      for (let i = 0; i < responseTimes.length; ++i) {
        const barValue: Bar = {
          time: chartBarTimeSeconds(responseTimes[i], resolution, symbol) * 1000,
          close: response.c[i],
          open: response.o[i],
          high: response.h[i],
          low: response.l[i],
          volume: response.v[i],
        };
        bars.push(barValue);
      }

      // 设置保存的key
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
        const chartTime = chartBarTimeSeconds(sourceTime as number, resolution, symbol);
        return chartTime >= windowFrom && chartTime <= windowTo;
      };

      const raw_times = responseTimes.map((t: number) => t * 1000);
      resultTimes = raw_times;
      const macdScale = fullResponse.macd_delta_scale;
      const macd_dif = decodeNumericDeltaColumn(
        fullResponse.macd_dif || [], macdScale, "macd_dif"
      );
      const macd_dea = decodeNumericDeltaColumn(
        fullResponse.macd_dea || [], macdScale, "macd_dea"
      );
      const macd_hist = decodeNumericDeltaColumn(
        fullResponse.macd_hist || [], macdScale, "macd_hist"
      );
      const higher_macd_dif = decodeNumericDeltaColumn(
        fullResponse.higher_macd_dif || [], macdScale, "higher_macd_dif"
      );
      const higher_macd_dea = decodeNumericDeltaColumn(
        fullResponse.higher_macd_dea || [], macdScale, "higher_macd_dea"
      );
      const higher_macd_hist = decodeNumericDeltaColumn(
        fullResponse.higher_macd_hist || [], macdScale, "higher_macd_hist"
      );

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
      if (
        canWriteCache &&
        requestGeneration !== undefined &&
        Number.isSafeInteger(historyFloor) &&
        historyFloor > 0 &&
        responseTimes.length > 0 &&
        historyFloor === responseTimes[0]
      ) {
        this._completeHistoryFloorByKey.set(res_key, historyFloor);
      }

      if (canWriteCache && (response.update == false || obj_res == undefined)) {
        const difObj = mergeAlignedArrays([], [], raw_times, macd_dif);
        const deaObj = mergeAlignedArrays([], [], raw_times, macd_dea);
        const histObj = mergeAlignedArrays([], [], raw_times, macd_hist);
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
          higher_macd_dif: hDifObj.values,
          higher_macd_dea: hDeaObj.values,
          higher_macd_hist: hHistObj.values,
          fxs: (response as HistoryFullDataResponse).fxs,
          bis: (response as HistoryFullDataResponse).bis,
          xds: (response as HistoryFullDataResponse).xds,
          strict_structure_mode: (response as HistoryFullDataResponse).strict_structure_mode,
          strict_structure: (response as HistoryFullDataResponse).strict_structure,
          strict_structure_error: (response as HistoryFullDataResponse).strict_structure_error,
        });
        this._pruneBarsResult();
        this._emitBarsReady(res_key, requestParams);
      } else if (canWriteCache && obj_res !== undefined) {
        // 更新存在的数据
        // 更新逻辑，找到大于等于返回的第一个时间的所有数据；
        // 保留小于返回的第一个时间的所有数据；
        // 然后添加返回的数据；
        // 最后按时间排序；

        // 分型是单点基础图元；权威窗口内未再次出现的点应被删除。
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

          // 权威窗口内按身份替换；向左分页只合并，不删除右侧图元。
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
          // 一根线段从某个起点出发，任意时刻只该有一个状态——
          // 端点(tail)随 K 线包含合并会漂移（CLKline.k.date 会变），
          // 状态也会从 forming 翻成 formed，再翻成 locked，
          // 把它们写进 key 会同时保留同一根段的前后状态 → 视觉上线段重叠/断裂。
          // 用 head 作为身份，Map.set 让本次响应覆盖先前状态即可。
          const segmentKey = (s: LineSegment): string => {
            const head = s.points[0];
            return `${head.time}_${head.price}`;
          };

          const isForming = (segment: LineSegment): boolean => {
            const state = String(segment.state || "").toLowerCase();
            return state ? state === "forming" : Number(segment.linestyle) === 1;
          };

          // 新响应是否带来形成中尾段。SSE 全量快照含当前唯一的形成中尾段；
          // 向左滚动的历史区间响应则不含。
          const newHasPending = newSegments.some(
            (s) => s.points && s.points.length > 0 && isForming(s)
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
              !(newHasPending && isForming(segment)) &&
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
        // SSE 全量快照直接整体替换基础图元。K线 bars/MACD 仍走下面增量合并
        // 保持视图不重置、随末根推进。scroll 等部分响应不带 full_snapshot, 仍走上面的合并(兜底)。
        if ((response as HistoryFullDataResponse).full_snapshot) {
          obj_res.fxs = (response as HistoryFullDataResponse).fxs || [];
          obj_res.bis = (response as HistoryFullDataResponse).bis || [];
          obj_res.xds = (response as HistoryFullDataResponse).xds || [];
        }
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
        const hDifObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_dif, raw_times, higher_macd_dif);
        const hDeaObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_dea, raw_times, higher_macd_dea);
        const hHistObj = mergeAlignedArrays(oldTimes, obj_res.higher_macd_hist, raw_times, higher_macd_hist);

        obj_res.times = difObj.times;
        obj_res.macd_dif = difObj.values;
        obj_res.macd_dea = deaObj.values;
        obj_res.macd_hist = histObj.values;
        obj_res.higher_macd_dif = hDifObj.values;
        obj_res.higher_macd_dea = hDeaObj.values;
        obj_res.higher_macd_hist = hHistObj.values;

        const strictMode = (response as HistoryFullDataResponse).strict_structure_mode;
        if (strictMode === "replace") {
          const strictStructure = (response as HistoryFullDataResponse).strict_structure;
          if (
            strictStructure &&
            strictStructure.schema === "chanlun-chart-structure"
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
          const cachedStrict = obj_res.strict_structure;
          const mergedLastRawMs = obj_res.times.length > 0
            ? obj_res.times[obj_res.times.length - 1]
            : undefined;
          const cachedSourceClosedAt = Number(cachedStrict?.source_closed_at);
          const cachedSnapshotStillAtomic = Boolean(
            cachedStrict
            && cachedStrict.schema === "chanlun-chart-structure"
            && Number.isInteger(cachedSourceClosedAt)
            && mergedLastRawMs !== undefined
            && cachedSourceClosedAt * 1000 === mergedLastRawMs
          );
          // ``unchanged`` is a transport delta, while bars_result represents
          // effective aggregate state. A backward pagination response can
          // arrive before charts.js consumes the initial ``replace`` snapshot.
          // Downgrading the aggregate to ``unchanged`` in that window loses the
          // only consumable snapshot and produces strict_snapshot_missing.
          // Keep ``replace`` while its source close still equals the merged bar
          // tail. If realtime advanced the tail, expose ``unchanged`` so the
          // chart may retain only an already-validated prior snapshot.
          obj_res.strict_structure_mode = cachedSnapshotStillAtomic
            ? "replace"
            : "unchanged";
          if (cachedSnapshotStillAtomic) delete obj_res.strict_structure_error;
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
      times: resultTimes,
      fxs: (response as HistoryFullDataResponse).fxs,
      bis: (response as HistoryFullDataResponse).bis,
      xds: (response as HistoryFullDataResponse).xds,
      strict_structure_mode: (response as HistoryFullDataResponse).strict_structure_mode,
      strict_structure: (response as HistoryFullDataResponse).strict_structure,
      strict_structure_error: (response as HistoryFullDataResponse).strict_structure_error,
    };

    return result;
  }
}
