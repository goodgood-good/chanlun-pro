import { Bar, LibrarySymbolInfo, ResolutionString, SubscribeBarsCallback } from '../../../charting_library/datafeed-api';

import {
	getErrorMessage,
	logMessage,
} from './helpers';
import { GetBarsResult, IDataPulseProvider, IHistoryProvider } from './provider-interfaces';

interface DataSubscriber {
	symbolInfo: LibrarySymbolInfo;
	resolution: ResolutionString;
	lastBarTime: number | null;
	listener: SubscribeBarsCallback;
	updateVersion: number;
}

interface DataSubscribers {
	[guid: string]: DataSubscriber;
}

const REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS = 60;
const A_SHARE_UTC_OFFSET_MS = 8 * 60 * 60 * 1000;
const A_SHARE_REALTIME_WINDOWS: ReadonlyArray<readonly [number, number]> = [
	// Poll through a short publication grace after each close so the final bar
	// can arrive without keeping every open chart busy throughout lunch/night.
	[9 * 60 + 30, 11 * 60 + 40],
	[13 * 60, 15 * 60 + 10],
];

function _symbolMarket(symbolInfo: LibrarySymbolInfo): string {
	const exchange = String(symbolInfo.exchange || '').trim().toLowerCase();
	if (exchange) {
		return exchange;
	}
	const ticker = String((symbolInfo as LibrarySymbolInfo & { ticker?: string }).ticker || symbolInfo.name || '');
	const separator = ticker.indexOf(':');
	return separator > 0 ? ticker.slice(0, separator).trim().toLowerCase() : '';
}

function _realtimePollingDue(symbolInfo: LibrarySymbolInfo, nowMs: number): boolean {
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
	return A_SHARE_REALTIME_WINDOWS.some(([start, end]) => (
		minuteOfDay >= start && minuteOfDay <= end
	));
}

export class DataPulseProvider implements IDataPulseProvider {
	private readonly _subscribers: DataSubscribers = {};
	private readonly _requestsPending: Map<string, DataSubscriber> = new Map();
	private readonly _requestTimeoutMs: number;
	private readonly _historyProvider: IHistoryProvider;
	private _timer: ReturnType<typeof setInterval> | null;
	private _disposed = false;

	public constructor(historyProvider: IHistoryProvider, updateFrequency: number) {
		this._historyProvider = historyProvider;
		this._requestTimeoutMs = Math.max(10_000, Number.isFinite(updateFrequency) ? updateFrequency * 2 : 10_000);
		this._timer = setInterval(this._updateData.bind(this), updateFrequency);
	}

	public dispose(): void {
		if (this._disposed) return;
		this._disposed = true;
		if (this._timer !== null) clearInterval(this._timer);
		this._timer = null;
		for (const guid of Object.keys(this._subscribers)) delete this._subscribers[guid];
		this._requestsPending.clear();
	}

	public subscribeBars(symbolInfo: LibrarySymbolInfo, resolution: ResolutionString, newDataCallback: SubscribeBarsCallback, listenerGuid: string): void {
		if (this._disposed) return;
		if (this._subscribers.hasOwnProperty(listenerGuid)) {
			logMessage(`DataPulseProvider: already has subscriber with id=${listenerGuid}`);
			return;
		}

		this._subscribers[listenerGuid] = {
			lastBarTime: null,
			listener: newDataCallback,
			resolution: resolution,
			symbolInfo: symbolInfo,
			updateVersion: 0,
		};

		logMessage(`DataPulseProvider: subscribed for #${listenerGuid} - {${symbolInfo.name}, ${resolution}}`);
	}

	public unsubscribeBars(listenerGuid: string): void {
		delete this._subscribers[listenerGuid];
		this._requestsPending.delete(listenerGuid);
		logMessage(`DataPulseProvider: unsubscribed for #${listenerGuid}`);
	}

	/**
	 * SSE 推送驱动：把最新 bar 直接喂给匹配的订阅者(绕过轮询/浏览器节流)。
	 * symbolResKey = (ticker||name).toLowerCase() + resolution.toLowerCase()。
	 */
	public feedBar(symbolResKey: string, bar: Bar, requireInitialized = false): void {
		for (const guid in this._subscribers) {
			const sub = this._subscribers[guid];
			const si: LibrarySymbolInfo = sub.symbolInfo || {} as LibrarySymbolInfo;
			const key = String((si as LibrarySymbolInfo & { ticker?: string }).ticker || si.name || '').toLowerCase()
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
			// A newly accepted push owns the candle over older in-flight polls.
			sub.updateVersion += 1;
			sub.lastBarTime = bar.time;
			try {
				sub.listener(bar);
			} catch (e) {
				/* ignore listener errors */
			}
		}
	}

