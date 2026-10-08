"""Retention for terminal screening runs; live/reviewed evidence is protected."""

from pathlib import Path
import json
import os
import re
import shutil
import sqlite3
import time

from .screening_lifecycle import reconcile_interrupted


RUN_ID = re.compile(r"[a-f0-9]{32}")
TERMINAL = {"completed", "failed", "cancelled", "canceled", "interrupted"}


def _read(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid screening run metadata")
    return value


def _references(root, protected):
    latest = _read(root / "latest.json").get("run_id")
    if not isinstance(latest, str) or not RUN_ID.fullmatch(latest):
        raise ValueError("screening latest run is unknown")
    values = protected() if callable(protected) else protected
    if not isinstance(values, (set, list, tuple)):
        raise ValueError("screening evidence references are unknown")
    result = {
        latest,
        *(v for v in values if isinstance(v, str) and RUN_ID.fullmatch(v)),
    }
    reviews = root / "manual_reviews.sqlite3"
    if reviews.exists():
        with sqlite3.connect(reviews.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            result.update(
                row[0] for row in db.execute("SELECT DISTINCT source FROM reviews")
            )
    return result


def prune_runs(
    root, protected=(), *, now=None, keep_days=None, keep_count=None, stop=None
):
    root = Path(root).resolve()
    stamp = time.time() if now is None else now
    days = (
        max(1, int(os.environ.get("CHANLUN_SCREENING_RUN_KEEP_DAYS", "7")))
        if keep_days is None
        else max(1, keep_days)
    )
    count = (
        max(1, int(os.environ.get("CHANLUN_SCREENING_RUN_KEEP_COUNT", "5")))
        if keep_count is None
        else max(1, keep_count)
    )
    result = {
        "checked_at": stamp,
        "keep_days": days,
        "keep_count": count,
        "removed": 0,
        "freed_bytes": 0,
        "protected": 0,
        "reconciled": 0,
        "error": "",
    }
    if not root.is_dir() or not (root / "latest.json").exists():
        return result
    try:
        references = _references(root, protected)
        candidates = []
        for path in root.iterdir():
            if stop is not None and stop.is_set():
                break
            if (
                not RUN_ID.fullmatch(path.name)
                or not path.is_dir()
                or path.is_symlink()
                or path.resolve().parent != root
            ):
                continue
            state = _read(path / "status.json")
            reconciled = reconcile_interrupted(path,state,now=stamp)
            result['reconciled'] += reconciled != state
            state = reconciled
            if state.get("run_id") != path.name or state.get("status") not in TERMINAL:
                references.add(path.name)
                continue
            modified = path.stat().st_mtime
            activity = max(
                modified,
                *(
                    float(state.get(k) or 0)
                    for k in ("started_at", "updated_at", "finished_at")
                ),
            )
            candidates.append((activity, path, state, modified))
        completed = sorted(
            (c for c in candidates if c[2]["status"] == "completed"),
            key=lambda c: c[0],
            reverse=True,
        )
        references.update(c[1].name for c in completed[:count])
        result["protected"] = len(references)
        for activity, path, state, modified in sorted(candidates, key=lambda c: c[0]):
            if stop is not None and stop.is_set():
                break
            if activity >= stamp - days * 86400 or path.name in references:
                continue
            # Re-read changing owners and the terminal record before each delete.
            # Any unreadable reference store aborts cleanup, without guessing.
            if path.name in _references(root, protected):
                continue
            size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            if (
                path.resolve().parent != root
                or path.is_symlink()
                or path.stat().st_mtime != modified
                or _read(path / "status.json") != state
            ):
                continue
            shutil.rmtree(path)
            result["removed"] += 1
            result["freed_bytes"] += size
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        result["error"] = "选股批次清理已暂停：" + type(exc).__name__
    return result
