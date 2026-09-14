"""Bounded background screening. No scheduler, chart-cache warmup or order path."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import gzip
import hashlib
import json
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
from pathlib import Path
import re
import time

from chanlun.screening.rules import CN, POINT_TYPES, audit_snapshot, frame_gaps, frame_quality, trading_context, validate_frame
from chanlun.screening.cache import (
    REPLAY_CHECKS, calculation_key, candidate_signature, input_fingerprint,
    read_calculation, write_calculation,
)
from chanlun.screening.sessions import suspension_evidence
from chanlun.screening.confirmation import confirmation_catalog
from chanlun.screening.nesting import (
    STRATEGY, attach_confirmation, is_nested_observation, matching_confirmations,
)

import requests

_EXCHANGE = None
SYMBOL_TIMEOUT_SECONDS = 180
CATALOG_TIMEOUT_SECONDS = 60


class ScreeningCancelled(Exception):
    """Cancellation is a terminal state, not a failed data calculation."""


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    # Windows readers / virus scanners can briefly deny replacement even
    # though the destination is writable. Preserve the old complete state
    # and retry the atomic rename; never truncate the published JSON file.
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(min(0.025 * 2 ** attempt, 0.4))


def source_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    project = root.parents[1]
    # Fingerprint complete runtime packages, including transitive price, time,
    # serialization and vendored market-data helpers. A handpicked entry-file
    # list silently missed changes to those dependencies.
    files = [*root.rglob("*.py"), *root.parent.joinpath("xtquant").rglob("*.py"),
             *root.joinpath("exchange/data").glob("a_share*.json"),
             project / "pyproject.toml", project / "poetry.lock"]
    digest = hashlib.sha256()
    for file in sorted(files):
        digest.update(file.relative_to(project).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def is_a_share(stock: dict) -> bool:
    code = stock.get("code", "")
    return (stock.get("type") == "stock_cn" and bool(re.fullmatch(
        r"(?:SH\.(?:60[0135]|68[89])\d{3}|SZ\.(?:00[0123]|30[01])\d{3}|BJ\.\d{6})", code)))


def snapshot_for(frame, code, frequency):
    from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
    from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart
    runtime = build_strict_chart_cd(market="a", code=code, frequency=frequency, frame=frame)
    if runtime.cd is None:
        raise ValueError(f"{runtime.error_code}: {runtime.error_message}")
    chart = cl_data_to_tv_chart(frame, {"chart_show_bi": "1", "chart_show_xd": "1", "chart_show_fx": "1"},
                               market="a", code=code, frequency=frequency, strict_runtime=runtime)
    if chart.get("strict_structure_mode") != "replace":
        raise ValueError(f"chart evidence unavailable: {chart.get('strict_structure_error')}")
    snapshot = chart["strict_structure"]
    snapshot["source_started_at"] = int(frame.date.iloc[0].timestamp())
    # Persist the exact display geometry from the screening runtime. Evidence
    # views must not rebuild historical lines with a later algorithm or input.
    snapshot["chart"] = {k: v for k, v in chart.items()
                         if k in ("fxs", "bis", "xds") or k.startswith(("macd_", "higher_macd_"))}
    evidence = runtime.cd.get_strict_evidence()
    levels = {level.structural_level: level for level in evidence.structure.levels}
    # Selection lifetime changes after point confirmation. Keep this causal
    # segment chain outside the immutable point/parent replay evidence.
    snapshot["screening_segments"] = [
        {"unit_id": u.unit_id, "direction": u.direction,
         "start_time": int(u.market_start.timestamp()), "end_time": int(u.market_end.timestamp()),
         "start_tick": u.start_tick, "end_tick": u.end_tick,
         "locked": u.locked, "forming": u.forming,
         "confirmed_at": None if u.confirmed_at is None else int(u.confirmed_at.timestamp())}
        for u in levels[0].units
    ]
    points = {p.point_id: p for p in (*evidence.confirmed_points, *evidence.approaching_points)}
    starts = {}

    def dependency_start(point):
        if point.point_id in starts:
            return starts[point.point_id]
        level = levels[point.structural_level]
        units = {unit.unit_id: unit for unit in level.units}
        identifiers = {point.anchor_unit_id, point.price_anchor_unit_id}
        if point.divergence:
            identifiers.update(point.divergence.compare_leg_unit_ids)
            identifiers.update(point.divergence.signal_leg_unit_ids)
        start = min(units[identifier].market_start for identifier in identifiers)
        # Completed movement context is part of the trend-divergence proof.
        for trend in level.completed_trends:
            if point.divergence and trend.terminal_divergence == point.divergence:
                start = min(start, trend.market_start)
        chart_level = next(l for l in snapshot["levels"] if l["structural_level"] == point.structural_level)
        center = next((c for c in chart_level["centers"] if c["center_id"] == point.center_id), None)
        epoch = int(start.timestamp())
        if center:
            for role in center.get("establishment_segments", []):
                epoch = min(epoch, role["start_time"])
        if point.parent_point_id:
            epoch = min(epoch, dependency_start(points[point.parent_point_id]))
        starts[point.point_id] = epoch
        return epoch

    for level in snapshot["levels"]:
        for point in level["points"]:
            point["dependency_from"] = dependency_start(points[point["point_id"]])
    return snapshot


def _exchange():
    global _EXCHANGE
    if _EXCHANGE is None:
        from chanlun.exchange.exchange_qmt import ExchangeQMT
        _EXCHANGE = ExchangeQMT()
    return _EXCHANGE


def _resolve_suspensions(frame, code, context):
    if frame is None or frame.empty or any(e not in {"STALE_DATA", "NO_VOLUME"} for e in validate_frame(frame, context)):
        return frame
    try:
        missing = frame_gaps(frame, context, int(frame.date.iloc[0].timestamp()), raw=True)
        if not missing:
            return frame
        evidence = suspension_evidence(code, missing[0], context["cutoff"])
        frame.attrs.pop("screening_suspensions", None)
        frame.attrs.pop("_screening_session_error", None)
        if evidence["intervals"]:
            frame.attrs["screening_suspensions"] = evidence
    except (requests.RequestException, ValueError, OSError) as exc:
        frame.attrs.pop("screening_suspensions", None)
        frame.attrs["_screening_session_error"] = f"{type(exc).__name__}: {exc}"
    return frame


def fetch_frame(code, frequency, context, *, repair_from=None):
    end = datetime.fromtimestamp(context["cutoff"], CN).strftime("%Y-%m-%d %H:%M:%S")
    exchange = _exchange()
    # Preserve the adapter's normal structural lookback, but extend it when a
    # trading-day setting exceeds that natural-day default (notably 1m/60 days).
    start = min(exchange.get_start_date_by_frequency(frequency)[:8],
                context["history_from"].replace("-", ""))
    if repair_from is not None:
        start = min(start, datetime.fromtimestamp(repair_from, CN).strftime("%Y%m%d"))
        frame = exchange.klines(code, frequency, start_date=start, end_date=end,
                                args={"exact_end": True})
        return _resolve_suspensions(frame, code, context)
    args = {"exact_end": True, "skip_download": True}
    frame = exchange.klines(code, frequency, start_date=start, end_date=end, args=args)
    errors = validate_frame(frame, context)
    if not errors:
        frame = _resolve_suspensions(frame, code, context)
        quality = frame_quality(frame, context)
        if not quality["errors"] or "DATA_GAPS" not in quality["errors"]:
            return frame
    if errors or quality["errors"]:
        args = {"exact_end": True}
        if (set(errors) <= {"STALE_DATA", "NO_VOLUME"} and "STALE_DATA" in errors
                and frame is not None and len(frame) >= 200):
            previous = {**context, "cutoff": int(frame.date.iloc[-1].timestamp())}
            try:
                intact = not frame_gaps(frame, previous, int(frame.date.iloc[0].timestamp()), raw=True)
            except ValueError:
                intact = False
            if intact:
                # Download from the actual last day, including its partial tail.
                # The read still retains the full prefix, independent of today.
                args["download_start_date"] = frame.date.iloc[-1].astimezone(CN).strftime("%Y%m%d")
        frame = exchange.klines(code, frequency, start_date=start, end_date=end, args=args)
    return _resolve_suspensions(frame, code, context)


def _same_point(left, right):
    # Render identities include the whole snapshot revision and can change
    # when later, unrelated structures arrive. Trading evidence cannot change:
    # in particular equal point ids/prices do not prove equal divergence legs,
    # MACD conditions, source levels or parent lineage.
    presentation = {"schema", "render_kind", "render_id", "evidence_revision", "strict_status", "points",
                    "anchor_price", "invalidation_price", "center_zd_price", "center_zg_price"}

    def proof(point):
        values = {key: value for key, value in point.items() if key not in presentation}
        if isinstance(values.get("divergence"), dict):
            values["divergence"] = {key: value for key, value in values["divergence"].items()
                                    if key not in presentation}
        return values

    return bool(left.get("point_id") and right.get("point_id") and proof(left) == proof(right))


def _evidence_index(snapshot):
    return ({p["point_id"]: p for level in snapshot["levels"] for p in level.get("points", ())},
            {c["center_id"]: c for level in snapshot["levels"] for c in level.get("centers", ())})


def _same_point_graph(point, original, replayed):
    """Replay the point's full causal dependency graph, not its display label."""
    source_points, source_centers = original
    target_points, target_centers = replayed
    active, checked = set(), set()
    # Center lifecycle annotations may develop after an already confirmed
    # third point. Its entry/core/departure/return proof must remain identical.
    center_keys = ("center_id", "structural_level", "source_kind", "price_basis_revision", "core",
                   "entry_unit_id", "core_unit_ids", "establishment_leave_unit_id", "establishment_segments",
                   "third_class_confirmed", "completion_direction", "completion_leave_unit_id",
                   "completion_return_unit_id", "lifecycle_leaving_segment", "completion_return_segment",
                   "completed_at")

    def matches(current):
        identifier = current.get("point_id")
        if identifier in active:
            return False
        if identifier in checked:
            return True
        if not _same_point(current, target_points.get(identifier, {})):
            return False
        center_id = current.get("center_id")
        if center_id:
            left, right = source_centers.get(center_id), target_centers.get(center_id)
            if left is None or right is None or any(left.get(k) != right.get(k) for k in center_keys):
                return False
        active.add(identifier)
        dependencies = {*current.get("related_point_ids", ()), current.get("parent_point_id")} - {None}
        for dependency in dependencies:
            parent = source_points.get(dependency)
            if parent is None or not matches(parent):
                return False
        active.remove(identifier)
        checked.add(identifier)
        return True

    return matches(point)


