"""Bound orphan replay inputs while protecting all current cache references."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time

NAME = re.compile(r"[a-f0-9]{64}\.[a-f0-9]{16}\.parquet")


def _version(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def save_reference(path, source, *, version=None):
    target = path.with_suffix(".input-ref.json")
    value = {
        "version": version if version is not None else _version(path),
        "file": source.get("file") if source else None,
    }
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _reference(path):
    version = _version(path)
    try:
        value = json.loads(
            path.with_suffix(".input-ref.json").read_text(encoding="utf-8")
        )
        if value["version"] == version:
            return value.get("file")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Existing installs migrate metadata once. Subsequent hourly passes read
    # tiny sidecars instead of decompressing every full structure snapshot.
    wrapper = json.loads(gzip.decompress(path.read_bytes()))
    raw = wrapper["payload"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != wrapper["sha256"]:
        raise ValueError("cache checksum mismatch; retention deferred")
    value = json.loads(raw)
    if _version(path) != version:
        raise ValueError("cache changed during retention; retry later")
    source = value.get("source_input")
    save_reference(path, source, version=version)
    return source.get("file") if source else None


def prune_inputs(
    root, *, now=None, keep_days=3, max_bytes=2 * 1024**3, grace_seconds=3600, stop=None
):
    """Delete only named orphan inputs in this exact cache's inputs directory.

    Run evidence is stored separately and is never visited. Current cache
    inputs remain even if they alone exceed the soft budget. A grace period
    protects writers publishing a cache/reference and touched reused inputs.
    """
    root = Path(root).resolve()
    folder = (root / "inputs").resolve()
    result = {
        "removed": 0,
        "freed_bytes": 0,
        "remaining_bytes": 0,
        "protected": 0,
        "error": "",
    }
    if not folder.is_dir():
        return result
    if folder.parent != root or (root / "inputs").is_symlink():
        return {**result, "error": "输入目录指向缓存范围外，未清理"}
    now = time.time() if now is None else now
    protected = set()
    try:
        for path in root.glob("*.json.gz"):
            if stop is not None and stop.is_set():
                return {**result, "error": "清理已停止"}
            if path.is_symlink() or path.resolve().parent != root:
                raise ValueError("cache path outside retention root")
            ref = _reference(path)
            if ref:
                target = (root / ref).resolve()
                if target.parent != folder or not NAME.fullmatch(target.name):
                    raise ValueError("cache input reference is invalid")
                protected.add(target)
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        return {**result, "error": "部分缓存引用无法核对，本次未清理"}
    files = []
    for path in folder.glob("*.parquet"):
        if (
            path.is_symlink()
            or path.resolve().parent != folder
            or not NAME.fullmatch(path.name)
        ):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, stat.st_size, path.resolve()))
    remaining = sum(size for _, size, _ in files)
    result["protected"] = len(protected)
    for stamp, size, path in sorted(files):
        if stop is not None and stop.is_set():
            break
        if path in protected or now - stamp < grace_seconds:
            continue
        if now - stamp < keep_days * 86400 and remaining <= max_bytes:
            continue
        try:
            # Validate the final absolute target and check a concurrent reuse
            # immediately before deleting this single file (never recursively).
            if (
                path.parent != folder
                or path.is_symlink()
                or path.stat().st_mtime != stamp
            ):
                continue
            path.unlink()
            remaining -= size
            result["removed"] += 1
            result["freed_bytes"] += size
        except FileNotFoundError:
            pass
    return {**result, "remaining_bytes": remaining}
