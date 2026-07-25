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

/**
 * Convert a raw market-close timestamp to the coordinate TradingView expects.
 *
 * The raw timestamp remains available separately for strict-snapshot identity
 * and MACD alignment.  Only the Bar passed to the chart uses this coordinate.
 */
export function chartBarTimeSeconds(sourceTime: number, resolution: string): number {
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
