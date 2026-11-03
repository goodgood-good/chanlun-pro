"""Read the saved input and geometry together, without running analysis."""
from __future__ import annotations

from functools import lru_cache
from collections import OrderedDict
import gzip
import hashlib
from io import BytesIO
import json
import os
import pickle
from pathlib import Path
import threading
import zlib

try:
    from msgspec import DecodeError as _FastDecodeError
    from msgspec.json import decode as _fast_json_decode
except ImportError:
    _fast_json_decode = None
    _fast_decode_errors = ()
else:
    _fast_decode_errors = (_FastDecodeError, UnicodeError, RecursionError)

import pandas as pd
from chanlun.cl_utils.point_exits import build_point_exit_plans

from .cache import input_fingerprint
from .rules import FREQUENCIES, _native_center_signal_source_valid, native_consolidation_source_valid
from .confirmation import confirmation_catalog


def evidence_paths(directory, code, frequency, market="a", namespace=None):
    from .markets import storage_symbol
    if frequency not in FREQUENCIES:
        raise ValueError("选股证据的标的或周期无效")
    if namespace not in (None, 'trade_5m'):
        raise ValueError('选股证据用途无效')
    suffix = '' if namespace is None else '.' + namespace
    stem = Path(directory) / "evidence" / f"{storage_symbol(code, market)}_{frequency}{suffix}"
    return Path(str(stem) + ".parquet"), Path(str(stem) + ".json.gz")


@lru_cache(maxsize=512)
def _file_digest(path, size, mtime_ns, ctime_ns):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence_health(directory, code, frequency, manifest):
    verified = True
    try:
        for path, name in zip(evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace")), ("parquet", "snapshot")):
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
    directory = Path(directory)
    try:
        # On Windows, DirEntry retains size/time metadata returned by the
        # directory enumeration, avoiding a separate stat call per file.
        with os.scandir(directory / 'evidence') as stream:
            entries = sorted(stream, key=lambda entry: Path(entry.name))
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    paths = [directory / 'results.jsonl', *entries]
    states = []
    for path in paths:
        try:
            stat = path.stat()
            states.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            states.append((path.name, None))
    return hashlib.sha256(json.dumps(states).encode()).hexdigest()


class _SnapshotDecodeCache:
    """Bounded, process-local acceleration of already validated JSON bytes.

    Only pickle streams produced here from parsed JSON are retained. No pickle
    is accepted from files or callers, and no mutable decoded object is shared.
    Content keys deliberately do not trust filenames, timestamps or manifests.
    """

    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_entries=64):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def decode(self, raw, parse):
        key = hashlib.sha256(raw).digest()
        with self._lock:
            packed = self._entries.get(key)
            if packed is not None:
                self._entries.move_to_end(key)
        if packed is not None:
            return pickle.loads(packed)
        snapshot = parse(raw)
        try:
            packed = pickle.dumps(snapshot, protocol=5)
        except (pickle.PickleError, RecursionError, OverflowError, MemoryError):
            # This optimization must not reject JSON accepted by the reader.
            return snapshot
        if len(packed) <= self.max_bytes and self.max_entries > 0:
            with self._lock:
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._bytes -= len(previous)
                while self._entries and (self._bytes + len(packed) > self.max_bytes
                                         or len(self._entries) >= self.max_entries):
                    _, evicted = self._entries.popitem(last=False)
                    self._bytes -= len(evicted)
                self._entries[key] = packed
                self._bytes += len(packed)
        return snapshot

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0


_SNAPSHOT_DECODE_CACHE = _SnapshotDecodeCache()


def _decode_snapshot(raw):
    return _SNAPSHOT_DECODE_CACHE.decode(raw, _parse_snapshot)


def _parse_snapshot(raw):
    """Normalize damaged or unsupported saved structures to a readable error."""
    try:
        snapshot = _snapshot_json(gzip.decompress(raw))
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise ValueError("本次筛选的结构证据损坏，请重新进行小范围验证") from exc
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("symbol"), str)
            or not isinstance(snapshot.get("source_frequency"), str)
            or not isinstance(snapshot.get("levels"), list)):
        raise ValueError("本次筛选的结构证据格式无效")
    for level in snapshot["levels"]:
        if not isinstance(level, dict):
            raise ValueError("本次筛选的结构层级格式无效")
        for collection, identity in (("points", "point_id"), ("centers", "center_id"),
                                     ("third_point_entry_precision_rule_states", "point_id")):
            items = level.get(collection, [])
            if (not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get(identity), str) or not item[identity]
                for item in items
            )):
                raise ValueError("本次筛选的买点或中枢证据格式无效")
    return snapshot