	private _updateData(): void {
		// A stalled symbol must not block refreshes for every other subscriber.
		// eslint-disable-next-line guard-for-in
		for (const listenerGuid in this._subscribers) {
			if (this._requestsPending.has(listenerGuid)) {
				continue;
			}

			const subscription = this._subscribers[listenerGuid];
			const version = ++subscription.updateVersion;
			const invalidate = () => {
				if (subscription.updateVersion === version) subscription.updateVersion += 1;
			};
			this._requestsPending.set(listenerGuid, subscription);
			this._withTimeout(Promise.resolve().then(() => this._updateDataForSubscriber(listenerGuid, subscription, version)), listenerGuid, invalidate)
				.then(() => {
					logMessage(`DataPulseProvider: data for #${listenerGuid} updated successfully`);
				})
				.catch((reason?: string | Error) => {
					logMessage(`DataPulseProvider: data for #${listenerGuid} updated with error=${getErrorMessage(reason)}`);
				})
				.finally(() => {
					if (this._requestsPending.get(listenerGuid) === subscription) {
						this._requestsPending.delete(listenerGuid);
					}
					// A timeout settles this wrapper before the network request.
					invalidate();
				});
		}
	}

	private _withTimeout<T>(request: Promise<T>, listenerGuid: string, onTimeout?: () => void): Promise<T> {
		let timeoutId: ReturnType<typeof setTimeout> | undefined;
		const timeout = new Promise<T>((_resolve, reject) => {
			timeoutId = setTimeout(() => {
				onTimeout?.();
				reject(new Error(`Data refresh timed out for #${listenerGuid}`));
			}, this._requestTimeoutMs);
		});

		return Promise.race([request, timeout]).finally(() => {
			if (timeoutId !== undefined) {
				clearTimeout(timeoutId);
			}
		});
	}

	private _isCurrentSubscriber(listenerGuid: string, subscription: DataSubscriber | undefined, version: number | undefined): boolean {
		return !!subscription && this._subscribers[listenerGuid] === subscription && subscription.updateVersion === version;
	}

	private _updateDataForSubscriber(listenerGuid: string, subscriptionRecord = this._subscribers[listenerGuid], version?: number): Promise<void> {
		if (!subscriptionRecord || this._subscribers[listenerGuid] !== subscriptionRecord || !_realtimePollingDue(subscriptionRecord.symbolInfo, Date.now())) {
			return Promise.resolve();
		}
		const requestVersion = version === undefined ? ++subscriptionRecord.updateVersion : version;
		const isCurrent = () => this._isCurrentSubscriber(listenerGuid, subscriptionRecord, requestVersion);
		if (!isCurrent()) return Promise.resolve();

		const rangeEndTime = parseInt((Date.now() / 1000).toString())
			+ REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS;

		// BEWARE: please note we really need 2 bars, not the only last one
		// see the explanation below. `10` is the `large enough` value to work around holidays
		const rangeStartTime = rangeEndTime - periodLengthSeconds(subscriptionRecord.resolution, 10);

		return this._historyProvider.getBars(
			subscriptionRecord.symbolInfo,
			subscriptionRecord.resolution,
			{
				from: rangeStartTime,
				to: rangeEndTime,
				countBack: 2,
				firstDataRequest: false,
			}, isCurrent)
			.then((result: GetBarsResult) => {
				this._onSubscriberDataReceived(listenerGuid, result, subscriptionRecord, requestVersion);
			});
	}

	private _onSubscriberDataReceived(listenerGuid: string, result: GetBarsResult,
		subscriptionRecord = this._subscribers[listenerGuid], version = subscriptionRecord?.updateVersion): void {
		if (!this._isCurrentSubscriber(listenerGuid, subscriptionRecord, version)) {
			logMessage(`DataPulseProvider: Data comes for already unsubscribed subscription #${listenerGuid}`);
			return;
		}

		const bars = result.bars;
		if (bars.length === 0) {
			return;
		}

		const lastBar = bars[bars.length - 1];

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
			if (subscriptionRecord.lastBarTime !== null && previousBar.time >= subscriptionRecord.lastBarTime) {
				subscriptionRecord.listener(previousBar);
			}
		}

		if (!this._isCurrentSubscriber(listenerGuid, subscriptionRecord, version)
			|| subscriptionRecord.lastBarTime !== null && lastBar.time < subscriptionRecord.lastBarTime) return;
		subscriptionRecord.lastBarTime = lastBar.time;
		subscriptionRecord.listener(lastBar);
	}
}

function periodLengthSeconds(resolution: string, requiredPeriodsCount: number): number {
	let daysCount = 0;

	if (resolution === 'D' || resolution === '1D') {
		daysCount = requiredPeriodsCount;
	} else if (resolution === 'M' || resolution === '1M') {
		daysCount = 31 * requiredPeriodsCount;
	} else if (resolution === 'W' || resolution === '1W') {
		daysCount = 7 * requiredPeriodsCount;
	} else {
		daysCount = requiredPeriodsCount * parseInt(resolution) / (24 * 60);
	}

	return daysCount * 24 * 60 * 60;
}
