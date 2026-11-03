"""Presentation and manual review for the screening workbench.

Publishes current results progressively, retaining labelled previous rows until
their replacement is checked. Opening the workbench never starts a market scan.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache, partial
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from chanlun import config
from chanlun.cl_utils.point_exits import EXIT_PRICE_SOURCES

from .screening import manager
from . import higher_context
from .screening_filter_facts import filter_facts


REVIEW_FIELDS = {
    "center_judgement": ("UNCERTAIN", "CONFIRMED", "REJECTED"),
    "trend_judgement": ("UNCERTAIN", "UP", "DOWN", "CONSOLIDATION"),
    "level_judgement": ("UNCERTAIN", "30M", "5M", "1M", "OTHER"),
    "point_judgement": ("UNCERTAIN", "BUY_1", "BUY_2", "BUY_3", "SELL_1", "SELL_2", "SELL_3", "NONE"),
    "decomposition_judgement": ("UNCERTAIN", "SAME_LEVEL", "CENTER", "COMBINED"),
    "center_expansion_judgement": ("UNCERTAIN", "CONFIRMED", "REJECTED"),
    "nine_segment_upgrade_judgement": ("UNCERTAIN", "CONFIRMED", "REJECTED"),
    "segment_difference_judgement": ("UNCERTAIN", "CONFIRMED", "REJECTED"),
    "disposition": ("WATCH", "REJECT", "NEEDS_MORE_DATA"),
}


@lru_cache(maxsize=16)
def _cached_json(path, mtime, size):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    return _cached_json(str(path), stat.st_mtime_ns, stat.st_size)


def _archive_root():
    return config.get_data_path() / "decision_support"


def _identity(source, code, frequency, point_id):
    return hashlib.sha256(json.dumps([source, code, frequency, point_id]).encode()).hexdigest()


def _sector_catalog():
    data = _read(_archive_root() / "trading_screening_sector_snapshot.json")
    payload = data.get("payload", {})
    snapshot = payload.get("snapshot", {})
    assessment = snapshot.get("assessments", {})
    return assessment.get("assessments", []), snapshot.get("members", {}), payload.get("captured_at")


def _current_rows(result, source, *, candidate_id=None, include_history=False):
    rows = []
    seen = set()
    for item in [*result.get("selected", []), *result.get("observations", []),
                 *result.get("entry_waiting", []), *(result.get('historical_confirmed', []) if include_history else [])]:
        point = item["point"]
        center = item.get("center") or {}
        identity = _identity(source, item["code"], item["frequency"], point.get("point_id", point.get("render_id")))
        if identity in seen:
            continue
        seen.add(identity)
        if candidate_id is not None and identity != candidate_id:
            continue
        waiting = "observation_validation" in item
        from chanlun.screening.timeframe_policy import POLICY
        historical = item.get('historical_only') is True
        prior_trade = bool(not historical and result.get('frozen_native_review') and item['frequency'] == '5m'
                           and item.get('timeframe_policy') != POLICY)
        rows.append({
            "id": identity,
            "candidate_evidence_version": hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20],
            "code": item["code"], "name": item.get("name", item["code"]), "market": item.get("market", "a"),
            "frequency": item["frequency"], "point": point,
            "exit_plan": item.get("exit_plan"), "confirmation_exit_plan": item.get("confirmation_exit_plan"),
            "latest_price": item.get("latest_price"), "source_closed_at": item.get("source_closed_at"),
            "input_fingerprint":item.get('input_fingerprint') or (item.get('evidence') or {}).get('input_fingerprint'),
            '_context_evidence': higher_context.frozen_binding(item, source),
            "gain_pct": item.get("distance_from_anchor_pct"),
            "anchor_age_sessions": item.get("anchor_age_sessions"),
            "confirmation_age_sessions": item.get("confirmation_age_sessions"),
            "confirmation_delay_sessions": item.get("confirmation_delay_sessions"),
            "confirmation_delay_bars": item.get("confirmation_delay_bars"),
            "audit": item.get("audit", {}),
            "stage": item.get("selection_status", point["status"] if waiting else "confirmed"),
            "origin": 'history' if historical else "screening", 'historical_only': historical,
            'history': item.get('history'),
            'filter_facts': filter_facts(item),
            "nested_confirmation": item.get("nested_confirmation"),
            "timeframe_policy": item.get('timeframe_policy'),
            "operating_role": item.get('operating_role') or ('trade' if prior_trade else None),
            "trade_confirmation_pending": prior_trade,
            "background_30m": item.get('background_30m'),
            'history_repair': item.get('history_repair'),
            'center_sequence': item.get('center_sequence'),
            "selection_available_at": item.get("selection_available_at", point.get("available_at")),
            "selection_missing_conditions": (['lower_1m_confirmation'] if prior_trade else
                item.get("selection_missing_conditions", point.get("missing_conditions", []))),
            "confirmation_segment": item.get("confirmation_segment"),
            "evidence_available": item.get("evidence_available", False),
            "evidence_chart_available": item.get("evidence_chart_available", False),
            "observation_validation": item.get("observation_validation"),
            "decision_reasons": item.get("decision_reasons", []),
            "entry_waiting": item.get("entry_waiting", False),
            "operational_macd_risk": item.get("operational_macd_risk"),
            "center": {k: center.get(k) for k in (
                "state", "center_id", "center_zd_price", "center_zg_price", "center_ended",
                "third_class_confirmed", "source_kind", "structural_level", "center_end_available_at",
            )},
        })
        if candidate_id is not None:
            break
    return rows


_POINT_INDEX_FIELDS = (
    "point_id", "center_id", "point_type", "status", "confirmed_at", "available_at", "anchor_at",
    "anchor_price", "invalidation_price", "evidence_scope", "center_ordinal", "variant", "structural_level",
)


def _compact_rows(rows, compact, *, context=False):
    if compact:
        fields = {"id", "code", "name", "market", "frequency", "origin", "stage", "latest_price", "candidate_evidence_version",
                  "source_closed_at", "gain_pct", "confirmation_age_sessions", "selection_available_at",
                  "evidence_available", "evidence_chart_available", "operational_macd_risk", "nested_confirmation", "exit_plan",
                  "latest_recheck_at", "latest_recheck_closed_at", "selection_check_version", "refresh_state", "result_source",
                  'timeframe_policy', 'operating_role', 'background_30m', 'trade_confirmation_pending', 'center_sequence',
                  'historical_only', 'history', 'confirmation_delay_bars', 'filter_facts'}
        point_fields = _POINT_INDEX_FIELDS
        if context:
            # Retain the identity, clocks and leg boundaries required by linkage,
            # without copying full structural proof into a list response.
            fields |= {"input_fingerprint", '_context_evidence'}
            point_fields += ("price_basis_revision", "terminal_start_at")
        rows = [{**{key: value for key, value in row.items() if key in fields and value is not None},
                 "point": {key: row["point"][key] for key in point_fields if row["point"].get(key) is not None},
                 "detail_loaded": bool(row.get('refresh_state'))} for row in rows]
        for row in rows:
            if row.get("exit_plan"):
                row["exit_plan"] = {key: value for key, value in row["exit_plan"].items() if key in {
                    "evidence_scope", "side", "point_status", "source_frequency", "stop_loss", "take_profit",
                }}
            nested = row.get("nested_confirmation")
            if nested:
                row["nested_confirmation"] = {key: nested.get(key) for key in (
                    "state", "frequency", 'policy', 'data_errors', 'check_state', 'assessment', 'evidence_type')}
    return rows if context else [{key:value for key,value in row.items() if key!='_context_evidence'} for row in rows]


def _index_rows(payload, source, *, compact):
    return _compact_rows(_current_rows(payload, source), compact, context=True)


_INDEX_LOCK = threading.Lock()
_INDEX_CACHE = None
_HISTORY_INDEX_CACHE = None


def _published_history(payload, compact):
    global _HISTORY_INDEX_CACHE
    with _INDEX_LOCK:
        if not payload.get('historical_confirmed'):
            _HISTORY_INDEX_CACHE = None
            return []
        if (payload.get('status') != 'completed' or _HISTORY_INDEX_CACHE is None
                or _HISTORY_INDEX_CACHE[0] is not payload or _HISTORY_INDEX_CACHE[1] != compact):
            rows = _compact_rows(_current_rows({'historical_confirmed': payload['historical_confirmed']},
                payload.get('run_id', 'idle'), include_history=True), compact)
            _HISTORY_INDEX_CACHE = (payload, compact, rows) if payload.get('status') == 'completed' else None
            return rows
        return _HISTORY_INDEX_CACHE[2]


def _published_index(payload, compact):
    """Reuse one immutable verified snapshot; live checks still run per request.

    ScreeningManager replaces its published payload when source, progress or
    evidence changes, and deep-copies the projected view for every caller.
    Holding the object itself also prevents an id from being reused. Active
    scans are projected afresh, without retaining their preceding full payload.
    """
    global _INDEX_CACHE
    source = payload.get('run_id', 'idle')
    if not compact:
        return _current_rows(payload, source)
    with _INDEX_LOCK:
        if payload.get('status') != 'completed':
            _INDEX_CACHE = None
            return _index_rows(payload, source, compact=True)
        if _INDEX_CACHE is None or _INDEX_CACHE[0] is not payload:
            _INDEX_CACHE = (payload, _index_rows(payload, source, compact=True))
        return _INDEX_CACHE[1]


def _compact_context(context):
    """The list needs explanations; complete peer evidence belongs in detail."""
    return {**{key: value for key, value in context.items() if key != "periods"},
            "periods": [{key: value for key, value in period.items() if key in {
                "frequency", "role", "state", "label", "source_closed_at", "anchor_aligned",
                'check_state', 'reason', 'evidence_source',
            }} for period in context.get("periods", [])]}


def _dashboard_projection(payload, compact):
    # Replacement cursors are only needed while a scan is still filling its
    # results. A completed list must not copy 16,000 task tuples on each read.
    progressive = payload.get('status') != 'completed' or payload.get('results_loading')
    failed = set()
    for error in payload.get('errors', []):
        if not error.get('code') or error.get('signal_type') == 'consolidation_divergence':
            continue
        for frequency in ([error['frequency']] if error.get('frequency') else payload.get('settings', {}).get('frequencies', [])):
            failed.add((error.get('market', 'a'), error['code'], frequency))
    coverage={'processed_periods':len(payload.get('processed_periods', [])),
              'verified_periods':sum((r.get('market','a'),r['code'],r['frequency']) not in failed
                                     for r in payload.get('verified_periods',[])),
              'failed_periods':len(failed),
              'unverified_contract_symbols':len({(r.get('market','a'),r['code']) for r in payload.get('errors',[])
                  if r.get('code') and set(r.get('reasons',[])) & {'SESSION_CALENDAR_UNAVAILABLE','BAR_TIME_LABEL_UNAVAILABLE'}})}
    return {**{key: value for key, value in payload.items() if key not in {
        "selected", "observations", "historical_confirmed", "entry_waiting", "consolidation_alerts", "processed_symbols", "processed_periods",
        "u295_lower_structural_witnesses", "recent_rejections", "verified_periods",
    }}, 'input_coverage':coverage, "_workbench_rows": _published_index(payload, compact),
        '_workbench_history': _published_history(payload, compact),
        '_workbench_failed': sorted(failed),
        '_workbench_verified': [(r.get('market','a'),r['code'],r['frequency']) for r in payload.get('verified_periods',[])
                                if (r.get('market','a'),r['code'],r['frequency']) not in failed] if progressive else [],
        '_workbench_processed': [(r.get('market','a'),r['code']) for r in payload.get('processed_symbols',[])] if progressive else [],
        '_workbench_processed_periods': [(r.get('market','a'),r['code'],r['frequency']) for r in payload.get('processed_periods',[])] if progressive else []}


def candidate_detail(source, candidate_id):
    """Project one verified candidate before copying the full result payload."""
    def project(payload):
        rows = _current_rows(payload, source, candidate_id=candidate_id, include_history=True)
        return {"candidate": rows[0] if rows else None}
    result = manager.results(project=project, available=True)
    if result.get("run_id") != source:
        raise ValueError("该批次已被新的选股替换，请刷新结果")
    if result.get("candidate") is None:
        raise ValueError("该候选已退出当前窗口或证据不可用，请刷新结果")
    historical = result['candidate'].get('historical_only') is True
    rows, _, version, unverified = (([result['candidate']], 0, 0, 0) if historical else
        _live_rows([result["candidate"]], source, result.get("current_source_revision")))
    if not rows:
        if unverified:
            raise ValueError("该候选正在复核或最新行情复核失败，核验通过后再显示")
        raise ValueError("最新复核已确认该候选退出，请刷新结果")
    context=higher_context.event_context(manager,result['candidate'],result.get('source_revision')) if result.get('source_current') and not historical else None
    short_background = (context or {}).get('background_30m') if result['candidate']['frequency'] == '1m' else None
    return {"source": source, "candidate": {**{k:v for k,v in result['candidate'].items() if k!='_context_evidence'}, "detail_loaded": True,
                                              **({'background_30m':short_background} if short_background and not result['candidate'].get('background_30m') else {}),
                                              **({'higher_context':context} if context else {})},
            "live_recheck_version": version,
            "evidence_version": result.get("evidence_version"),
            "result_reader_revision": result.get("result_reader_revision")}


def _check_version(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20] if records else 0


def _live_rows(rows, source, revision, *, preserve_unverified=False):
    from .signal_monitor import service as signal_monitor
    overlay = signal_monitor.selection_overlay(source, revision)
    kept, removed, unverified = [], 0, 0
    for row in rows:
        period = overlay.get(f"{row['market']}:{row['code']}:{row['frequency']}")
        cutoff = row.get("source_closed_at") or 0
        verified = period and type(period.get("cutoff")) is int and period["cutoff"] >= cutoff
        identity = None
        if period and (verified or period.get("unverified_identities")):
            identity = hashlib.sha256(json.dumps([row["market"], row["code"], row["frequency"], row["point"]["point_id"]]).encode()).hexdigest()
        pending = verified and identity in period.get('pending_identities', ())
        if verified:
            if (identity in period.get('inactive_identities', ())
                    or identity not in period["identities"] and not pending):
                removed += 1
                continue
        if pending or (period and identity in period.get("unverified_identities", ())):
            unverified += 1
            if preserve_unverified:
                kept.append({**row, 'refresh_state': 'limited', 'result_source': row.get('result_source') or source,
                             'detail_loaded': True, 'evidence_chart_available': False})
            continue
        if period and period.get("error") and (period.get("failed_cutoff", period.get("cutoff")) or 0) >= cutoff:
            unverified += 1
            if preserve_unverified:
                kept.append({**row, 'refresh_state': 'limited', 'result_source': row.get('result_source') or source,
                             'detail_loaded': True, 'evidence_chart_available': False})
            continue
        if verified:
            row["latest_recheck_at"] = period["checked_at"]
            row["latest_recheck_closed_at"] = period["cutoff"]
            row["selection_check_version"] = _check_version(period)
        kept.append(row)
    return kept, removed, _check_version(overlay), unverified


def live_recheck_version(source, revision):
    return _live_rows([], source, revision)[2]


def dashboard(*, background=False, compact=False):
    result = manager.results(background=background, project=lambda payload: _dashboard_projection(payload, compact))
    source = result.get("run_id", "idle")
    history = result.get('_workbench_history')
    if history is None:
        history = _compact_rows(_current_rows({'historical_confirmed': result.get('historical_confirmed', [])}, source, include_history=True), compact)
    projected_rows = result.get("_workbench_rows")
    if projected_rows is None:
        projected_rows = _current_rows(result, source)
    rows, removed, live_version, unverified = _live_rows(projected_rows,
                                            source, result.get("current_source_revision"), preserve_unverified=True)
    from .screening_rollover import display_for
    display = display_for(str(manager.root))
    rows, previous_count = display.merge(result, rows, project=partial(_index_rows, compact=compact),
        live_filter=partial(_live_rows, preserve_unverified=True))
    previous_count = sum(bool(row.get('refresh_state')) for row in rows)
    context = higher_context.snapshot(manager, source, result.get('source_revision'),candidates=rows,
                                      source_current=result.get('source_current') is True)
    rows = _compact_rows(rows, compact)
    status = {k: v for k, v in result.items() if k not in (
        "selected", "observations", "entry_waiting", "consolidation_alerts", "recent_rejections", "rejection_counts", "reason_labels", "errors",
        "_workbench_rows", "_workbench_history", "historical_confirmed", "_workbench_verified", "_workbench_processed", "_workbench_processed_periods", "_workbench_failed",
        "processed_symbols", "processed_periods", "u295_lower_structural_witnesses", "verified_periods",
    )}
    status["error_count"] = (result.get("error_count") if result.get("results_loading")
                             else len(result.get("errors", [])))
    status['error_symbol_count'] = len({(row.get('market', 'a'), row['code'])
                                        for row in result.get('errors', [])})
    status["higher_context_version"] = higher_context.status_version(manager, source)
    status["live_exclusion_count"] = removed
    status["live_unverified_count"] = unverified
    status["live_recheck_version"] = live_version
    status['previous_result_count'] = previous_count
    status['current_result_count'] = len(rows) - previous_count
    status['previous_results_loading'] = bool(source != 'idle' and (result.get('status') != 'completed' or result.get('results_loading'))
                                             and display.waiting_for_baseline(source))
    for row in rows:
        row['higher_context']=context['rows'].get(row['id']) or {
            'schema':'native-period-link-v1','primary_frequency':row['frequency'],'category':'unverified',
            'label':'联动待核验','quality':'pending','priority':5,'periods':[],
            'notes':['等待对应周期的已核验计算结果。']}
        if row['frequency'] == '1m' and not row.get('background_30m'):
            row['background_30m'] = row['higher_context'].get('background_30m')
        if compact:
            row['higher_context'] = _compact_context(row['higher_context'])
    sectors, members, catalog_at = _sector_catalog()
    membership = {}
    for sector_id, codes in members.items():
        for code in codes:
            membership.setdefault(code, []).append(sector_id)
    counts = Counter()
    symbols = {}
    for row in [*rows, *history]:
        row["sectors"] = membership.get(row["code"], []) if row["market"] == "a" else []
    for row in rows:
        for sector_id in row["sectors"]:
            counts[sector_id] += 1
            symbols.setdefault(sector_id, set()).add(row["code"])
    sector_rows = [{
        "id": s["sector_id"], "name": s["sector_name"],
        "signal_count": counts[s["sector_id"]], "symbol_count": len(symbols.get(s["sector_id"], ())),
        "member_count": len(members.get(s["sector_id"], ())),
        "historical_regime": s.get("regime"), "historical_rank": s.get("horizontal_rank"),
        "historical_strength": s.get("horizontal_strength"),
    } for s in sectors]
    sector_rows.sort(key=lambda s: (-s["signal_count"], s["name"]))
    return {"source": source, "status": status, "candidates": rows, 'historical_candidates': history, "sectors": sector_rows,
            "compact": compact,
            "higher_context": {k: v for k, v in context.items() if k != "rows"},
            "point_exit_sources": EXIT_PRICE_SOURCES,
            "diagnostics": {key: result.get(key, {} if key != "errors" else [])
                            for key in ("rejection_counts", "reason_labels", "errors")},
            "sector_catalog_at": catalog_at, "stage_counts": dict(Counter(r["stage"] for r in rows)),
            "point_counts": dict(Counter(r["point"]["point_type"] for r in rows))}


def _review_db():
    return manager.root / "manual_reviews.sqlite3"


def notification_history():
    data = _read(_archive_root() / "trading_notification_state.json")
    fields = ("code", "market", "recorded_at", "notification_evidence_at", "setup_point_type",
              "old_stage", "new_stage", "status", "reason")
    rows = [{k: row.get(k) for k in fields} for row in data.get("event_audit", [])]
    rows.sort(key=lambda row: row.get("recorded_at") or "", reverse=True)
    return {"events": rows, "last_success_at": data.get("last_success_at"),
            "mode": "historical", "automatic_notifications": False}


def reviews(user_id, source):
    if not _review_db().is_file() or source != manager.status().get("run_id", "idle"):
        return {"latest": {}, "history": []}
    with sqlite3.connect(_review_db(), timeout=10) as conn:
        found = conn.execute(
            "SELECT payload FROM reviews WHERE owner = ? AND source = ? ORDER BY id DESC",
            (str(user_id), source),
        ).fetchall()
    items = [json.loads(row[0]) for row in found]
    latest = {}
    for item in items:
        latest.setdefault(item["candidate_id"], item)
    return {"latest": latest, "history": items}


def save_review(user_id, body):
    if not isinstance(body, dict) or set(body) != {"source", "candidate_id", "judgements", "notes"}:
        raise ValueError("复核记录字段不完整或包含未知字段")
    source, candidate_id = body["source"], body["candidate_id"]
    if not isinstance(source, str) or not isinstance(candidate_id, str):
        raise ValueError("候选标识无效")
    judgements = body["judgements"]
    if (not isinstance(judgements, dict) or set(judgements) != set(REVIEW_FIELDS)
            or any(judgements[k] not in options for k, options in REVIEW_FIELDS.items())):
        raise ValueError("复核判断不合法")
    notes = body["notes"]
    if not isinstance(notes, str) or len(notes) > 4000:
        raise ValueError("复核笔记最多 4000 字")
    actual = dashboard()
    if actual["source"] != source or candidate_id not in {row["id"] for row in [*actual["candidates"], *actual.get('historical_candidates', [])]}:
        raise ValueError("该候选不属于本次快照，请刷新后复核")
    item = {**body, "saved_at": datetime.now(timezone.utc).isoformat()}
    path = _review_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS review_owner_source ON reviews(owner, source)")
        conn.execute("INSERT INTO reviews (owner, source, payload) VALUES (?, ?, ?)",
                     (str(user_id), source, json.dumps(item, ensure_ascii=False)))
    return item
