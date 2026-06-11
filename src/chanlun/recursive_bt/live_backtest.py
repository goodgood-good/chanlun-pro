"""Market-aware live-parity portfolio backtest for A shares and US stocks."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, is_dataclass
from typing import Optional

import numpy as np
import pandas as pd

from chanlun.base import Market
from chanlun.core.cl import CL
from chanlun.exchange import get_exchange
from chanlun.recursive_bt.chanlun_selector import (
    ASelectionConfig,
    DEFAULT_FUND_DATA,
    OriginalChanlunASelector,
)
from chanlun.recursive_bt.engine import CL_CFG, MTFStrategy, collect_branch_signals
from chanlun.recursive_bt.market_runtime import (
    BT_DATA_DIR,
    CHART_CACHE_DIR,
    ashare_board,
    default_backtest_report_paths,
    list_chart_cache_codes,
    load_chart_cache_klines,
    market_rules_for_code,
    normalize_code,
    normalize_market,
    parse_codes,
)
from chanlun.recursive_bt import portfolio as portfolio_mod

DEFAULT_MAX_POS = 10
DEFAULT_BS_POINT_RATIO_OVERRIDES = "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
ASHARE_BOARD_ALIASES = {
    "all": None,
    "shsz": {"main", "gem", "star"},
    "non_bj": {"main", "gem", "star"},
    "non-bj": {"main", "gem", "star"},
}
ASHARE_BOARD_ORDER = ("main", "gem", "star", "bj", "other")


def _freq_delay(level: str) -> pd.Timedelta:
    if level.endswith("m"):
        return pd.Timedelta(minutes=int(level[:-1]))
    if level == "d":
        return pd.Timedelta(days=1)
    if level == "w":
        return pd.Timedelta(days=7)
    return pd.Timedelta("0min")


def _coerce_window_ts(value: Optional[str], syms: dict):
    if not value:
        return None
    ts = pd.Timestamp(value)
    ref = None
    for sym in syms.values():
        dates = sym.get("dates") if isinstance(sym, dict) else None
        if dates:
            ref = pd.Timestamp(dates[0])
            break
    if ref is None:
        return ts
    ref_tz = ref.tz
    if ref_tz is None:
        return ts.tz_convert(None) if ts.tzinfo is not None else ts
    return ts.tz_localize(ref_tz) if ts.tzinfo is None else ts.tz_convert(ref_tz)


def resolve_max_pos(requested_max_pos: Optional[int], universe_size: int) -> int:
    if requested_max_pos is not None and requested_max_pos > 0:
        return int(requested_max_pos)
    if universe_size > 0:
        return max(1, min(DEFAULT_MAX_POS, int(universe_size)))
    return DEFAULT_MAX_POS


def _parse_buy_classes(value) -> tuple[int, ...]:
    raw = value.replace(";", ",").split(",") if isinstance(value, str) else value
    out: list[int] = []
    for item in raw or ():
        try:
            cls = int(str(item).strip()[0])
        except Exception:
            continue
        if cls in (1, 2, 3) and cls not in out:
            out.append(cls)
    return tuple(out) or (3, 2, 1)


def _parse_sell_classes(value) -> tuple[int, ...]:
    return _parse_buy_classes(value)


def _parse_optional_buy_classes(value) -> tuple[int, ...]:
    if value is None or str(value).strip() == "":
        return ()
    return _parse_buy_classes(value)


def _parse_sell_ratio_overrides(value) -> dict[str, float]:
    if isinstance(value, dict):
        raw_items = value.items()
    else:
        raw_items = []
        for part in str(value or "").replace(";", ",").split(","):
            item = part.strip()
            if not item:
                continue
            sep = ":" if ":" in item else "=" if "=" in item else ""
            if not sep:
                continue
            key, ratio = item.split(sep, 1)
            raw_items.append((key, ratio))
    out: dict[str, float] = {}
    for key, ratio in raw_items:
        try:
            cls = int(str(key).strip()[0])
            value = float(ratio)
        except Exception:
            continue
        if cls in (1, 2, 3):
            out[str(cls)] = min(max(value, 0.0), 1.0)
    return out


def _merge_require(require: tuple[str, ...], extras: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for item in tuple(require or ()) + tuple(extras or ()):
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _parse_selection_boards(value: str | None) -> Optional[set[str]]:
    raw = (value or "all").strip().lower()
    if raw in ASHARE_BOARD_ALIASES:
        alias = ASHARE_BOARD_ALIASES[raw]
        return None if alias is None else set(alias)
    boards: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        board = part.strip().lower()
        if not board:
            continue
        if board in ASHARE_BOARD_ALIASES and ASHARE_BOARD_ALIASES[board] is not None:
            boards.update(ASHARE_BOARD_ALIASES[board] or set())
        elif board in ASHARE_BOARD_ORDER:
            boards.add(board)
    return boards or None


def _filter_selection_boards(syms: dict, board_filter: str | None) -> dict:
    boards = _parse_selection_boards(board_filter)
    if not boards:
        return syms
    return {code: data for code, data in syms.items() if ashare_board(code) in boards}


def _limit_selection_sample(syms: dict, limit: int, sample_mode: str = "stratified") -> dict:
    if limit <= 0 or len(syms) <= limit:
        return syms
    items = sorted(syms.items())
    if sample_mode == "sorted":
        return dict(items[:limit])

    grouped: dict[str, list[tuple[str, object]]] = {board: [] for board in ASHARE_BOARD_ORDER}
    for code, data in items:
        board = ashare_board(code)
        grouped.setdefault(board, []).append((code, data))
    active = [board for board in ASHARE_BOARD_ORDER if grouped.get(board)]
    selected: list[tuple[str, object]] = []
    while active and len(selected) < limit:
        next_active: list[str] = []
        for board in active:
            bucket = grouped.get(board) or []
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
            if bucket:
                next_active.append(board)
        active = next_active
    return dict(selected)


def _board_counts(market: str, syms: dict) -> dict[str, int]:
    if normalize_market(market) != "a":
        return {}
    counts: dict[str, int] = {}
    for code in syms:
        board = ashare_board(code)
        counts[board] = counts.get(board, 0) + 1
    return dict(sorted(counts.items()))


def _compound_return(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return float((1.0 + returns.astype(float)).prod() - 1.0)


def _curve_max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    curve = (1.0 + returns.astype(float)).cumprod()
    peak = curve.cummax()
    return float(((peak - curve) / peak).max())


def _annualized_sharpe(returns: pd.Series, periods_per_year: float = 252.0) -> float:
    returns = returns.astype(float)
    if returns.empty:
        return 0.0
    std = float(returns.std(ddof=0))
    if std <= 1e-12:
        return 0.0
    return float(returns.mean() / std * np.sqrt(periods_per_year))


def _signals_by_main_bar(signals, dates: list[pd.Timestamp], delay) -> dict[int, list]:
    out: dict[int, list] = {}
    ordered = sorted(signals, key=lambda sig: sig.date)
    si = 0
    for i, date in enumerate(dates):
        while si < len(ordered) and ordered[si].date + delay <= date:
            out.setdefault(i, []).append(ordered[si])
            si += 1
    return out


def _trade_entry_date(trade) -> Optional[object]:
    value = getattr(trade, "entry_date", None)
    if value is None and isinstance(trade, dict):
        value = trade.get("entry_date")
    if value is None:
        return None
    return pd.Timestamp(value).date()


def _market_regime_segments(result: dict, lookback_days: int = 20) -> dict:
    master = result.get("master") or []
    equity = result.get("equity")
    bench = result.get("bench")
    if equity is None or bench is None or len(master) < 3:
        return {}
    idx = pd.to_datetime(master)
    df = pd.DataFrame(
        {
            "equity": np.asarray(equity, dtype=float),
            "bench": np.asarray(bench, dtype=float),
        },
        index=idx,
    ).sort_index()
    daily = df.resample("1D").last().dropna()
    if len(daily) < 3:
        return {}
    daily["strategy_ret"] = daily["equity"].pct_change().fillna(0.0)
    daily["bench_ret"] = daily["bench"].pct_change().fillna(0.0)
    rolling = daily["bench"].pct_change(lookback_days).fillna(0.0)
    bench_dd = daily["bench"] / daily["bench"].cummax() - 1.0
    daily["regime"] = "range"
    daily.loc[(rolling >= 0.05) & (bench_dd > -0.05), "regime"] = "bull"
    daily.loc[(rolling <= -0.05) | (bench_dd <= -0.10), "regime"] = "bear"

    trade_dates = [_trade_entry_date(t) for t in result.get("trades", [])]
    out = {
        "method": "daily_equal_weight_benchmark",
        "lookback_days": lookback_days,
        "bull_rule": "20d benchmark return >= 5% and drawdown > -5%",
        "bear_rule": "20d benchmark return <= -5% or drawdown <= -10%",
        "segments": {},
        "daily_regimes": [
            {
                "date": str(ts.date()),
                "regime": str(row["regime"]),
                "strategy_return": float(row["strategy_ret"]),
                "benchmark_return": float(row["bench_ret"]),
                "benchmark_drawdown": float(bench_dd.loc[ts]),
                "benchmark_lookback_return": float(rolling.loc[ts]),
            }
            for ts, row in daily.iterrows()
        ],
    }
    for regime in ("bull", "range", "bear"):
        part = daily[daily["regime"] == regime]
        dates = {d.date() for d in part.index}
        sret = part["strategy_ret"]
        bret = part["bench_ret"]
        out["segments"][regime] = {
            "days": int(len(part)),
            "strategy_return": _compound_return(sret),
            "benchmark_return": _compound_return(bret),
            "excess_return": _compound_return(sret) - _compound_return(bret),
            "max_drawdown": _curve_max_drawdown(sret),
            "sharpe": _annualized_sharpe(sret),
            "trade_count": int(sum(1 for d in trade_dates if d in dates)),
        }
    return out


def _attach_walk_forward_scores(syms: dict, bt_data_dir: str, fund_data: str) -> bool:
    old_dir = portfolio_mod.BT_DATA
    portfolio_mod.BT_DATA = bt_data_dir
    try:
        market = portfolio_mod.load_cached("SH.000001")
    finally:
        portfolio_mod.BT_DATA = old_dir
    if market is None:
        return False
    from chanlun.recursive_bt import systems as systems_mod

    old_fund_dir = systems_mod.FUND_DIR
    systems_mod.FUND_DIR = fund_data
    try:
        systems_mod.attach_scores(syms, market)
    finally:
        systems_mod.FUND_DIR = old_fund_dir
    return True


def build_symbol_from_klines(
    market: str,
    code: str,
    df_op: pd.DataFrame,
    df_big=None,
    op_level: str = "5m",
    big_level: str = "30m",
    df_mid=None,
    mid_level: Optional[str] = None,
):
    market = normalize_market(market)
    code = normalize_code(market, code)
    df_op = df_op.reset_index(drop=True)
    if df_big is not None:
        df_big = df_big.reset_index(drop=True)
    if df_mid is not None:
        df_mid = df_mid.reset_index(drop=True)
    cd_op = CL(code, op_level, dict(CL_CFG))
    cd_op.process_klines(df_op)
    small = collect_branch_signals(cd_op, use_xd=False, annotate_nest=True)
    big = []
    if df_big is not None and len(df_big) >= 50:
        cd_big = CL(code, big_level, dict(CL_CFG))
        cd_big.process_klines(df_big)
        big = collect_branch_signals(cd_big, use_xd=False)
    dates = list(df_op["date"])
    strat = MTFStrategy(
        small, big, dates, f"{op_level}+{big_level}",
        gate="not_down", big_delay=_freq_delay(big_level),
    )
    out = {
        "name": code,
        "code": code,
        "rules": market_rules_for_code(market, code),
        "dates": dates,
        "open": df_op["open"].to_numpy(),
        "close": df_op["close"].to_numpy(),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": strat.small_by_bar,
        "big_dir_at": strat.big_dir_at,
        "levels": {"op": op_level, "big": big_level, "mid": mid_level},
    }
    if mid_level and df_mid is not None and len(df_mid) >= 50:
        cd_mid = CL(code, mid_level, dict(CL_CFG))
        cd_mid.process_klines(df_mid)
        mid = collect_branch_signals(cd_mid, use_xd=False)
        mid_strat = MTFStrategy(
            [], mid, dates, f"{op_level}+{mid_level}",
            gate="not_down", big_delay=_freq_delay(mid_level),
        )
        out["mid_dir_at"] = mid_strat.big_dir_at
        out["mid_by_bar"] = _signals_by_main_bar(mid, dates, _freq_delay(mid_level))
    return out


def load_chart_cache_syms(
    market: str,
    codes: Optional[list[str]] = None,
    cache_dir: str = CHART_CACHE_DIR,
    pool_size: int = 50,
    op_level: str = "5m",
    big_level: str = "30m",
    mid_level: Optional[str] = None,
) -> dict:
    market = normalize_market(market)
    codes = codes or list_chart_cache_codes(market, cache_dir)
    if pool_size > 0:
        codes = codes[:pool_size]
    syms = {}
    for code in codes:
        code = normalize_code(market, code)
        df_op = load_chart_cache_klines(market, code, op_level, cache_dir)
        if df_op is None or len(df_op) < 100:
            continue
        df_big = load_chart_cache_klines(market, code, big_level, cache_dir)
        df_mid = (
            load_chart_cache_klines(market, code, mid_level, cache_dir)
            if mid_level else None
        )
        syms[code] = build_symbol_from_klines(
            market, code, df_op, df_big,
            op_level=op_level, big_level=big_level,
            df_mid=df_mid, mid_level=mid_level,
        )
    return syms


def load_online_syms(
    market: str,
    codes: Optional[list[str]],
    pool_size: int = 20,
    op_level: str = "5m",
    big_level: str = "30m",
    mid_level: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    market = normalize_market(market)
    ex = get_exchange(Market(market))
    if not codes:
        stocks = ex.all_stocks()
        codes = [s["code"] for s in stocks[:pool_size]]
    syms = {}
    for code in codes:
        code = normalize_code(market, code)
        kline_args = {"allow_long_history": True} if start_date else None
        df_op = ex.klines(
            code, op_level, start_date=start_date, end_date=end_date, args=kline_args
        )
        if df_op is None or len(df_op) < 100:
            continue
        df_big = ex.klines(
            code, big_level, start_date=start_date, end_date=end_date, args=kline_args
        )
        df_mid = (
            ex.klines(
                code, mid_level,
                start_date=start_date, end_date=end_date, args=kline_args,
            )
            if mid_level else None
        )
        syms[code] = build_symbol_from_klines(
            market, code, df_op, df_big,
            op_level=op_level, big_level=big_level,
            df_mid=df_mid, mid_level=mid_level,
        )
    return syms


def load_bt_data_syms(
    market: str,
    codes: Optional[list[str]] = None,
    bt_data_dir: str = BT_DATA_DIR,
    pool_size: int = 0,
    pool_mode: str = "selector",
    fund_data: str = DEFAULT_FUND_DATA,
    selection_scan_limit: int = 0,
    selection_lookback_bars: int = 240,
    selection_buy_classes: tuple[int, ...] = (3, 2, 1),
    selection_require_three_systems: bool = True,
    selection_sample_mode: str = "stratified",
    selection_board_filter: str = "all",
) -> dict:
    market = normalize_market(market)
    if market != "a":
        raise ValueError("bt_data source is only available for A shares")
    old_dir = portfolio_mod.BT_DATA
    portfolio_mod.BT_DATA = bt_data_dir
    try:
        if codes:
            syms = {}
            for code in codes:
                d = portfolio_mod.load_cached(normalize_code(market, code))
                if d and len(d["dates"]) > 500:
                    syms[normalize_code(market, code)] = d
        elif pool_mode == "selector":
            max_codes = pool_size if pool_size > 0 else DEFAULT_MAX_POS * 5
            selector = OriginalChanlunASelector(
                ASelectionConfig(
                    bt_data=bt_data_dir,
                    fund_data=fund_data,
                    scan_limit=selection_scan_limit,
                    max_codes=max_codes,
                    lookback_bars=selection_lookback_bars,
                    buy_classes=selection_buy_classes,
                    require_three_systems=selection_require_three_systems,
                    min_bars=500,
                )
            )
            syms = {}
            for candidate in selector.select():
                d = portfolio_mod.load_cached(candidate.code)
                if d and len(d["dates"]) > 500:
                    syms[candidate.code] = d
            syms = _filter_selection_boards(syms, selection_board_filter)
        elif pool_mode in {"all", "walk_forward"}:
            syms = portfolio_mod._load_bt_universe()
            syms = _filter_selection_boards(syms, selection_board_filter)
            if pool_mode == "walk_forward" and selection_scan_limit > 0:
                syms = _limit_selection_sample(
                    syms,
                    selection_scan_limit,
                    sample_mode=selection_sample_mode,
                )
        else:
            syms = portfolio_mod._load_bt_universe()
            if pool_size > 0:
                syms = dict(list(sorted(syms.items()))[:pool_size])
    finally:
        portfolio_mod.BT_DATA = old_dir
    return syms


def _validate_bt_data_levels(args, syms: dict) -> None:
    op_level = str(args.op_level or "5m")
    big_level = str(args.big_level or "30m")
    mid_level = str(args.mid_level or "") or None
    if (op_level, big_level, mid_level) == ("5m", "30m", None):
        return
    if (op_level, big_level, mid_level) == ("1m", "30m", "5m"):
        missing_mid = [code for code, data in syms.items() if "mid_dir_at" not in data]
        if not missing_mid:
            return
        sample = ", ".join(missing_mid[:5])
        raise ValueError(
            "bt_data source was requested as 1m+5m+30m but cache files "
            f"do not contain mid_dir_at; sample={sample}"
        )
    raise ValueError(
        "bt_data source supports 5m+30m caches or explicit "
        "1m+5m+30m caches with mid_dir_at"
    )


def _load_bs_point_ratio_multipliers(args) -> dict[str, float]:
    if not getattr(args, "bs_point_ratio_overrides_enabled", False):
        return {}
    path = str(getattr(args, "bs_point_ratio_overrides_json", "") or "")
    if not path:
        return {}
    try:
        from chanlun.recursive_bt.strategy_optimizer import (
            bs_point_ratio_multipliers_for_market,
        )

        return bs_point_ratio_multipliers_for_market(path, args.market)
    except Exception:
        return {}


def _load_regime_bs_ratio_multipliers(args) -> dict:
    """解析按行情(bull/range/bear)的买点比例乘数:内联 JSON 或文件路径。
    形如 {"bear": {"3": 1.25}, "bull": {"1": 0.5}};非法行情键与非数值乘数被丢弃。"""
    raw = str(getattr(args, "regime_bs_ratio_multipliers_json", "") or "").strip()
    if not raw:
        return {}
    try:
        if raw.startswith("{"):
            data = json.loads(raw)
        else:
            with open(raw, "r", encoding="utf-8") as fp:
                data = json.load(fp)
    except Exception:
        return {}
    out: dict = {}
    for regime, mults in (data or {}).items():
        regime_key = str(regime).strip().lower()
        if regime_key not in {"bull", "range", "bear"} or not isinstance(mults, dict):
            continue
        inner = {}
        for cls, val in mults.items():
            try:
                inner[str(cls).strip()] = float(val)
            except Exception:
                continue
        if inner:
            out[regime_key] = inner
    return out


def _trade_rows(trades) -> list[dict]:
    rows = []
    for trade in trades:
        if is_dataclass(trade):
            row = asdict(trade)
        else:
            row = dict(trade)
        rows.append(row)
    return rows


def write_outputs(result: dict, args, syms: dict) -> tuple[str, str]:
    summary_path = args.output_summary
    trades_path = args.output_trades
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    os.makedirs(os.path.dirname(trades_path), exist_ok=True)
    master = result["master"]
    symbol_codes = list(syms.keys())
    summary = {
        "market": args.market,
        "source": args.source,
        "universe_size": len(syms),
        "board_counts": _board_counts(args.market, syms),
        "symbol_codes": symbol_codes[:200],
        "symbol_codes_truncated": len(symbol_codes) > 200,
        "max_pos": args.max_pos,
        "requested_max_pos": args.requested_max_pos,
        "buy_priority": args.buy_priority,
        "bt_pool_mode": getattr(args, "bt_pool_mode", ""),
        "selection_scan_limit": getattr(args, "selection_scan_limit", 0),
        "selection_sample_mode": getattr(args, "selection_sample_mode", "stratified"),
        "selection_board_filter": getattr(args, "selection_board_filter", "all"),
        "selection_lookback_bars": getattr(args, "selection_lookback_bars", 0),
        "selection_buy_classes": list(getattr(args, "selection_buy_classes", ()) or ()),
        "sell_classes": list(getattr(args, "sell_classes", ()) or ()),
        "sell_ratio_overrides": dict(getattr(args, "sell_ratio_overrides", {}) or {}),
        "sell_ratio_override_scope": str(
            getattr(args, "sell_ratio_override_scope", "all") or "all"
        ),
        "after_3sell_reentry_buy_classes": list(
            getattr(args, "after_3sell_reentry_buy_classes", ()) or ()
        ),
        "after_3sell_reentry_mid_buy_classes": list(
            getattr(args, "after_3sell_reentry_mid_buy_classes", ()) or ()
        ),
        "after_3sell_reentry_scope": str(
            getattr(args, "after_3sell_reentry_scope", "all") or "all"
        ),
        "selection_require_three_systems": getattr(args, "selection_require_three_systems", None),
        "walk_forward_scores": bool(getattr(args, "walk_forward_scores", False)),
        "require": args.require,
        "big_gate": args.big_gate,
        "regime_mode": args.regime_mode,
        "mid_gate": args.mid_gate,
        "bs_point_ratio_overrides_enabled": bool(
            getattr(args, "bs_point_ratio_overrides_enabled", False)
        ),
        "bs_point_ratio_overrides_json": str(
            getattr(args, "bs_point_ratio_overrides_json", "") or ""
        ),
        "bs_point_ratio_multipliers": dict(
            getattr(args, "bs_point_ratio_multipliers", {}) or {}
        ),
        "regime_bs_ratio_multipliers": {
            str(k): dict(v)
            for k, v in (getattr(args, "regime_bs_ratio_multipliers", {}) or {}).items()
        },
        "regime_lookback_days": int(getattr(args, "regime_lookback_days", 20) or 20),
        "op_level": args.op_level,
        "big_level": args.big_level,
        "mid_level": args.mid_level,
        "init_cash": args.init_cash,
        "start": str(master[0]) if master else None,
        "end": str(master[-1]) if master else None,
        "total": float(result["total"]),
        "total_return": float(result["total"]),
        "buy_hold": float(result["bh"]),
        "excess": float(result["total"] - result["bh"]),
        "max_dd": float(result["max_dd"]),
        "max_drawdown": float(result["max_dd"]),
        "bench_dd": float(result.get("bench_dd", 0.0)),
        "sharpe": float(result["sharpe"]),
        "win_rate": float(result["wr"]),
        "trade_count": int(result["n"]),
        "market_regime_segments": _market_regime_segments(result),
    }
    with open(summary_path, "w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)
    pd.DataFrame(_trade_rows(result["trades"])).to_csv(
        trades_path, index=False, encoding="utf-8-sig"
    )
    return summary_path, trades_path


def run_backtest(args) -> tuple[dict, dict]:
    codes = parse_codes(args.market, args.codes)
    source = args.source
    if source == "auto":
        source = "bt_data" if args.market == "a" else "chart_cache"
    args.source = source
    if source == "bt_data":
        syms = load_bt_data_syms(
            args.market,
            codes,
            args.bt_data,
            args.pool_size,
            pool_mode=args.bt_pool_mode,
            fund_data=args.selection_fund_data,
            selection_scan_limit=args.selection_scan_limit,
            selection_lookback_bars=args.selection_lookback_bars,
            selection_buy_classes=args.selection_buy_classes,
            selection_require_three_systems=args.selection_require_three_systems,
            selection_sample_mode=args.selection_sample_mode,
            selection_board_filter=args.selection_board_filter,
        )
        _validate_bt_data_levels(args, syms)
        args.walk_forward_scores = False
        if args.bt_pool_mode == "walk_forward":
            args.walk_forward_scores = _attach_walk_forward_scores(
                syms,
                args.bt_data,
                args.selection_fund_data,
            )
            if args.selection_require_three_systems:
                args.require = _merge_require(tuple(args.require), ("fund", "value"))
    elif source == "chart_cache":
        syms = load_chart_cache_syms(
            args.market, codes, args.chart_cache, args.pool_size,
            op_level=args.op_level,
            big_level=args.big_level,
            mid_level=args.mid_level,
        )
    elif source == "online":
        syms = load_online_syms(
            args.market, codes, args.pool_size,
            op_level=args.op_level,
            big_level=args.big_level,
            mid_level=args.mid_level,
            start_date=args.start,
            end_date=args.end,
        )
    else:
        raise ValueError(f"unsupported source: {source}")
    if not syms:
        raise RuntimeError("no symbols loaded for backtest")
    args.requested_max_pos = args.max_pos
    args.max_pos = resolve_max_pos(args.max_pos, len(syms))
    args.bs_point_ratio_multipliers = _load_bs_point_ratio_multipliers(args)
    args.regime_bs_ratio_multipliers = _load_regime_bs_ratio_multipliers(args)
    args.regime_lookback_days = int(getattr(args, "regime_lookback_days", 20) or 20)
    args.sell_classes = tuple(getattr(args, "sell_classes", (1, 2, 3)) or (1, 2, 3))
    args.sell_ratio_overrides = dict(getattr(args, "sell_ratio_overrides", {}) or {})
    args.sell_ratio_override_scope = str(
        getattr(args, "sell_ratio_override_scope", "all") or "all"
    )
    args.after_3sell_reentry_buy_classes = tuple(
        getattr(args, "after_3sell_reentry_buy_classes", ()) or ()
    )
    args.after_3sell_reentry_mid_buy_classes = tuple(
        getattr(args, "after_3sell_reentry_mid_buy_classes", ()) or ()
    )
    args.after_3sell_reentry_scope = str(
        getattr(args, "after_3sell_reentry_scope", "all") or "all"
    )

    t_start = _coerce_window_ts(args.start, syms)
    t_end = _coerce_window_ts(args.end, syms)
    label = f"{args.market}-{len(syms)}只-live-parity"
    mid_label = f"+{args.mid_level}" if args.mid_level else ""
    label = f"{args.market}-{len(syms)}-{args.big_level}{mid_label}+{args.op_level}"
    result = portfolio_mod.portfolio_backtest(
        syms=syms,
        filt=None,
        max_pos=args.max_pos,
        label=label,
        buy_priority=args.buy_priority,
        require=tuple(args.require),
        big_gate=args.big_gate,
        regime_mode=args.regime_mode,
        mid_gate=args.mid_gate,
        bs_point_ratio_multipliers=args.bs_point_ratio_multipliers,
        regime_bs_ratio_multipliers=args.regime_bs_ratio_multipliers,
        regime_lookback_days=args.regime_lookback_days,
        sell_classes=set(args.sell_classes),
        sell_ratio_overrides=args.sell_ratio_overrides,
        sell_ratio_override_scope=args.sell_ratio_override_scope,
        after_3sell_reentry_buy_classes=set(args.after_3sell_reentry_buy_classes)
        if args.after_3sell_reentry_buy_classes
        else None,
        after_3sell_reentry_mid_buy_classes=set(args.after_3sell_reentry_mid_buy_classes)
        if args.after_3sell_reentry_mid_buy_classes
        else None,
        after_3sell_reentry_scope=args.after_3sell_reentry_scope,
        init_cash=args.init_cash,
        t_start=t_start,
        t_end=t_end,
    )
    return result, syms


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run A/US live-parity Chanlun backtest")
    parser.add_argument("--market", choices=("a", "us"), default="a")
    parser.add_argument("--source", choices=("auto", "bt_data", "chart_cache", "online"), default="auto")
    parser.add_argument("--codes", help="Comma separated codes or a text file path")
    parser.add_argument("--pool-size", type=int, default=0, help="0 means all available symbols")
    parser.add_argument("--bt-pool-mode", choices=("selector", "walk_forward", "all", "sorted"), default="selector")
    parser.add_argument("--bt-data", default=BT_DATA_DIR)
    parser.add_argument("--selection-fund-data", default=DEFAULT_FUND_DATA)
    parser.add_argument("--selection-scan-limit", type=int, default=0)
    parser.add_argument("--selection-sample-mode", choices=("stratified", "sorted"), default="stratified")
    parser.add_argument(
        "--selection-board-filter",
        default="all",
        help="A-share boards: all, shsz/non_bj, or comma list of main,gem,star,bj",
    )
    parser.add_argument("--selection-lookback-bars", type=int, default=240)
    parser.add_argument("--selection-buy-classes", default="3,2,1")
    parser.add_argument("--sell-classes", default="1,2,3")
    parser.add_argument(
        "--sell-ratio-overrides",
        default="",
        help="Comma list like 3:0.5 to test partial exits for small-level sell points",
    )
    parser.add_argument(
        "--sell-ratio-override-scope",
        choices=("all", "up", "not_down"),
        default="all",
        help="Limit sell-ratio overrides to all small sells, only 30m-up sells, or not-down sells",
    )
    parser.add_argument(
        "--after-3sell-reentry-buy-classes",
        default="",
        help="Optional buy classes allowed for the next reentry after a full small-level 3sell, e.g. 3",
    )
    parser.add_argument(
        "--after-3sell-reentry-mid-buy-classes",
        default="",
        help="Optional mid-level buy classes required before reentry after a full small-level 3sell, e.g. 3",
    )
    parser.add_argument(
        "--after-3sell-reentry-scope",
        choices=("all", "up", "not_up", "neutral", "down", "not_down"),
        default="all",
        help="Limit after-3sell reentry locks by the 30m direction at the 3sell bar",
    )
    parser.add_argument(
        "--selection-require-three-systems",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--chart-cache", default=CHART_CACHE_DIR)
    parser.add_argument("--max-pos", type=int, help="Positive value fixes slots; omit or use 0 for auto")
    parser.add_argument("--op-level", default="5m")
    parser.add_argument("--big-level", default="30m")
    parser.add_argument("--mid-level")
    parser.add_argument("--init-cash", type=float, default=1_000_000)
    parser.add_argument("--buy-priority", choices=("3first", "1first"), default="3first")
    parser.add_argument("--require", default="tech")
    parser.add_argument("--big-gate", choices=("bsp", "trend"), default="bsp")
    parser.add_argument("--regime-mode", choices=("off", "adaptive"), default="off")
    parser.add_argument("--mid-gate", choices=("strict", "soft", "bull_relaxed"), default="strict")
    parser.add_argument(
        "--regime-bs-ratio-multipliers-json",
        default="",
        help='Inline JSON or file path, e.g. {"bear": {"3": 1.25}}: point-in-time '
        "regime (bull/range/bear) buy-ratio multipliers by buy class",
    )
    parser.add_argument("--regime-lookback-days", type=int, default=20)
    parser.add_argument(
        "--bs-point-ratio-overrides-json",
        default=DEFAULT_BS_POINT_RATIO_OVERRIDES,
    )
    parser.add_argument(
        "--bs-point-ratio-overrides-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output-summary")
    parser.add_argument("--output-trades")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = make_arg_parser().parse_args(argv)
    args.market = normalize_market(args.market)
    if not hasattr(args, "requested_max_pos"):
        args.requested_max_pos = args.max_pos
    args.require = tuple(s.strip() for s in args.require.split(",") if s.strip())
    args.selection_buy_classes = _parse_buy_classes(args.selection_buy_classes)
    args.sell_classes = _parse_sell_classes(args.sell_classes)
    args.sell_ratio_overrides = _parse_sell_ratio_overrides(args.sell_ratio_overrides)
    args.after_3sell_reentry_buy_classes = _parse_optional_buy_classes(
        args.after_3sell_reentry_buy_classes
    )
    args.after_3sell_reentry_mid_buy_classes = _parse_optional_buy_classes(
        args.after_3sell_reentry_mid_buy_classes
    )
    default_summary, default_trades = default_backtest_report_paths(args.market)
    args.output_summary = args.output_summary or default_summary
    args.output_trades = args.output_trades or default_trades
    result, syms = run_backtest(args)
    summary_path, trades_path = write_outputs(result, args, syms)
    print(f"summary={summary_path}")
    print(f"trades={trades_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
