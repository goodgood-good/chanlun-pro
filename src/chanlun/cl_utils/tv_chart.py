import logging
from typing import Union

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.types import Kline
from chanlun.core.macd import MACD



def cl_data_to_tv_chart(
    frame: pd.DataFrame,
    config: dict,
    *,
    market: str,
    code: str,
    frequency: str,
    strict_runtime,
) -> Union[dict, None]:
    """Serialize bars and structure from one mandatory strict chart runtime."""

    from chanlun.cl_utils.strict_chart import build_strict_structure_snapshot

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("chart frame must be a pandas DataFrame")
    required = {"date", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"chart frame is missing columns: {sorted(missing)}")
    if frame.empty:
        return None
    if strict_runtime is None:
        raise TypeError("strict chart runtime is required")

    klines = frame.copy(deep=False)
    kline_ts = klines["date"].map(fun.datetime_to_int).tolist()
    kline_cs = klines["close"].tolist()
    kline_os = klines["open"].tolist()
    kline_hs = klines["high"].tolist()
    kline_ls = klines["low"].tolist()
    kline_vs = (
        klines["volume"].tolist()
        if "volume" in klines.columns
        else [0.0] * len(klines)
    )

    def _enabled(name: str) -> bool:
        return config.get(name) in ("1", 1, True)

    strict_cd = strict_runtime.cd
    runtime_available = (
        strict_runtime.error_code is None and strict_cd is not None
    )

    fx_data = []
    bi_chart_data = []
    xd_chart_data = []
    if runtime_available:
        if _enabled("chart_show_fx"):
            valid_fxs = {}
            for bi in strict_cd.get_bis():
                for fx in (bi.start, bi.end):
                    if fx is not None:
                        valid_fxs[fun.datetime_to_int(fx.k.date)] = fx
            fx_data = [
                {
                    "points": [
                        {"time": timestamp, "price": fx.val},
                        {"time": timestamp, "price": fx.val},
                    ],
                    "text": fx.type,
                }
                for timestamp, fx in sorted(valid_fxs.items())
            ]

        if _enabled("chart_show_bi"):
            bi_chart_data = [
                {
                    "points": [
                        {
                            "time": fun.datetime_to_int(bi.start.k.date),
                            "price": bi.start.val,
                        },
                        {
                            "time": fun.datetime_to_int(bi.end.k.date),
                            "price": bi.end.val,
                        },
                    ],
                    "linestyle": "0" if bi.is_done() else "1",
                }
                for bi in strict_cd.get_bis()
            ]
            bi_chart_data.sort(key=lambda value: value["points"][0]["time"])

        if _enabled("chart_show_xd"):
            xd_chart_data = [
                {
                    "points": [
                        {
                            "time": fun.datetime_to_int(xd.start.k.date),
                            "price": xd.start.val,
                        },
                        {
                            "time": fun.datetime_to_int(xd.end.k.date),
                            "price": xd.end.val,
                        },
                    ],
                    "linestyle": (
                        "1" if getattr(xd, "forming", False) else "0"
                    ),
                }
                for xd in strict_cd.get_xds()
            ]
            xd_chart_data.sort(key=lambda value: value["points"][0]["time"])

    if runtime_available:
        macd_idx = strict_cd.get_idx()["macd"]
        if any(
            len(macd_idx.get(name, ())) != len(klines)
            for name in ("dif", "dea", "hist")
        ):
            raise ValueError(
                "strict chart MACD is not aligned with displayed bars"
            )
        strict_htf = getattr(strict_cd, "_strict_htf_macd_by_level", {}).get(
            0,
            {},
        )
        if strict_htf and any(
            len(strict_htf.get(name, ())) != len(klines)
            for name in ("dif", "dea", "hist")
        ):
            raise ValueError(
                "strict chart higher-timeframe MACD is not aligned with displayed bars"
            )
    else:
        # A structure failure never resurrects another recognition engine.
        # Bars remain visible with the fixed production MACD parameters only.
        macd = MACD()
        macd.process_macd(
            [
                Kline(
                    index=index,
                    date=row["date"],
                    h=float(row["high"]),
                    l=float(row["low"]),
                    o=float(row["open"]),
                    c=float(row["close"]),
                    a=float(row.get("volume") or 0.0),
                )
                for index, row in enumerate(klines.to_dict("records"))
            ]
        )
        macd_idx = macd.get_results()["macd"]
        strict_htf = {}

    result = {
        "t": kline_ts,
        "c": kline_cs,
        "o": kline_os,
        "h": kline_hs,
        "l": kline_ls,
        "v": kline_vs,
        "macd_dif": np.round(macd_idx["dif"], 6).tolist(),
        "macd_dea": np.round(macd_idx["dea"], 6).tolist(),
        "macd_hist": np.round(macd_idx["hist"], 6).tolist(),
        "macd_area": np.round(macd_idx.get("hist_area", []), 6).tolist(),
        "higher_macd_dif": np.round(strict_htf.get("dif", []), 6).tolist(),
        "higher_macd_dea": np.round(strict_htf.get("dea", []), 6).tolist(),
        "higher_macd_hist": np.round(strict_htf.get("hist", []), 6).tolist(),
        "fxs": fx_data,
        "bis": bi_chart_data,
        "xds": xd_chart_data,
    }

    if not runtime_available:
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {
            "code": strict_runtime.error_code or "strict_evidence_invalid"
        }
        return result

    error_code = "strict_evidence_invalid"
    try:
        evidence = strict_cd.get_strict_evidence()
        strict_structure = build_strict_structure_snapshot(
            evidence,
            interval=frequency,
        )
        if (
            strict_structure["symbol"] != code
            or strict_structure["source_frequency"] != frequency
            or strict_structure["display_frequency"] != frequency
            or strict_structure["source_closed_at"] != kline_ts[-1]
        ):
            error_code = "strict_context_mismatch"
            raise ValueError(
                "strict snapshot context does not match displayed bars"
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "strict chart structure unavailable "
            "market=%s code=%s frequency=%s error_code=%s error=%s: %s",
            market,
            code,
            frequency,
            error_code,
            type(exc).__name__,
            exc,
        )
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {"code": error_code}
    else:
        result["strict_structure_mode"] = "replace"
        result["strict_structure"] = strict_structure

    return result
