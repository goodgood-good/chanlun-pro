import { RequestParams, UdfErrorResponse, UdfResponse, logMessage } from './helpers';
import { IRequester } from './irequester';

export class Requester implements IRequester {
	private _headers: HeadersInit | undefined;
	private readonly _timeoutMs: number;
	private readonly _controllersByKey: Map<string, AbortController> = new Map();

	public constructor(headers?: HeadersInit, timeoutMs: number = 15_000) {
		if (headers) {
			this._headers = headers;
		}
		this._timeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 15_000;
	}

	public sendRequest<T extends UdfResponse>(datafeedUrl: string, urlPath: string, params?: RequestParams, timeoutMs?: number, requestKey?: string): Promise<T | UdfErrorResponse>;
	public sendRequest<T>(datafeedUrl: string, urlPath: string, params?: RequestParams, timeoutMs?: number, requestKey?: string): Promise<T>;
	public sendRequest<T>(datafeedUrl: string, urlPath: string, params?: RequestParams, timeoutMs?: number, requestKey?: string): Promise<T> {
		if (params !== undefined) {
			const paramKeys = Object.keys(params);
			if (paramKeys.length !== 0) {
				urlPath += '?';
			}

			urlPath += paramKeys.map((key: string) => {
				return `${encodeURIComponent(key)}=${encodeURIComponent(params[key].toString())}`;
			}).join('&');
		}

		logMessage('New request: ' + urlPath);
		const effectiveTimeoutMs = Number.isFinite(timeoutMs) && Number(timeoutMs) > 0
			? Number(timeoutMs)
			: this._timeoutMs;

		// Send user cookies if the URL is on the same origin as the calling script.
		const controller = typeof AbortController === 'undefined' ? undefined : new AbortController();
		if (controller !== undefined && requestKey) {
			const previous = this._controllersByKey.get(requestKey);
			if (previous !== undefined) {
				previous.abort();
			}
			this._controllersByKey.set(requestKey, controller);
		}
		const options: RequestInit = { credentials: 'same-origin' };
		if (controller !== undefined) {
			options.signal = controller.signal;
		}
		let timeoutId: ReturnType<typeof setTimeout>;
		const timeout = new Promise<never>((_resolve, reject) => {
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
			.then((response: Response) => {
				if (response.ok === false) {
					throw new Error(`Request failed with HTTP ${response.status}`);
				}
				return response.text();
			})
			.then((responseText: string) => JSON.parse(responseText)),
			timeout,
		])
			.finally(() => {
				clearTimeout(timeoutId);
				if (
					controller !== undefined &&
					requestKey &&
					this._controllersByKey.get(requestKey) === controller
				) {
					this._controllersByKey.delete(requestKey);
				}
			});
	}
}
