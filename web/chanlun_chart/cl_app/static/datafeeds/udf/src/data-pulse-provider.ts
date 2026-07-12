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
}

interface DataSubscribers {
	[guid: string]: DataSubscriber;
}

export class DataPulseProvider implements IDataPulseProvider {
	private readonly _subscribers: DataSubscribers = {};
	private readonly _requestsPending: Set<string> = new Set();
	private readonly _requestTimeoutMs: number;
	private readonly _historyProvider: IHistoryProvider;

	public constructor(historyProvider: IHistoryProvider, updateFrequency: number) {
		this._historyProvider = historyProvider;
		this._requestTimeoutMs = Math.max(10_000, Number.isFinite(updateFrequency) ? updateFrequency * 2 : 10_000);
		setInterval(this._updateData.bind(this), updateFrequency);
	}

	public subscribeBars(symbolInfo: LibrarySymbolInfo, resolution: ResolutionString, newDataCallback: SubscribeBarsCallback, listenerGuid: string): void {
		if (this._subscribers.hasOwnProperty(listenerGuid)) {
			logMessage(`DataPulseProvider: already has subscriber with id=${listenerGuid}`);
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

	public unsubscribeBars(listenerGuid: string): void {
		delete this._subscribers[listenerGuid];
		logMessage(`DataPulseProvider: unsubscribed for #${listenerGuid}`);
	}

	/**
	 * SSE 推送驱动：把最新 bar 直接喂给匹配的订阅者(绕过轮询/浏览器节流)。
	 * symbolResKey = (ticker||name).toLowerCase() + resolution.toLowerCase()。
	 */
	public feedBar(symbolResKey: string, bar: Bar): void {
		for (const guid in this._subscribers) {
			const sub = this._subscribers[guid];
			const si: LibrarySymbolInfo = sub.symbolInfo || {} as LibrarySymbolInfo;
			const key = String((si as LibrarySymbolInfo & { ticker?: string }).ticker || si.name || '').toLowerCase()
				+ String(sub.resolution).toLowerCase();
			if (key !== symbolResKey) {
				continue;
			}
			if (sub.lastBarTime !== null && bar.time < sub.lastBarTime) {
				continue;
			}
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

			this._requestsPending.add(listenerGuid);
			this._withTimeout(Promise.resolve().then(() => this._updateDataForSubscriber(listenerGuid)), listenerGuid)
				.then(() => {
					logMessage(`DataPulseProvider: data for #${listenerGuid} updated successfully`);
				})
				.catch((reason?: string | Error) => {
					logMessage(`DataPulseProvider: data for #${listenerGuid} updated with error=${getErrorMessage(reason)}`);
				})
				.finally(() => {
					this._requestsPending.delete(listenerGuid);
				});
		}
	}

	private _withTimeout<T>(request: Promise<T>, listenerGuid: string): Promise<T> {
		let timeoutId: ReturnType<typeof setTimeout> | undefined;
		const timeout = new Promise<T>((_resolve, reject) => {
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

	private _updateDataForSubscriber(listenerGuid: string): Promise<void> {
		const subscriptionRecord = this._subscribers[listenerGuid];

		const rangeEndTime = parseInt((Date.now() / 1000).toString());

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
			})
			.then((result: GetBarsResult) => {
				this._onSubscriberDataReceived(listenerGuid, result);
			});
	}

	private _onSubscriberDataReceived(listenerGuid: string, result: GetBarsResult): void {
		// means the subscription was cancelled while waiting for data
		if (!this._subscribers.hasOwnProperty(listenerGuid)) {
			logMessage(`DataPulseProvider: Data comes for already unsubscribed subscription #${listenerGuid}`);
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
