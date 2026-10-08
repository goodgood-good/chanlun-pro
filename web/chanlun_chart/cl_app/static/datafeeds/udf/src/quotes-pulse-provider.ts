import {
	QuoteData,
	QuotesCallback,
} from '../../../charting_library/datafeed-api';

import {
	getErrorMessage,
	logMessage,
} from './helpers';

import { IQuotesProvider } from './iquotes-provider';

interface QuoteSubscriber {
	symbols: string[];
	fastSymbols: string[];
	listener: QuotesCallback;
}

interface QuoteSubscribers {
	[listenerId: string]: QuoteSubscriber;
}

const enum SymbolsType {
	General,
	Fast,
}

interface QuoteRequest {
	subscription: QuoteSubscriber;
	updateType: SymbolsType;
	nextType?: SymbolsType;
}

const enum UpdateTimeouts {
	Fast = 10 * 1000,
	General = 60 * 1000,
}

interface Timers {
	fastTimer: number;
	generalTimer: number;
}

export class QuotesPulseProvider {
	private readonly _quotesProvider: IQuotesProvider;
	private readonly _subscribers: QuoteSubscribers = {};
	private _requestsPending: number = 0;
	private readonly _pendingSubscriptions: Map<string, QuoteRequest> = new Map();

	private _timers: Timers | null = null;
	private _disposed = false;

	public constructor(quotesProvider: IQuotesProvider) {
		this._quotesProvider = quotesProvider;
	}

	public dispose(): void {
		if (this._disposed) return;
		this._disposed = true;
		this._destroyTimers();
		for (const guid of Object.keys(this._subscribers)) delete this._subscribers[guid];
		this._pendingSubscriptions.clear();
	}

	public subscribeQuotes(symbols: string[], fastSymbols: string[], onRealtimeCallback: QuotesCallback, listenerGuid: string): void {
		if (this._disposed) return;
		this._subscribers[listenerGuid] = {
			symbols: symbols,
			fastSymbols: fastSymbols,
			listener: onRealtimeCallback,
		};
		this._createTimersIfRequired();
		logMessage(`QuotesPulseProvider: subscribed quotes with #${listenerGuid}`);
	}

	public unsubscribeQuotes(listenerGuid: string): void {
		delete this._subscribers[listenerGuid];
		this._pendingSubscriptions.delete(listenerGuid);
		if (Object.keys(this._subscribers).length === 0) {
			this._destroyTimers();
		}
		logMessage(`QuotesPulseProvider: unsubscribed quotes with #${listenerGuid}`);
	}

	private _createTimersIfRequired(): void {
		if (this._timers === null) {
			const fastTimer = window.setInterval(this._updateQuotes.bind(this, SymbolsType.Fast), UpdateTimeouts.Fast);
			const generalTimer = window.setInterval(this._updateQuotes.bind(this, SymbolsType.General), UpdateTimeouts.General);
			this._timers = { fastTimer, generalTimer };
		}
	}

	private _destroyTimers(): void {
		if (this._timers !== null) {
			clearInterval(this._timers.fastTimer);
			clearInterval(this._timers.generalTimer);
			this._timers = null;
		}
	}

	private _updateQuotes(updateType: SymbolsType): void {
		// eslint-disable-next-line guard-for-in
		for (const listenerGuid in this._subscribers) {
			this._updateSubscription(listenerGuid, updateType);
		}
	}

	private _updateSubscription(listenerGuid: string, updateType: SymbolsType): void {
		const subscriptionRecord = this._subscribers[listenerGuid];
		if (this._disposed || !subscriptionRecord) return;
		const symbols = updateType === SymbolsType.Fast ? subscriptionRecord.fastSymbols : subscriptionRecord.symbols;
		if (symbols.length === 0) return;
		const pending = this._pendingSubscriptions.get(listenerGuid);
		if (pending && pending.subscription === subscriptionRecord) {
			// Fast and general timers can fire together. Preserve the other
			// update once, while coalescing repeats of the in-flight kind.
			if (pending.updateType !== updateType) pending.nextType = updateType;
			return;
		}
		const request: QuoteRequest = {subscription: subscriptionRecord, updateType};
		this._pendingSubscriptions.set(listenerGuid, request);
		this._requestsPending++;
		Promise.resolve().then(() => {
			if (this._subscribers[listenerGuid] !== subscriptionRecord || this._pendingSubscriptions.get(listenerGuid) !== request) return [];
			return this._quotesProvider.getQuotes(symbols);
		})
			.then((data: QuoteData[]) => {
				if (this._subscribers[listenerGuid] !== subscriptionRecord || this._pendingSubscriptions.get(listenerGuid) !== request) {
					return;
				}

				subscriptionRecord.listener(data);
				logMessage(`QuotesPulseProvider: data for #${listenerGuid} (${updateType}) updated successfully, pending=${this._requestsPending}`);
			})
			.catch((reason?: string | Error) => {
				logMessage(`QuotesPulseProvider: data for #${listenerGuid} (${updateType}) updated with error=${getErrorMessage(reason)}, pending=${this._requestsPending}`);
			})
			.finally(() => {
				this._requestsPending--;
				if (this._pendingSubscriptions.get(listenerGuid) !== request) return;
				this._pendingSubscriptions.delete(listenerGuid);
				if (request.nextType !== undefined && this._subscribers[listenerGuid] === subscriptionRecord) {
					this._updateSubscription(listenerGuid, request.nextType);
				}
			});
	}
}
