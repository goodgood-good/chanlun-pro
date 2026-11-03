"""Bounded post-selection maintenance; it never changes published scan results."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from chanlun.persistence.file_lock import (
    interprocess_file_lock,
    InterprocessLockTimeout,
)
from .state_io import read_state_json

REQUEST = "rolling_maintenance.request.json"
STATUS = "rolling_maintenance.status.json"


def eligible(row):
    return (
        row.get("frequency") == "5m"
        and not row.get("data_errors")
        and bool(row.get("input_fingerprint"))
        and (
            row.get("confirmed_buys", 0) > 0
            or row.get("confirmed_sells", 0) > 0
            or bool(row.get("historical_confirmed"))
            or any(
                (item.get("alert") or item).get("status") == "confirmed"
                for item in row.get("consolidation_alerts", [])
            )
        )
    )


def launch(run_dir, request, contexts, stocks, revision):
    """Only a committed, current full scan may schedule optional future work."""
    from .runner import source_revision, write_json

    run_dir = Path(run_dir).resolve()
    try:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", str(revision))
            or source_revision() != revision
            or not request.get("rolling_window_targets")
            or request.get("force_rebuild")
            or read_state_json(run_dir / "status.json").get("status") != "completed"
        ):
            return {"state": "skipped"}
        payload = {
            "schema": "screening-rolling-maintenance-v1",
            "source_revision": revision,
            "settings": request["settings"],
            "contexts": contexts,
            "targets": request["rolling_window_targets"],
            "compute_budget": request.get("compute_budget"),
            "stocks": [
                {
                    key: stock[key]
                    for key in ("code", "market", "name", "holding", "_monitor_periods")
                    if key in stock
                }
                for stock in stocks
                if stock.get("market", "a") == "a"
            ],
        }
        write_json(run_dir / REQUEST, payload, durable=True)
        write_json(
            run_dir / STATUS,
            {
                "state": "queued",
                "completed": 0,
                "total": None,
                "source_revision": revision,
            },
        )
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            if os.name == "nt"
            else 0
        )
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = source_root + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
        with (run_dir / "rolling_maintenance.log").open("ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "chanlun.screening.rolling_maintenance",
                    "--run-dir",
                    str(run_dir),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                cwd=Path(__file__).resolve().parents[3],
                env=environment,
            )
        return {
            "state": "scheduled",
            "pid": process.pid,
            "status_file": STATUS,
            "workers": 2,
        }
    except Exception as exc:
        return {"state": "unavailable", "error_type": type(exc).__name__}


def _worker(connection):
    from .compute_capacity import compute_slot, MaintenanceDeferred
    from .rolling_proofs import prepare
    from .worker_transport import JobInputReader, store_reply

    reader = JobInputReader()
    try:
        while (message := connection.recv()) is not None:
            task = reader.load(message)
            stock = task["stock"]
            began = time.monotonic()
            try:
                with compute_slot(task["request"], task["run_dir"]):
                    outcome = prepare(
                        stock,
                        task["settings"],
                        task["contexts"],
                        task["run_dir"],
                        task["revision"],
                        {"rows": [stock["_rolling_source"]]},
                        task["request"]["rolling_window_targets"],
                    )
            except MaintenanceDeferred:
                outcome = {"state": "deferred", "proofs": 0}
            result = {
                "market": stock.get("market", "a"),
                "code": stock["code"],
                "name": stock["name"],
                "rows": [],
                "requested_frequencies": ["5m"],
                "maintenance": outcome,
                "seconds": round(time.monotonic() - began, 3),
            }
            connection.send(store_reply(task, result, prefer_inline=True))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


def execute(run_dir):
    from .runner import source_revision, write_json
    from .worker_pool import ReusableWorkerPool
    from .worker_lifetime import process_identity

    run_dir = Path(run_dir).resolve()
    state = {
        "state": "starting",
        "started_at": time.time(),
        "completed": 0,
        "total": 0,
        "prepared": 0,
        "reused": 0,
        "failed": 0,
    }
    pool = None
    try:
        # Different completed scans cannot run competing warmup pools.
        with interprocess_file_lock(
            run_dir.parent / "rolling_maintenance.lock", timeout_seconds=0.1
        ):
            request = read_state_json(run_dir / REQUEST)
            if request.get("schema") != "screening-rolling-maintenance-v1":
                raise ValueError("Invalid rolling maintenance request")
            revision = request["source_revision"]
            if revision != source_revision():
                state["state"] = "obsolete"
                return
            if read_state_json(run_dir / "status.json").get("status") != "completed":
                raise ValueError("Selection must be completed before maintenance")
            index = {(s.get("market", "a"), s["code"]): s for s in request["stocks"]}
            tasks = []
            for line in (
                (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
            ):
                result = json.loads(line)
                stock = index.get((result.get("market", "a"), result["code"]))
                if stock is None:
                    continue
                for row in result.get("rows", []):
                    if eligible(row):
                        source = {
                            key: row[key]
                            for key in (
                                "frequency",
                                "input_fingerprint",
                                "data_quality",
                            )
                            if key in row
                        }
                        tasks.append(
                            {
                                **stock,
                                "_monitor_periods": ["5m"],
                                "_rolling_source": source,
                            }
                        )
            identity = process_identity(os.getpid())
            state.update(
                state="running",
                total=len(tasks),
                source_revision=revision,
                pid=os.getpid(),
                process_birth=identity.get("birth"),
                workers=2,
            )
            write_json(run_dir / STATUS, state)
            execution = {
                "rolling_window_targets": request["targets"],
                "compute_budget": request.get("compute_budget"),
                "background_maintenance": True,
            }
            pool = ReusableWorkerPool(revision, worker_target=_worker)
            for result, _ in pool.scan(
                tasks,
                request["settings"],
                request["contexts"],
                run_dir,
                2,
                revision,
                request=execution,
            ):
                if result is not None:
                    state["completed"] += 1
                    outcome = (result.get("maintenance") or {}).get(
                        "state", "unavailable"
                    )
                    if outcome in ("prepared", "reused"):
                        state[outcome] += 1
                    elif outcome == "unavailable":
                        state["failed"] += 1
                    elif outcome == "deferred":
                        state["deferred"] = state.get("deferred", 0) + 1
                state.update(
                    updated_at=time.time(),
                    elapsed_seconds=round(time.time() - state["started_at"], 1),
                )
                write_json(run_dir / STATUS, state)
            state["state"] = (
                "completed" if state["completed"] == state["total"] else "cancelled"
            )
    except InterprocessLockTimeout:
        state["state"] = "already_running"
    except Exception as exc:
        state.update(state="failed", error_type=type(exc).__name__, error=str(exc))
    finally:
        if pool is not None:
            try:
                pool.close()
            except Exception as exc:
                state.update(state="failed", error_type=type(exc).__name__)
        state.update(
            finished_at=time.time(),
            elapsed_seconds=round(time.time() - state["started_at"], 1),
        )
        write_json(run_dir / STATUS, state)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    from chanlun.screening.rolling_maintenance import execute as run

    run(parser.parse_args().run_dir)
