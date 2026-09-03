function calendarResolution(resolution: string): "d" | "w" | "m" | "q" | "y" | null {
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

function intradayResolutionSeconds(resolution: string): number | null {
	const value = String(resolution || "").trim();
	let matched = /^([1-9][0-9]*)$/.exec(value);
	if (matched) {
		return Number(matched[1]) * 60;
	}
	matched = /^([1-9][0-9]*)([mMsS])$/.exec(value);
	if (!matched) {
		return null;
	}
	const amount = Number(matched[1]);
	return matched[2].toLowerCase() === "s" ? amount : amount * 60;
}

function marketFromSymbol(symbol: string): string {
	const matched = /^([^:]+):/.exec(String(symbol || "").trim());
	return matched ? matched[1].toLowerCase() : "";
}

/**
 * Convert a raw market-close timestamp to the coordinate TradingView expects.
 *
 * The raw timestamp remains available separately for strict-snapshot identity
 * and MACD alignment.  Only the Bar passed to the chart uses this coordinate.
 */
export function chartBarTimeSeconds(
	sourceTime: number,
	resolution: string,
	symbol: string = ""
): number {
	if (!Number.isInteger(sourceTime)) {
		throw new Error("history bar time must be epoch seconds");
	}
	const calendar = calendarResolution(resolution);
	if (calendar === null) {
		// QMT A-share bars are identified by their close timestamp while
		// TradingView requires an intraday bar's opening coordinate. Keep the
		// raw close timestamp in bars_result.times for strict evidence identity,
		// and normalize only the Bar sent to the chart. Other providers (for
		// example Binance) already return opening timestamps and must not shift.
		const duration = intradayResolutionSeconds(resolution);
		return marketFromSymbol(symbol) === "a" && duration !== null
			? sourceTime - duration
			: sourceTime;
	}

	const source = new Date(sourceTime * 1000);
	let year = source.getUTCFullYear();
	let month = source.getUTCMonth();
	let day = source.getUTCDate();
	if (calendar === "w") {
		const daysSinceMonday = (source.getUTCDay() + 6) % 7;
		day -= daysSinceMonday;
	} else if (calendar === "m") {
		day = 1;
	} else if (calendar === "q") {
		month = Math.floor(month / 3) * 3;
		day = 1;
	} else if (calendar === "y") {
		month = 0;
		day = 1;
	}
	return Date.UTC(year, month, day) / 1000;
}
