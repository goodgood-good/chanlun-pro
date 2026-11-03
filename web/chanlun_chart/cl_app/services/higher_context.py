"""Incremental 1m/5m/30m context from existing calculations, without quote requests."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re
import threading
import time

import pandas as pd

from chanlun import config
from chanlun.screening.cache import calculation_key, input_fingerprint
from chanlun.screening.markets import closed_bar_cutoff, storage_symbol, validate_symbol
from chanlun.screening.rules import CN
from chanlun.screening.runner import source_revision, snapshot_for
from chanlun.screening.evidence import evidence_paths, load_evidence
from .native_period_link import PERIODS, SCHEMA, POINT_NAMES, relate, summarize

COMPILED_REVISION = source_revision()


def frozen_binding(row, run_id):
    """Internal references from the already verified result, never user input."""
    if not re.fullmatch(r"[0-9a-f]{32}", str(run_id)) or not row.get("evidence"):
        return None
    manifests = {row["frequency"]: row["evidence"]}
    for frequency, field in [
        ("1m", "confirmation_evidence"),
        ("30m", "background_evidence"),
    ]:
        if row.get(field):
            manifests[frequency] = row[field]
    return {"run_id": run_id, "manifests": manifests}


def load_frozen_period(directory, market, code, period, cutoff, revision, manifest):
    """Consume the original calculation, not a later rolling-cache rebuild."""
    state = json.loads((Path(directory) / "status.json").read_text(encoding="utf-8"))
    if revision != COMPILED_REVISION or state.get("source_revision") != revision:
        raise ValueError("saved period calculation revision changed")
    if manifest.get("market", "a") != market:
        raise ValueError("saved period market differs")
    frame, snapshot, health = load_evidence(directory, code, period, manifest)
    if not health.get("verified") or snapshot["source_closed_at"] != cutoff:
        raise ValueError("saved period is unverified or has a different cutoff")
    value = summarize(
        snapshot,
        float(frame.close.iloc[-1]),
        input_fingerprint=health["input_fingerprint"],
        frame=frame,
    )
    value["_evidence_source"] = "frozen_run"
    return value


@lru_cache(maxsize=8192)
def expected_clocks(market, code, as_of):
    observed = datetime.fromtimestamp(as_of, CN)
    clocks = {}
    for period in PERIODS:
        try:
            clocks[period] = closed_bar_cutoff(market, code, observed, period)
        except (OSError, ValueError, TypeError):
            clocks[period] = None
    return clocks


def _signature(paths):
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


def load_period(paths, market, code, period, cutoff, revision):
    """Verify cached input identity, truncating before any historical rebuild."""
    if revision != COMPILED_REVISION:
        raise ValueError("calculation implementation changed")
    candidates = []
    for path in paths:
        try:
            with gzip.open(path, "rb") as stream:
                raw = stream.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                continue
            wrapper = json.loads(raw)
            encoded = wrapper["payload"].encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != wrapper["sha256"]:
                continue
            payload = json.loads(encoded)
            snapshot = payload["snapshot"]
            source = payload.get("source_input") or {}
            if (
                payload.get("schema") != "screening-structure-v2"
                or payload.get("calculation_revision")
                not in {
                    revision,
                    revision + ":holding-consolidation-v1",
                    revision + ":native-strength-v2",
                }
                or snapshot.get("symbol") != code
                or snapshot.get("source_frequency") != period
                or source.get("code") != code
                or source.get("frequency") != period
                or type(snapshot.get("source_closed_at")) is not int
                or snapshot["source_closed_at"] < cutoff
            ):
                continue
            original = (path.parent / source["file"]).resolve()
            if original.parent != (path.parent / "inputs").resolve():
                continue
            candidates.append((snapshot["source_closed_at"], path, original, payload))
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            continue
    for _, path, original, payload in sorted(candidates, key=lambda row: row[0]):
        try:
            if (
                hashlib.sha256(original.read_bytes()).hexdigest()
                != payload["source_input"]["sha256"]
            ):
                continue
            frame = pd.read_parquet(original)
            if (
                frame.empty
                or calculation_key(frame, code, period, payload["calculation_revision"])
                != payload["key"]
            ):
                continue
            snapshot = payload["snapshot"]
            calculated_through = snapshot["source_closed_at"]
            if int(frame.date.iloc[-1].timestamp()) != snapshot["source_closed_at"]:
                continue
            if snapshot["source_closed_at"] > cutoff:
                frame = frame.loc[
                    frame.date <= datetime.fromtimestamp(cutoff, CN)
                ].copy()
                if frame.empty or int(frame.date.iloc[-1].timestamp()) != cutoff:
                    continue
                snapshot = snapshot_for(frame, code, period)
            if snapshot["source_closed_at"] != cutoff:
                continue
            value = summarize(
                snapshot,
                float(frame.close.iloc[-1]),
                input_fingerprint=input_fingerprint(frame, code, period),
                frame=frame,
            )
            value["_calculated_through"] = calculated_through
            return value, original
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise ValueError("matching period evidence is not available")


class PeriodContexts:
    def __init__(
        self,
        roots,
        *,
        loader=load_period,
        workers=2,
        evidence_root=None,
        additional_evidence_roots=(),
    ):
        self.roots = tuple(Path(root) for root in roots)
        self.loader = loader
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.inflight = set()
        self.executor = None
        self.workers = workers
        self.version = 0
        self.evidence_root = Path(evidence_root) if evidence_root is not None else None
        self.evidence_roots = tuple(
            dict.fromkeys(
                Path(root).resolve()
                for root in (self.evidence_root, *additional_evidence_roots)
                if root is not None
            )
        )

    def _paths(self, market, code, period):
        name = storage_symbol(code, market) + "_" + period + ".json.gz"
        return tuple(root / name for root in self.roots)

    def _load(self, key, previous):
        revision, market, code, period, cutoff = key[:5]
        if len(key) > 7:
            run_id, manifest = json.loads(key[7])
            directories = [
                root / run_id
                for root in self.evidence_roots
                if (root / run_id).is_dir()
            ]
            if len(directories) > 1:
                raise ValueError("ambiguous frozen run identity")
            directory = directories[0] if directories else self.evidence_root / run_id
            paths = evidence_paths(
                directory, code, period, market, manifest.get("namespace")
            )
            signature = _signature(paths)
            try:
                state = json.loads(
                    (directory / "status.json").read_text(encoding="utf-8")
                )
                if state.get("source_revision") != revision:
                    raise ValueError("saved period revision changed")
                if (
                    previous
                    and previous.get("signature") == signature
                    and previous.get("value")
                ):
                    return {**previous, "checked": time.monotonic()}
                value = load_frozen_period(
                    directory, market, code, period, cutoff, revision, manifest
                )
                if _signature(paths) != signature:
                    raise ValueError("saved period changed while reading")
                return {
                    "value": value,
                    "signature": signature,
                    "checked": time.monotonic(),
                }
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                # A damaged frozen binding must not be replaced by an unrelated
                # current cache merely because its symbol and cutoff match.
                return {
                    "value": None,
                    "signature": signature,
                    "checked": time.monotonic(),
                    "error": "frozen_evidence_unavailable",
                }
        if (
            previous
            and previous.get("value")
            and previous["value"].get("_calculated_through", cutoff) > cutoff
            and previous.get("input_signature") == _signature((previous["input"],))
        ):
            # Once a historical prefix is verified, subsequent appended candles
            # must not force that same frozen prefix to be rebuilt every minute.
            return {**previous, "checked": time.monotonic()}
        paths = self._paths(market, code, period)
        watched = (
            *paths,
            *([previous["input"]] if previous and previous.get("input") else []),
        )
        signature = _signature(watched)
        if previous and previous.get("signature") == signature:
            return {**previous, "checked": time.monotonic()}
        try:
            value, original = self.loader(paths, market, code, period, cutoff, revision)
            if _signature(paths) != signature[: len(paths)]:
                raise ValueError("period changed while reading")
            return {
                "value": value,
                "input": original,
                "signature": _signature((*paths, original)),
                "input_signature": _signature((original,)),
                "checked": time.monotonic(),
            }
        except Exception:
            return {
                "value": None,
                "signature": signature,
                "checked": time.monotonic(),
                "error": "cached_evidence_unavailable",
            }

    def _finished(self, key, future):
        try:
            entry = future.result()
        except Exception:
            entry = {
                "value": None,
                "checked": time.monotonic(),
                "error": "evidence_read_failed",
            }
        with self.lock:
            self.inflight.discard(key)
            old = self.entries.get(key)
            if (
                not old
                or old.get("value") != entry.get("value")
                or old.get("error") != entry.get("error")
            ):
                self.version += 1
            self.entries[key] = entry
            self.entries.move_to_end(key)
            while len(self.entries) > 8192:
                self.entries.popitem(last=False)

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                self.entries.move_to_end(key)
            if (
                key not in self.inflight
                and len(self.inflight) < 2048
                and (not entry or time.monotonic() - entry.get("checked", 0) >= 5)
            ):
                if self.executor is None:
                    self.executor = ThreadPoolExecutor(
                        max_workers=self.workers, thread_name_prefix="period-context"
                    )
                self.inflight.add(key)
                future = self.executor.submit(self._load, key, entry)
            else:
                future = None
            value = entry.get("value") if entry else None
        # A completed callback may run synchronously, outside the state lock.
        if future is not None:
            future.add_done_callback(lambda done: self._finished(key, done))
        return value

    def context(self, row, revision, *, run_id=None):
        market = row.get("market", "a")
        code = row.get("code")
        period = row.get("frequency")
        point = row.get("point", row)
        cutoff = row.get("source_closed_at")
        if (
            period not in PERIODS
            or point.get("point_type") not in POINT_NAMES
            or type(cutoff) not in (int, float)
            or not revision
            or not validate_symbol(market, code)
        ):
            return None
        expected = expected_clocks(market, code, int(cutoff))
        basis = point.get("price_basis_revision") or row.get("price_basis_revision")
        fingerprint = row.get("input_fingerprint") or (row.get("evidence") or {}).get(
            "input_fingerprint"
        )
        binding = row.get("_context_evidence") or frozen_binding(row, run_id)
        summaries = {}
        keys = {}
        for p, at in expected.items():
            if type(at) is not int:
                continue
            key = (
                revision,
                market,
                code,
                p,
                at,
                basis,
                fingerprint if p == period else None,
            )
            manifest = (binding or {}).get("manifests", {}).get(p)
            saved_run = (binding or {}).get("run_id")
            if (
                manifest
                and self.evidence_root is not None
                and re.fullmatch(r"[0-9a-f]{32}", str(saved_run))
            ):
                key += (
                    json.dumps(
                        [saved_run, manifest], sort_keys=True, separators=(",", ":")
                    ),
                )
            keys[p] = key
            summaries[p] = self.get(key)
        result = relate(row, summaries, expected)
        with self.lock:
            entries = {p: self.entries.get(key) for p, key in keys.items()}
            checking = {p for p, key in keys.items() if key in self.inflight}
        for peer in result["periods"]:
            frequency = peer["frequency"]
            entry = entries.get(frequency)
            if frequency not in keys:
                peer.update(
                    check_state="unavailable",
                    reason="calendar_unavailable",
                    label="周期截止不可核验",
                )
            elif not entry:
                peer.update(
                    check_state="checking" if frequency in checking else "queued"
                )
            elif entry.get("error"):
                peer.update(
                    check_state="unavailable",
                    reason=entry["error"],
                    label="已检查，周期证据不可用",
                )
            else:
                peer["check_state"] = "checked"
                if summaries.get(frequency):
                    peer["evidence_source"] = summaries[frequency].get(
                        "_evidence_source", "calculation_cache"
                    )
        result["checks_finished"] = all(
            p.get("check_state") in {"checked", "unavailable"}
            for p in result["periods"]
        )
        return result

    def close(self):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)


_services = {}
_services_lock = threading.Lock()


def _service(screening):
    root = Path(screening.root).resolve()
    key = str(root)
    with _services_lock:
        if key not in _services:
            _services[key] = PeriodContexts(
                (
                    root / "analysis_cache",
                    config.get_data_path() / "signal_monitor/runs/analysis_cache",
                ),
                evidence_root=root,
                additional_evidence_roots=(
                    config.get_data_path() / "signal_monitor/runs",
                ),
            )
        return _services[key]


def snapshot(screening, run_id, revision, *, candidates=(), source_current=True):
    service = _service(screening)
    rows = {}
    if source_current:
        for row in candidates:
            context = service.context(row, revision, run_id=run_id)
            if context:
                rows[row["id"]] = context
    complete = sum(value["quality"] == "complete" for value in rows.values())
    checked = sum(value.get("checks_finished") is True for value in rows.values())
    return {
        "schema": SCHEMA,
        "status": "completed"
        if rows and checked == len(rows)
        else "running"
        if rows
        else "not_started",
        "completed": complete,
        "checked": checked,
        "total": len(rows),
        "rows": rows,
    }


def event_context(screening, event, revision):
    return _service(screening).context(event, revision)


def prefetch(screening, result):
    if result.get("source_current") is not True:
        return
    service = _service(screening)
    for row in [*result.get("selected", ()), *result.get("observations", ())]:
        if (row.get("point") or {}).get("evidence_scope") == "native_center_signal":
            service.context(
                row, result.get("source_revision"), run_id=result.get("run_id")
            )


def status_version(screening, run_id):
    if not run_id:
        return SCHEMA + ":idle"
    service = _service(screening)
    return f"{SCHEMA}:{run_id}:{service.version}"
