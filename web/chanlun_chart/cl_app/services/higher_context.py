"""Launch/read the optional 30m second pass without blocking screening or monitoring."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from chanlun.screening.higher_context import SCHEMA
from chanlun.screening.runner import write_json


def _directory(screening, run_id):
    return screening.root / run_id / "higher_context"


def _read(path):
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 4:
                return None
            time.sleep(0.02 * (attempt + 1))
        except (OSError, UnicodeError, ValueError):
            return None


def _candidates(result):
    found = {}
    for row in [*result.get("selected", []), *result.get("observations", [])]:
        if row.get("frequency") != "5m" or (row.get("point") or {}).get("status") != "confirmed":
            continue
        market, code, cutoff = row.get("market", "a"), row.get("code"), row.get("source_closed_at")
        if not isinstance(code, str) or type(cutoff) is not int:
            continue
        found[(market, code, cutoff)] = {"market": market, "code": code, "source_closed_at": cutoff}
    return [found[key] for key in sorted(found)]


def ensure(screening, manual, *, monitor_runs=None):
    """Start once for a complete current run; GET endpoints only read its state."""
    if manual.get("status") != "completed" or not manual.get("source_current"):
        return
    run_id, revision = manual.get("run_id"), manual.get("source_revision")
    if not run_id or not revision:
        return
    directory = _directory(screening, run_id)
    status = _read(directory / "status.json") or {}
    if status.get("run_id") == run_id and status.get("source_revision") == revision:
        if status.get("status") in {"completed", "failed"}:
            return
        if status.get("status") in {"starting", "running"}:
            if time.time() - status.get("updated_at", 0) < 90:
                return
            # A stale worker should be visible rather than silently replaced.
            from .screening import _pid_alive
            launcher = _read(directory / "launcher.json") or {}
            if _pid_alive(status.get("worker_pid") or launcher.get("worker_pid")):
                return
            write_json(directory / "status.json", {**status, "status": "failed",
                       "error": "30m context worker stopped before completion", "updated_at": time.time()})
            return
    result = screening.results()
    if (result.get("run_id") != run_id or result.get("source_revision") != revision
            or result.get("status") != "completed"):
        return
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": SCHEMA, "run_id": run_id, "source_revision": revision,
                "screening_cutoffs": result.get("cutoffs", {}), "candidates": _candidates(result),
                "created_at": time.time()}
    if monitor_runs is not None:
        manifest["monitor_runs"] = str(monitor_runs)
    write_json(directory / "request.json", manifest)
    write_json(directory / "status.json", {"schema": SCHEMA, "run_id": run_id,
               "source_revision": revision, "status": "starting", "completed": 0,
               "total": len(manifest["candidates"]), "updated_at": time.time()})
    project = Path(__file__).resolve().parents[4]
    env = {**os.environ, "PYTHONPATH": str(project / "src"), "PYTHONIOENCODING": "utf-8",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    try:
        with (directory / "worker.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "chanlun.screening.higher_context", "--run-dir", str(directory)],
                cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                if os.name == "nt" else 0,
            )
        write_json(directory / "launcher.json", {"worker_pid": process.pid, "launched_at": time.time()})
    except Exception as exc:
        write_json(directory / "status.json", {"schema": SCHEMA, "run_id": run_id,
                   "source_revision": revision, "status": "failed", "error": str(exc)[:300],
                   "updated_at": time.time()})
        raise


def snapshot(screening, run_id, revision):
    """Read only rows belonging to this exact screening run and source revision."""
    empty = {"status": "not_started", "completed": 0, "total": 0, "rows": {}}
    if not run_id or not revision:
        return empty
    directory = _directory(screening, run_id)
    manifest = _read(directory / "request.json") or {}
    status = _read(directory / "status.json") or {}
    if (manifest.get("schema") != SCHEMA or manifest.get("run_id") != run_id
            or manifest.get("source_revision") != revision
            or status.get("run_id") != run_id or status.get("source_revision") != revision):
        return empty
    expected = {(row["market"], row["code"], row["source_closed_at"])
                for row in manifest.get("candidates", [])}
    rows = {}
    path = directory / "rows.jsonl"
    if path.exists():
        # The worker flushes one complete JSONL line before publishing progress.
        with path.open("rb") as stream:
            for index, line in enumerate(stream):
                if index >= status.get("completed", 0) or not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                except (UnicodeError, ValueError):
                    break
                key = (row.get("market"), row.get("code"), row.get("source_closed_at"))
                if key in expected:
                    rows[key] = row
    return {"status": status.get("status", "not_started"), "completed": status.get("completed", 0),
            "total": status.get("total", 0), "error": status.get("error"), "rows": rows,
            "updated_at": status.get("updated_at")}


def status_version(screening, run_id):
    if not run_id:
        return "not_started"
    status = _read(_directory(screening, run_id) / "status.json") or {}
    return ":".join(str(status.get(key, "")) for key in ("status", "completed", "phase", "error"))
