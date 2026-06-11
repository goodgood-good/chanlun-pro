"""Market helpers for recursive live monitoring and backtesting."""
from __future__ import annotations

import glob
import json
import os
import pickle
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from chanlun import config as app_config
from chanlun.recursive_bt.engine import A_BJ, A_GEM, A_STOCK, US_STOCK, MarketRules

CHART_CACHE_DIR = "D:/chanlun_pro/chart_cache"
_MONITOR_CONFIG = getattr(app_config, "RECURSIVE_MONITOR_CONFIG", {})
BT_DATA_DIR = (
    (_MONITOR_CONFIG.get("a") or {}).get("bt_data")
    if isinstance(_MONITOR_CONFIG, dict)
    else None
) or "D:/chanlun_pro/bt_data_all_a"
INDEX_BY_MARKET = {"a": "SH.000001", "us": ""}


def normalize_market(market: str) -> str:
    value = (market or "a").lower()
    if value not in {"a", "us"}:
        raise ValueError(f"unsupported market: {market!r}")
    return value


def normalize_code(market: str, code: str) -> str:
    market = normalize_market(market)
    code = (code or "").strip().upper()
    if market == "us":
        return code if code.endswith(".US") else f"{code}.US"
    return code


def classify_visible_regime(daily_closes: Sequence[float], lookback_days: int = 20) -> str:
    """日线行情状态分类:bull/range/bear,与回测 `_regime_by_date_lookup` 同规则。

    输入必须是**截至前一交易日收盘**的日线收盘序列(实盘调用方负责丢掉当日
    未完成日线);数据不足或异常一律返回 range(=不调整买入比例)。"""
    closes = pd.Series([float(c) for c in daily_closes if c is not None]).dropna()
    if len(closes) < 2:
        return "range"
    rolling = float(closes.pct_change(lookback_days).fillna(0.0).iloc[-1])
    drawdown = float((closes / closes.cummax() - 1.0).iloc[-1])
    if rolling <= -0.05 or drawdown <= -0.10:
        return "bear"
    if rolling >= 0.05 and drawdown > -0.05:
        return "bull"
    return "range"


def parse_regime_ratio_multipliers(raw) -> dict:
    """解析按行情的买点比例乘数:dict、内联 JSON 字符串或 JSON 文件路径。
    形如 {"bear": {"3": 1.25}};非法行情键与非数值乘数被丢弃。"""
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            if text.startswith("{"):
                data = json.loads(text)
            else:
                with open(text, "r", encoding="utf-8") as fp:
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


def code_to_chart_prefix(market: str, code: str) -> str:
    market = normalize_market(market)
    code = normalize_code(market, code)
    if market == "us":
        return f"us_{code[:-3].replace('.', '_')}_US"
    return f"a_{code.replace('.', '_')}"


def chart_prefix_to_code(market: str, prefix: str) -> str:
    market = normalize_market(market)
    if market == "us":
        body = prefix[3:]
        if body.endswith("_US"):
            body = body[:-3]
        return f"{body.replace('_', '.')}.US"
    body = prefix[2:] if prefix.startswith("a_") else prefix
    parts = body.split("_", 1)
    return f"{parts[0]}.{parts[1]}" if len(parts) == 2 else body


def market_rules_for_code(market: str, code: str) -> MarketRules:
    market = normalize_market(market)
    if market == "us":
        return US_STOCK
    norm = normalize_code(market, code)
    if norm.startswith("BJ."):
        return A_BJ
    num = norm.split(".")[-1]
    if num.startswith("8") or num.startswith("920"):
        return A_BJ
    if num[:3] in ("688", "300", "301"):
        return A_GEM
    return A_STOCK


def ashare_board(code: str) -> str:
    norm = normalize_code("a", code)
    num = norm.split(".")[-1]
    if norm.startswith("BJ.") or num.startswith("8") or num.startswith("920"):
        return "bj"
    if num.startswith("688"):
        return "star"
    if num.startswith(("300", "301")):
        return "gem"
    return "main"


def default_ledger_path(market: str) -> str:
    market = normalize_market(market)
    suffix = "" if market == "a" else f"_{market}"
    return f"D:/chanlun_pro/paper{suffix}/ledger.json"


def default_monitor_state_path(market: str) -> str:
    market = normalize_market(market)
    return f"D:/chanlun_pro/monitor/recursive_live_monitor_state_{market}.json"


def default_backtest_report_paths(market: str) -> tuple[str, str]:
    market = normalize_market(market)
    base = f"D:/chanlun_pro/reports/{market}_live_parity_backtest"
    return f"{base}_summary.json", f"{base}_trades.csv"


def list_chart_cache_codes(
    market: str,
    cache_dir: os.PathLike | str = CHART_CACHE_DIR,
    frequency: str = "5m",
) -> list[str]:
    market = normalize_market(market)
    cache_dir = str(cache_dir)
    pattern = f"{cache_dir}/v*_{market}_*_{frequency}_*.pkl"
    prefixes = set()
    rx = re.compile(rf"^v\d+_({market}_.+)_{re.escape(frequency)}_[^.]+\.pkl$")
    for file in glob.glob(pattern):
        m = rx.match(os.path.basename(file))
        if m:
            prefixes.add(m.group(1))
    return sorted(chart_prefix_to_code(market, prefix) for prefix in prefixes)


def _cache_version(path: str) -> int:
    m = re.search(r"[\\/]v(\d+)_", path)
    return int(m.group(1)) if m else 0


def latest_chart_cache_file(
    market: str,
    code: str,
    frequency: str,
    cache_dir: os.PathLike | str = CHART_CACHE_DIR,
) -> Optional[str]:
    prefix = code_to_chart_prefix(market, code)
    files = glob.glob(f"{cache_dir}/v*_{prefix}_{frequency}_*.pkl")
    if not files:
        return None
    return max(files, key=lambda p: (_cache_version(p), os.path.getmtime(p)))


def load_chart_cache_klines(
    market: str,
    code: str,
    frequency: str,
    cache_dir: os.PathLike | str = CHART_CACHE_DIR,
) -> Optional[pd.DataFrame]:
    path = latest_chart_cache_file(market, code, frequency, cache_dir)
    if path is None:
        return None
    data = pickle.load(open(path, "rb")).get("data", {})
    required = ("t", "o", "h", "l", "c", "v")
    if not all(k in data for k in required):
        return None
    return pd.DataFrame(
        {
            "date": pd.to_datetime(data["t"], unit="s", utc=True),
            "open": np.asarray(data["o"], float),
            "high": np.asarray(data["h"], float),
            "low": np.asarray(data["l"], float),
            "close": np.asarray(data["c"], float),
            "volume": np.asarray(data["v"], float),
        }
    )


def parse_codes(market: str, value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    p = Path(value)
    if p.exists():
        raw = p.read_text(encoding="utf-8").splitlines()
    else:
        raw = value.replace(";", ",").split(",")
    codes = [normalize_code(market, item) for item in raw if item.strip()]
    return list(dict.fromkeys(codes))


def merge_codes(base: Iterable[str], extra: Iterable[str], market: str) -> list[str]:
    out = [normalize_code(market, code) for code in base if code]
    for code in extra:
        norm = normalize_code(market, code)
        if norm not in out:
            out.append(norm)
    return out
