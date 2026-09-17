"""Presentation and manual review for the screening workbench.

Reads only the latest screening run. Opening the workbench never starts a
market scan. Run identities keep current candidates and manual reviews aligned.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3

from chanlun import config
from chanlun.cl_utils.point_exits import EXIT_PRICE_SOURCES

from .screening import manager


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


def _current_rows(result, source):
    rows = []
    seen = set()
    for item in [*result.get("selected", []), *result.get("observations", [])]:
        point = item["point"]
        center = item.get("center") or {}
        identity = _identity(source, item["code"], item["frequency"], point.get("point_id", point.get("render_id")))
        if identity in seen:
            continue
        seen.add(identity)
        waiting = "observation_validation" in item
        rows.append({
            "id": identity,
            "code": item["code"], "name": item.get("name", item["code"]), "market": item.get("market", "a"),
            "frequency": item["frequency"], "point": point,
            "exit_plan": item.get("exit_plan"), "confirmation_exit_plan": item.get("confirmation_exit_plan"),
            "latest_price": item.get("latest_price"), "source_closed_at": item.get("source_closed_at"),
            "gain_pct": item.get("distance_from_anchor_pct"),
            "confirmation_delay_sessions": item.get("confirmation_delay_sessions"),
            "audit": item.get("audit", {}),
            "stage": item.get("selection_status", point["status"] if waiting else "confirmed"), "origin": "screening",
            "nested_confirmation": item.get("nested_confirmation"),
            "selection_available_at": item.get("selection_available_at", point.get("available_at")),
            "selection_missing_conditions": item.get("selection_missing_conditions", point.get("missing_conditions", [])),
            "confirmation_segment": item.get("confirmation_segment"),
            "evidence_available": item.get("evidence_available", False),
            "evidence_chart_available": item.get("evidence_chart_available", False),
            "observation_validation": item.get("observation_validation"),
            "center": {k: center.get(k) for k in (
                "state", "center_id", "center_zd_price", "center_zg_price", "center_ended",
                "third_class_confirmed", "source_kind", "structural_level", "center_end_available_at",
            )},
        })
    return rows


def dashboard():
    result = manager.results()
    source = result.get("run_id", "idle")
    rows = _current_rows(result, source)
    status = {k: v for k, v in result.items() if k not in (
        "selected", "observations", "recent_rejections", "rejection_counts", "reason_labels", "errors",
    )}
    status["error_count"] = len(result.get("errors", []))
    sectors, members, catalog_at = _sector_catalog()
    membership = {}
    for sector_id, codes in members.items():
        for code in codes:
            membership.setdefault(code, []).append(sector_id)
    counts = Counter()
    symbols = {}
    for row in rows:
        row["sectors"] = membership.get(row["code"], []) if row["market"] == "a" else []
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
    return {"source": source, "status": status, "candidates": rows, "sectors": sector_rows,
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
    if actual["source"] != source or candidate_id not in {row["id"] for row in actual["candidates"]}:
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
