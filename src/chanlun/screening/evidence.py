"""Read the saved input and geometry together, without running analysis."""
from __future__ import annotations

from functools import lru_cache
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import zlib

import pandas as pd
from chanlun.cl_utils.point_exits import build_point_exit_plans

from .cache import input_fingerprint
from .rules import FREQUENCIES
from .confirmation import confirmation_catalog


def evidence_paths(directory, code, frequency, market="a"):
    from .markets import storage_symbol
    if frequency not in FREQUENCIES:
        raise ValueError("选股证据的标的或周期无效")
    stem = Path(directory) / "evidence" / f"{storage_symbol(code, market)}_{frequency}"
    return Path(str(stem) + ".parquet"), Path(str(stem) + ".json.gz")


@lru_cache(maxsize=512)
def _file_digest(path, size, mtime_ns, ctime_ns):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence_health(directory, code, frequency, manifest):
    verified = True
    try:
        for path, name in zip(evidence_paths(directory, code, frequency, manifest.get("market", "a")), ("parquet", "snapshot")):
            stat = path.stat()
            expected_size = manifest.get(name + "_bytes")
            if stat.st_size <= 0 or (expected_size is not None and stat.st_size != expected_size):
                return {"complete": False, "verified": False}
            expected_digest = manifest.get(name + "_sha256")
            if expected_digest is None:
                verified = False  # Legacy results had only file sizes.
                if name == "snapshot":
                    symbol, interval, _ = _selection_catalog(
                        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, None,
                    )
                    if symbol != code or interval != frequency:
                        return {"complete": False, "verified": False}
            elif expected_digest != _file_digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                return {"complete": False, "verified": False}
    except (OSError, ValueError):
        return {"complete": False, "verified": False}
    return {"complete": True, "verified": verified}


def evidence_version(directory):
    """Cheap polling token; file damage/removal must refresh an idle workbench."""
    paths = [Path(directory) / "results.jsonl", *sorted((Path(directory) / "evidence").glob("*"))]
    states = []
    for path in paths:
        try:
            stat = path.stat()
            states.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            states.append((path.name, None))
    return hashlib.sha256(json.dumps(states).encode()).hexdigest()


def _decode_snapshot(raw):
    """Normalize damaged or unsupported saved structures to a readable error."""
    try:
        snapshot = json.loads(gzip.decompress(raw))
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise ValueError("本次筛选的结构证据损坏，请重新进行小范围验证") from exc
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("symbol"), str)
            or not isinstance(snapshot.get("source_frequency"), str)
            or not isinstance(snapshot.get("levels"), list)):
        raise ValueError("本次筛选的结构证据格式无效")
    for level in snapshot["levels"]:
        if not isinstance(level, dict):
            raise ValueError("本次筛选的结构层级格式无效")
        for collection, identity in (("points", "point_id"), ("centers", "center_id")):
            items = level.get(collection, [])
            if (not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get(identity), str) or not item[identity]
                for item in items
            )):
                raise ValueError("本次筛选的买点或中枢证据格式无效")
    return snapshot


@lru_cache(maxsize=512)
def _selection_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    # Keep only per-point selection lifetime, not the full historical geometry.
    raw = Path(path).read_bytes()
    if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("选股结构证据校验失败")
    snapshot = _decode_snapshot(raw)
    return snapshot["symbol"], snapshot["source_frequency"], confirmation_catalog(snapshot)