def _snapshot_json(raw):
    """Decode into ordinary Python values; retain legacy JSON compatibility.

    Untyped decoding keeps dates/IDs as strings and arbitrary-size integers as
    integers. Nonstandard legacy literals or encodings use the original parser.
    Hash, structure, identity, clock and input checks remain in their callers.
    """
    if _fast_json_decode is not None:
        try:
            return _fast_json_decode(raw)
        except _fast_decode_errors:
            pass
    return json.loads(raw)


@lru_cache(maxsize=4)
def _catalog_snapshot(path, size, mtime_ns, ctime_ns, expected_digest):
    """Share read-only decoding between catalogs of the same immutable bytes."""
    raw = Path(path).read_bytes()
    if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("选股结构证据校验失败")
    return _decode_snapshot(raw)


@lru_cache(maxsize=512)
def _selection_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    # Keep only per-point selection lifetime, not the full historical geometry.
    snapshot = _catalog_snapshot(path,size,mtime_ns,ctime_ns,expected_digest)
    catalog = {
        point_id: record
        for level in snapshot["levels"]
        for point_id, record in confirmation_catalog(
            snapshot, level.get("structural_level", 0)
        ).items()
    }
    return snapshot["symbol"], snapshot["source_frequency"], catalog


def evidence_confirmation_catalog(directory, code, frequency, manifest):
    """Recheck saved candidates with current lifetime rules, without scanning."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
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
            {c["center_id"]: c for level in snapshot["levels"] for c in level.get("centers", ())},
            {state["point_id"]: state for level in snapshot["levels"]
             for state in level.get("third_point_entry_precision_rule_states", ())})


def evidence_semantic_catalog(directory, code, frequency, manifest):
    """Read saved point, center, and entry evidence; never recalculate bars."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
    stat = path.stat()
    symbol, interval, points, centers, precision = _semantic_catalog(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        manifest.get("snapshot_sha256"),
    )
    if symbol != code or interval != frequency:
        raise ValueError("选股结构证据的标的或周期不匹配")
    return points, centers, precision


@lru_cache(maxsize=64)
def _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest):
    if not expected_digest:
        raise ValueError("区间内结构确认证据校验失败")
    snapshot = _catalog_snapshot(path,size,mtime_ns,ctime_ns,expected_digest)
    return {k: snapshot[k] for k in ("symbol", "source_frequency", "source_closed_at", "source_started_at",
                                    "analysis_scope", "native_member_policy", "center_view_selections",
                                   "price_basis_revision", "structure_price_quantum", "levels",
                                   "screening_segments", "screening_units_by_level", "native_consolidation_alerts", 'native_consolidation_error',
                                   "screening_interval_signals", "screening_interval_divergences", "segment_construction",
                                    "unresolved_segment_ranges", "macd_zero_axis_operational_state", "history_repair") if k in snapshot}


@lru_cache(maxsize=128)
def _native_signal_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    snapshot = _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest)
    units = snapshot.get("screening_segments")
    if snapshot.get("analysis_scope") != "native_centers" or not isinstance(units, list):
        raise ValueError("本周期买卖点缺少完整线段来源证据")
    points = {point["point_id"]: point for level in snapshot["levels"]
              for point in level.get("points", ())}
    centers = {center["center_id"]: center for level in snapshot["levels"]
               for center in level.get("centers", ())}
    valid = {point_id: _native_center_signal_source_valid(snapshot, point, centers, units)
             for point_id, point in points.items()}
    return (snapshot["symbol"], snapshot["source_frequency"], snapshot["source_closed_at"],
            points, centers, valid)


