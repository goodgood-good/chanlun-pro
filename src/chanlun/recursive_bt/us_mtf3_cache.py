"""Build US core MTF3 chart-cache files for recursive backtests."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pickle
import time
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from chanlun.base import Market
from chanlun.exchange import get_exchange
from chanlun.recursive_bt.market_runtime import (
    CHART_CACHE_DIR,
    code_to_chart_prefix,
    normalize_code,
)

US_CORE9 = (
    "SPY.US",
    "QQQ.US",
    "AAPL.US",
    "MSFT.US",
    "NVDA.US",
    "AMZN.US",
    "META.US",
    "GOOGL.US",
    "TSLA.US",
)
MTF3_FREQS = ("1m", "5m", "30m")
DEFAULT_VERSION = "v33"
DEFAULT_CACHE_TAG = "recursivebt"


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _entry_from_klines(df: pd.DataFrame) -> dict:
    df = df.sort_values("date").reset_index(drop=True)
    ts = (pd.to_datetime(df["date"], utc=True).astype("int64") // 1_000_000_000)
    data = {
        "t": ts.astype(int).tolist(),
        "o": df["open"].astype(float).tolist(),
        "h": df["high"].astype(float).tolist(),
        "l": df["low"].astype(float).tolist(),
        "c": df["close"].astype(float).tolist(),
        "v": df.get("volume", pd.Series([0] * len(df))).astype(float).tolist(),
    }
    return {
        "data": data,
        "min_time": data["t"][0] if data["t"] else None,
        "max_time": data["t"][-1] if data["t"] else None,
        "validated_at": time.time(),
        "is_full_snapshot": True,
        "cache_source": "recursive_bt_us_mtf3",
    }


def _cache_path(
    out_dir: str | Path,
    code: str,
    frequency: str,
    *,
    version: str = DEFAULT_VERSION,
    cache_tag: str = DEFAULT_CACHE_TAG,
) -> Path:
    prefix = code_to_chart_prefix("us", code)
    safe_tag = str(cache_tag or DEFAULT_CACHE_TAG).replace("/", "_").replace(".", "_")
    return Path(out_dir) / f"{version}_{prefix}_{frequency}_{safe_tag}.pkl"


def _write_manifest(path: str | Path, manifest: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def build_us_mtf3_chart_cache(
    codes: Iterable[str] = US_CORE9,
    *,
    out_dir: str | Path = CHART_CACHE_DIR,
    start_date: str | None = None,
    end_date: str | None = None,
    frequencies: Iterable[str] = MTF3_FREQS,
    min_bars: int = 100,
    version: str = DEFAULT_VERSION,
    cache_tag: str = DEFAULT_CACHE_TAG,
    manifest_path: str | Path | None = None,
    exchange=None,
) -> dict:
    codes = [normalize_code("us", code) for code in codes]
    frequencies = tuple(frequencies)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = Path(manifest_path or out_dir / "_us_mtf3_build_manifest.json")
    manifest = {
        "version": 1,
        "label": "us_core9_mtf3_chart_cache",
        "started_at": _now_iso(),
        "completed_at": None,
        "out_dir": str(out_dir),
        "codes": list(codes),
        "frequencies": list(frequencies),
        "start_date": start_date,
        "end_date": end_date,
        "min_bars": int(min_bars),
        "cache_version": version,
        "cache_tag": cache_tag,
        "counts": {"ok": 0, "skip": 0, "fail": 0},
        "entries": [],
    }
    _write_manifest(manifest_file, manifest)
    ex = exchange or get_exchange(Market("us"))
    ok = skip = fail = 0
    t0 = time.time()
    for code in codes:
        for frequency in frequencies:
            path = _cache_path(out_dir, code, frequency, version=version, cache_tag=cache_tag)
            try:
                df = ex.klines(
                    code,
                    frequency,
                    start_date=start_date,
                    end_date=end_date,
                    args={"allow_long_history": True},
                )
                if df is None or len(df) < min_bars:
                    fail += 1
                    manifest["entries"].append(
                        {
                            "code": code,
                            "frequency": frequency,
                            "status": "fail",
                            "reason": "insufficient_data",
                            "bars": 0 if df is None else int(len(df)),
                            "path": str(path),
                            "time": _now_iso(),
                        }
                    )
                    continue
                entry = _entry_from_klines(df)
                with path.open("wb") as fp:
                    pickle.dump(entry, fp)
                ok += 1
                manifest["entries"].append(
                    {
                        "code": code,
                        "frequency": frequency,
                        "status": "ok",
                        "bars": int(len(df)),
                        "path": str(path),
                        "min_time": entry["min_time"],
                        "max_time": entry["max_time"],
                        "time": _now_iso(),
                    }
                )
            except Exception as exc:
                fail += 1
                manifest["entries"].append(
                    {
                        "code": code,
                        "frequency": frequency,
                        "status": "fail",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "path": str(path),
                        "time": _now_iso(),
                    }
                )
            manifest["counts"] = {"ok": ok, "skip": skip, "fail": fail}
            _write_manifest(manifest_file, manifest)
    manifest["completed_at"] = _now_iso()
    manifest["elapsed_seconds"] = round(time.time() - t0, 3)
    manifest["counts"] = {"ok": ok, "skip": skip, "fail": fail}
    _write_manifest(manifest_file, manifest)
    return manifest


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build US core-9 MTF3 chart cache")
    parser.add_argument("--codes", default=",".join(US_CORE9))
    parser.add_argument("--out-dir", default=CHART_CACHE_DIR)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--min-bars", type=int, default=100)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--cache-tag", default=DEFAULT_CACHE_TAG)
    parser.add_argument("--manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    codes = [item.strip() for item in args.codes.replace(";", ",").split(",") if item.strip()]
    manifest = build_us_mtf3_chart_cache(
        codes,
        out_dir=args.out_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        min_bars=args.min_bars,
        version=args.version,
        cache_tag=args.cache_tag,
        manifest_path=args.manifest,
    )
    print(
        "完成 US MTF3 chart_cache "
        f"ok={manifest['counts']['ok']} fail={manifest['counts']['fail']} "
        f"manifest={args.manifest or str(Path(args.out_dir) / '_us_mtf3_build_manifest.json')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