def evidence_confirmation_catalog(directory, code, frequency, manifest):
    """Recheck saved candidates with current lifetime rules, without scanning."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"))
    stat = path.stat()
    symbol, interval, catalog = _selection_catalog(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, manifest.get("snapshot_sha256"),
    )
    if symbol != code or interval != frequency:
        raise ValueError("选股结构证据的标的或周期不匹配")
    return catalog


@lru_cache(maxsize=128)
def _semantic_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    raw = Path(path).read_bytes()
    if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("选股结构证据校验失败")
    snapshot = _decode_snapshot(raw)
    return (snapshot.get("symbol"), snapshot.get("source_frequency"),
            {p["point_id"]: p for level in snapshot["levels"] for p in level.get("points", ())},
            {c["center_id"]: c for level in snapshot["levels"] for c in level.get("centers", ())})


def evidence_semantic_catalog(directory, code, frequency, manifest):
    """Read only saved point lineage, cached by file version; never recalculate bars."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"))
    stat = path.stat()
    symbol, interval, points, centers = _semantic_catalog(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        manifest.get("snapshot_sha256"),
    )
    if symbol != code or interval != frequency:
        raise ValueError("选股结构证据的标的或周期不匹配")
    return points, centers


@lru_cache(maxsize=64)
def _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest):
    raw = Path(path).read_bytes()
    if not expected_digest or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("区间套结构证据校验失败")
    snapshot = _decode_snapshot(raw)
    return {k: snapshot[k] for k in ("symbol", "source_frequency", "source_closed_at", "source_started_at",
                                   "price_basis_revision", "structure_price_quantum", "levels",
                                   "screening_segments", "screening_interval_signals", "segment_construction",
                                   "unresolved_segment_ranges") if k in snapshot}


def evidence_nested_snapshots(directory, code, main_manifest, lower_manifest):
    snapshots = []
    for frequency, manifest in (("5m", main_manifest), ("1m", lower_manifest)):
        health = evidence_health(directory, code, frequency, manifest)
        if not health["complete"] or not health["verified"]:
            raise ValueError("区间套需要完整且经过校验的 5m 与 1m 证据")
        _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"))
        stat = path.stat()
        snapshot = _nested_snapshot(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                    manifest.get("snapshot_sha256"))
        if snapshot["symbol"] != code or snapshot["source_frequency"] != frequency:
            raise ValueError("区间套证据的标的或周期不一致")
        snapshots.append(snapshot)
    return snapshots


@lru_cache(maxsize=128)
def _exit_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    snapshot = _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest)
    return snapshot["symbol"], snapshot["source_frequency"], build_point_exit_plans(snapshot)


def evidence_exit_catalog(directory, code, frequency, manifest):
    """Derive display references from verified saved structure, without a scan."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"))
    stat = path.stat()
    symbol, interval, plans = _exit_catalog(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                            manifest.get("snapshot_sha256"))
    if symbol != code or interval != frequency:
        raise ValueError("退出价格依据的标的或周期不匹配")
    return plans


def load_evidence(directory, code, frequency, manifest):
    health = evidence_health(directory, code, frequency, manifest)
    if not health["complete"]:
        raise ValueError("本次筛选证据缺失或校验失败，请重新进行小范围验证")
    parquet, packed = evidence_paths(directory, code, frequency, manifest.get("market", "a"))
    raw_frame, raw_snapshot = parquet.read_bytes(), packed.read_bytes()
    # Hash the actual bytes consumed by this request. The cached stat/hash is
    # only a polling optimization, not authority across concurrent file writes.
    for raw, name in ((raw_frame, "parquet"), (raw_snapshot, "snapshot")):
        expected = manifest.get(name + "_sha256")
        if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("本次筛选证据在读取时发生变化，请刷新后重试")
    frame = pd.read_parquet(BytesIO(raw_frame))
    snapshot = _decode_snapshot(raw_snapshot)
    fingerprint = input_fingerprint(frame, code, frequency)
    if (frame.empty or snapshot["symbol"] != code or snapshot["source_frequency"] != frequency
            or int(frame.date.iloc[-1].timestamp()) != snapshot["source_closed_at"]
            or ("code" in frame and set(frame.code) != {code})
            or (manifest.get("input_fingerprint") is not None and manifest["input_fingerprint"] != fingerprint)):
        raise ValueError("本次筛选的行情与结构证据不一致")
    return frame, snapshot, {**health, "input_fingerprint": fingerprint}
