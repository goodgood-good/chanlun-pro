"""One replaceable verified calculation per symbol/period, outside chart caches."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import zlib

import pandas as pd


REPLAY_CHECKS = ("cold_rebuild", "confirmation_replay", "confirmation_boundary")


def input_fingerprint(frame, code, frequency):
    digest = hashlib.sha256()
    digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    # Check every candle (including volume), not only length and last close.
    # Include the price basis. Read mode and diagnostic timestamps do not alter
    # the input. Eligibility windows are checked again on every scan.
    payload = {"code": code, "frequency": frequency,
               "columns": list(frame.columns), "dtypes": [str(t) for t in frame.dtypes],
               "attrs": {k: v for k, v in frame.attrs.items()
                         if not k.startswith("_screening_") and k not in {"qmt_history_read_mode", "screening_suspensions"}}}
    digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str, allow_nan=False).encode())
    return digest.hexdigest()


def calculation_key(frame, code, frequency, revision):
    return hashlib.sha256(f"{input_fingerprint(frame, code, frequency)}:{revision}".encode()).hexdigest()


def candidate_signature(candidate):
    proof = {k: candidate.get(k) for k in ("point", "center")}
    return hashlib.sha256(json.dumps(proof, sort_keys=True, allow_nan=False).encode()).hexdigest()


def read_calculation(path: Path, key: str):
    try:
        wrapper = json.loads(gzip.decompress(path.read_bytes()))
        raw = wrapper["payload"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != wrapper["sha256"]:
            return None
        value = json.loads(raw)
        if value.get("schema") != "screening-structure-v2" or value["key"] != key:
            return None
        if not isinstance(value["snapshot"], dict) or not isinstance(value["snapshot"]["levels"], list):
            return None
        for checks in value["reviewed"].values():
            if not all(checks.get(k) is True for k in REPLAY_CHECKS):
                return None
        return value
    except (OSError, EOFError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, zlib.error):
        return None


def write_calculation(path: Path, key: str, row: dict, snapshot, reviewed):
    # Unresolved data or structural replay failures must get another cold check.
    if row.get("data_errors") or any(row["reason_counts"].get(reason) for reason in (
        "DATA_GAPS", "DEPENDENCY_MISSING", "CALENDAR_COVERAGE_UNKNOWN",
        "CONFIRMATION_REPLAY_FAILED", "CONFIRMATION_TIME_MISMATCH", "REBUILD_MISMATCH",
    )):
        return
    raw = json.dumps({"schema": "screening-structure-v2", "key": key,
                     "snapshot": snapshot, "reviewed": reviewed},
                     ensure_ascii=False, allow_nan=False)
    wrapper = {"payload": raw, "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temp.write_bytes(gzip.compress(json.dumps(wrapper, ensure_ascii=False).encode("utf-8")))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
