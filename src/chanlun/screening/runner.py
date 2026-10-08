"""Bounded background screening. No scheduler, chart-cache warmup or order path."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
import json
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
from pathlib import Path
import re
import time
import traceback

from chanlun.screening.rules import CN, POINT_TYPES, audit_native_center_signals, audit_snapshot, frame_gaps, frame_quality, trading_context, validate_frame
from chanlun.screening.cache import (
    REPLAY_CHECKS, calculation_key, candidate_signature, input_fingerprint,
    read_calculation, write_calculation,
)
from chanlun.screening.sessions import suspension_evidence
from chanlun.screening.confirmation import confirmation_catalog, is_confirmed
from chanlun.screening.errors import MarketDataContractError
from chanlun.core.strict_structure.operational_risk import apply_declared_minimum_operating_frequency
from chanlun.screening.nesting import (
    STRATEGY, attach_confirmation, is_nested_observation, matching_confirmations,
)
from chanlun.screening.lower_first_timeline import observe_independent_lower_structural_witnesses

import requests

_EXCHANGE = None
DIRECT_STRATEGY = "direct_structural"
SYMBOL_TIMEOUT_SECONDS = 180
CATALOG_TIMEOUT_SECONDS = 60


class ScreeningCancelled(Exception):
    """Cancellation is a terminal state, not a failed data calculation."""


def write_json(path: Path, value, *, durable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False))
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    # Windows readers / virus scanners can briefly deny replacement even
    # though the destination is writable. Preserve the old complete state
    # and retry the atomic rename; never truncate the published JSON file.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 19:
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


def snapshot_for(frame, code, frequency, *, include_consolidation=False):
    """Snapshot the completed, end-labelled screening input (including replays)."""
    from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
    from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart
    market = frame.attrs.get("screening_market", "a")
    runtime = build_strict_chart_cd(
        market=market, code=code, frequency=frequency, frame=frame, last_bar_closed=True,
    )
    if runtime.cd is None:
        raise ValueError(f"{runtime.error_code}: {runtime.error_message}")
    chart = cl_data_to_tv_chart(frame, {"chart_show_bi": "1", "chart_show_xd": "1", "chart_show_fx": "1"},
                               market=market, code=code, frequency=frequency, strict_runtime=runtime)
    if chart.get("strict_structure_mode") != "replace":
        raise ValueError(f"chart evidence unavailable: {chart.get('strict_structure_error')}")
    snapshot = chart["strict_structure"]
    snapshot["source_started_at"] = int(frame.date.iloc[0].timestamp())
    # Persist the exact display geometry from the screening runtime. Evidence
    # views must not rebuild historical lines with a later algorithm or input.
    snapshot["chart"] = {k: v for k, v in chart.items()
                         if k in ("fxs", "bis", "xds") or k.startswith(("macd_", "higher_macd_"))}
    if snapshot.get("analysis_scope") != "native_centers":
        raise ValueError("retired recursive chart scope cannot enter screening")
    snapshot["screening_segments"] = [
        {"unit_id": unit.unit_id, "direction": unit.direction,
         "source_kind": unit.source_kind.value, "structural_level": unit.structural_level,
         "price_basis_revision": unit.price_basis_revision,
         "start_time": int(unit.market_start.timestamp()),
         "end_time": int(unit.market_end.timestamp()),
         "start_tick": unit.start_tick, "end_tick": unit.end_tick,
         "low_tick": unit.low_tick, "high_tick": unit.high_tick,
         "locked": unit.locked, "forming": unit.forming,
         "confirmed_at": None if unit.confirmed_at is None else int(unit.confirmed_at.timestamp()),
         "available_at": int(unit.available_at.timestamp())}
        for unit in runtime.cd.get_segment_units()
    ]
    snapshot["screening_units_by_level"] = {}
    if include_consolidation:
        from chanlun.core.strict_structure.native_consolidation import build_native_consolidation_alerts
        from chanlun.core.strict_structure.native_consolidation_preview import build_native_consolidation_previews
        from chanlun.core.strict_structure.strength import MacdStrengthProvider
        quantum = Decimal(str(snapshot["structure_price_quantum"]))
        try:
            consolidation_strength = MacdStrengthProvider(runtime.cd)
        except (AttributeError, TypeError, ValueError) as exc:
            consolidation_strength = None
            snapshot["native_consolidation_error"] = f"MACD evidence unavailable: {type(exc).__name__}"
        alerts = build_native_consolidation_alerts(
            tuple(runtime.cd.get_segment_units()), tuple(runtime.cd.get_native_centers().centers),
            consolidation_strength, as_of=runtime.cd._strict_as_of(),
        )
        try:
            preview_checks = {}
            alerts.extend(build_native_consolidation_previews(
                tuple(runtime.cd.get_segment_units()), tuple(runtime.cd.get_native_centers().centers),
                consolidation_strength, frame, runtime.cd.get_idx()["macd"]["hist"],
                as_of=runtime.cd._strict_as_of(), diagnostics=preview_checks,
            ))
            snapshot["native_consolidation_checks"] = preview_checks
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            snapshot["native_consolidation_error"] = f"Preview evidence unavailable: {type(exc).__name__}"
        snapshot["native_consolidation_alerts"] = [
            {**alert, "anchor_price": float(alert["anchor_tick"] * quantum)} for alert in alerts
        ]
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
    if context.get("market", "a") != "a":
        from chanlun.screening.markets import external_frame
        return external_frame(code, frequency, context)
    closed_at = datetime.fromtimestamp(context["cutoff"], CN)
    end = closed_at.strftime("%Y-%m-%d %H:%M:%S")
    exchange = _exchange()
    # Preserve the adapter's normal structural lookback, but extend it when a
    # trading-day setting exceeds that natural-day default (notably 1m/60 days).
    # Both ends must use the same closed market clock. Wall-clock midnight or
    # holidays cannot truncate an unchanged input and alter existing signals.
    start = min(exchange.get_start_date_by_frequency(frequency, as_of=closed_at)[:8],
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
    grades = {candidate["point"]["structural_level"] for candidate in candidates}
    original_confirmations = {
        point_id: record for grade in grades
        for point_id, record in confirmation_catalog(snapshot, grade).items()
    }
    rebuilt_confirmations = {
        point_id: record for grade in grades
        for point_id, record in confirmation_catalog(rebuilt, grade).items()
    }
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
            for p in prefixes[before_at][0].values()
            if p.get("structural_level") == point["structural_level"])
        if already_confirmed:
            candidate["reasons"].append("CONFIRMATION_TIME_MISMATCH")
        candidate["audit"] = {"cold_rebuild": "REBUILD_MISMATCH" not in candidate["reasons"],
                              "confirmation_replay": prefix_matches,
                              "confirmation_boundary": prefix_matches and not already_confirmed}
        (rejected if candidate["reasons"] else selected).append(candidate)
    return selected, rejected


def _review_native_center_signals(snapshot, frame, candidates, code, frequency):
    """Cold and prefix replay for the declared one-period signal policy."""

    if not candidates:
        return [], []
    rebuilt = snapshot_for(frame.copy(), code, frequency)
    if rebuilt.get("analysis_scope") != "native_centers":
        raise ValueError("native center replay returned another analysis scope")
    selected, rejected, prefixes = [], [], {}

    def matches(candidate, source):
        point = candidate["point"]
        level = source["levels"][0]
        copied = next((item for item in level["points"]
                       if item["point_id"] == point["point_id"]), None)
        if copied != point:
            return False
        center = next((item for item in level["centers"]
                       if item["center_id"] == point["center_id"]), None)
        original = candidate.get("center") or {}
        immutable = ("center_id", "price_basis_revision", "core", "entry_unit_id",
                     "core_unit_ids", "establishment_leave_unit_id")
        return center is not None and all(center.get(key) == original.get(key)
                                          for key in immutable)

    for candidate in candidates:
        point = candidate["point"]
        if not matches(candidate, rebuilt):
            candidate["reasons"].append("REBUILD_MISMATCH")
        known_at = point["available_at"]
        if known_at not in prefixes:
            prefix = frame.loc[frame.date <= datetime.fromtimestamp(known_at, CN)].copy()
            prefixes[known_at] = snapshot_for(prefix, code, frequency)
        prefix_matches = matches(candidate, prefixes[known_at])
        if not prefix_matches:
            candidate["reasons"].append("CONFIRMATION_REPLAY_FAILED")
        earlier = frame.loc[frame.date < datetime.fromtimestamp(known_at, CN)]
        before_at = None if earlier.empty else int(earlier.date.iloc[-1].timestamp())
        if before_at is not None and before_at not in prefixes:
            prefixes[before_at] = snapshot_for(earlier.copy(), code, frequency)
        already_confirmed = before_at is not None and any(
            item["point_id"] == point["point_id"]
            for item in prefixes[before_at]["levels"][0]["points"]
        )
        if already_confirmed:
            candidate["reasons"].append("CONFIRMATION_TIME_MISMATCH")
        candidate["audit"] = {
            "cold_rebuild": "REBUILD_MISMATCH" not in candidate["reasons"],
            "confirmation_replay": prefix_matches,
            "confirmation_boundary": prefix_matches and not already_confirmed,
        }
        (rejected if candidate["reasons"] else selected).append(candidate)
    return selected, rejected


def _review_consolidation_alerts(snapshot, frame, candidates, code, frequency):
    if not candidates:
        return [], []
    rebuilt = snapshot_for(frame.copy(), code, frequency, include_consolidation=True)
    prefixes, selected, rejected = {}, [], []
    catalog = lambda source: {alert["alert_id"]: alert for alert in source.get("native_consolidation_alerts", [])}
    full = catalog(rebuilt)
    for candidate in candidates:
        alert = candidate["alert"]
        known = alert["available_at"]
        if alert.get("status") == "warning":
            candidate["audit"] = {"cold_rebuild": full.get(alert["alert_id"]) == alert,
                                  "closed_bar_replay": known == snapshot["source_closed_at"]}
            (selected if all(candidate["audit"].values()) else rejected).append(candidate)
            if not all(candidate["audit"].values()):
                candidate["reasons"] = ["CONSOLIDATION_EVIDENCE_MISSING"]
            continue
        if known not in prefixes:
            prefix = frame.loc[frame.date <= datetime.fromtimestamp(known, CN)].copy()
            prefixes[known] = catalog(snapshot_for(prefix, code, frequency, include_consolidation=True))
        before = frame.loc[frame.date < datetime.fromtimestamp(known, CN)]
        previous = None if before.empty else int(before.date.iloc[-1].timestamp())
        if previous is not None and previous not in prefixes:
            prefixes[previous] = catalog(snapshot_for(before.copy(), code, frequency, include_consolidation=True))
        candidate["audit"] = {
            "cold_rebuild": full.get(alert["alert_id"]) == alert,
            "confirmation_replay": prefixes[known].get(alert["alert_id"]) == alert,
            "confirmation_boundary": previous is None or alert["alert_id"] not in prefixes[previous],
        }
        if all(candidate["audit"].values()):
            selected.append(candidate)
        else:
            candidate["reasons"] = ["CONSOLIDATION_EVIDENCE_MISSING"]
            rejected.append(candidate)
    return selected, rejected


def _select_consolidation_alerts(snapshot, frame, context, reviewed, code, frequency):
    from chanlun.screening.rules import audit_native_consolidation_alerts
    if snapshot.get("native_consolidation_error"):
        raise ValueError(snapshot["native_consolidation_error"])
    candidates = audit_native_consolidation_alerts(snapshot, frame, context)
    reused, pending = [], []
    signature = lambda item: ("consolidation-preview:" if item["alert"].get("status") == "warning" else "consolidation:") + hashlib.sha256(json.dumps(item["alert"], sort_keys=True).encode()).hexdigest()
    for candidate in candidates:
        checks = reviewed.get(signature(candidate))
        if checks is None:
            pending.append(candidate)
        else:
            candidate["audit"] = dict(checks)
            reused.append(candidate)
    selected, rejected = (_review_consolidation_alerts(snapshot, frame, pending, code, frequency)
                          if pending else ([], []))
    for candidate in selected:
        reviewed[signature(candidate)] = candidate["audit"]
    return reused + selected, rejected, bool(pending)


def _save_evidence(run_dir, code, frequency, frame, snapshot):
    from chanlun.screening.markets import storage_symbol
    market = frame.attrs.get("screening_market", "a")
    base = Path(run_dir, "evidence", f"{storage_symbol(code, market)}_{frequency}")
    base.parent.mkdir(parents=True, exist_ok=True)
    parquet, packed = Path(str(base) + ".parquet"), Path(str(base) + ".json.gz")
    temp_parquet, temp_packed = Path(str(parquet) + ".tmp"), Path(str(packed) + ".tmp")
    try:
        frame.to_parquet(temp_parquet, index=False)
        # Level 3 keeps the same lossless format without spending most of the
        # persistence budget on maximum compression of frequently replaced data.
        temp_packed.write_bytes(gzip.compress(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8"),compresslevel=3))
        os.replace(temp_parquet, parquet)
        os.replace(temp_packed, packed)
        return {"status": "complete", "market": market, "chart_saved": bool(snapshot.get("chart")), "parquet_bytes": parquet.stat().st_size,
                "snapshot_bytes": packed.stat().st_size,
                "parquet_sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
                "snapshot_sha256": hashlib.sha256(packed.read_bytes()).hexdigest(),
                "input_fingerprint": input_fingerprint(frame, code, frequency)}
    finally:
        temp_parquet.unlink(missing_ok=True)
        temp_packed.unlink(missing_ok=True)


def scan_symbol(stock, settings, contexts, run_dir, *, cache_root=None, revision=None,
                _confirmation_only=False, _inputs=None, force_rebuild=False):
    structural_levels = tuple(settings.get("structural_levels", (0,)))
    if settings.get("strategy") == STRATEGY and structural_levels != (0,):
        raise ValueError("nested confirmation supports native structural level only")
    if settings.get("strategy") == STRATEGY and not _confirmation_only:
        return _scan_nested_symbol(stock, settings, contexts, run_dir, cache_root=cache_root, revision=revision,
                                   force_rebuild=force_rebuild)
    started = time.monotonic()
    rows = []
    code = stock["code"]
    include_consolidation = stock.get("holding") is True
    for frequency in settings["frequencies"]:
        if Path(run_dir, "cancel").exists():
            break
        row = {"code": code, "market": stock.get("market", "a"), "name": stock["name"], "frequency": frequency,
               "selected": [], "observations": [], "recent_rejections": [], "reason_counts": {},
               "confirmed_buys": 0, "unconfirmed_buys": 0, "confirmed_sells": 0, "unconfirmed_sells": 0}
        row["consolidation_checked"] = False
        if stock.get("holding_lookup_failed"):
            row["consolidation_error"] = "持仓名单读取失败，本周期盘整背驰尚未检查"
        context = {**contexts[frequency], "symbol": code}
        phase = "compute"
        period_started = time.monotonic()
        timings = {}
        row['processing_timeline'] = {'calculation_started_at': time.time()}
        try:
            if context.get('calendar_error'):
                raise MarketDataContractError('SESSION_CALENDAR_UNAVAILABLE', context['calendar_error'])
            frame = fetch_frame(code, frequency, context)
            row['processing_timeline']['fetched_at'] = time.time()
            timings['fetch'] = (time.monotonic()-period_started)*1000
            quality = frame_quality(frame, context)
            errors = quality["errors"]
            row.update(bars=0 if frame is None else len(frame), data_errors=errors, data_quality=quality,
                       source_closed_at=None if frame is None or frame.empty else int(frame.date.iloc[-1].timestamp()))
            if errors:
                rows.append(row)
                continue
            from chanlun.screening.markets import storage_symbol
            cache_path = None if cache_root is None else Path(cache_root, f"{storage_symbol(code, stock.get('market', 'a'))}_{frequency}.json.gz")
            calculation_revision = (revision or source_revision()) + (":holding-consolidation-v1" if include_consolidation else "")
            key = None if cache_path is None else calculation_key(frame, code, frequency, calculation_revision)
            cached = None if cache_path is None or force_rebuild else read_calculation(cache_path, key)
            from chanlun.screening.cache import read_append_proofs
            append_proofs = (read_append_proofs(cache_path,frame,code,frequency,calculation_revision)
                             if cached is None and cache_path is not None and not force_rebuild else {})
            structure_started = time.monotonic()
            snapshot = cached["snapshot"] if cached is not None else (
                snapshot_for(frame, code, frequency, include_consolidation=True) if include_consolidation
                else snapshot_for(frame, code, frequency))
            reviewed = cached["reviewed"] if cached is not None else append_proofs
            timings['structure'] = (time.monotonic()-structure_started)*1000
            validation_started = time.monotonic()
            if snapshot.get("analysis_scope") == "native_centers":
                result = audit_native_center_signals(
                    snapshot, frame, context, settings["point_types"],
                    settings.get("max_anchor_gain_pct", 10),
                )
            else:
                result = audit_snapshot(snapshot, frame, context, settings["point_types"],
                                        settings.get("max_anchor_gain_pct", 10),
                                        include_weak_second=settings.get("include_weak_second", False),
                                        as_confirmation=_confirmation_only,
                                        structural_levels=structural_levels)
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
            selected, audit_rejected = (
                _review_native_center_signals(snapshot, frame, pending, code, frequency)
                if snapshot.get("analysis_scope") == "native_centers"
                else _review_candidates(snapshot, frame, pending, code, frequency)
            )
            for candidate in selected:
                if all(candidate.get("audit", {}).get(k) is True for k in REPLAY_CHECKS):
                    reviewed[candidate_signature(candidate)] = candidate["audit"]
            selected = result["selected"] if _confirmation_only else reused + selected
            rejected = result["rejected"] + audit_rejected
            consolidation_selected, consolidation_pending = [], False
            if include_consolidation:
                consolidation_rejected = []
                try:
                    consolidation_selected, consolidation_rejected, consolidation_pending = _select_consolidation_alerts(
                        snapshot, frame, context, reviewed, code, frequency)
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    row["consolidation_error"] = f"{type(exc).__name__}: {exc}"
                row["consolidation_alerts"] = consolidation_selected
                row["consolidation_rejections"] = consolidation_rejected
                row["consolidation_checked"] = not row.get("consolidation_error") and not consolidation_rejected
            row.update(
                confirmed_buys=result["confirmed_buys"],
                unconfirmed_buys=result["unconfirmed_buys"],
                confirmed_sells=result["confirmed_sells"],
                unconfirmed_sells=result["unconfirmed_sells"],
                reason_counts=dict(Counter(reason for item in rejected for reason in item["reasons"])),
                snapshot_revision=snapshot["snapshot_revision"],
                price_basis_revision=snapshot["price_basis_revision"],
                input_fingerprint=input_fingerprint(frame, code, frequency),
                latest_close=float(frame.close.iloc[-1]),
                structure_price_quantum=snapshot["structure_price_quantum"],
            )
            if include_consolidation:
                row["consolidation_checks"] = snapshot.get("native_consolidation_checks", {})
            timings['validation'] = (time.monotonic()-validation_started)*1000
            persistence_started = time.monotonic()
            if (snapshot.get("analysis_scope") != "native_centers" and
                settings.get("strategy") == DIRECT_STRATEGY
                and frequency == "1m" and not _confirmation_only
            ):
                anchor_from = context.get("anchor_from")
                known_from = (
                    int(datetime.fromisoformat(anchor_from).replace(tzinfo=CN).timestamp())
                    if anchor_from else snapshot.get("source_started_at", snapshot["source_closed_at"])
                )
                row["u295_witness_known_from"] = known_from
                row["u295_lower_structural_witnesses"] = list(
                    observe_independent_lower_structural_witnesses(
                        snapshot, known_from=known_from,
                    )
                )
            minimum_period = settings.get("minimum_operating_frequency")
            if minimum_period == frequency and snapshot.get("analysis_scope") != "native_centers":
                try:
                    row["operational_macd_risk"] = apply_declared_minimum_operating_frequency(
                        snapshot["macd_zero_axis_operational_state"],
                        physical_frequency=frequency, minimum_frequency=minimum_period,
                        source_closed_at=snapshot["source_closed_at"],
                    )
                except (KeyError, TypeError, ValueError):
                    row["operational_macd_risk"] = {
                        "rule_id": "U176", "physical_frequency": frequency,
                        "declared_minimum_frequency": minimum_period,
                        "risk_filter_state": "pending_frozen_evidence",
                        "formal_structure_independent": True, "tradable": False,
                    }
                row["operational_risk_evidence_frequency"] = frequency
            # Keep concise evidence for every recent rejected signal; don't
            # duplicate years of historical centers for the entire universe.
            row["recent_rejections"] = [
                {k: v for k, v in item.items() if k != "center"}
                for item in rejected if "OLD_CONFIRMATION" not in item["reasons"]]
            observations = result["observations"]
            entry_waiting = any(
                item["reasons"] == ["THIRD_ENTRY_PRECISION_PENDING"]
                and item["point"].get("point_type") == "3buy"
                and item["point"].get("structural_level", 0) > 0
                and is_confirmed(item["point"])
                for item in row["recent_rejections"]
            )
            if not _confirmation_only and (
                selected or observations or audit_rejected or entry_waiting or consolidation_selected or include_consolidation
                or row.get("u295_lower_structural_witnesses")
            ):
                phase = "evidence"
                row["evidence"] = _save_evidence(run_dir, code, frequency, frame, snapshot)
            row["selected"] = selected
            row["observations"] = observations
            row["calculation_reused"] = cached is not None
            row["replays_reused"] = len(reused)
            if _inputs is not None:
                _inputs[frequency] = {"frame": frame, "snapshot": snapshot, "reviewed": reviewed,
                                      "cache_path": cache_path, "key": key}
            if cache_path is not None and (cached is None or pending or consolidation_pending or not cached.get('source_input')):
                try:
                    write_calculation(cache_path, key, row, snapshot, reviewed, source_frame=frame,
                                      calculation_revision=calculation_revision)
                except OSError:
                    # Optional reuse storage cannot undo successfully persisted
                    # evidence or turn a usable result into an engine failure.
                    pass
            timings['persistence'] = (time.monotonic()-persistence_started)*1000
        except Exception as exc:
            reason = ("EVIDENCE_WRITE_FAILED" if phase == "evidence" else
                      exc.reason if isinstance(exc, MarketDataContractError) else "ENGINE_ERROR")
            row.update(selected=[], observations=[], consolidation_alerts=[], data_errors=[reason],
                       error=f"{type(exc).__name__}: {exc}")
        finally:
            row['processing_timeline']['calculation_finished_at'] = time.time()
            row['timings_ms'] = {**{k:round(v,2) for k,v in timings.items()},
                                 'total':round((time.monotonic()-period_started)*1000,2)}
        rows.append(row)
    return {"code": code, "market": stock.get("market", "a"), "name": stock["name"], "rows": rows,
            "seconds": round(time.monotonic() - started, 3)}


def _scan_nested_symbol(stock, settings, contexts, run_dir, *, cache_root=None, revision=None, force_rebuild=False):
    """Calculate 1m only for usable 5m setups; publish one joined 5m result."""
    started = time.monotonic()
    inputs = {}
    main_settings = {**settings, "strategy": None, "frequencies": ["5m"]}
    result = scan_symbol(stock, main_settings, contexts, run_dir, cache_root=cache_root,
                         revision=revision, _inputs=inputs, force_rebuild=force_rebuild)
    if not result["rows"]:
        return result
    row = result["rows"][0]
    row["strategy"] = STRATEGY
    candidates = [*row["selected"], *row.get("observations", [])]
    if candidates and all(item["point"].get("evidence_scope") == "native_center_signal"
                          for item in candidates):
        row["strategy"] = "native_center_signals"
        return result
    if not candidates or Path(run_dir, "cancel").exists():
        return result
    try:
        lower_settings = {**main_settings, "frequencies": ["1m"], "point_types": list(POINT_TYPES)}
        lower_result = scan_symbol(stock, lower_settings, contexts, run_dir, cache_root=cache_root,
                                   revision=revision, _confirmation_only=True, _inputs=inputs, force_rebuild=force_rebuild)
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
            if settings.get("minimum_operating_frequency") == "1m":
                row["operational_macd_risk"] = lower_row["operational_macd_risk"]
                row["operational_risk_evidence_frequency"] = "1m"
        if lower_input["cache_path"] is not None and to_review:
            try:
                # The cache contains one-period structure; the association is
                # checked afresh against both inputs on every scan.
                lower_input["snapshot"].pop("screening_interval_signals", None)
                write_calculation(lower_input["cache_path"], lower_input["key"], lower_row,
                                  lower_input["snapshot"], lower_input["reviewed"], source_frame=lower_input['frame'])
            except OSError:
                pass
    except Exception as exc:
        row.update(selected=[], observations=[], data_errors=["NESTED_CONFIRMATION_ERROR"],
                   error=f"{type(exc).__name__}: {exc}")
    finally:
        result["seconds"] = round(time.monotonic() - started, 3)
    return result


def _a_calendar_context(observed, frequency, recent_sessions, max_anchor_sessions):
    """A missing calendar is a period failure, not a fabricated market clock."""
    try:
        return trading_context(observed, frequency, recent_sessions, max_anchor_sessions)
    except (ValueError, OSError) as exc:
        return {'frequency': frequency, 'calendar_error': f'{type(exc).__name__}: {exc}'}


def _external_calendar_context(market, code, observed, frequency, recent_sessions, max_anchor_sessions,
                               *, completed_session=False):
    from chanlun.screening.markets import market_context
    try:
        return market_context(market, code, observed, frequency, recent_sessions, max_anchor_sessions,
                              completed_session=completed_session)
    except (ValueError, OSError) as exc:
        return {'market': market, 'symbol': code, 'frequency': frequency,
                'calendar_error': f'{type(exc).__name__}: {exc}'}


def _scan_worker_symbol(stock, settings, contexts, run_dir, revision, request):
    cache_options = {"force_rebuild": True} if request.get("force_rebuild") is True else {}
    try:
        symbol_settings={**settings,'frequencies':stock.get('_monitor_periods',settings['frequencies'])}
        symbol_contexts = dict(contexts)
        if stock.get('_monitor_periods') and stock.get('market','a')=='a':
            observed=datetime.fromisoformat(request['observed_at'])
            for frequency in symbol_settings['frequencies']:
                if frequency not in symbol_contexts:
                    symbol_contexts[frequency]=_a_calendar_context(observed,frequency,settings['recent_sessions'],settings['max_anchor_sessions'])
        if stock.get("market", "a") != "a":
            observed = datetime.fromisoformat(request["observed_at"])
            if settings.get("strategy") == STRATEGY:
                main = _external_calendar_context(stock["market"], stock["code"], observed, "5m", settings["recent_sessions"], settings["max_anchor_sessions"],
                                      completed_session=settings.get("scope") in {"all_a_watchlist", "watchlist"})
                lower = ({'frequency': '1m', 'calendar_error': main['calendar_error']}
                         if main.get('calendar_error') else _external_calendar_context(
                             stock['market'], stock['code'], datetime.fromtimestamp(main['cutoff'], CN),
                             '1m', settings['recent_sessions'], settings['max_anchor_sessions']))
                symbol_contexts = {"5m": main, "1m": lower}
            else:
                symbol_contexts = {
                    frequency: _external_calendar_context(stock["market"], stock["code"], observed, frequency,
                                              settings["recent_sessions"], settings["max_anchor_sessions"])
                    for frequency in symbol_settings["frequencies"]
                }
        result = scan_symbol(stock, symbol_settings, symbol_contexts, run_dir,
                             cache_root=Path(run_dir).parent / "analysis_cache", revision=revision, **cache_options)
    except Exception as exc:
        result = {"code": stock["code"], "market": stock.get("market", "a"), "name": stock["name"], "rows": [],
                  "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(exc, MarketDataContractError):
            result["reasons"] = [exc.reason]
    return result




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
                 timeout_seconds=SYMBOL_TIMEOUT_SECONDS, worker_target=None):
    """A one-run pool shares the same bounded transport as live monitoring."""
    if Path(run_dir, "cancel").exists():
        return
    from chanlun.screening.worker_pool import ReusableWorkerPool
    request_path = Path(run_dir, "request.json")
    request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.is_file() else {}
    options = {"worker_target": worker_target, "revision_reader": lambda: revision} if worker_target else {}
    pool = ReusableWorkerPool(revision, max_tasks=max(1, len(stocks)), **options)
    try:
        yield from pool.scan(stocks, settings, contexts, run_dir, workers, revision,
                             request=request, timeout_seconds=timeout_seconds)
    finally:
        pool.close()


def _catalog_worker(connection, settings, _run_dir):
    try:
        requested = set(settings.get("codes", []))
        if settings.get("scope") in {"all_a", "all_a_watchlist"}:
            stocks = _exchange().all_stocks(full_market_authorized=True)
        elif settings.get("scope") in {"watchlist", "symbols"}:
            stocks = []
        elif settings.get("scope") == "codes" and requested:
            stocks = [{**info, "type": "stock_cn"} for code in sorted(requested)
                      if (info := _exchange().stock_info(code))]
        else:
            raise ValueError("选股范围必须明确指定")
        stocks = sorted((s for s in stocks if is_a_share(s)
                         and (not requested or s["code"] in requested)
                         and not (settings["exclude_st"] and ("ST" in s["name"].upper() or "退" in s["name"]))),
                        key=lambda s: s["code"])
        if settings.get("scope") in {"all_a_watchlist", "watchlist", "symbols"}:
            from chanlun.screening.markets import watchlist_symbols
            additions = settings.get("symbols", []) if settings["scope"] == "symbols" else watchlist_symbols()
            combined = {("a", s["code"]): {**s, "market": "a"} for s in stocks}
            for item in additions:
                item = {**item, "name": item.get("name", item["code"])}
                combined[(item["market"], item["code"])] = item
            # User-maintained groups are checked first, without enumerating any
            # non-A-share exchange. The remaining A-share pool follows.
            stocks = sorted(combined.values(), key=lambda s: (s.get("origin") != "watchlist", s["market"], s["code"]))
        if not stocks:
            raise ValueError("所选范围没有符合股票类别条件的标的")
        from chanlun.screening.markets import holding_symbols
        holding_lookup_failed = False
        try:
            held = {(row["market"], row["code"]) for row in holding_symbols()}
        except Exception:
            # Optional holding comparisons must not break ordinary selection.
            held = set()
            holding_lookup_failed = True
        stocks = [{**stock, "holding": True} if (stock.get("market", "a"), stock["code"]) in held
                  else {**stock, "holding_lookup_failed": True} if holding_lookup_failed else stock for stock in stocks]
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


def successful_period_count(result):
    """Count usable period results independently of symbol-level error counts."""
    if result.get('error'):
        return 0
    return sum(not row.get('error') and not row.get('data_errors')
               and type(row.get('source_closed_at')) is int for row in result.get('rows', []))


def run_screening(run_dir: Path, *, worker_pool=None, pinned_revision=None, heartbeat=None):
    if (worker_pool is None and pinned_revision is not None
            or worker_pool is not None and pinned_revision != worker_pool.revision):
        raise ValueError('Pinned screening revision requires its matching worker pool')
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    settings = request["settings"]
    observed = datetime.fromisoformat(request["observed_at"])
    state = {**request, "status": "running", "phase": "catalog", "completed": 0,
             "total": 0, "selected_count": 0, "error_count": 0, "successful_period_count": 0, "started_at": time.time(),
             "source_revision": pinned_revision or source_revision(), "worker_pid": os.getpid()}
    if os.name == 'nt':
        from chanlun.screening.worker_lifetime import process_identity
        owner = process_identity(os.getpid())
        if owner['alive'] is not True or not owner['birth']:
            raise RuntimeError('Cannot attest the screening process owner')
        state['worker_process_birth'] = owner['birth']
    def publish_status():
        write_json(run_dir / "status.json", state)
        if heartbeat is not None:
            heartbeat()
    publish_status()
    stream = None
    try:
        catalog = None
        holding_snapshot=request.get('holding_snapshot')
        if settings.get('scope')=='symbols' and holding_snapshot is not None:
            from chanlun.screening.markets import validate_symbol
            if (holding_snapshot.get('schema')!='monitor-holdings-v1'
                    or not isinstance(holding_snapshot.get('keys'),list)
                    or any(not isinstance(key,str) or ':' not in key or not validate_symbol(*key.split(':',1))
                           for key in holding_snapshot['keys'])
                    or not settings.get('symbols')
                    or any(not validate_symbol(s.get('market'),s.get('code')) for s in settings['symbols'])):
                raise ValueError('invalid verified monitor catalog')
            held=set(holding_snapshot['keys'])
            # Keep the scheduler's holdings-first order. This explicit bounded
            # list needs no vendor catalog process or second holdings lookup.
            stocks=[{**stock,'name':stock.get('name') or stock['code'],
                     'holding':f"{stock['market']}:{stock['code']}" in held} for stock in settings['symbols']]
            catalog={'stocks':stocks,'excluded_requested_codes':[]}
            state['catalog_reused']=True
        else:
            for result in bounded_catalog(settings, run_dir):
                state.update(updated_at=time.time(), elapsed_seconds=round(time.time()-state["started_at"], 1))
                publish_status()
                if result is not None:
                    catalog = result
        if catalog is None or (run_dir / "cancel").exists():
            raise ScreeningCancelled()
        stocks = catalog["stocks"]
        state["excluded_requested_codes"] = catalog["excluded_requested_codes"]
        contexts = {}
        if any(stock.get('market', 'a') == 'a' for stock in stocks):
            contexts = {frequency: _a_calendar_context(observed, frequency, settings['recent_sessions'],
                                                       settings['max_anchor_sessions'])
                        for frequency in settings['frequencies']}
            if settings.get('strategy') == STRATEGY:
                # Pin lower confirmation to the actual completed main candle;
                # an unavailable main clock cannot authorize any lower input.
                main = contexts['5m']
                contexts['1m'] = ({'frequency': '1m', 'calendar_error': main['calendar_error']}
                    if main.get('calendar_error') else _a_calendar_context(
                        datetime.fromtimestamp(main['cutoff'], CN), '1m',
                        settings['recent_sessions'], settings['max_anchor_sessions']))
        clock_symbols = {}
        for stock in stocks:
            clock_symbols.setdefault(stock.get("market", "a"), stock["code"])
        state["market_clock_symbols"] = clock_symbols
        state["market_cutoffs"] = {
            market: {frequency: None for frequency in settings["frequencies"]}
            for market in clock_symbols
        }
        exchanges = dict(Counter(s["code"].split(".")[0] for s in stocks if s.get("market", "a") == "a"))
        state.update(total=len(stocks), phase="screening",
                     cutoffs={f: c["cutoff"] for f, c in contexts.items() if not c.get('calendar_error')},
                     calendar_source=next((c.get('calendar_source') for c in contexts.values() if not c.get('calendar_error')), None),
                     universe_coverage={"source": "QMT + 人工关注组行情源", "exchanges": exchanges,
                         "markets": dict(Counter(s.get("market", "a") for s in stocks)),
                         "absent_exchanges": [e for e in ("SH", "SZ", "BJ") if e not in exchanges],
                         "scope_label": {"all_a": "行情源 A 股股票池", "all_a_watchlist": "A 股 + 人工关注组（各市场）",
                                         "watchlist": "人工关注组（各市场）", "symbols": "选股候选（跨市场）"}.get(settings["scope"], "指定股票")})
        write_json(run_dir / "universe.json", stocks)
        publish_status()
        workers = min(settings.get("workers", 12), max(1, (os.cpu_count() or 2) - 2), 12)
        state["effective_workers"] = workers
        state["catalog_elapsed_seconds"] = round(time.time() - state["started_at"], 1)
        scan_started = time.time()
        stream = (bounded_scan(stocks, settings, contexts, run_dir, workers, state["source_revision"])
                  if worker_pool is None else worker_pool.scan(
                      stocks, settings, contexts, run_dir, workers, state["source_revision"], request=request))
        for result, current_codes in stream:
            if result is not None:
                with (run_dir / "results.jsonl").open("a", encoding="utf-8") as result_file:
                    result_file.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                state["completed"] += 1
                state["selected_count"] += int(any(r["selected"] for r in result["rows"]))
                state["error_count"] += int(bool(result.get("error")) or any(r.get("data_errors") for r in result["rows"]))
                state['successful_period_count'] += successful_period_count(result)
                state["reused_combinations"] = state.get("reused_combinations", 0) + sum(bool(r.get("calculation_reused")) for r in result["rows"])
                state["symbol_seconds_total"] = round(state.get("symbol_seconds_total", 0) + result.get("seconds", 0), 3)
                for row in result["rows"]:
                    market = row.get("market", result.get("market", "a"))
                    cutoff = row.get("source_closed_at")
                    periods = state.get("market_cutoffs", {}).get(market, {})
                    if row.get("frequency") in periods and type(cutoff) is int:
                        previous = periods[row["frequency"]]
                        periods[row["frequency"]] = cutoff if previous is None else max(previous, cutoff)
            state.update(current_codes=current_codes,
                         elapsed_seconds=round(time.time() - state["started_at"], 1), updated_at=time.time())
            publish_status()
        state["status"] = "cancelled" if (run_dir / "cancel").exists() else "completed"
        state["scan_elapsed_seconds"] = round(time.time() - scan_started, 1)
        state["phase"] = "finished"
    except ScreeningCancelled:
        state.update(status="cancelled", phase="finished")
    except Exception as exc:
        traceback.print_exc()
        state.update(status="failed", phase="finished", error=f"{type(exc).__name__}: {exc}")
    finally:
        if stream is not None and hasattr(stream, 'close'):
            stream.close()
    state.update(current_codes=[], finished_at=time.time(), elapsed_seconds=round(time.time() - state["started_at"], 1))
    publish_status()



if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    run_screening(parser.parse_args().run_dir.resolve())
