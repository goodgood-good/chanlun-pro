"""Bind a fresh, complete closed-minute query to its exact returned frame."""

import hashlib
import time

import pandas as pd


def _signature(frame):
    columns = ["date", "code", "open", "high", "low", "close", "volume"]
    source = frame.loc[:, columns]
    digest = hashlib.sha256(pd.util.hash_pandas_object(source, index=False).values.tobytes())
    digest.update(repr(tuple((name, str(source[name].dtype)) for name in columns)).encode())
    digest.update(repr(tuple((key, frame.attrs.get(key)) for key in (
        "structure_price_quantum", "price_basis_revision", "price_basis_provider",
        "price_basis_adjustment", "bar_time_label",
    ))).encode())
    return digest.hexdigest()


def seal_minute_source(frame, *, owner, code, args):
    if (frame is None or frame.empty or frame.attrs.get("fetch_incomplete")
            or frame.attrs.get("bar_time_label") != "end"):
        return frame
    cutoff = frame.attrs.get("canonical_minute_cutoff")
    if cutoff is None or pd.Timestamp(cutoff) != frame.iloc[-1]["date"]:
        return frame
    frame.attrs["_closed_minute_receipt"] = dict(
        owner=owner, code=code, args=repr(sorted(args.items())),
        loaded_at=time.monotonic(), signature=_signature(frame),
    )
    return frame


def matches_minute_source(frame, *, owner, code, args):
    receipt = frame.attrs.get("_closed_minute_receipt")
    if not isinstance(receipt, dict) or frame.empty or frame.attrs.get("fetch_incomplete"):
        return False
    try:
        return (
            receipt["owner"] == owner and receipt["code"] == code
            and receipt["args"] == repr(sorted(args.items()))
            and 0 <= time.monotonic() - receipt["loaded_at"] < 30.0
            and frame.attrs.get("bar_time_label") == "end"
            and pd.Timestamp(frame.attrs["canonical_minute_cutoff"]) == frame.iloc[-1]["date"]
            and receipt["signature"] == _signature(frame)
        )
    except (KeyError, TypeError, ValueError):
        return False
