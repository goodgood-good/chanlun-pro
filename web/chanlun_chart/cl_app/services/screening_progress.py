"""Publish verified prefixes while a screening worker appends more symbols."""
from __future__ import annotations

from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import threading

from chanlun.screening.evidence import evidence_paths


LISTS = ("selected", "observations", "entry_waiting", "consolidation_alerts", "verified_periods",
         "u295_lower_structural_witnesses", "recent_rejections", "processed_symbols", "errors")
COUNTS = ("semantic_exclusion_count", "lifetime_exclusion_count", "selection_policy_exclusion_count")


def record_lines(directory, completed, after=0, offset=0):
    path = Path(directory) / "results.jsonl"
    if not path.exists():
        return
    with path.open("rb") as stream:
        stream.seek(offset)
        for index, line in enumerate(stream, start=after if offset else 0):
            if completed is not None and index >= completed:
                break
            if index < after:
                continue
            if not line.endswith(b"\n"):
                break
            yield index, line


def _file_state(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def _file_states(paths):
    """Use directory metadata for watched paths, preserving direct-stat fallback."""
    parents = {}
    for name in paths:
        parent, leaf = os.path.split(name)
        parents.setdefault(parent or os.curdir, {}).setdefault(os.path.normcase(leaf), []).append(name)
    result = {}
    for parent, names in parents.items():
        try:
            found = {}
            with os.scandir(parent) as entries:
                for entry in entries:
                    key = os.path.normcase(entry.name)
                    if key not in names:
                        continue
                    try:
                        stat = entry.stat()
                        found[key] = stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
                    except FileNotFoundError:
                        found[key] = None
            for key, aliases in names.items():
                for name in aliases:
                    result[name] = found.get(key)
        except OSError:
            # Some files remain accessible even when directory listing is not.
            # Keep the former error/missing-file behavior in that case.
            for aliases in names.values():
                for name in aliases:
                    result[name] = _file_state(Path(name))
    return result


def _guard(directory, base, through):
    digest = base["digest"].copy() if base else hashlib.sha256()
    size = base["size"] if base else 0
    files = dict(base["files"]) if base else {}
    after = base["through"] if base else 0
    actual = after
    touched = {}
    for index, line in record_lines(directory, through, after, size):
        digest.update(line)
        size += len(line)
        actual = index + 1
        item = json.loads(line)
        for row in item["rows"]:
            periods = {row["frequency"]}
            if row.get("confirmation_evidence"):
                periods.add("1m")
            for period in periods:
                manifest = row.get("confirmation_evidence", {}) if period != row["frequency"] else row.get("evidence", {})
                for path in evidence_paths(directory, row["code"], period, manifest.get("market", row.get("market", item.get("market", "a")))):
                    touched[str(path)] = None
    if actual != through:
        raise ValueError("已完成进度对应的结果尚未完整写入，请稍后刷新")
    files.update(_file_states(touched))
    return {"digest": digest, "size": size, "files": files, "through": through}


def _unchanged(directory, guard):
    if guard is None:
        return True
    try:
        if guard["size"]:
            with (Path(directory) / "results.jsonl").open("rb") as stream:
                if hashlib.sha256(stream.read(guard["size"])).digest() != guard["digest"].digest():
                    return False
        return _file_states(guard["files"]) == guard["files"]
    except OSError:
        return False


def _merge(previous, chunk):
    result = {**previous, **chunk}
    for key in LISTS:
        result[key] = [*previous.get(key, []), *chunk.get(key, [])]
    for key in COUNTS:
        result[key] = previous.get(key, 0) + chunk.get(key, 0)
    counts = dict(previous.get("rejection_counts", {}))
    for key, count in chunk.get("rejection_counts", {}).items():
        counts[key] = counts.get(key, 0) + count
    result["rejection_counts"] = counts
    for key in ("selected", "observations", "entry_waiting"):
        result[key].sort(key=lambda row: (-row.get("selection_available_at", row["point"]["available_at"]), row["code"], row["frequency"]))
    result["u295_lower_structural_witnesses"].sort(key=lambda row: (-row["known_at"], row["code"], row["point_id"]))
    result["u295_lower_structural_witness_count"] = len(result["u295_lower_structural_witnesses"])
    result["observation_count"] = len(result["observations"])
    result["selected_symbols"] = len({(row.get("market", "a"), row["code"]) for row in result["selected"]})
    return result


class ProgressiveResults:
    """One bounded reader; appends never discard a verified prefix."""
    batch_size = 64

    def __init__(self, read):
        self.read = read
        self.lock = threading.Lock()
        self.context = None
        self.payload = {}
        self.guard = None
        self.future = None
        self.persisted_key = None
        self.requested_context = None
        self.requested_completed = 0
        self.request_version = 0

    @staticmethod
    def key(directory, state):
        return (str(directory), json.dumps({key: state.get(key) for key in (
            "run_id", "settings", "cutoffs", "source_revision", "current_source_revision",
            "source_current", "frozen_native_review", "result_reader_revision", "selection_policy_version",
        )}, sort_keys=True))

    def matches(self, directory, state):
        return self.context == self.key(directory, state)

    def _prepare(self, future, directory, state, previous, guard):
        try:
            context = self.key(directory, state)
            target = state.get("completed", 0)
            while (guard["through"] if guard else 0) < target:
                with self.lock:
                    if self.requested_context != context or self.requested_completed < target:
                        break
                through = min(target, (guard["through"] if guard else 0) + self.batch_size)
                before = _guard(directory, guard, through)
                chunk = self.read(directory, {**state, "completed": through},
                                  after_completed=guard["through"] if guard else 0,
                                  record_offset=guard["size"] if guard else 0)
                if (chunk.get("results_through") != through or not _unchanged(directory, guard)
                        or not _unchanged(directory, before)):
                    raise ValueError("校验期间已有结果或证据发生变化，请稍后刷新")
                previous, guard = _merge(previous, chunk), before
                with self.lock:
                    self.payload, self.guard = previous, guard
                    self.persisted_key = None
            future.set_result(None)
        except BaseException as exc:
            future.set_exception(exc)

    @staticmethod
    def _pending(state):
        return {**state, **{key: [] for key in LISTS}, "rejection_counts": {},
                "results_through": 0, "results_loading": True,
                "results_available": False, "results_partial": True}

    def results(self, directory, state, *, background=False, available=False):
        context = self.key(directory, state)
        completed = state.get("completed", 0)
        with self.lock:
            if (self.requested_context, self.requested_completed) != (context, completed):
                self.request_version += 1
            request_version = self.request_version
            self.requested_context, self.requested_completed = context, completed
        while True:
            launch = None
            with self.lock:
                if self.request_version != request_version:
                    return self._pending(state)
                owned_context, guard = self.context, self.guard
                compatible = (owned_context == context
                              and (guard is None or guard["through"] <= completed))
            # Filesystem validation must leave background publication and
            # replacement requests free to acquire the state lock.
            valid = compatible and _unchanged(directory, guard)
            with self.lock:
                if self.request_version != request_version:
                    return self._pending(state)
                if self.context != owned_context or self.guard is not guard:
                    continue
                future = self.future
                finished = future is not None and future.done()
                reading = future is not None and not finished
                if not valid and not reading:
                    self.context, self.payload, self.guard = context, {}, None
                    self.future, self.persisted_key = None, None
                    future = None
                    finished = False
                    valid = True
                if finished:
                    self.future = None
                    future.result()
                    future = None
                    reading = False
                payload = self.payload if valid else {}
                through = self.guard["through"] if valid and self.guard else 0
                if not reading and through < completed:
                    future = self.future = Future()
                    launch = (future, directory, state, self.payload, self.guard)
                    reading = True
            if launch:
                try:
                    threading.Thread(target=self._prepare, args=launch, name="screening-progress", daemon=True).start()
                except Exception as error:
                    with self.lock:
                        if self.future is future:
                            self.future = None
                    if not future.done():
                        future.set_exception(error)
                    raise
            loading = reading or through < completed
            if not loading or background or available and through:
                return {**{key: [] for key in LISTS}, "rejection_counts": {}, **payload, **state, "results_through": through,
                        "results_loading": loading, "results_available": through > 0,
                        "results_partial": loading or state.get("status") != "completed"}
            try:
                future.result()
            except BaseException:
                with self.lock:
                    retired = self.request_version != request_version
                    owned = self.context == context
                    if self.future is future:
                        self.future = None
                if retired:
                    return self._pending(state)
                if not owned:
                    continue
                raise