def _review_candidates(snapshot, frame, candidates, code, frequency):
    """Every admitted result needs independent full and confirmation-prefix replay."""
    if not candidates:
        return [], []
    rebuilt = snapshot_for(frame.copy(), code, frequency)
    original_index, rebuilt_index = _evidence_index(snapshot), _evidence_index(rebuilt)
    original_confirmations, rebuilt_confirmations = confirmation_catalog(snapshot), confirmation_catalog(rebuilt)
    selected, rejected, prefixes = [], [], {}
    for candidate in candidates:
        point = candidate["point"]
        if (not _same_point_graph(point, original_index, rebuilt_index)
                or original_confirmations.get(point["point_id"]) != rebuilt_confirmations.get(point["point_id"])):
            candidate["reasons"].append("REBUILD_MISMATCH")
        at = point["available_at"]
        if at not in prefixes:
            prefix = frame.loc[frame.date <= datetime.fromtimestamp(at, CN)].copy()
            prefixes[at] = _evidence_index(snapshot_for(prefix, code, frequency))
        prefix_matches = _same_point_graph(point, original_index, prefixes[at])
        if not prefix_matches:
            candidate["reasons"].append("CONFIRMATION_REPLAY_FAILED")
        earlier = frame.loc[frame.date < datetime.fromtimestamp(at, CN)]
        before_at = None if earlier.empty else int(earlier.date.iloc[-1].timestamp())
        if before_at is not None and before_at not in prefixes:
            prefixes[before_at] = _evidence_index(snapshot_for(earlier.copy(), code, frequency))
        already_confirmed = before_at is not None and any(
            p["point_id"] == point["point_id"] and p["status"] == "confirmed"
            for p in prefixes[before_at][0].values() if p.get("structural_level") == 0)
        if already_confirmed:
            candidate["reasons"].append("CONFIRMATION_TIME_MISMATCH")
        candidate["audit"] = {"cold_rebuild": "REBUILD_MISMATCH" not in candidate["reasons"],
                              "confirmation_replay": prefix_matches,
                              "confirmation_boundary": prefix_matches and not already_confirmed}
        (rejected if candidate["reasons"] else selected).append(candidate)
    return selected, rejected


