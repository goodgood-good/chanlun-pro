"""Independent, frozen 30m context for an already completed screening run.

This is a descriptive second pass over confirmed 5m candidates. It neither
selects a point nor changes the live monitor's seed or notification policy.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import time

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.screening.cache import input_fingerprint
from chanlun.screening.markets import market_context
from chanlun.screening.rules import CN, frame_quality, trading_context
from chanlun.screening.runner import fetch_frame, source_revision, write_json


SCHEMA = "screening-higher-context-v1"


def _epoch(value):
    return None if value is None else int(value.timestamp())


def _enum(value):
    return getattr(value, "value", value)


def classify_level(level, cutoff):
    """Describe evidence current at cutoff; never promote a historical tail."""
    trend = level.trend_types[-1] if level.trend_types else None
    unit = level.units[-1] if level.units else None
    trend_at = _epoch(trend.available_at) if trend else None
    unit_at = _epoch(unit.available_at) if unit else None
    current_trend = trend if trend_at == cutoff and _enum(trend.state) == "forming" else None
    # A unit locked at this candle has already ended. Only an open unit can
    # describe the direction currently in progress.
    current_unit = unit if unit_at == cutoff and unit.forming and not unit.locked else None
    category = "unknown"
    if current_trend is not None and _enum(trend.kind) == "trend":
        category = "up_trend_forming" if trend.direction == "up" else "down_trend_forming"
    elif current_unit is not None:
        category = "up_segment" if unit.direction == "up" else "down_segment"
    elif current_trend is not None and _enum(trend.kind) == "consolidation":
        category = "consolidation"
    return {
        "category": category,
        "trend": None if trend is None else {
            "kind": _enum(trend.kind), "direction": trend.direction,
            "state": _enum(trend.state), "available_at": trend_at,
            "market_end": _epoch(trend.market_end), "center_count": len(trend.centers),
        },
        "segment": None if unit is None else {
            "direction": unit.direction, "available_at": unit_at,
            "market_end": _epoch(unit.market_end), "forming": unit.forming,
            "locked": unit.locked,
        },
        "center_count": len(level.center_result.centers),
        "evidence_available_at": cutoff if category != "unknown" else max(trend_at or 0, unit_at or 0) or None,
    }


def assess_symbol(candidate):
    market, code, cutoff = candidate["market"], candidate["code"], candidate["source_closed_at"]
    result = {"market": market, "code": code, "source_closed_at": cutoff,
              "frequency": "30m", "category": "unknown", "quality": "unknown"}
    try:
        observed = datetime.fromtimestamp(cutoff, CN)
        context = (trading_context(observed, "30m", 5, 20) if market == "a" else
                   market_context(market, code, observed, "30m", 5, 20))
        context = {**context, "market": market, "symbol": code}
        result["context_cutoff"] = context["cutoff"]
        if context["cutoff"] != cutoff:
            result["reason"] = "30M_CUTOFF_NOT_ALIGNED"
            return result
        frame = fetch_frame(code, "30m", context)
        quality = frame_quality(frame, context)
        result["quality"] = quality["status"]
        if quality["status"] != "complete":
            result["reason"] = ",".join(quality["errors"]) or "30M_DATA_INCOMPLETE"
            return result
        result["input_fingerprint"] = input_fingerprint(frame, code, "30m")
        runtime = build_strict_chart_cd(market=market, code=code, frequency="30m",
                                        frame=frame, last_bar_closed=True)
        if runtime.cd is None:
            result["reason"] = runtime.error_code or "30M_STRUCTURE_UNAVAILABLE"
            return result
        structure = runtime.cd.get_strict_evidence().structure
        level = next((item for item in structure.levels if item.structural_level == 0), None)
        if level is None:
            result["reason"] = "30M_STRUCTURE_EMPTY"
            return result
        result.update(classify_level(level, cutoff))
        result["quality"] = "complete"
        if result["category"] == "unknown":
            result["reason"] = "NO_CURRENT_30M_DIRECTION"
    except Exception as exc:
        result["reason"] = type(exc).__name__
        result["detail"] = str(exc)[:300]
    return result


def _monitor_active(root):
    if not root:
        return False
    try:
        root = Path(root)
        run_id = json.loads((root / "latest.json").read_text(encoding="utf-8"))["run_id"]
        status = json.loads((root / run_id / "status.json").read_text(encoding="utf-8"))
        if status.get("status") not in {"starting", "running"}:
            return False
        if time.time() - status.get("updated_at", status.get("started_at", 0)) < 300:
            return True
        pid = status.get("worker_pid")
        if type(pid) is not int or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_job(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or directory.parent.name != manifest.get("run_id"):
        raise ValueError("higher-context run identity mismatch")
    if manifest.get("source_revision") != source_revision():
        write_json(directory / "status.json", {"schema": SCHEMA, "status": "failed",
                   "run_id": manifest["run_id"], "error": "source revision changed"})
        return
    candidates = manifest["candidates"]
    status = {"schema": SCHEMA, "run_id": manifest["run_id"],
              "source_revision": manifest["source_revision"], "status": "running",
              "total": len(candidates), "completed": 0, "started_at": time.time(),
              "updated_at": time.time(), "worker_pid": os.getpid()}
    write_json(directory / "status.json", status)
    try:
        with (directory / "rows.jsonl").open("w", encoding="utf-8") as output:
            if candidates:
                with ProcessPoolExecutor(max_workers=2) as pool:
                    pending = {}
                    next_index = 0
                    while pending or next_index < len(candidates):
                        monitor_busy = _monitor_active(manifest.get("monitor_runs"))
                        if not monitor_busy:
                            while len(pending) < 2 and next_index < len(candidates):
                                candidate = candidates[next_index]
                                next_index += 1
                                pending[pool.submit(assess_symbol, candidate)] = candidate
                        status["phase"] = "waiting_for_monitor" if monitor_busy and not pending else "computing"
                        if pending:
                            done, _ = wait(pending, timeout=3, return_when=FIRST_COMPLETED)
                            for future in done:
                                pending.pop(future)
                                row = future.result()
                                output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                                output.flush()
                                status["completed"] += 1
                        else:
                            time.sleep(3)
                        status["updated_at"] = time.time()
                        write_json(directory / "status.json", status)
        status.update(status="completed", finished_at=time.time(), updated_at=time.time())
    except Exception as exc:
        status.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500],
                      updated_at=time.time())
        raise
    finally:
        write_json(directory / "status.json", status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_job(args.run_dir)


if __name__ == "__main__":
    main()
