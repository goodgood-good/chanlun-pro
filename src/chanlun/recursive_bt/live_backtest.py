"""Market-aware live-parity portfolio backtest for A shares and US stocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
from dataclasses import asdict, is_dataclass
from pathlib import Path
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
from chanlun.recursive_bt.engine import CL_CFG, MTFStrategy, collect_branch_signals, collect_nest_cascade_signals
from chanlun.recursive_bt.engine import collect_signals as collect_upgrade_signals
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
DEFAULT_SIGNAL_MODE = "walk_forward"
DEFAULT_SIGNAL_CACHE_DIR = "D:/chanlun_pro/reports/live_backtest_signal_cache"
DEFAULT_SIGNAL_SCAN_CHUNK_BARS = 0
DEFAULT_TREND_DIR_WARMUP_BARS = 200
DEFAULT_RECURSIVE_L0_MIN_ZS_LINES = 3
DEFAULT_BS_POINT_RATIO_OVERRIDES = "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
_SIGNAL_CACHE_VERSION = "v9"
_SIGNAL_CHECKPOINT_VERSION = "v1"
UPGRADE_CHAIN = getattr(
    CL,
    "_UPGRADE_CHAIN",
    {"1m": [("5m", "kuozhan"), ("30m", "tongjibie")], "5m": [("30m", "tongjibie")]},
)
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


def _coerce_ts_to_df_tz(value: Optional[str], df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if not value:
        return None
    ts = pd.Timestamp(value)
    if df is None or "date" not in df or len(df) == 0:
        return ts
    dates = pd.to_datetime(df["date"])
    ref = pd.Timestamp(dates.iloc[0])
    ref_tz = ref.tz
    if ref_tz is None:
        return ts.tz_convert(None) if ts.tzinfo is not None else ts
    return ts.tz_localize(ref_tz) if ts.tzinfo is None else ts.tz_convert(ref_tz)


def _slice_df_for_signal_window(
    df: Optional[pd.DataFrame],
    *,
    start: Optional[str],
    end: Optional[str],
    warmup_bars: int,
) -> Optional[pd.DataFrame]:
    """Trim raw cache data to the requested trade window plus prior warmup.

    The slice never adds future bars: ``end`` is a hard upper bound.  When a
    ``start`` is supplied, the scanner keeps only ``warmup_bars`` rows before
    the first tradable row for that frequency.  A negative warmup keeps all
    prior history, which is the closest replay to a real desk with a fully
    built historical chart state.
    """
    if df is None or len(df) == 0 or (not start and not end):
        return df
    out = df.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(out["date"])
    t_end = _coerce_ts_to_df_tz(end, out)
    if t_end is not None:
        mask = dates <= t_end
        out = out.loc[mask].reset_index(drop=True)
        dates = pd.to_datetime(out["date"])
    if start and len(out) > 0:
        t_start = _coerce_ts_to_df_tz(start, out)
        pos = int(dates.searchsorted(t_start, side="left"))
        warmup = int(warmup_bars or 0)
        begin = 0 if warmup < 0 else max(0, pos - max(warmup, 0))
        out = out.iloc[begin:].reset_index(drop=True)
    return out


def _slice_df_for_trade_window(
    df: pd.DataFrame,
    *,
    start: Optional[str],
    end: Optional[str],
) -> pd.DataFrame:
    if df is None or len(df) == 0 or (not start and not end):
        return df.reset_index(drop=True)
    out = df.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(out["date"])
    t_start = _coerce_ts_to_df_tz(start, out)
    if t_start is not None:
        out = out.loc[dates >= t_start].reset_index(drop=True)
        dates = pd.to_datetime(out["date"])
    t_end = _coerce_ts_to_df_tz(end, out)
    if t_end is not None:
        out = out.loc[dates <= t_end].reset_index(drop=True)
    return out


def _registry_scan_clock(
    df_signal: pd.DataFrame,
    trade_dates: list[pd.Timestamp],
    *,
    trade_start: Optional[str],
    warmup_bars: int,
) -> tuple[list[pd.Timestamp], int, bool]:
    """Return the signal scan clock and the trade-window output offset.

    For ``signal_warmup_bars=-1`` a real desk has not only a full CL chart
    state, but also a first-seen signal registry accumulated before the trade
    window.  Therefore the scanner must walk the prior main clock to seed
    ``seen_keys`` and only start emitting events at ``emit_start_idx``.
    """
    if df_signal is None or len(df_signal) == 0 or not trade_dates:
        return list(trade_dates or []), 0, False
    warmup = int(warmup_bars or 0)
    if warmup >= 0:
        return list(trade_dates), 0, False

    scan_dates = list(pd.to_datetime(df_signal["date"]))
    if not scan_dates:
        return list(trade_dates), 0, False

    trade_start_ts = pd.to_datetime(trade_dates[0])
    idx = int(pd.DatetimeIndex(scan_dates).searchsorted(trade_start_ts, side="left"))
    idx = max(0, min(idx, len(scan_dates)))
    if not trade_start:
        return scan_dates, idx, True

    t_start = _coerce_ts_to_df_tz(trade_start, df_signal)
    first_scan = pd.to_datetime(scan_dates[0])
    registry_complete = bool(t_start is not None and first_scan < t_start)
    return scan_dates, idx, registry_complete


def _stable_hash(obj) -> str:
    try:
        raw = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        raw = str(obj)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cl_config(
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    *,
    skip_legacy_mmd: bool = False,
    skip_legacy_zslx: bool = False,
) -> dict:
    cfg = dict(CL_CFG)
    l0_min = int(recursive_l0_min_zs_lines or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES)
    cfg["recursive_l0_min_zs_lines"] = l0_min
    if skip_legacy_mmd:
        cfg["skip_legacy_mmd"] = True
    if skip_legacy_zslx:
        cfg["skip_legacy_zslx"] = True
    return cfg


def _series_hash(values) -> str:
    idx = pd.to_datetime(pd.Series(list(values)))
    arr = idx.astype("int64").to_numpy()
    return hashlib.md5(arr.tobytes()).hexdigest()


def _df_signature(df: pd.DataFrame) -> dict:
    cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df]
    frame = df[cols].copy()
    if "date" in frame:
        frame["date"] = pd.to_datetime(frame["date"]).astype("int64")
    row_hash = pd.util.hash_pandas_object(frame, index=False).to_numpy()
    dates = pd.to_datetime(df["date"])
    return {
        "rows": int(len(df)),
        "start": str(dates.iloc[0]) if len(dates) else "",
        "end": str(dates.iloc[-1]) if len(dates) else "",
        "hash": hashlib.md5(row_hash.tobytes()).hexdigest(),
    }


def _signal_cache_meta(
    code: str,
    frequency: str,
    df: pd.DataFrame,
    main_dates: list[pd.Timestamp],
    *,
    available_delay: pd.Timedelta,
    warmup_bars: int,
    annotate_nest: bool,
    use_xd: bool = False,
    signal_source: str = "branch",
    level_filter: Optional[tuple[int, ...]] = None,
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    include_l0_upgrade_signals: bool = False,
    emit_start_idx: int = 0,
) -> dict:
    meta = {
        "version": _SIGNAL_CACHE_VERSION,
        "code": str(code),
        "frequency": str(frequency),
        "df": _df_signature(df),
        "main_dates": {
            "rows": int(len(main_dates)),
            "start": str(main_dates[0]) if main_dates else "",
            "end": str(main_dates[-1]) if main_dates else "",
            "hash": _series_hash(main_dates),
        },
        "available_delay": str(available_delay or pd.Timedelta("0min")),
        "warmup_bars": int(warmup_bars or 0),
        "emit_start_idx": int(emit_start_idx or 0),
        "annotate_nest": bool(annotate_nest),
        "cl_cfg_hash": _stable_hash(CL_CFG),
    }
    if use_xd:
        meta["use_xd"] = True
    if signal_source != "branch":
        meta["signal_source"] = str(signal_source)
    if level_filter is not None:
        meta["level_filter"] = tuple(int(x) for x in level_filter)
    if include_l0_upgrade_signals:
        meta["include_l0_upgrade_signals"] = True
    meta["recursive_l0_min_zs_lines"] = int(
        recursive_l0_min_zs_lines or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES
    )
    return meta


def _signal_cache_path(cache_dir: Optional[str], meta: dict) -> Optional[Path]:
    if not cache_dir:
        return None
    digest = _stable_hash(meta)
    return Path(cache_dir) / f"{digest}.pkl"


def _load_signal_cache(
    path: Optional[Path],
    meta: dict,
    initial_state_out: Optional[dict] = None,
) -> Optional[dict[int, list]]:
    if path is None or not path.exists():
        return None
    try:
        payload = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("meta") != meta:
        return None
    if initial_state_out is not None:
        initial_state = payload.get("initial_state")
        if not isinstance(initial_state, dict):
            return None
        initial_state_out.update(initial_state)
    by_bar = payload.get("by_bar")
    return by_bar if isinstance(by_bar, dict) else None


def _write_signal_cache(
    path: Optional[Path],
    meta: dict,
    by_bar: dict[int, list],
    initial_state: Optional[dict] = None,
) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(pickle.dumps(
            {
                "meta": meta,
                "by_bar": by_bar,
                "initial_state": dict(initial_state or {}),
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        ))
        tmp.replace(path)
        return True
    except Exception:
        return False


def _signal_checkpoint_settings(cache_path: Optional[Path], meta: dict) -> tuple[Optional[Path], int]:
    """Return optional resumable scan checkpoint path and write interval.

    This is deliberately opt-in via environment variables.  It preserves the
    strict continuous replay semantics: the checkpoint stores the live CL object
    plus first-seen registry state, then resumes the same scan instead of
    rebuilding a bounded warmup approximation.
    """
    try:
        every = int(os.environ.get("CHANLUN_WF_CHECKPOINT_EVERY", "0") or 0)
    except Exception:
        every = 0
    if every <= 0:
        return None, 0
    checkpoint_dir = str(os.environ.get("CHANLUN_WF_CHECKPOINT_DIR", "") or "")
    if checkpoint_dir:
        return Path(checkpoint_dir) / f"{_stable_hash(meta)}.checkpoint.pkl", every
    if cache_path is not None:
        return cache_path.with_suffix(cache_path.suffix + ".checkpoint"), every
    return None, 0


def _load_signal_checkpoint(path: Optional[Path], meta: dict) -> Optional[dict]:
    if path is None or not path.exists():
        return None
    try:
        payload = pickle.loads(path.read_bytes())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _SIGNAL_CHECKPOINT_VERSION:
        return None
    if payload.get("meta") != meta:
        return None
    state = payload.get("state")
    return state if isinstance(state, dict) else None


def _write_signal_checkpoint(path: Optional[Path], meta: dict, state: dict) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(pickle.dumps(
            {
                "version": _SIGNAL_CHECKPOINT_VERSION,
                "meta": meta,
                "state": state,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        ))
        tmp.replace(path)
        return True
    except Exception:
        return False


def _remove_signal_checkpoint(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _chunked_walk_forward_signals_by_main_bar(
    code: str,
    frequency: str,
    df: pd.DataFrame,
    main_dates: list[pd.Timestamp],
    *,
    available_delay: pd.Timedelta,
    warmup_bars: int,
    annotate_nest: bool,
    use_xd: bool,
    signal_source: str,
    level_filter: Optional[tuple[int, ...]],
    recursive_l0_min_zs_lines: int,
    include_l0_upgrade_signals: bool,
    initial_state_out: Optional[dict],
    signal_cache_dir: Optional[str],
    signal_cache_stats: Optional[dict],
    signal_scan_chunk_bars: int,
    emit_start_idx: int = 0,
) -> dict[int, list]:
    """Scan long windows in bounded chunks without using future bars.

    Each chunk is rescanned with its own prior ``warmup_bars`` frequency rows.
    Only events whose visible main-clock index falls inside that chunk are
    merged into the output.  This is intentionally opt-in because it trades
    continuous CL state for bounded warmup state.
    """
    chunk = max(int(signal_scan_chunk_bars or 0), 0)
    if chunk <= 0 or len(main_dates) <= chunk:
        return {}
    row_dates = list(pd.to_datetime(df["date"]))
    available_times = pd.DatetimeIndex([d + available_delay for d in row_dates])
    main_index = pd.DatetimeIndex(pd.to_datetime(main_dates))
    out: dict[int, list] = {}
    warmup = int(warmup_bars or 0)
    n = len(main_dates)

    for chunk_start in range(0, n, chunk):
        chunk_end = min(n, chunk_start + chunk)
        chunk_start_time = main_dates[chunk_start]
        chunk_end_time = main_dates[chunk_end - 1]
        first_freq = int(available_times.searchsorted(chunk_start_time, side="left"))
        begin_freq = 0 if warmup < 0 else max(0, first_freq - max(warmup, 0))
        end_freq = int(available_times.searchsorted(chunk_end_time, side="right"))
        if end_freq <= begin_freq:
            continue
        df_chunk = df.iloc[begin_freq:end_freq].reset_index(drop=True)
        main_begin_time = available_times[begin_freq]
        main_begin = int(main_index.searchsorted(main_begin_time, side="left"))
        main_chunk = main_dates[main_begin:chunk_end]
        if not main_chunk:
            continue
        local_emit_start = max(0, int(emit_start_idx or 0) - main_begin)
        chunk_events = _walk_forward_signals_by_main_bar(
            code,
            frequency,
            df_chunk,
            main_chunk,
            available_delay=available_delay,
            warmup_bars=warmup,
            annotate_nest=annotate_nest,
            use_xd=use_xd,
            signal_source=signal_source,
            level_filter=level_filter,
            recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=include_l0_upgrade_signals,
            signal_cache_dir=signal_cache_dir,
            signal_cache_stats=signal_cache_stats,
            signal_scan_chunk_bars=0,
            emit_start_idx=local_emit_start,
        )
        for local_idx, signals in chunk_events.items():
            global_idx = main_begin + local_emit_start + int(local_idx)
            if max(chunk_start, int(emit_start_idx or 0)) <= global_idx < chunk_end:
                out.setdefault(global_idx - int(emit_start_idx or 0), []).extend(signals)
    return out


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


def _signal_identity(sig) -> tuple:
    return (
        str(getattr(sig, "date", "")),
        int(getattr(sig, "level", 0) or 0),
        str(getattr(sig, "bs_type", "")),
    )


def _float_attr(sig, name: str) -> Optional[float]:
    try:
        value = getattr(sig, name, None)
        if value is None:
            return None
        value = float(value)
    except Exception:
        return None
    return value if value == value else None


def _signal_preference(sig) -> tuple:
    bs_type = str(getattr(sig, "bs_type", "")).lower()
    if "buy" in bs_type:
        stop = _float_attr(sig, "structural_stop_below")
        zg = _float_attr(sig, "zs_zg")
        price = _float_attr(sig, "price")
        return (
            1 if stop is not None else 0,
            stop if stop is not None else float("-inf"),
            zg if zg is not None else float("-inf"),
            price if price is not None else float("-inf"),
        )
    if "sell" in bs_type:
        stop = _float_attr(sig, "structural_stop_above")
        zd = _float_attr(sig, "zs_zd")
        price = _float_attr(sig, "price")
        return (
            1 if stop is not None else 0,
            -(stop if stop is not None else float("inf")),
            -(zd if zd is not None else float("inf")),
            -(price if price is not None else float("inf")),
        )
    return (0,)


def _dedupe_signals_by_identity(signals) -> list:
    chosen: dict[tuple, object] = {}
    order: list[tuple] = []
    for sig in signals or []:
        key = _signal_identity(sig)
        if key not in chosen:
            chosen[key] = sig
            order.append(key)
            continue
        if _signal_preference(sig) > _signal_preference(chosen[key]):
            chosen[key] = sig
    return [chosen[key] for key in order]


def _line_signature(line) -> tuple:
    if line is None:
        return ()
    start = getattr(line, "start", None)
    end = getattr(line, "end", None)
    sk = getattr(start, "k", None)
    ek = getattr(end, "k", None)
    return (
        str(getattr(line, "type", getattr(line, "_type", ""))),
        str(getattr(sk, "date", "")),
        round(float(getattr(start, "val", 0.0) or 0.0), 8),
        str(getattr(ek, "date", "")),
        round(float(getattr(end, "val", 0.0) or 0.0), 8),
        bool(getattr(line, "done", False)),
    )


def _collection_state_signature(cd: CL, signal_source: str):
    if signal_source in ("upgrade", "nest_cascade") and hasattr(cd, "get_xds"):
        try:
            xds = list(cd.get_xds())
            tail = xds[-3:] if len(xds) >= 3 else xds
            return ("upgrade_xd", len(xds), tuple(_line_signature(x) for x in tail))
        except Exception:
            pass
    return getattr(cd, "_last_mmd_sig", None)


def _collect_visible_signals(
    cd: CL,
    *,
    signal_source: str,
    use_xd: bool,
    annotate_nest: bool,
    level_filter: Optional[tuple[int, ...]] = None,
    include_l0_upgrade_signals: bool = False,
) -> list:
    if signal_source in ("upgrade", "nest_cascade"):
        signals = list(collect_upgrade_signals(cd))
        if include_l0_upgrade_signals:
            l0_signals = [
                sig
                for sig in collect_branch_signals(cd, use_xd=True, annotate_nest=False)
                if int(getattr(sig, "level", 0) or 0) == 0
            ]
            signals.extend(l0_signals)
        if signal_source == "nest_cascade":
            # R78 区间套介入:原生 upgrade 流(L1+ CONFIRMED 闭合与门控方向不缺位)
            # + 候选×次级别确认的提前介入事件(3buy_nest)
            signals.extend(collect_nest_cascade_signals(cd))
        signals.sort(key=lambda sig: (getattr(sig, "date", ""), int(getattr(sig, "level", 0) or 0)))
    else:
        signals = collect_branch_signals(
            cd,
            use_xd=use_xd,
            annotate_nest=annotate_nest,
        )
    if level_filter is not None:
        allowed = {int(x) for x in level_filter}
        signals = [
            sig for sig in signals if int(getattr(sig, "level", 0) or 0) in allowed
        ]
    return _dedupe_signals_by_identity(signals)


def _split_signal_bars_by_level(signal_by_bar: dict[int, list]) -> dict[int, dict[int, list]]:
    out: dict[int, dict[int, list]] = {}
    for bar, signals in (signal_by_bar or {}).items():
        for sig in signals or []:
            level = int(getattr(sig, "level", 0) or 0)
            out.setdefault(level, {}).setdefault(bar, []).append(sig)
    return out


def _upgrade_level_for(base_level: str, target_level: Optional[str]) -> Optional[int]:
    if not base_level or not target_level:
        return None
    for i, (target, _method) in enumerate(UPGRADE_CHAIN.get(str(base_level), []), start=1):
        if target == target_level:
            return i
    return None


def _walk_forward_signals_by_main_bar(
    code: str,
    frequency: str,
    df: pd.DataFrame,
    main_dates: list[pd.Timestamp],
    *,
    available_delay: pd.Timedelta,
    warmup_bars: int = 200,
    annotate_nest: bool = False,
    use_xd: bool = False,
    signal_source: str = "branch",
    level_filter: Optional[tuple[int, ...]] = None,
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    include_l0_upgrade_signals: bool = False,
    initial_state_out: Optional[dict] = None,
    signal_cache_dir: Optional[str] = None,
    signal_cache_stats: Optional[dict] = None,
    signal_scan_chunk_bars: int = DEFAULT_SIGNAL_SCAN_CHUNK_BARS,
    emit_start_idx: int = 0,
) -> dict[int, list]:
    """Collect signals as a live process would see them.

    ``batch`` mode computes final signals on the whole series and maps them back
    to historical bars. This helper instead advances the CL object only with
    bars that are visible at each main clock tick, then emits each signal
    identity once at its first visible snapshot. If a right-edge signal
    disappears and later reappears with the same anchor/type/level, it is not
    emitted again as a tradable event.
    """
    if df is None or len(df) == 0 or not main_dates:
        return {}
    cache_stats = signal_cache_stats if signal_cache_stats is not None else {}
    df = df.reset_index(drop=True)
    emit_start_idx = max(0, min(int(emit_start_idx or 0), len(main_dates) - 1))
    delay = available_delay or pd.Timedelta("0min")
    warmup = int(warmup_bars or 0)
    warmup_gate = max(warmup, 0)
    signal_source = str(signal_source or "branch").strip().lower()
    if signal_source not in {"branch", "upgrade", "nest_cascade"}:
        raise ValueError(f"unsupported signal_source: {signal_source!r}")
    if level_filter is not None:
        level_filter = tuple(int(x) for x in level_filter)
    recursive_l0_min_zs_lines = int(
        recursive_l0_min_zs_lines or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES
    )
    chunk_bars = max(int(signal_scan_chunk_bars or 0), 0)
    # A full-history replay seeds the live CL state from every prior bar and
    # then advances the same object bar by bar.  Rechunking that mode would
    # repeatedly replay the same prior history, which is slower and less like a
    # continuous trading process.  Bounded warmup can still opt into chunking.
    if warmup < 0:
        chunk_bars = 0
    if chunk_bars > 0 and len(main_dates) > chunk_bars:
        return _chunked_walk_forward_signals_by_main_bar(
            code,
            frequency,
            df,
            main_dates,
            available_delay=delay,
            warmup_bars=warmup,
            annotate_nest=annotate_nest,
            use_xd=use_xd,
            signal_source=signal_source,
            level_filter=level_filter,
            recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=include_l0_upgrade_signals,
            initial_state_out=initial_state_out,
            signal_cache_dir=signal_cache_dir,
            signal_cache_stats=cache_stats,
            signal_scan_chunk_bars=chunk_bars,
            emit_start_idx=emit_start_idx,
        )
    meta = _signal_cache_meta(
        code,
        frequency,
        df,
        main_dates,
        available_delay=delay,
        warmup_bars=warmup,
        annotate_nest=annotate_nest,
        use_xd=use_xd,
        signal_source=signal_source,
        level_filter=level_filter,
        recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
        include_l0_upgrade_signals=include_l0_upgrade_signals,
        emit_start_idx=emit_start_idx,
    )
    cache_path = _signal_cache_path(signal_cache_dir, meta)
    cached = _load_signal_cache(cache_path, meta, initial_state_out=initial_state_out)
    if cached is not None:
        cache_stats["hits"] = int(cache_stats.get("hits", 0)) + 1
        cache_stats["entries"] = int(cache_stats.get("entries", 0)) + sum(len(v) for v in cached.values())
        return cached
    if cache_path is not None:
        cache_stats["misses"] = int(cache_stats.get("misses", 0)) + 1
    row_dates = list(pd.to_datetime(df["date"]))
    open_values = df["open"].to_numpy()
    close_values = df["close"].to_numpy()
    high_values = df["high"].to_numpy() if "high" in df else close_values
    low_values = df["low"].to_numpy() if "low" in df else close_values
    volume_values = df["volume"].to_numpy() if "volume" in df else None
    checkpoint_path, checkpoint_every = _signal_checkpoint_settings(cache_path, meta)
    checkpoint = _load_signal_checkpoint(checkpoint_path, meta)
    if checkpoint is not None:
        cd = checkpoint.get("cd")
        if cd is None:
            checkpoint = None
    if checkpoint is not None:
        row_pos = int(checkpoint.get("row_pos", 0) or 0)
        active_keys = set(checkpoint.get("active_keys") or set())
        seen_keys = set(checkpoint.get("seen_keys") or set())
        ready = bool(checkpoint.get("ready", False))
        last_collect_sig = checkpoint.get("last_collect_sig")
        by_bar = dict(checkpoint.get("by_bar") or {})
        initial_state = dict(checkpoint.get("initial_state") or {})
        start_main_idx = max(0, int(checkpoint.get("next_main_idx", 0) or 0))
        if initial_state_out is not None and initial_state:
            initial_state_out.update(initial_state)
        cache_stats["checkpoint_resumes"] = int(cache_stats.get("checkpoint_resumes", 0)) + 1
    else:
        cd = CL(
            code,
            frequency,
            _cl_config(
                recursive_l0_min_zs_lines,
                skip_legacy_mmd=(signal_source == "upgrade"),
                skip_legacy_zslx=(signal_source == "upgrade"),
            ),
        )
        row_pos = 0
        active_keys: set[tuple] = set()
        seen_keys: set[tuple] = set()
        ready = False
        last_collect_sig = object()
        by_bar: dict[int, list] = {}
        initial_state: dict = {}
        start_main_idx = 0
    progress_every = max(int(os.environ.get("CHANLUN_WF_PROGRESS_EVERY", "0") or 0), 0)

    def _maybe_write_checkpoint(completed_main_idx: int) -> None:
        if (
            checkpoint_path is None
            or checkpoint_every <= 0
            or completed_main_idx + 1 >= len(main_dates)
            or (completed_main_idx + 1) % checkpoint_every != 0
        ):
            return
        state = {
            "cd": cd,
            "row_pos": int(row_pos),
            "active_keys": set(active_keys),
            "seen_keys": set(seen_keys),
            "ready": bool(ready),
            "last_collect_sig": last_collect_sig,
            "by_bar": dict(by_bar),
            "initial_state": dict(initial_state),
            "next_main_idx": int(completed_main_idx + 1),
        }
        if _write_signal_checkpoint(checkpoint_path, meta, state):
            cache_stats["checkpoint_writes"] = int(cache_stats.get("checkpoint_writes", 0)) + 1

    for main_idx in range(start_main_idx, len(main_dates)):
        main_date = main_dates[main_idx]
        if progress_every and main_idx and main_idx % progress_every == 0:
            print(
                "wf-scan "
                f"{code} {frequency} {main_idx}/{len(main_dates)} "
                f"row_pos={row_pos} emit_start_idx={emit_start_idx} "
                f"events={sum(len(v) for v in by_bar.values())}",
                flush=True,
            )
        end_pos = row_pos
        while end_pos < len(df) and row_dates[end_pos] + delay <= main_date:
            end_pos += 1
        if end_pos <= row_pos:
            continue
        if end_pos == row_pos + 1 and hasattr(cd, "process_kline_values"):
            cd.process_kline_values(
                row_dates[row_pos],
                open_values[row_pos],
                high_values[row_pos],
                low_values[row_pos],
                close_values[row_pos],
                volume_values[row_pos] if volume_values is not None else 0.0,
            )
        else:
            cd.process_klines(df.iloc[row_pos:end_pos].reset_index(drop=True))
        row_pos = end_pos
        state_sig = _collection_state_signature(cd, signal_source)
        must_collect = state_sig is None or state_sig != last_collect_sig
        current = []
        if must_collect:
            current = _collect_visible_signals(
                cd,
                signal_source=signal_source,
                use_xd=use_xd,
                annotate_nest=annotate_nest,
                level_filter=level_filter,
                include_l0_upgrade_signals=include_l0_upgrade_signals,
            )
            current_keys = {_signal_identity(sig) for sig in current}
            last_collect_sig = state_sig
        else:
            current_keys = active_keys
        if row_pos < warmup_gate or main_idx < emit_start_idx:
            active_keys = current_keys
            seen_keys.update(current_keys)
            _maybe_write_checkpoint(main_idx)
            continue
        if not ready:
            initial_state = {
                "signals": list(current),
                "dir": _last_signal_dir(current),
            }
            if initial_state_out is not None:
                initial_state_out.update(initial_state)
            active_keys = current_keys
            seen_keys.update(current_keys)
            ready = True
            _maybe_write_checkpoint(main_idx)
            continue
        fresh = (
            [
                sig
                for sig in current
                if (
                    _signal_identity(sig) not in active_keys
                    and _signal_identity(sig) not in seen_keys
                )
            ]
            if must_collect
            else []
        )
        if fresh:
            by_bar.setdefault(main_idx - emit_start_idx, []).extend(fresh)
            seen_keys.update(_signal_identity(sig) for sig in fresh)
        active_keys = current_keys
        _maybe_write_checkpoint(main_idx)
        if row_pos >= len(df):
            # Nothing more can become visible from this frequency.
            continue
    if _write_signal_cache(cache_path, meta, by_bar, initial_state=initial_state):
        cache_stats["writes"] = int(cache_stats.get("writes", 0)) + 1
        cache_stats["entries"] = int(cache_stats.get("entries", 0)) + sum(len(v) for v in by_bar.values())
        _remove_signal_checkpoint(checkpoint_path)
    return by_bar


def _last_signal_dir(signals) -> str:
    cur = "neutral"
    for sig in sorted(signals or [], key=lambda s: getattr(s, "date", "")):
        if getattr(sig, "is_buy", False):
            cur = "up"
        elif getattr(sig, "is_sell", False):
            cur = "down"
    return cur


def _dir_series_from_signal_bars(
    signal_by_bar: dict[int, list],
    n: int,
    initial_dir: str = "neutral",
) -> list[str]:
    out: list[str] = []
    cur = str(initial_dir or "neutral")
    for i in range(n):
        for sig in sorted(signal_by_bar.get(i, []) or [], key=lambda s: getattr(s, "date", "")):
            if getattr(sig, "is_buy", False):
                cur = "up"
            elif getattr(sig, "is_sell", False):
                cur = "down"
        out.append(cur)
    return out


def _signal_event_rows_for_bars(
    code: str,
    stream: str,
    signal_by_bar: dict[int, list],
    dates: list[pd.Timestamp],
    opens,
) -> list[dict]:
    rows: list[dict] = []
    d2i = {pd.Timestamp(d): i for i, d in enumerate(dates)}
    open_arr = np.asarray(opens, dtype=float) if opens is not None else np.empty(0)
    for bar, signals in sorted((signal_by_bar or {}).items()):
        if bar < 0 or bar >= len(dates):
            continue
        visible_time = pd.Timestamp(dates[bar])
        fill_bar = bar + 1 if bar + 1 < len(dates) else None
        fill_time = pd.Timestamp(dates[fill_bar]) if fill_bar is not None else None
        fill_open = (
            float(open_arr[fill_bar])
            if fill_bar is not None and fill_bar < len(open_arr)
            else None
        )
        for sig in signals or []:
            anchor_time = pd.Timestamp(getattr(sig, "date", visible_time))
            anchor_bar = d2i.get(anchor_time)
            rows.append({
                "code": code,
                "stream": stream,
                "level": int(getattr(sig, "level", 0) or 0),
                "bs_type": str(getattr(sig, "bs_type", "")),
                "anchor_time": str(anchor_time),
                "visible_time": str(visible_time),
                "next_fill_time": str(fill_time) if fill_time is not None else "",
                "anchor_bar": "" if anchor_bar is None else int(anchor_bar),
                "visible_bar": int(bar),
                "next_fill_bar": "" if fill_bar is None else int(fill_bar),
                "anchor_to_visible_bars": (
                    "" if anchor_bar is None else int(bar - anchor_bar)
                ),
                "signal_price": float(getattr(sig, "price", 0.0) or 0.0),
                "next_fill_open": fill_open,
                "nest_operable": getattr(sig, "nest_operable", None),
                "nest_depth": int(getattr(sig, "nest_depth", 0) or 0),
                "structural_stop_below": getattr(sig, "structural_stop_below", None),
                "structural_stop_above": getattr(sig, "structural_stop_above", None),
                "zs_zd": getattr(sig, "zs_zd", None),
                "zs_zg": getattr(sig, "zs_zg", None),
            })
    return rows


def _walk_forward_trend_dir_by_main_bar(
    code: str,
    frequency: str,
    df: pd.DataFrame,
    main_dates: list[pd.Timestamp],
    *,
    available_delay: pd.Timedelta,
    warmup_bars: int = DEFAULT_TREND_DIR_WARMUP_BARS,
) -> list[str]:
    """Visible high-level stroke direction at each main-clock bar.

    This intentionally tracks only the latest visible 30m structure direction,
    not a final full-series trend.  It is used as a core-position gate; buy/sell
    points remain the event source for activity trades.
    """
    if df is None or len(df) == 0 or not main_dates:
        return ["neutral"] * len(main_dates)
    cd = CL(code, frequency, dict(CL_CFG))
    df = df.reset_index(drop=True)
    row_dates = list(pd.to_datetime(df["date"]))
    delay = available_delay or pd.Timedelta("0min")
    warmup = max(int(warmup_bars or 0), 0)
    row_pos = 0
    cur = "neutral"
    out: list[str] = []

    for main_date in main_dates:
        end_pos = row_pos
        while end_pos < len(df) and row_dates[end_pos] + delay <= main_date:
            end_pos += 1
        if end_pos > row_pos:
            cd.process_klines(df.iloc[row_pos:end_pos].reset_index(drop=True))
            row_pos = end_pos
            if row_pos >= warmup:
                bis = list(cd.get_bis())
                if bis:
                    last_type = str(getattr(bis[-1], "type", getattr(bis[-1], "_type", "")))
                    if last_type in {"up", "down"}:
                        cur = last_type
        out.append(cur if row_pos >= warmup else "neutral")
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
    signal_mode: str = DEFAULT_SIGNAL_MODE,
    signal_warmup_bars: int = 200,
    annotate_nest: bool = False,
    signal_cache_dir: Optional[str] = None,
    signal_scan_chunk_bars: int = DEFAULT_SIGNAL_SCAN_CHUNK_BARS,
    trend_dir_warmup_bars: int = DEFAULT_TREND_DIR_WARMUP_BARS,
    signal_unit: str = "bi",
    signal_source: str = "branch",
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    include_l0_upgrade_signals: bool = False,
    trade_start: Optional[str] = None,
    trade_end: Optional[str] = None,
):
    market = normalize_market(market)
    code = normalize_code(market, code)
    signal_mode = str(signal_mode or DEFAULT_SIGNAL_MODE).strip().lower()
    if signal_mode not in {"batch", "walk_forward"}:
        raise ValueError(f"unsupported signal_mode: {signal_mode!r}")
    signal_unit = str(signal_unit or "bi").strip().lower()
    if signal_unit not in {"bi", "xd"}:
        raise ValueError(f"unsupported signal_unit: {signal_unit!r}")
    signal_source = str(signal_source or "branch").strip().lower()
    if signal_source not in {"branch", "upgrade", "nest_cascade"}:
        raise ValueError(f"unsupported signal_source: {signal_source!r}")
    recursive_l0_min_zs_lines = int(
        recursive_l0_min_zs_lines or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES
    )
    if recursive_l0_min_zs_lines not in {3, 4}:
        raise ValueError(
            f"unsupported recursive_l0_min_zs_lines: {recursive_l0_min_zs_lines!r}"
        )
    use_xd_signals = signal_unit == "xd"
    df_op = df_op.reset_index(drop=True)
    if df_big is not None:
        df_big = df_big.reset_index(drop=True)
    if df_mid is not None:
        df_mid = df_mid.reset_index(drop=True)
    df_trade = _slice_df_for_trade_window(df_op, start=trade_start, end=trade_end)
    if df_trade is None or len(df_trade) == 0:
        df_trade = df_op
    dates = list(df_trade["date"])
    scan_dates, emit_start_idx, registry_complete = _registry_scan_clock(
        df_op,
        dates,
        trade_start=trade_start,
        warmup_bars=signal_warmup_bars,
    )
    signal_cache_stats = {"hits": 0, "misses": 0, "writes": 0, "entries": 0}
    if signal_mode == "walk_forward":
        mid_by_bar_upgrade = None
        mid_dir_at_upgrade = None
        big_initial_dir = "neutral"
        if signal_source == "upgrade":
            upgrade_initial_state: dict = {}
            small_by_bar = _walk_forward_signals_by_main_bar(
                code,
                op_level,
                df_op,
                scan_dates,
                available_delay=pd.Timedelta("0min"),
                warmup_bars=signal_warmup_bars,
                annotate_nest=False,
                use_xd=use_xd_signals,
                signal_source="upgrade",
                recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
                include_l0_upgrade_signals=include_l0_upgrade_signals,
                initial_state_out=upgrade_initial_state,
                signal_cache_dir=signal_cache_dir,
                signal_cache_stats=signal_cache_stats,
                signal_scan_chunk_bars=signal_scan_chunk_bars,
                emit_start_idx=emit_start_idx,
            )
            by_level = _split_signal_bars_by_level(small_by_bar)
            initial_by_level = _split_signal_bars_by_level({
                0: upgrade_initial_state.get("signals", []) or []
            })
            big_level_num = _upgrade_level_for(op_level, big_level)
            big_by_bar = by_level.get(big_level_num, {}) if big_level_num is not None else {}
            if big_level_num is not None:
                big_initial_dir = _last_signal_dir(initial_by_level.get(big_level_num, {}).get(0, []))
            mid_level_num = _upgrade_level_for(op_level, mid_level)
            if mid_level_num is not None:
                mid_by_bar_upgrade = by_level.get(mid_level_num, {})
                mid_initial_dir = _last_signal_dir(initial_by_level.get(mid_level_num, {}).get(0, []))
                mid_dir_at_upgrade = _dir_series_from_signal_bars(
                    mid_by_bar_upgrade,
                    len(dates),
                    initial_dir=mid_initial_dir,
                )
        else:
            small_by_bar = _walk_forward_signals_by_main_bar(
                code,
                op_level,
                df_op,
                scan_dates,
                available_delay=pd.Timedelta("0min"),
                warmup_bars=signal_warmup_bars,
                annotate_nest=annotate_nest,
                use_xd=use_xd_signals,
                signal_source="branch",
                recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
                include_l0_upgrade_signals=False,
                signal_cache_dir=signal_cache_dir,
                signal_cache_stats=signal_cache_stats,
                signal_scan_chunk_bars=signal_scan_chunk_bars,
                emit_start_idx=emit_start_idx,
            )
            big_by_bar = {}
        trend_dir_at = None
        if signal_source == "branch" and df_big is not None and len(df_big) >= 50:
            big_initial_state: dict = {}
            big_by_bar = _walk_forward_signals_by_main_bar(
                code,
                big_level,
                df_big,
                scan_dates,
                available_delay=_freq_delay(big_level),
                warmup_bars=signal_warmup_bars,
                annotate_nest=False,
                use_xd=use_xd_signals,
                signal_source="branch",
                recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
                include_l0_upgrade_signals=False,
                initial_state_out=big_initial_state,
                signal_cache_dir=signal_cache_dir,
                signal_cache_stats=signal_cache_stats,
                signal_scan_chunk_bars=signal_scan_chunk_bars,
                emit_start_idx=emit_start_idx,
            )
            big_initial_dir = str(big_initial_state.get("dir") or "neutral")
        if df_big is not None and len(df_big) >= 50:
            trend_dir_at = _walk_forward_trend_dir_by_main_bar(
                code,
                big_level,
                df_big,
                dates,
                available_delay=_freq_delay(big_level),
                warmup_bars=trend_dir_warmup_bars,
            )
        big_dir_at = _dir_series_from_signal_bars(
            big_by_bar,
            len(dates),
            initial_dir=big_initial_dir,
        )
    else:
        cd_op = CL(
            code,
            op_level,
            _cl_config(
                recursive_l0_min_zs_lines,
                skip_legacy_mmd=(signal_source == "upgrade"),
                skip_legacy_zslx=(signal_source == "upgrade"),
            ),
        )
        cd_op.process_klines(df_op)
        if signal_source == "upgrade":
            small = list(collect_upgrade_signals(cd_op))
            if include_l0_upgrade_signals:
                small.extend(
                    sig
                    for sig in collect_branch_signals(cd_op, use_xd=True, annotate_nest=False)
                    if int(getattr(sig, "level", 0) or 0) == 0
                )
                small.sort(key=lambda sig: (getattr(sig, "date", ""), int(getattr(sig, "level", 0) or 0)))
            big_level_num = _upgrade_level_for(op_level, big_level)
            big = [
                sig for sig in small
                if big_level_num is not None and int(getattr(sig, "level", 0) or 0) == big_level_num
            ]
        else:
            small = collect_branch_signals(cd_op, use_xd=use_xd_signals, annotate_nest=annotate_nest)
            big = []
            if df_big is not None and len(df_big) >= 50:
                cd_big = CL(code, big_level, _cl_config(recursive_l0_min_zs_lines))
                cd_big.process_klines(df_big)
                big = collect_branch_signals(cd_big, use_xd=use_xd_signals)
        strat = MTFStrategy(
            small, big, dates, f"{op_level}+{big_level}",
            gate="not_down", big_delay=_freq_delay(big_level),
        )
        small_by_bar = strat.small_by_bar
        big_dir_at = strat.big_dir_at
        big_by_bar = _signals_by_main_bar(big, dates, _freq_delay(big_level))
    out = {
        "name": code,
        "code": code,
        "rules": market_rules_for_code(market, code),
        "dates": dates,
        "open": df_trade["open"].to_numpy(),
        "high": df_trade["high"].to_numpy() if "high" in df_trade else df_trade["close"].to_numpy(),
        "low": df_trade["low"].to_numpy() if "low" in df_trade else df_trade["close"].to_numpy(),
        "close": df_trade["close"].to_numpy(),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": small_by_bar,
        "big_by_bar": big_by_bar,
        "big_dir_at": big_dir_at,
        "levels": {"op": op_level, "big": big_level, "mid": mid_level},
            "signal_mode": signal_mode,
            "signal_source": signal_source,
            "include_l0_upgrade_signals": bool(include_l0_upgrade_signals),
            "signal_unit": signal_unit,
            "recursive_l0_min_zs_lines": recursive_l0_min_zs_lines,
            "signal_warmup_bars": int(signal_warmup_bars or 0),
            "signal_scan_chunk_bars": int(signal_scan_chunk_bars or 0),
            "signal_seen_registry_complete": bool(registry_complete),
            "signal_seen_registry_first_date": str(scan_dates[0]) if scan_dates else "",
            "signal_seen_registry_emit_start_idx": int(emit_start_idx),
            "signal_cache_stats": signal_cache_stats,
    }
    if signal_mode == "walk_forward" and trend_dir_at is not None:
        out["trend_dir_at"] = trend_dir_at
    if signal_mode == "walk_forward" and signal_source == "upgrade":
        if mid_by_bar_upgrade is not None:
            out["mid_dir_at"] = mid_dir_at_upgrade
            out["mid_by_bar"] = mid_by_bar_upgrade
    elif mid_level and df_mid is not None and len(df_mid) >= 50:
        if signal_mode == "walk_forward":
            mid_initial_state: dict = {}
            mid_by_bar = _walk_forward_signals_by_main_bar(
                code,
                mid_level,
                df_mid,
                scan_dates,
                available_delay=_freq_delay(mid_level),
                warmup_bars=signal_warmup_bars,
                annotate_nest=False,
                use_xd=use_xd_signals,
                signal_source="branch",
                recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
                include_l0_upgrade_signals=False,
                initial_state_out=mid_initial_state,
                signal_cache_dir=signal_cache_dir,
                signal_cache_stats=signal_cache_stats,
                signal_scan_chunk_bars=signal_scan_chunk_bars,
                emit_start_idx=emit_start_idx,
            )
            out["mid_dir_at"] = _dir_series_from_signal_bars(
                mid_by_bar,
                len(dates),
                initial_dir=str(mid_initial_state.get("dir") or "neutral"),
            )
            out["mid_by_bar"] = mid_by_bar
        else:
            cd_mid = CL(code, mid_level, _cl_config(recursive_l0_min_zs_lines))
            cd_mid.process_klines(df_mid)
            mid = collect_branch_signals(cd_mid, use_xd=use_xd_signals)
            mid_strat = MTFStrategy(
                [], mid, dates, f"{op_level}+{mid_level}",
                gate="not_down", big_delay=_freq_delay(mid_level),
            )
            out["mid_dir_at"] = mid_strat.big_dir_at
            out["mid_by_bar"] = _signals_by_main_bar(mid, dates, _freq_delay(mid_level))
    if signal_source == "upgrade":
        signal_events = _signal_event_rows_for_bars(
            code, "upgrade", small_by_bar, dates, out["open"]
        )
    else:
        signal_events = _signal_event_rows_for_bars(
            code, "op", small_by_bar, dates, out["open"]
        )
        if out.get("mid_by_bar"):
            signal_events.extend(_signal_event_rows_for_bars(
                code, "mid", out["mid_by_bar"], dates, out["open"]
            ))
        if big_by_bar:
            signal_events.extend(_signal_event_rows_for_bars(
                code, "big", big_by_bar, dates, out["open"]
            ))
    out["signal_events"] = signal_events
    return out


def load_chart_cache_syms(
    market: str,
    codes: Optional[list[str]] = None,
    cache_dir: str = CHART_CACHE_DIR,
    pool_size: int = 50,
    op_level: str = "5m",
    big_level: str = "30m",
    mid_level: Optional[str] = None,
    signal_mode: str = DEFAULT_SIGNAL_MODE,
    signal_warmup_bars: int = 200,
    annotate_nest: bool = False,
    start: Optional[str] = None,
    end: Optional[str] = None,
    signal_cache_dir: Optional[str] = None,
    signal_scan_chunk_bars: int = DEFAULT_SIGNAL_SCAN_CHUNK_BARS,
    trend_dir_warmup_bars: int = DEFAULT_TREND_DIR_WARMUP_BARS,
    signal_unit: str = "bi",
    signal_source: str = "branch",
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    include_l0_upgrade_signals: bool = False,
) -> dict:
    market = normalize_market(market)
    codes = codes or list_chart_cache_codes(market, cache_dir)
    if pool_size > 0:
        codes = codes[:pool_size]
    syms = {}
    for code in codes:
        code = normalize_code(market, code)
        df_op = load_chart_cache_klines(market, code, op_level, cache_dir)
        df_op = _slice_df_for_signal_window(
            df_op, start=start, end=end, warmup_bars=signal_warmup_bars
        )
        if df_op is None or len(df_op) < 100:
            continue
        df_big = load_chart_cache_klines(market, code, big_level, cache_dir)
        df_big = _slice_df_for_signal_window(
            df_big, start=start, end=end, warmup_bars=signal_warmup_bars
        )
        df_mid = (
            load_chart_cache_klines(market, code, mid_level, cache_dir)
            if mid_level else None
        )
        df_mid = _slice_df_for_signal_window(
            df_mid, start=start, end=end, warmup_bars=signal_warmup_bars
        )
        syms[code] = build_symbol_from_klines(
            market, code, df_op, df_big,
            op_level=op_level, big_level=big_level,
            df_mid=df_mid, mid_level=mid_level,
            signal_mode=signal_mode,
            signal_warmup_bars=signal_warmup_bars,
            annotate_nest=annotate_nest,
            signal_cache_dir=signal_cache_dir,
            signal_scan_chunk_bars=signal_scan_chunk_bars,
            trend_dir_warmup_bars=trend_dir_warmup_bars,
            signal_unit=signal_unit,
            signal_source=signal_source,
            recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=include_l0_upgrade_signals,
            trade_start=start,
            trade_end=end,
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
    signal_mode: str = DEFAULT_SIGNAL_MODE,
    signal_warmup_bars: int = 200,
    annotate_nest: bool = False,
    signal_cache_dir: Optional[str] = None,
    signal_scan_chunk_bars: int = DEFAULT_SIGNAL_SCAN_CHUNK_BARS,
    trend_dir_warmup_bars: int = DEFAULT_TREND_DIR_WARMUP_BARS,
    signal_unit: str = "bi",
    signal_source: str = "branch",
    recursive_l0_min_zs_lines: int = DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
    include_l0_upgrade_signals: bool = False,
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
            signal_mode=signal_mode,
            signal_warmup_bars=signal_warmup_bars,
            annotate_nest=annotate_nest,
            signal_cache_dir=signal_cache_dir,
            signal_scan_chunk_bars=signal_scan_chunk_bars,
            trend_dir_warmup_bars=trend_dir_warmup_bars,
            signal_unit=signal_unit,
            signal_source=signal_source,
            recursive_l0_min_zs_lines=recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=include_l0_upgrade_signals,
            trade_start=start_date,
            trade_end=end_date,
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
    from chanlun.recursive_bt.market_runtime import parse_regime_ratio_multipliers

    return parse_regime_ratio_multipliers(
        getattr(args, "regime_bs_ratio_multipliers_json", "")
    )


def _trade_rows(trades) -> list[dict]:
    rows = []
    for trade in trades:
        if is_dataclass(trade):
            row = asdict(trade)
        else:
            row = dict(trade)
        rows.append(row)
    return rows


def _aggregate_signal_cache_stats(syms: dict) -> dict[str, int]:
    total = {"hits": 0, "misses": 0, "writes": 0, "entries": 0}
    for sym in syms.values():
        stats = sym.get("signal_cache_stats") if isinstance(sym, dict) else None
        if not isinstance(stats, dict):
            continue
        for key in total:
            total[key] += int(stats.get(key, 0) or 0)
    return total


def _aggregate_signal_seen_registry_complete(syms: dict) -> bool:
    if not syms:
        return False
    return all(
        isinstance(sym, dict) and bool(sym.get("signal_seen_registry_complete", False))
        for sym in syms.values()
    )


def _signal_event_rows(syms: dict) -> list[dict]:
    rows: list[dict] = []
    for code, sym in sorted((syms or {}).items()):
        for row in sym.get("signal_events", []) or []:
            out = dict(row)
            out.setdefault("code", code)
            rows.append(out)
    return rows


def _no_future_policy_summary(args, *, signal_seen_registry_complete: Optional[bool] = None) -> dict:
    """Machine-readable replay fidelity flags for backtest reports.

    ``walk_forward`` prevents future bars from entering the decision clock, but
    a bounded warmup still means the Chanlun state is rebuilt from a truncated
    historical prefix.  The user-facing "strict real practice" evidence should
    therefore distinguish no-future execution from full-history state fidelity.
    """
    signal_mode = str(getattr(args, "signal_mode", DEFAULT_SIGNAL_MODE) or DEFAULT_SIGNAL_MODE)
    warmup = int(getattr(args, "signal_warmup_bars", 0) or 0)
    requested_chunk = int(getattr(args, "signal_scan_chunk_bars", 0) or 0)
    chunk = 0 if warmup < 0 else max(requested_chunk, 0)
    full_history = warmup < 0
    if signal_seen_registry_complete is None:
        start_arg = getattr(args, "start", None)
        registry_complete = full_history and not bool(start_arg)
    else:
        registry_complete = full_history and bool(signal_seen_registry_complete)
    warning_parts: list[str] = []
    if not full_history:
        warning_parts.append(
            "signal scan uses a bounded warmup; it is no-future but can differ "
            "from a replay initialized with all prior bars"
        )
    elif not registry_complete:
        warning_parts.append(
            "full prior bars seed the Chanlun state, but the signal first-seen "
            "registry is seeded at the trade window start; old right-edge "
            "signals that disappeared before the window can reappear unless a "
            "continuous registry scan starts from the raw data beginning"
        )
    warning = "; ".join(warning_parts)
    policy = {
        "strict_no_future": signal_mode == "walk_forward",
        "signal_mode": signal_mode,
        "anchor_time_tradeable": False,
        "decision_time": "visible bar close",
        "execution_time": "next bar open",
        "history_state": "full_prior_history" if full_history else "bounded_warmup",
        "history_state_complete": bool(full_history),
        "signal_warmup_bars": warmup,
        "chunked_signal_scan": chunk > 0,
        "signal_scan_chunk_bars": chunk,
        "signal_seen_registry": (
            "continuous_from_data_start"
            if registry_complete
            else "seeded_at_trade_window_start"
        ),
        "signal_seen_registry_complete": bool(registry_complete),
        "stale_reappearing_signal_risk": not bool(registry_complete),
        "warning": warning,
    }
    if requested_chunk != chunk:
        policy["requested_signal_scan_chunk_bars"] = requested_chunk
    return policy


def write_outputs(result: dict, args, syms: dict) -> tuple[str, str, str]:
    summary_path = args.output_summary
    trades_path = args.output_trades
    signals_path = getattr(args, "output_signals", "") or ""
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    os.makedirs(os.path.dirname(trades_path), exist_ok=True)
    if signals_path:
        os.makedirs(os.path.dirname(signals_path), exist_ok=True)
    master = result["master"]
    symbol_codes = list(syms.keys())
    signal_rows = _signal_event_rows(syms)
    registry_complete = _aggregate_signal_seen_registry_complete(syms)
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
        "sell_ratio_policy": str(getattr(args, "sell_ratio_policy", "all_out") or "all_out"),
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
        "signal_mode": str(getattr(args, "signal_mode", DEFAULT_SIGNAL_MODE) or DEFAULT_SIGNAL_MODE),
        "signal_source": str(getattr(args, "signal_source", "branch") or "branch"),
        "include_l0_upgrade_signals": bool(
            getattr(args, "include_l0_upgrade_signals", False)
        ),
        "signal_unit": str(getattr(args, "signal_unit", "bi") or "bi"),
        "recursive_l0_min_zs_lines": int(
            getattr(args, "recursive_l0_min_zs_lines", DEFAULT_RECURSIVE_L0_MIN_ZS_LINES)
            or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES
        ),
        "signal_warmup_bars": int(getattr(args, "signal_warmup_bars", 0) or 0),
        "signal_scan_chunk_bars": int(getattr(args, "signal_scan_chunk_bars", 0) or 0),
        "trend_dir_warmup_bars": int(
            getattr(args, "trend_dir_warmup_bars", DEFAULT_TREND_DIR_WARMUP_BARS)
            or 0
        ),
        "signal_cache_dir": str(getattr(args, "signal_cache_dir", "") or ""),
        "signal_cache_stats": _aggregate_signal_cache_stats(syms),
        "signal_seen_registry_complete": bool(registry_complete),
        "signal_event_count": int(len(signal_rows)),
        "no_future_policy": _no_future_policy_summary(
            args,
            signal_seen_registry_complete=registry_complete,
        ),
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
        "regime_source_code": str(getattr(args, "regime_source_code", "") or ""),
        "trend_core_hold_ratio": float(getattr(args, "trend_core_hold_ratio", 0.0) or 0.0),
        "trend_core_source": str(getattr(args, "trend_core_source", "gate") or "gate"),
        "core_signal_level": int(getattr(args, "core_signal_level", 0) or 0),
        "swing_signal_level": int(getattr(args, "swing_signal_level", 0) or 0),
        "big_down_activity_buy_ratio_multiplier": float(
            getattr(args, "big_down_activity_buy_ratio_multiplier", 0.0) or 0.0
        ),
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
    if signals_path:
        pd.DataFrame(signal_rows).to_csv(signals_path, index=False, encoding="utf-8-sig")
    return summary_path, trades_path, signals_path


def run_backtest(args) -> tuple[dict, dict]:
    codes = parse_codes(args.market, args.codes)
    source = args.source
    if source == "auto":
        source = "bt_data" if args.market == "a" else "chart_cache"
    args.source = source
    raw_signal_mode = getattr(args, "signal_mode", None)
    if raw_signal_mode is None:
        raw_signal_mode = "batch" if source == "bt_data" else DEFAULT_SIGNAL_MODE
    args.signal_mode = str(raw_signal_mode or DEFAULT_SIGNAL_MODE).strip().lower()
    args.signal_source = str(getattr(args, "signal_source", "branch") or "branch").strip().lower()
    if args.signal_source not in {"branch", "upgrade", "nest_cascade"}:
        raise ValueError(f"unsupported signal_source: {args.signal_source!r}")
    args.include_l0_upgrade_signals = bool(
        getattr(args, "include_l0_upgrade_signals", False)
    )
    args.signal_unit = str(getattr(args, "signal_unit", "bi") or "bi").strip().lower()
    if args.signal_unit not in {"bi", "xd"}:
        raise ValueError(f"unsupported signal_unit: {args.signal_unit!r}")
    args.recursive_l0_min_zs_lines = int(
        getattr(args, "recursive_l0_min_zs_lines", DEFAULT_RECURSIVE_L0_MIN_ZS_LINES)
        or DEFAULT_RECURSIVE_L0_MIN_ZS_LINES
    )
    if args.recursive_l0_min_zs_lines not in {3, 4}:
        raise ValueError(
            f"unsupported recursive_l0_min_zs_lines: {args.recursive_l0_min_zs_lines!r}"
        )
    args.signal_warmup_bars = int(getattr(args, "signal_warmup_bars", 200) or 0)
    args.trend_dir_warmup_bars = int(
        getattr(args, "trend_dir_warmup_bars", DEFAULT_TREND_DIR_WARMUP_BARS)
        or 0
    )
    annotate_nest = "nest" in tuple(args.require or ()) or "nest_soft" in tuple(args.require or ())
    if args.signal_mode == "walk_forward" and source == "bt_data":
        raise ValueError("signal_mode=walk_forward requires raw klines from chart_cache or online")
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
            signal_mode=args.signal_mode,
            signal_warmup_bars=args.signal_warmup_bars,
            annotate_nest=annotate_nest,
            start=args.start,
            end=args.end,
            signal_cache_dir=getattr(args, "signal_cache_dir", ""),
            signal_scan_chunk_bars=getattr(args, "signal_scan_chunk_bars", 0),
            trend_dir_warmup_bars=args.trend_dir_warmup_bars,
            signal_unit=args.signal_unit,
            signal_source=args.signal_source,
            recursive_l0_min_zs_lines=args.recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=args.include_l0_upgrade_signals,
        )
    elif source == "online":
        syms = load_online_syms(
            args.market, codes, args.pool_size,
            op_level=args.op_level,
            big_level=args.big_level,
            mid_level=args.mid_level,
            start_date=args.start,
            end_date=args.end,
            signal_mode=args.signal_mode,
            signal_warmup_bars=args.signal_warmup_bars,
            annotate_nest=annotate_nest,
            signal_cache_dir=getattr(args, "signal_cache_dir", ""),
            signal_scan_chunk_bars=getattr(args, "signal_scan_chunk_bars", 0),
            trend_dir_warmup_bars=args.trend_dir_warmup_bars,
            signal_unit=args.signal_unit,
            signal_source=args.signal_source,
            recursive_l0_min_zs_lines=args.recursive_l0_min_zs_lines,
            include_l0_upgrade_signals=args.include_l0_upgrade_signals,
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
    args.regime_source_code = str(getattr(args, "regime_source_code", "") or "")
    args.trend_core_hold_ratio = min(
        max(float(getattr(args, "trend_core_hold_ratio", 0.0) or 0.0), 0.0),
        1.0,
    )
    args.trend_core_source = str(getattr(args, "trend_core_source", "gate") or "gate")
    args.core_signal_level = max(int(getattr(args, "core_signal_level", 0) or 0), 0)
    if args.core_signal_level <= 0 and args.signal_source == "upgrade":
        args.core_signal_level = _upgrade_level_for(args.op_level, args.big_level) or 0
    args.swing_signal_level = max(int(getattr(args, "swing_signal_level", 0) or 0), 0)
    if args.swing_signal_level <= 0 and args.signal_source == "upgrade":
        args.swing_signal_level = _upgrade_level_for(args.op_level, args.mid_level) or 0
    args.big_down_activity_buy_ratio_multiplier = min(
        max(
            float(getattr(args, "big_down_activity_buy_ratio_multiplier", 0.0) or 0.0),
            0.0,
        ),
        1.0,
    )
    regime_source_sym = None
    if args.regime_source_code and args.regime_bs_ratio_multipliers:
        regime_source_sym = syms.get(args.regime_source_code)
        if regime_source_sym is None and source == "bt_data":
            old_dir = portfolio_mod.BT_DATA
            portfolio_mod.BT_DATA = args.bt_data
            try:
                regime_source_sym = portfolio_mod.load_cached(args.regime_source_code)
            finally:
                portfolio_mod.BT_DATA = old_dir
        if regime_source_sym is None:
            raise RuntimeError(
                f"regime source {args.regime_source_code} not found in pool or bt_data"
            )
    args.sell_classes = tuple(getattr(args, "sell_classes", (1, 2, 3)) or (1, 2, 3))
    args.sell_ratio_overrides = dict(getattr(args, "sell_ratio_overrides", {}) or {})
    args.sell_ratio_override_scope = str(
        getattr(args, "sell_ratio_override_scope", "all") or "all"
    )
    args.sell_ratio_policy = str(
        getattr(args, "sell_ratio_policy", "all_out") or "all_out"
    ).strip().lower()
    if args.sell_ratio_policy not in {"all_out", "original_layered"}:
        raise ValueError(f"unsupported sell_ratio_policy: {args.sell_ratio_policy!r}")
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
        regime_source_sym=regime_source_sym,
        trend_core_hold_ratio=args.trend_core_hold_ratio,
        trend_core_source=args.trend_core_source,
        core_signal_level=args.core_signal_level,
        swing_signal_level=args.swing_signal_level,
        big_down_activity_buy_ratio_multiplier=args.big_down_activity_buy_ratio_multiplier,
        sell_classes=set(args.sell_classes),
        sell_ratio_overrides=args.sell_ratio_overrides,
        sell_ratio_override_scope=args.sell_ratio_override_scope,
        sell_ratio_policy=args.sell_ratio_policy,
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
        "--sell-ratio-policy",
        choices=("all_out", "original_layered"),
        default="all_out",
        help=(
            "all_out preserves legacy full sell behavior; original_layered uses "
            "level-aware partial exits while higher-level trend has not broken"
        ),
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
    parser.add_argument(
        "--signal-mode",
        choices=("batch", "walk_forward"),
        default=DEFAULT_SIGNAL_MODE,
        help="batch computes final full-series Chanlun signals; walk_forward emits only signals visible bar by bar",
    )
    parser.add_argument(
        "--signal-unit",
        choices=("bi", "xd"),
        default="bi",
        help="Structure unit for buy/sell point events: bi for pen-level observation, xd for formal segment-level trading",
    )
    parser.add_argument(
        "--signal-source",
        choices=("branch", "upgrade", "nest_cascade"),
        default="branch",
        help="branch=current branch buy/sell points; upgrade=get_kuozhan_levels() L1/L2 recursive visible signals; nest_cascade=upgrade + 区间套介入事件(3buy_nest)",
    )
    parser.add_argument(
        "--include-l0-upgrade-signals",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When signal-source=upgrade, also include formal 1m/L0 line-level "
            "branch buy/sell points as scalp-layer events"
        ),
    )
    parser.add_argument(
        "--recursive-l0-min-zs-lines",
        type=int,
        choices=(3, 4),
        default=DEFAULT_RECURSIVE_L0_MIN_ZS_LINES,
        help="L0 center construction lines for recursive branch: 3 follows original definition, 4 keeps legacy confirmation gate",
    )
    parser.add_argument(
        "--signal-warmup-bars",
        type=int,
        default=200,
        help="Non-trading warmup bars per signal level for walk_forward mode; -1 keeps all prior history",
    )
    parser.add_argument(
        "--signal-cache-dir",
        default=DEFAULT_SIGNAL_CACHE_DIR,
        help="Directory for strict walk-forward signal event cache; empty disables cache",
    )
    parser.add_argument(
        "--signal-scan-chunk-bars",
        type=int,
        default=DEFAULT_SIGNAL_SCAN_CHUNK_BARS,
        help="Optional main-clock chunk size for bounded walk-forward signal scans; 0 keeps continuous scan",
    )
    parser.add_argument(
        "--trend-dir-warmup-bars",
        type=int,
        default=DEFAULT_TREND_DIR_WARMUP_BARS,
        help="Non-trading warmup bars for visible high-level trend direction",
    )
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
        "--regime-source-code",
        default="",
        help="External regime source symbol (e.g. SH.000001): regime classification "
        "uses its closes instead of the equal-weight benchmark; it does not trade",
    )
    parser.add_argument(
        "--trend-core-hold-ratio",
        type=float,
        default=0.0,
        help=(
            "0..1 fraction of an entry to hold as 30m trend core when the visible "
            "big-level direction is up; small-level sells only sell activity shares"
        ),
    )
    parser.add_argument(
        "--trend-core-source",
        choices=("gate", "trend", "bsp"),
        default="gate",
        help="Direction source for trend core: active big gate, visible trend_dir_at, or 30m BSP direction",
    )
    parser.add_argument(
        "--core-signal-level",
        type=int,
        default=0,
        help=(
            "Upgrade signal level that controls core exits; 0 means auto "
            "(1m->30m maps to L2, 5m->30m maps to L1)"
        ),
    )
    parser.add_argument(
        "--swing-signal-level",
        type=int,
        default=0,
        help=(
            "Upgrade signal level for the swing/activity layer; 0 means auto "
            "(1m->5m maps to L1)"
        ),
    )
    parser.add_argument(
        "--big-down-activity-buy-ratio-multiplier",
        type=float,
        default=0.0,
        help=(
            "0..1 multiplier for lower-than-core-level activity buys while the "
            "visible big-level direction is down; 0 keeps the strict old gate"
        ),
    )
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
    parser.add_argument("--output-signals")
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
    args.output_signals = args.output_signals or str(
        Path(args.output_trades).with_name(
            f"{Path(args.output_trades).stem}_signals.csv"
        )
    )
    result, syms = run_backtest(args)
    summary_path, trades_path, signals_path = write_outputs(result, args, syms)
    print(f"summary={summary_path}")
    print(f"trades={trades_path}")
    if signals_path:
        print(f"signals={signals_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