def _save_evidence(run_dir, code, frequency, frame, snapshot):
    base = Path(run_dir, "evidence", f"{code}_{frequency}")
    base.parent.mkdir(parents=True, exist_ok=True)
    parquet, packed = Path(str(base) + ".parquet"), Path(str(base) + ".json.gz")
    temp_parquet, temp_packed = Path(str(parquet) + ".tmp"), Path(str(packed) + ".tmp")
    try:
        frame.to_parquet(temp_parquet, index=False)
        temp_packed.write_bytes(gzip.compress(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")))
        os.replace(temp_parquet, parquet)
        os.replace(temp_packed, packed)
        return {"status": "complete", "chart_saved": bool(snapshot.get("chart")), "parquet_bytes": parquet.stat().st_size,
                "snapshot_bytes": packed.stat().st_size,
                "parquet_sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "snapshot_sha256": hashlib.sha256(packed.read_bytes()).hexdigest(),
                "input_fingerprint": input_fingerprint(frame, code, frequency)}
    finally:
        temp_parquet.unlink(missing_ok=True)
        temp_packed.unlink(missing_ok=True)


def scan_symbol(stock, settings, contexts, run_dir, *, cache_root=None, revision=None,
                _confirmation_only=False, _inputs=None):
    if settings.get("strategy") == STRATEGY and not _confirmation_only:
        return _scan_nested_symbol(stock, settings, contexts, run_dir, cache_root=cache_root, revision=revision)
    started = time.monotonic()
    rows = []
    code = stock["code"]
    for frequency in settings["frequencies"]:
        if Path(run_dir, "cancel").exists():
            break
        row = {"code": code, "name": stock["name"], "frequency": frequency,
               "selected": [], "observations": [], "recent_rejections": [], "reason_counts": {},
               "confirmed_buys": 0, "unconfirmed_buys": 0, "confirmed_sells": 0, "unconfirmed_sells": 0}
        context = {**contexts[frequency], "symbol": code}
        phase = "compute"
        try:
            frame = fetch_frame(code, frequency, context)
            quality = frame_quality(frame, context)
            errors = quality["errors"]
            row.update(bars=0 if frame is None else len(frame), data_errors=errors, data_quality=quality,
                       source_closed_at=None if frame is None or frame.empty else int(frame.date.iloc[-1].timestamp()))
            if errors:
                rows.append(row)
                continue
            cache_path = None if cache_root is None else Path(cache_root, f"{code}_{frequency}.json.gz")
            key = None if cache_path is None else calculation_key(frame, code, frequency, revision or source_revision())
            cached = None if cache_path is None else read_calculation(cache_path, key)
            snapshot = cached["snapshot"] if cached is not None else snapshot_for(frame, code, frequency)
            reviewed = cached["reviewed"] if cached is not None else {}
            result = audit_snapshot(snapshot, frame, context, settings["point_types"],
                                    settings.get("max_anchor_gain_pct", 10),
                                    include_weak_second=settings.get("include_weak_second", False),
                                    as_confirmation=_confirmation_only)
            if result["data_errors"]:
                row.update(data_errors=result["data_errors"], data_quality=result["data_quality"])
                rows.append(row)
                continue
            reused, pending = [], []
            for candidate in ([] if _confirmation_only else result["selected"]):
                checks = reviewed.get(candidate_signature(candidate))
                if checks is not None:
                    candidate["audit"] = dict(checks)
                    reused.append(candidate)
                else:
                    pending.append(candidate)
            selected, audit_rejected = _review_candidates(snapshot, frame, pending, code, frequency)
            for candidate in selected:
                if all(candidate.get("audit", {}).get(k) is True for k in REPLAY_CHECKS):
                    reviewed[candidate_signature(candidate)] = candidate["audit"]
            selected = result["selected"] if _confirmation_only else reused + selected
            rejected = result["rejected"] + audit_rejected
            row.update(
                confirmed_buys=result["confirmed_buys"],
                unconfirmed_buys=result["unconfirmed_buys"],
                confirmed_sells=result["confirmed_sells"],
                unconfirmed_sells=result["unconfirmed_sells"],
                reason_counts=dict(Counter(reason for item in rejected for reason in item["reasons"])),
                snapshot_revision=snapshot["snapshot_revision"],
                price_basis_revision=snapshot["price_basis_revision"],
                input_fingerprint=input_fingerprint(frame, code, frequency),
            )
            # Keep concise evidence for every recent rejected signal; don't
            # duplicate years of historical centers for the entire universe.
            row["recent_rejections"] = [
                {k: v for k, v in item.items() if k != "center"}
                for item in rejected if "OLD_CONFIRMATION" not in item["reasons"]]
            observations = result["observations"]
            if not _confirmation_only and (selected or observations or audit_rejected):
                phase = "evidence"
                row["evidence"] = _save_evidence(run_dir, code, frequency, frame, snapshot)
            row["selected"] = selected
            row["observations"] = observations
            row["calculation_reused"] = cached is not None
            row["replays_reused"] = len(reused)
            if _inputs is not None:
                _inputs[frequency] = {"frame": frame, "snapshot": snapshot, "reviewed": reviewed,
                                      "cache_path": cache_path, "key": key}
            if cache_path is not None and (cached is None or pending):
                try:
                    write_calculation(cache_path, key, row, snapshot, reviewed)
                except OSError:
                    # Optional reuse storage cannot undo successfully persisted
                    # evidence or turn a usable result into an engine failure.
                    pass
        except Exception as exc:
            row.update(selected=[], observations=[], data_errors=["EVIDENCE_WRITE_FAILED" if phase == "evidence" else "ENGINE_ERROR"],
                       error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
    return {"code": code, "name": stock["name"], "rows": rows,
            "seconds": round(time.monotonic() - started, 3)}


def _scan_nested_symbol(stock, settings, contexts, run_dir, *, cache_root=None, revision=None):
    """Calculate 1m only for usable 5m setups; publish one joined 5m result."""
    started = time.monotonic()
    inputs = {}
    main_settings = {**settings, "strategy": None, "frequencies": ["5m"]}
    result = scan_symbol(stock, main_settings, contexts, run_dir, cache_root=cache_root,
                         revision=revision, _inputs=inputs)
    if not result["rows"]:
        return result
    row = result["rows"][0]
    row["strategy"] = STRATEGY
    candidates = [*row["selected"], *row.get("observations", [])]
    if not candidates or Path(run_dir, "cancel").exists():
        return result
    try:
        lower_settings = {**main_settings, "frequencies": ["1m"], "point_types": list(POINT_TYPES)}
        lower_result = scan_symbol(stock, lower_settings, contexts, run_dir, cache_root=cache_root,
                                   revision=revision, _confirmation_only=True, _inputs=inputs)
        lower_row = next(iter(lower_result["rows"]), {})
        if lower_row.get("data_errors") or "1m" not in inputs:
            row.update(selected=[], observations=[], data_errors=["LOWER_DATA_ERROR"],
                       confirmation_data_errors=lower_row.get("data_errors", ["ENGINE_ERROR"]),
                       confirmation_data_quality=lower_row.get("data_quality"), error=lower_row.get("error"))
            return result
        lower_input = inputs["1m"]
        lower_signals = [*lower_row["selected"], *lower_row.get("observations", [])]
        bindings, to_review, failures = [], {}, []
        for candidate in candidates:
            try:
                interval, matches = matching_confirmations(
                    inputs["5m"]["snapshot"], candidate["point"], lower_input["snapshot"], lower_signals)
            except (ValueError, KeyError, ArithmeticError) as exc:
                failures.append({**candidate, "reasons": ["NESTED_INTERVAL_INVALID"], "error": str(exc)})
                continue
            lower = matches[0] if matches else None
            bindings.append((candidate, interval, lower))
            if lower is not None and lower["point"]["status"] == "confirmed":
                checks = lower_input["reviewed"].get(candidate_signature(lower))
                if checks:
                    lower["audit"] = dict(checks)
                else:
                    to_review[lower["point"]["point_id"]] = lower
        reviewed, rejected = _review_candidates(lower_input["snapshot"], lower_input["frame"],
                                               list(to_review.values()), stock["code"], "1m")
        failed_ids = {c["point"]["point_id"] for c in rejected}
        for candidate in reviewed:
            lower_input["reviewed"][candidate_signature(candidate)] = dict(candidate["audit"])
        selected, observations = [], []
        for candidate, interval, lower in bindings:
            if lower is not None and lower["point"]["point_id"] in failed_ids:
                failures.append({**candidate, "reasons": ["LOWER_REPLAY_FAILED"],
                                 "confirmation_rejections": lower.get("reasons", [])})
                continue
            joined = attach_confirmation(candidate, interval, lower)
            if not joined["reasons"]:
                selected.append(joined)
            elif is_nested_observation(joined):
                observations.append({**joined, "observation_validation": "checked"})
        counts = Counter(row["reason_counts"])
        for candidate in candidates:
            counts.subtract(candidate.get("reasons", []))
        for candidate in [*failures, *observations]:
            counts.update(candidate["reasons"])
        ids = {c["point"]["point_id"] for c in candidates}
        row.update(selected=selected, observations=observations,
                   reason_counts={k: v for k, v in counts.items() if v > 0},
                   recent_rejections=[r for r in row["recent_rejections"] if r["point"]["point_id"] not in ids]
                                     + failures + observations,
                   confirmation_bars=lower_row.get("bars"),
                   confirmation_source_closed_at=lower_input["snapshot"]["source_closed_at"])
        if selected or observations:
            lower_input["snapshot"]["screening_interval_signals"] = [
                {"point": c["point"], "audit": c.get("audit", {}), "reasons": c.get("reasons", [])}
                for c in lower_signals]
            row["confirmation_evidence"] = _save_evidence(
                run_dir, stock["code"], "1m", lower_input["frame"], lower_input["snapshot"])
        if lower_input["cache_path"] is not None and to_review:
            try:
                # The cache contains one-period structure; the association is
                # checked afresh against both inputs on every scan.
                lower_input["snapshot"].pop("screening_interval_signals", None)
                write_calculation(lower_input["cache_path"], lower_input["key"], lower_row,
                                  lower_input["snapshot"], lower_input["reviewed"])
            except OSError:
                pass
    except Exception as exc:
        row.update(selected=[], observations=[], data_errors=["NESTED_CONFIRMATION_ERROR"],
                   error=f"{type(exc).__name__}: {exc}")
    finally:
        result["seconds"] = round(time.monotonic() - started, 3)
    return result


def _worker_main(connection, settings, contexts, run_dir, revision):
    try:
        while True:
            stock = connection.recv()
            if stock is None:
                return
            try:
                result = scan_symbol(stock, settings, contexts, run_dir,
                                     cache_root=Path(run_dir).parent / "analysis_cache", revision=revision)
            except Exception as exc:
                result = {"code": stock["code"], "name": stock["name"], "rows": [],
                          "error": f"{type(exc).__name__}: {exc}"}
            connection.send(result)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


def _stop_worker(slot):
    if slot.get("stopped"):
        return
    slot["stopped"] = True
    process, connection = slot["process"], slot["connection"]
    if process.is_alive() and slot["stock"] is None:
        try:
            connection.send(None)
        except (OSError, EOFError):
            pass
    connection.close()
    process.join(timeout=0.25)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    process.close()


def bounded_scan(stocks, settings, contexts, run_dir, workers, revision, *,
                 timeout_seconds=SYMBOL_TIMEOUT_SECONDS, worker_target=_worker_main):
    """Persistent isolated workers with a deadline for each assigned symbol.

    A blocked native QMT call is terminated with its worker; other workers and
    completed records survive. Cancellation does not wait for that native call.
    Yield heartbeats so progress remains readable while a job is slow.
    """
    ctx = multiprocessing.get_context("spawn")
    slots, pending, exhausted = [], iter(stocks), False

    def start_worker():
        parent, child = ctx.Pipe()
        process = ctx.Process(target=worker_target, args=(child, settings, contexts, str(run_dir), revision))
        process.start()
        child.close()
        return {"process": process, "connection": parent, "stock": None, "started": 0., "stopped": False}

    try:
        if Path(run_dir, "cancel").exists():
            return
        for _ in range(workers):
            slots.append(start_worker())
        while True:
            if Path(run_dir, "cancel").exists():
                return
            for slot in slots:
                if slot["stock"] is None and not exhausted:
                    stock = next(pending, None)
                    if stock is None:
                        exhausted = True
                        break
                    if not slot["process"].is_alive():
                        _stop_worker(slot)
                        slot.update(start_worker())
                    slot["connection"].send(stock)
                    slot.update(stock=stock, started=time.monotonic())
            active = [s for s in slots if s["stock"] is not None]
            if not active:
                return
            ready = wait_connections([s["connection"] for s in active], timeout=min(2, timeout_seconds))
            now = time.monotonic()
            for slot in active:
                result, failed = None, False
                stock = slot["stock"]
                if slot["connection"] in ready:
                    try:
                        result = slot["connection"].recv()
                    except (EOFError, OSError):
                        failed = True
                timed_out = now - slot["started"] >= timeout_seconds
                if result is None and (failed or timed_out or not slot["process"].is_alive()):
                    reason = "WORKER_TIMEOUT" if timed_out else "ENGINE_ERROR"
                    result = {"code": stock["code"], "name": stock["name"], "rows": [
                        {"code": stock["code"], "name": stock["name"], "frequency": f,
                         "selected": [], "recent_rejections": [], "reason_counts": {}, "data_errors": [reason]}
                        for f in settings["frequencies"]]}
                    _stop_worker(slot)
                    slot.update(start_worker())
                if result is not None:
                    slot["stock"] = None
                    yield result, [s["stock"]["code"] for s in slots if s["stock"]]
            yield None, [s["stock"]["code"] for s in slots if s["stock"]]
    finally:
        for slot in slots:
            _stop_worker(slot)


def _catalog_worker(connection, settings, _run_dir):
    try:
        requested = set(settings.get("codes", []))
        if settings.get("scope") == "all_a":
            stocks = _exchange().all_stocks(full_market_authorized=True)
        elif settings.get("scope") == "codes" and requested:
            stocks = [{**info, "type": "stock_cn"} for code in sorted(requested)
                      if (info := _exchange().stock_info(code))]
        else:
            raise ValueError("选股范围必须明确指定")
        stocks = sorted((s for s in stocks if is_a_share(s)
                         and (not requested or s["code"] in requested)
                         and not (settings["exclude_st"] and ("ST" in s["name"].upper() or "退" in s["name"]))),
                        key=lambda s: s["code"])
        if not stocks:
            raise ValueError("所选范围没有符合股票类别条件的标的")
        connection.send({"stocks": stocks,
                         "excluded_requested_codes": sorted(requested - {s["code"] for s in stocks})})
    except Exception as exc:
        connection.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


def bounded_catalog(settings, run_dir, *, timeout_seconds=CATALOG_TIMEOUT_SECONDS,
                    worker_target=_catalog_worker):
    """Yield catalog heartbeats while native symbol discovery remains killable."""
    if Path(run_dir, "cancel").exists():
        raise ScreeningCancelled()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=worker_target, args=(child, settings, str(run_dir)))
    process.start()
    child.close()
    slot = {"process": process, "connection": parent, "stock": "catalog"}
    started = time.monotonic()
    try:
        while True:
            if Path(run_dir, "cancel").exists():
                raise ScreeningCancelled()
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"股票目录读取超过 {timeout_seconds:g} 秒，请检查行情连接后重试")
            if parent.poll(min(1, remaining)):
                try:
                    result = parent.recv()
                except EOFError as exc:
                    raise RuntimeError("股票目录读取进程异常退出") from exc
                if result.get("error"):
                    raise RuntimeError(result["error"])
                yield result
                return
            if not process.is_alive():
                raise RuntimeError("股票目录读取进程异常退出")
            yield None
    finally:
        _stop_worker(slot)


def run_screening(run_dir: Path):
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    settings = request["settings"]
    observed = datetime.fromisoformat(request["observed_at"])
    state = {**request, "status": "running", "phase": "catalog", "completed": 0,
             "total": 0, "selected_count": 0, "error_count": 0, "started_at": time.time(),
             "source_revision": source_revision(), "worker_pid": os.getpid()}
    write_json(run_dir / "status.json", state)
    try:
        contexts = {f: trading_context(observed, f, settings["recent_sessions"],
                                      settings["max_anchor_sessions"])
                    for f in settings["frequencies"]}
        if settings.get("strategy") == STRATEGY:
            # A closed 5m setup cannot borrow newer 1m information from the
            # following, still-open five-minute candle.
            contexts["1m"] = trading_context(datetime.fromtimestamp(contexts["5m"]["cutoff"], CN),
                                             "1m", settings["recent_sessions"], settings["max_anchor_sessions"])
        catalog = None
        for result in bounded_catalog(settings, run_dir):
            state.update(updated_at=time.time(), elapsed_seconds=round(time.time()-state["started_at"], 1))
            write_json(run_dir / "status.json", state)
            if result is not None:
                catalog = result
        if catalog is None or (run_dir / "cancel").exists():
            raise ScreeningCancelled()
        stocks = catalog["stocks"]
        state["excluded_requested_codes"] = catalog["excluded_requested_codes"]
        exchanges = dict(Counter(s["code"].split(".")[0] for s in stocks))
        state.update(total=len(stocks), phase="screening",
                     cutoffs={f: c["cutoff"] for f, c in contexts.items()},
                     calendar_source=next(iter(contexts.values()))["calendar_source"],
                     universe_coverage={"source": "QMT", "exchanges": exchanges,
                         "absent_exchanges": [e for e in ("SH", "SZ", "BJ") if e not in exchanges],
                         "scope_label": "行情源 A 股股票池" if settings["scope"] == "all_a" else "指定股票"})
        write_json(run_dir / "universe.json", stocks)
        write_json(run_dir / "status.json", state)
        workers = min(settings.get("workers", 4), max(1, (os.cpu_count() or 2) - 2), 6)
        for result, current_codes in bounded_scan(stocks, settings, contexts, run_dir, workers, state["source_revision"]):
            if result is not None:
                with (run_dir / "results.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                state["completed"] += 1
                state["selected_count"] += int(any(r["selected"] for r in result["rows"]))
                state["error_count"] += int(bool(result.get("error")) or any(r.get("data_errors") for r in result["rows"]))
                state["reused_combinations"] = state.get("reused_combinations", 0) + sum(bool(r.get("calculation_reused")) for r in result["rows"])
            state.update(current_codes=current_codes,
                         elapsed_seconds=round(time.time() - state["started_at"], 1), updated_at=time.time())
            write_json(run_dir / "status.json", state)
        state["status"] = "cancelled" if (run_dir / "cancel").exists() else "completed"
        state["phase"] = "finished"
    except ScreeningCancelled:
        state.update(status="cancelled", phase="finished")
    except Exception as exc:
        state.update(status="failed", phase="finished", error=f"{type(exc).__name__}: {exc}")
    state.update(current_codes=[], finished_at=time.time(), elapsed_seconds=round(time.time() - state["started_at"], 1))
    write_json(run_dir / "status.json", state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    run_screening(parser.parse_args().run_dir.resolve())