def evidence_native_signal_catalog(directory, code, frequency, manifest, *, source_closed_at):
    """Check frozen local signal clocks and source chains without recalculation."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
    stat = path.stat()
    symbol, interval, cutoff, points, centers, valid = _native_signal_catalog(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        manifest.get("snapshot_sha256"),
    )
    if symbol != code or interval != frequency:
        raise ValueError("本周期买卖点来源证据的标的或周期不匹配")
    if type(source_closed_at) is not int or cutoff != source_closed_at:
        raise ValueError("本周期买卖点记录与保存图表的行情截止时刻不匹配")
    return points, centers, valid


@lru_cache(maxsize=128)
def _consolidation_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    snapshot = _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest)
    if snapshot.get("analysis_scope") != "native_centers":
        raise ValueError("盘整背驰来源范围错误")
    alerts = snapshot.get("native_consolidation_alerts", [])
    catalog = {alert["alert_id"]: alert for alert in alerts}
    if len(catalog) != len(alerts):
        raise ValueError("盘整背驰身份重复")
    valid = {identity: native_consolidation_source_valid(snapshot, alert) for identity, alert in catalog.items()}
    return snapshot["symbol"], snapshot["source_frequency"], snapshot["source_closed_at"], catalog, valid


def evidence_consolidation_catalog(directory, code, frequency, manifest, *, source_closed_at):
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
    stat = path.stat()
    symbol, interval, cutoff, catalog, valid = _consolidation_catalog(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, manifest.get("snapshot_sha256"))
    if symbol != code or interval != frequency or type(source_closed_at) is not int or source_closed_at != cutoff:
        raise ValueError("盘整背驰的标的、周期或截止时间不匹配")
    return catalog, valid


def evidence_consolidation_lifetimes(directory, code, frequency, manifest, *, source_closed_at):
    from .confirmation import consolidation_lifetime_reasons
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
    stat = path.stat()
    snapshot = _nested_snapshot(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                manifest.get("snapshot_sha256"))
    if (snapshot.get("symbol") != code or snapshot.get("source_frequency") != frequency
            or snapshot.get("source_closed_at") != source_closed_at):
        raise ValueError("盘整背驰时效证据不匹配")
    return {alert["alert_id"]: consolidation_lifetime_reasons(snapshot, alert)
            for alert in snapshot.get("native_consolidation_alerts", [])}


def evidence_operational_macd_risk(directory, code, frequency, manifest):
    """Read a verified frozen U176 fact, including its physical-period cutoff."""

    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
    stat = path.stat()
    snapshot = _nested_snapshot(
        str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        manifest.get("snapshot_sha256"),
    )
    if snapshot["symbol"] != code or snapshot["source_frequency"] != frequency:
        raise ValueError("冻结的MACD风险证据标的或物理周期不匹配")
    return snapshot.get("macd_zero_axis_operational_state"), snapshot["source_closed_at"]


def evidence_nested_snapshots(directory, code, main_manifest, lower_manifest):
    snapshots = []
    for frequency, manifest in (("5m", main_manifest), ("1m", lower_manifest)):
        health = evidence_health(directory, code, frequency, manifest)
        if not health["complete"] or not health["verified"]:
            raise ValueError("组合确认需要完整且经过校验的 5m 与 1m 证据")
        _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
        stat = path.stat()
        snapshot = _nested_snapshot(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                    manifest.get("snapshot_sha256"))
        if snapshot["symbol"] != code or snapshot["source_frequency"] != frequency:
            raise ValueError("组合确认证据的标的或周期不一致")
        snapshots.append(snapshot)
    return snapshots


@lru_cache(maxsize=128)
def _exit_catalog(path, size, mtime_ns, ctime_ns, expected_digest):
    snapshot = _nested_snapshot(path, size, mtime_ns, ctime_ns, expected_digest)
    valid = (_native_signal_catalog(path, size, mtime_ns, ctime_ns, expected_digest)[-1]
             if snapshot.get("analysis_scope") == "native_centers" else None)
    return snapshot["symbol"], snapshot["source_frequency"], build_point_exit_plans(snapshot, native_source_valid=valid)


def evidence_exit_catalog(directory, code, frequency, manifest):
    """Derive display references from verified saved structure, without a scan."""
    _, path = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
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
    parquet, packed = evidence_paths(directory, code, frequency, manifest.get("market", "a"), manifest.get("namespace"))
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


def evidence_u295_lower_structural_witnesses(
    directory, code, manifest, *, known_from: int,
):
    """Recalculate direct-1m U295 prerequisites from both hash-checked sources."""

    from chanlun.screening.lower_first_timeline import (
        observe_independent_lower_structural_witnesses,
    )

    _frame, snapshot, health = load_evidence(directory, code, "1m", manifest)
    if not health["complete"] or not health["verified"]:
        raise ValueError("U295独立1m来源未通过冻结证据校验")
    return observe_independent_lower_structural_witnesses(
        snapshot, known_from=known_from,
    )
