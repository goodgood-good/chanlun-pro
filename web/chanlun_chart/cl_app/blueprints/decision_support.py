"""当前唯一人工辅助选股策略的只读路由。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
import gzip
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, make_response, render_template, request
from flask_login import current_user, login_required

from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)
from chanlun.decision_support.fingerprints import sha256_json

from ..services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_status_snapshot,
    build_research_audit_snapshot,
)
from ..services.trading_screening import SCHEMA
from ..services.holding_group_monitor import (
    SCHEMA as HOLDING_GROUP_MONITOR_SCHEMA,
)
from ..services.human_review_screening import (
    HumanReviewScreenUnavailable,
    HumanReviewScreeningService,
    WEB_SCHEMA as HUMAN_REVIEW_WEB_SCHEMA,
)
from ..services.realtime_review_inbox import SCHEMA as REALTIME_REVIEW_SCHEMA
from ..services.realtime_quotes import isolated_a_share_quote_batch


decision_support_bp = Blueprint("decision_support", __name__)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_JSON_GZIP_MIN_BYTES = 32 * 1024
_JSON_GZIP_CACHE_CAPACITY = 3
_JSON_GZIP_CACHE: OrderedDict[str, bytes] = OrderedDict()
_JSON_GZIP_CACHE_LOCK = RLock()
_EARLY_SIGNALS_CATALOG_TRANSPORT = "signal-catalog-v1"
_EARLY_SIGNALS_CATALOG_SCHEMA = "chanlun-early-signals-signal-catalog-v1"
_EARLY_SIGNALS_CATALOG_FIELDS = (
    "execution_profile",
    "higher_timeframe_risk",
    "position_recommendation",
    "sector",
    "context_30m",
    "context_d",
    "decision_reasons",
    "warmup",
)
_EARLY_SIGNALS_COMPACT_OMITTED_FIELDS = frozenset(
    {
        "admitted_universe_codes",
        "decision_source_snapshot",
        "sector_exclusions",
        "sector_parent_relations",
        "sector_strength_evidence",
    }
)
_EARLY_SIGNALS_VOLATILE_HEALTH_FIELDS = frozenset(
    {
        "heartbeat_age_seconds",
        "heartbeat_at",
        "priority_monitor_age_seconds",
        "refresh_elapsed_seconds",
    }
)
_EARLY_SIGNALS_NATIVE_HEALTH_FIELDS = (
    "schema",
    "required",
    "ready",
    "status",
    "reasons",
    "worker_alive",
    "isolated_process",
    "loopback_authenticated",
    "application_source_revision_match",
    "expected_application_source_revision",
    "worker_application_source_revision",
    "minimum_market_data_frequency",
    "tick_data_used",
    "real_account_access",
    "real_order_transport",
    "failure_count",
    "last_error",
    "last_remote_error",
    "restart_count",
    "recycle_count",
    "last_recycle_reason",
    "market_data_probe",
    "sector_snapshot_cache",
)
_EARLY_SIGNALS_WORKER_POOL_HEALTH_FIELDS = (
    "affinity_contract_id",
    "application_source_revision_consistent",
    "candidate_disk_runtime_cache_enabled",
    "candidate_released_worker_count",
    "candidate_worker_count",
    "configured_worker_count",
    "coverage_sector_affinity",
    "lane_isolation_active",
    "priority_burst_worker_count",
    "priority_reserved_worker_count",
    "ready_worker_count",
    "running_application_source_revisions",
    "running_worker_count",
)
_CURRENT_SELECTION_LIFECYCLE_STAGES = frozenset(
    {
        "observed",
        "approaching",
        "triggered",
        "executable",
        "active",
    }
)


class DecisionSupportError(RuntimeError):
    def __init__(self, code: str, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@decision_support_bp.errorhandler(DecisionSupportError)
def _decision_support_error(error: DecisionSupportError):
    return {
        "ok": False,
        "code": error.code,
        "errmsg": error.code,
    }, error.status_code


def _ok(data: object) -> dict[str, object]:
    return {"ok": True, "data": data}


def _no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _private_revalidate(response, cache_revision: str):
    response.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Content-Revision"] = cache_revision
    response.set_etag(cache_revision, weak=True)
    return response


def _large_json_response(
    payload: object,
    *,
    cache_revision: str | None = None,
):
    """为大型轮询响应做标准 HTTP 压缩，同时保留测试和旧客户端兼容性。"""

    accepted = request.accept_encodings.best_match(("gzip", "identity"))
    if (
        cache_revision is not None
        and request.if_none_match.contains_weak(cache_revision)
    ):
        response = make_response("", 304)
        response.vary.add("Accept-Encoding")
        return _private_revalidate(response, cache_revision)
    if accepted == "gzip" and cache_revision is not None:
        with _JSON_GZIP_CACHE_LOCK:
            compressed = _JSON_GZIP_CACHE.pop(cache_revision, None)
            if compressed is not None:
                _JSON_GZIP_CACHE[cache_revision] = compressed
        if compressed is not None:
            response = make_response(compressed)
            response.mimetype = "application/json"
            response.headers["Content-Encoding"] = "gzip"
            response.vary.add("Accept-Encoding")
            return _private_revalidate(response, cache_revision)

    response = make_response(payload)
    response.vary.add("Accept-Encoding")
    if accepted == "gzip":
        raw = response.get_data()
        if len(raw) >= _JSON_GZIP_MIN_BYTES:
            compressed = gzip.compress(raw, compresslevel=5, mtime=0)
            if len(compressed) < len(raw):
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                if cache_revision is not None:
                    with _JSON_GZIP_CACHE_LOCK:
                        _JSON_GZIP_CACHE.pop(cache_revision, None)
                        _JSON_GZIP_CACHE[cache_revision] = compressed
                        while len(_JSON_GZIP_CACHE) > _JSON_GZIP_CACHE_CAPACITY:
                            _JSON_GZIP_CACHE.popitem(last=False)
    if cache_revision is None:
        return _no_store(response)
    return _private_revalidate(response, cache_revision)


def _runtime_health_http_revision(value: object) -> object:
    """忽略只会随请求跳动、但不改变页面健康结论的诊断计数。"""

    if not isinstance(value, Mapping):
        return value
    output = dict(value)
    for field in _EARLY_SIGNALS_VOLATILE_HEALTH_FIELDS:
        output.pop(field, None)
    for field in (
        "candidate_monitor_five_minute",
        "candidate_monitor_thirty_minute",
    ):
        lane = output.get(field)
        if isinstance(lane, Mapping):
            compact_lane = dict(lane)
            compact_lane.pop("oldest_observation_age_seconds", None)
            output[field] = compact_lane
    native_gateway = output.get("native_gateway")
    if isinstance(native_gateway, Mapping):
        compact_gateway = {
            field: native_gateway[field]
            for field in _EARLY_SIGNALS_NATIVE_HEALTH_FIELDS
            if field in native_gateway
        }
        worker_pool = native_gateway.get("structure_worker_pool")
        if isinstance(worker_pool, Mapping):
            compact_gateway["structure_worker_pool"] = {
                field: worker_pool[field]
                for field in _EARLY_SIGNALS_WORKER_POOL_HEALTH_FIELDS
                if field in worker_pool
            }
        output["native_gateway"] = compact_gateway
    return output


def _compact_early_signals_transport(
    data: Mapping[str, object],
) -> dict[str, object]:
    """去掉页面未读取的审计正文，并把重复的信号证据编成共享目录。"""

    output = {
        key: value
        for key, value in data.items()
        if key not in _EARLY_SIGNALS_COMPACT_OMITTED_FIELDS
    }
    catalogs: dict[str, list[object]] = {
        field: [] for field in _EARLY_SIGNALS_CATALOG_FIELDS
    }
    catalog_indexes: dict[str, dict[str, int]] = {
        field: {} for field in _EARLY_SIGNALS_CATALOG_FIELDS
    }

    def compact_rows(value: object) -> object:
        if not isinstance(value, list):
            return value
        rows: list[object] = []
        for source in value:
            if not isinstance(source, Mapping):
                rows.append(source)
                continue
            row = dict(source)
            references: list[int] = []
            for field in _EARLY_SIGNALS_CATALOG_FIELDS:
                field_value = row.pop(field, None)
                fingerprint = sha256_json(field_value)
                index = catalog_indexes[field].get(fingerprint)
                if index is None:
                    index = len(catalogs[field])
                    catalog_indexes[field][fingerprint] = index
                    catalogs[field].append(field_value)
                references.append(index)
            row["signal_catalog_refs"] = references
            rows.append(row)
        return rows

    output["signals"] = compact_rows(output.get("signals"))
    output["manual_attention_signals"] = compact_rows(
        output.get("manual_attention_signals")
    )
    output["signal_catalog"] = {
        "schema": _EARLY_SIGNALS_CATALOG_SCHEMA,
        "fields": list(_EARLY_SIGNALS_CATALOG_FIELDS),
        "values": catalogs,
    }
    output["signal_transport"] = _EARLY_SIGNALS_CATALOG_TRANSPORT
    return output


def _early_signals_response_revision(
    data: Mapping[str, object],
    *,
    scope: str,
    transport: str,
) -> str | None:
    """用小型动态文档与页面版本组成压缩缓存键，避免重哈希整棵信号树。"""

    presentation_revision = data.get("presentation_revision")
    if not isinstance(presentation_revision, str) or not presentation_revision:
        return None
    try:
        return sha256_json(
            {
                "schema": "chanlun-early-signals-http-revision",
                "presentation_revision": presentation_revision,
                "scope": scope,
                "transport": transport,
                "runtime_health": _runtime_health_http_revision(
                    data.get("runtime_health")
                ),
                "manual_attention": data.get("manual_attention"),
                "us_monitor": data.get("us_monitor"),
                "realtime_notifications": data.get("realtime_notifications"),
            }
        )
    except (TypeError, ValueError):
        # 版本缓存属于纯性能优化；新增运行时诊断若暂时不能规范哈希，仍按旧路径响应。
        return None


def _no_store_html(template: str, *, status: int = 200, **context):
    return _no_store(make_response(render_template(template, **context), status))


def _trading_screening_service():
    value = current_app.extensions.get("decision_support_trading_screening")
    if (
        not callable(getattr(value, "snapshot", None))
        or not callable(getattr(value, "ensure_refresh", None))
    ):
        raise DecisionSupportError("trading_screening_unavailable")
    return value


def _presentation_scope(
    output: dict[str, object],
    scope: str,
) -> dict[str, object]:
    """限制每分钟轮询响应的展示范围，但不改变审计快照。"""

    signals = []
    for value in output.get("signals", []):
        if not isinstance(value, Mapping):
            continue
        effective_stage = lifecycle_stage_from_signal(value)
        if effective_stage not in _CURRENT_SELECTION_LIFECYCLE_STAGES:
            continue
        # 服务层投影已经写入规范阶段。仅兼容旧测试替身或旧缓存中的缺失/过期值，
        # 避免每分钟为数千个信号无条件再复制一次字典。
        if (
            effective_stage is not None
            and value.get("lifecycle_stage") != effective_stage
        ):
            signal = dict(value)
            signal["lifecycle_stage"] = effective_stage
        else:
            signal = value
        signals.append(signal)
    sector_triggered = [
        value
        for value in signals
        if isinstance(value.get("selection_sources"), (list, tuple))
        and "QMT_SECTOR_TRIGGER" in value["selection_sources"]
    ]
    manual_attention = output.get("manual_attention")
    manual_a_codes = {
        str(value.get("code"))
        for value in (
            manual_attention.get("symbols", [])
            if isinstance(manual_attention, Mapping)
            else []
        )
        if isinstance(value, Mapping) and value.get("market") == "a"
    }
    manual_attention_signals = [
        value for value in signals if str(value.get("code")) in manual_a_codes
    ]
    selected = sector_triggered if scope == "sector-trigger" else signals
    output["signals"] = selected
    output["manual_attention_signals"] = (
        manual_attention_signals if scope == "sector-trigger" else []
    )
    counts_by_stage: dict[str, int] = {}
    counts_by_point_type: dict[str, int] = {}
    for value in selected:
        stage = str(value.get("lifecycle_stage") or "unknown")
        counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
        point_type = str(value.get("point_type") or "unknown")
        counts_by_point_type[point_type] = (
            counts_by_point_type.get(point_type, 0) + 1
        )
    output["counts_by_stage"] = counts_by_stage
    output["counts_by_point_type"] = counts_by_point_type
    output["presentation_scope"] = scope
    output["presentation_signal_count"] = len(selected)
    output["sector_trigger_signal_count"] = len(sector_triggered)
    output["total_qualified_signal_count"] = len(signals)
    output["presentation_scope_hash_coverage"] = (
        "EXCLUDED_OPERATIONAL_PROJECTION"
    )
    return output


def _trading_screening_snapshot(
    *,
    scope: str = "all-qualified",
) -> dict[str, object]:
    service = _trading_screening_service()
    try:
        reference_provider = getattr(
            service,
            "presentation_snapshot_reference",
            None,
        )
        presentation_provider = getattr(
            service,
            "presentation_snapshot",
            None,
        )
        payload = (
            reference_provider()
            if callable(reference_provider)
            else (
                presentation_provider()
                if callable(presentation_provider)
                else service.snapshot()
            )
        )
    except Exception as exc:
        raise DecisionSupportError("trading_screening_unavailable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != SCHEMA
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or not isinstance(payload.get("sectors"), list)
        or not isinstance(payload.get("signals"), list)
        or not isinstance(payload.get("data_quality"), Mapping)
    ):
        raise DecisionSupportError("trading_screening_unavailable")
    output = dict(payload)
    if current_app.config.get("TRADING_SCREENING_BACKGROUND_ENABLED", True):
        health_provider = getattr(service, "health_snapshot", None)
        try:
            if not callable(health_provider):
                raise RuntimeError("trading screening health unavailable")
            runtime_health = dict(health_provider())
            runtime_health["required"] = True
        except Exception as exc:
            current_app.logger.exception(
                "trading screening runtime health snapshot failed"
            )
            runtime_health = {
                "required": True,
                "ready": False,
                "status": "not_ready",
                "last_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "reasons": ["screening_health_failed"],
            }
    else:
        runtime_health = {
            "required": False,
            "ready": True,
            "status": "disabled",
            "reasons": [],
        }
    screening_scope = output.get("screening_scope")
    if isinstance(screening_scope, Mapping):
        runtime_health.setdefault("screening_scope_mode", screening_scope.get("mode"))
        runtime_health.setdefault(
            "validation_cohort_size",
            screening_scope.get("validation_cohort_size"),
        )
        runtime_health.setdefault(
            "effective_monitor_universe_limit",
            screening_scope.get("effective_monitor_universe_limit"),
        )
    runtime_health["snapshot_hash_coverage"] = (
        "EXCLUDED_OPERATIONAL_METADATA"
    )
    output["runtime_health"] = runtime_health
    output["manual_attention"] = _manual_attention_snapshot()
    output["us_monitor"] = _us_monitor_snapshot()
    output["realtime_notifications"] = _realtime_review_snapshot()
    return _presentation_scope(output, scope)


def _realtime_review_snapshot() -> dict[str, object]:
    unavailable = {
        "schema": REALTIME_REVIEW_SCHEMA,
        "events": [],
        "event_count": 0,
        "pending_review_count": 0,
        "delivery_counts": {},
        "credentials_exposed": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    inbox = current_app.extensions.get("realtime_review_inbox")
    provider = getattr(inbox, "snapshot", None)
    if not callable(provider):
        return unavailable
    try:
        payload = provider()
    except Exception:
        current_app.logger.exception("realtime human review inbox snapshot failed")
        return unavailable
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != REALTIME_REVIEW_SCHEMA
        or not isinstance(payload.get("events"), list)
        or payload.get("credentials_exposed") is not False
        or payload.get("real_account_accessed") is not False
        or payload.get("real_order_transport_enabled") is not False
        or payload.get("automated_order_authorized") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        current_app.logger.error("realtime human review inbox contract invalid")
        return unavailable
    # Cross-market events restored from the inbox are page-visible only after
    # the holding monitor has proved the current admitted universe.  A-share
    # review events use a separate authority and remain untouched here.
    monitor = current_app.extensions.get("holding_group_monitor")
    scope_provider = getattr(monitor, "admitted_identities", None)
    try:
        raw_scope = scope_provider() if callable(scope_provider) else ()
        admitted = {
            (str(market).strip().lower(), str(code).strip())
            for market, code in raw_scope
            if str(market).strip() and str(code).strip()
        }
    except Exception:
        current_app.logger.exception("cross-market review scope unavailable")
        admitted = set()
    events = [
        dict(event)
        for event in payload["events"]
        if isinstance(event, Mapping)
        and (
            event.get("source") != "CROSS_MARKET_ATTENTION_MONITOR"
            or (
                str(event.get("market") or "").strip().lower(),
                str(event.get("code") or "").strip(),
            )
            in admitted
        )
    ]
    output = dict(payload)
    output["events"] = events
    output["event_count"] = len(events)
    output["pending_review_count"] = len(events)
    delivery_counts = payload.get("delivery_counts")
    output["delivery_counts"] = {
        str(status): sum(event.get("delivery_status") == status for event in events)
        for status in (
            delivery_counts if isinstance(delivery_counts, Mapping) else ()
        )
    }
    return output


def _manual_attention_snapshot() -> dict[str, object]:
    """把内部优先分组投影为不含账户语义的人工关注列表。"""

    unavailable = {
        "schema": "chanlun-local-manual-attention",
        "source": "LOCAL_GLOBAL_ATTENTION_GROUP",
        "group_name": "人工关注组",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": False,
        "status": "unavailable",
        "symbols": [],
        "declared_count": 0,
        "priority_monitor_count": 0,
        "cross_market_monitor_count": 0,
        "covered_monitor_count": 0,
        "unsupported_market_count": 0,
    }
    provider = current_app.config.get(
        "TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"
    )
    if not callable(provider):
        return unavailable
    try:
        value = provider()
    except Exception:
        current_app.logger.exception("local manual attention snapshot failed")
        return unavailable
    positions = value.get("positions") if isinstance(value, Mapping) else None
    allowed_scopes = {
        "A_SHARE_STRICT_DECISION_CORE",
        "NON_A_AUXILIARY_STRUCTURE_RADAR",
    }
    valid_positions = (
        isinstance(positions, list)
        and all(
            isinstance(row, Mapping)
            and isinstance(row.get("market"), str)
            and bool(row.get("market"))
            and isinstance(row.get("code"), str)
            and bool(row.get("code"))
            and isinstance(row.get("name"), str)
            and bool(row.get("name"))
            and row.get("monitoring_scope") in allowed_scopes
            and (
                (row.get("market") == "a")
                == (
                    row.get("monitoring_scope")
                    == "A_SHARE_STRICT_DECISION_CORE"
                )
            )
            for row in positions
        )
    )
    priority_count = (
        sum(
            row.get("monitoring_scope") == "A_SHARE_STRICT_DECISION_CORE"
            for row in positions
        )
        if valid_positions
        else -1
    )
    cross_market_count = (
        sum(
            row.get("monitoring_scope")
            == "NON_A_AUXILIARY_STRUCTURE_RADAR"
            for row in positions
        )
        if valid_positions
        else -1
    )
    covered_count = (
        priority_count + cross_market_count if valid_positions else -1
    )
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != "chanlun-local-manual-holdings"
        or value.get("source") != "LOCAL_GLOBAL_WATCHLIST_GROUP"
        or value.get("group_scope") != unavailable["group_scope"]
        or value.get("real_account_accessed") is not False
        or value.get("real_order_transport_enabled") is not False
        or value.get("automated_order_authorized") is not False
        or value.get("live_status") != "LIVE_DISABLED"
        or value.get("available") is not True
        or value.get("status") != "ready"
        or value.get("quantity_available") is not False
        or value.get("cost_basis_available") is not False
        or value.get("sellable_quantity_available") is not False
        or not valid_positions
        or value.get("declared_count") != len(positions)
        or value.get("priority_monitor_count") != priority_count
        or value.get("cross_market_monitor_count") != cross_market_count
        or value.get("covered_monitor_count") != covered_count
        or value.get("unsupported_market_count") != 0
    ):
        current_app.logger.error("local manual attention source contract invalid")
        return unavailable
    output = {
        **unavailable,
        "available": True,
        "status": "ready",
        "declared_count": len(positions),
        "priority_monitor_count": priority_count,
        "cross_market_monitor_count": cross_market_count,
        "covered_monitor_count": covered_count,
        "unsupported_market_count": 0,
    }
    a_share_codes = tuple(
        sorted(
            str(position["code"])
            for position in positions
            if position["market"] == "a"
        )
    )
    quote_batch = None
    if a_share_codes:
        try:
            quote_batch = isolated_a_share_quote_batch(current_app, a_share_codes)
        except Exception as exc:
            # The quote lane is display-only and deliberately non-blocking.  A
            # busy or restarting native process must not hide the declared
            # holding/watchlist universe or weaken monitor health semantics.
            current_app.logger.warning(
                "A-share manual holding quote snapshot unavailable: %s: %s",
                type(exc).__name__,
                str(exc)[:160],
            )
    quotes_by_code = {} if quote_batch is None else quote_batch.ticks()
    output["quote_status"] = (
        "unavailable"
        if quote_batch is None
        else "ready"
        if len(quotes_by_code) == len(a_share_codes)
        else "partial"
    )
    output["quote_market_open"] = (
        None if quote_batch is None else quote_batch.market_open
    )
    output["quote_requested_count"] = len(a_share_codes)
    output["quote_available_count"] = len(quotes_by_code)
    monitor = current_app.extensions.get("holding_group_monitor")
    try:
        monitor_health = (
            dict(monitor.health_snapshot())
            if monitor is not None
            and callable(getattr(monitor, "health_snapshot", None))
            else {
                "ready": False,
                "status": "unavailable",
                "reason_code": "HOLDING_MONITOR_UNAVAILABLE",
                "positions": [],
            }
        )
    except Exception:
        current_app.logger.exception("holding group monitor health failed")
        monitor_health = {
            "ready": False,
            "status": "unavailable",
            "reason_code": "HOLDING_MONITOR_HEALTH_UNAVAILABLE",
            "positions": [],
        }
    statuses = {
        (str(row.get("market")), str(row.get("code"))): row
        for row in monitor_health.get("positions", [])
        if isinstance(row, Mapping)
    }
    screening = current_app.extensions.get("decision_support_trading_screening")
    try:
        screening_health = (
            dict(screening.health_snapshot())
            if screening is not None
            and callable(getattr(screening, "health_snapshot", None))
            else {}
        )
    except Exception:
        current_app.logger.exception("A-share strict holding monitor health failed")
        screening_health = {}
    enriched = []
    for position in positions:
        quote = (
            quotes_by_code.get(position["code"])
            if position["market"] == "a"
            else None
        )
        if position["monitoring_scope"] == "A_SHARE_STRICT_DECISION_CORE":
            if screening_health.get("priority_monitoring_enabled") is not True:
                realtime_status = "error"
                realtime_reason = "A_SHARE_STRICT_MONITOR_DISABLED"
            elif screening_health.get("priority_monitor_ready") is not True:
                realtime_status = "error"
                realtime_reason = "A_SHARE_STRICT_MONITOR_DEGRADED"
            elif screening_health.get("priority_monitor_session_open") is True:
                realtime_status = "monitoring"
                realtime_reason = "A_SHARE_STRICT_DECISION_CORE_ACTIVE"
            else:
                realtime_status = "market_closed"
                realtime_reason = "A_SHARE_MARKET_CLOSED"
        else:
            status = statuses.get((position["market"], position["code"]), {})
            realtime_status = status.get("status", "awaiting_first_run")
            realtime_reason = status.get(
                "reason_code", "ATTENTION_MONITOR_AWAITING_FIRST_RUN"
            )
        realtime_reason = str(realtime_reason).replace(
            "HOLDING_", "ATTENTION_"
        )
        enriched.append(
            {
                "market": position["market"],
                "code": position["code"],
                "name": position["name"],
                "monitoring_scope": position["monitoring_scope"],
                "decision_mode": position.get("decision_mode"),
                "realtime_status": realtime_status,
                "realtime_reason_code": realtime_reason,
                "quote_available": quote is not None,
                "current_price": None if quote is None else float(quote.last),
                "change_percent": None if quote is None else float(quote.rate),
            }
        )
    output["symbols"] = enriched
    return output


def _unavailable_us_monitor(reason_code: str) -> dict[str, object]:
    return {
        "schema": "chanlun-us-realtime-monitor",
        "source_schema": "chanlun-attention-group-monitor",
        "market": "us",
        "market_scope": "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS",
        "decision_mode": "STRICT_STRUCTURE_OBSERVATION_ONLY",
        "auxiliary_only": True,
        "full_market_screening": False,
        "selection_candidates": False,
        "available": False,
        "ready": False,
        "status": "unavailable",
        "reason_code": reason_code,
        "job_registered": False,
        "notification_configured": False,
        "interval_seconds": None,
        "op_level": "5m",
        "mid_level": "1m",
        "big_level": "30m",
        "last_run_at": None,
        "last_completed_at": None,
        "stale": False,
        "declared_count": 0,
        "monitored_count": 0,
        "covered_count": 0,
        "active_count": 0,
        "closed_count": 0,
        "awaiting_count": 0,
        "failed_count": 0,
        "scope_limit": 12,
        "requested_count": 0,
        "mandatory_count": 0,
        "deferred_count": 0,
        "symbols": [],
        "notification_delivery": {},
        "research_only": True,
        "no_order_execution": True,
        "manual_review_required": True,
    }


def _us_monitor_snapshot() -> dict[str, object]:
    """提供美股全局分组雷达，页面将其作为非板块线索与 A 股并列。"""

    monitor = current_app.extensions.get("holding_group_monitor")
    if monitor is None or not callable(getattr(monitor, "health_snapshot", None)):
        return _unavailable_us_monitor("US_MONITOR_UNAVAILABLE")
    try:
        health = monitor.health_snapshot()
    except Exception:
        current_app.logger.exception("US attention monitor health failed")
        return _unavailable_us_monitor("US_MONITOR_HEALTH_UNAVAILABLE")
    if (
        not isinstance(health, Mapping)
        or health.get("schema") != HOLDING_GROUP_MONITOR_SCHEMA
        or health.get("real_account_accessed") is not False
        or health.get("real_order_transport_enabled") is not False
        or health.get("automated_order_authorized") is not False
        or health.get("live_status") != "LIVE_DISABLED"
        or (
            health.get("op_level"),
            health.get("mid_level"),
            health.get("big_level"),
        )
        != ("5m", "1m", "30m")
        or not isinstance(health.get("positions"), list)
    ):
        current_app.logger.error("US attention monitor health contract invalid")
        return _unavailable_us_monitor("US_MONITOR_CONTRACT_INVALID")

    allowed_statuses = {
        "monitoring",
        "market_closed",
        "warming_up",
        "awaiting_first_run",
        "error",
    }
    positions: list[dict[str, object]] = []
    for raw in health["positions"]:
        if not isinstance(raw, Mapping) or raw.get("market") != "us":
            continue
        groups = raw.get("groups", [])
        if (
            not isinstance(raw.get("code"), str)
            or not raw.get("code")
            or not isinstance(raw.get("name"), str)
            or not raw.get("name")
            or raw.get("status") not in allowed_statuses
            or raw.get("monitoring_scope") not in {"HOLDING", "WATCHLIST"}
            or not isinstance(groups, (list, tuple))
            or any(not isinstance(group, str) or not group for group in groups)
        ):
            current_app.logger.error("US attention monitor symbol invalid")
            return _unavailable_us_monitor("US_MONITOR_CONTRACT_INVALID")
        positions.append(
            {
                "market": "us",
                "code": raw["code"],
                "name": raw["name"],
                "status": raw["status"],
                "reason_code": str(raw.get("reason_code") or "").replace(
                    "HOLDING_", "ATTENTION_"
                ),
                "monitoring_scope": (
                    "MANUAL_ATTENTION"
                    if raw["monitoring_scope"] == "HOLDING"
                    else "WATCHLIST"
                ),
                "groups": [
                    "人工关注组" if group == "我的持仓" else group
                    for group in groups
                ],
            }
        )
    positions.sort(key=lambda row: str(row["code"]))

    active_count = sum(row["status"] == "monitoring" for row in positions)
    closed_count = sum(row["status"] == "market_closed" for row in positions)
    awaiting_count = sum(
        row["status"] in {"warming_up", "awaiting_first_run"}
        for row in positions
    )
    failed_count = sum(row["status"] == "error" for row in positions)
    covered_count = len(positions) - failed_count
    job_registered = health.get("job_registered") is True
    notification_configured = health.get("notification_configured") is True
    stale = health.get("stale") is True
    source_status = str(health.get("status") or "")
    if not job_registered:
        status, reason = "not_registered", "US_MONITOR_JOB_NOT_REGISTERED"
    elif not notification_configured:
        status, reason = "not_ready", "US_NOTIFICATION_NOT_CONFIGURED"
    elif source_status == "awaiting_first_run":
        status, reason = "awaiting_first_run", (
            "US_MONITOR_AWAITING_FIRST_RUN"
        )
    elif source_status in {"not_ready", "error"}:
        status, reason = source_status, (
            "US_MONITOR_NOT_READY"
        )
    elif (
        source_status == "degraded"
        and not positions
        and int(health.get("failed_count") or 0) > 0
    ):
        status, reason = "degraded", (
            "US_MONITOR_DEGRADED"
        )
    elif failed_count:
        status, reason = "degraded", "US_MONITOR_DEGRADED"
    elif stale:
        status, reason = "stale", "US_MONITOR_STALE"
    elif awaiting_count:
        status, reason = "warming_up", "MULTI_TIMEFRAME_WARMUP_INCOMPLETE"
    else:
        status, reason = "ready", "READY"
    ready = status == "ready"
    return {
        "schema": "chanlun-us-realtime-monitor",
        "source_schema": "chanlun-attention-group-monitor",
        "market": "us",
        "market_scope": "ADMITTED_US_SYMBOLS_IN_GLOBAL_GROUPS",
        "decision_mode": "STRICT_STRUCTURE_OBSERVATION_ONLY",
        "auxiliary_only": True,
        "full_market_screening": False,
        "selection_candidates": False,
        "available": True,
        "ready": ready,
        "status": status,
        "reason_code": reason,
        "job_registered": job_registered,
        "notification_configured": notification_configured,
        "interval_seconds": health.get("interval_seconds"),
        "op_level": health.get("op_level"),
        "mid_level": health.get("mid_level"),
        "big_level": health.get("big_level"),
        "last_run_at": health.get("last_run_at"),
        "last_completed_at": health.get("last_completed_at"),
        "stale": stale,
        "declared_count": len(positions),
        "monitored_count": active_count,
        "covered_count": covered_count,
        "active_count": active_count,
        "closed_count": closed_count,
        "awaiting_count": awaiting_count,
        "failed_count": failed_count,
        "scope_limit": health.get("scope_limit"),
        "requested_count": health.get("requested_count", len(positions)),
        "mandatory_count": health.get("mandatory_count", 0),
        "deferred_count": health.get("deferred_count", 0),
        "symbols": positions,
        "notification_delivery": (
            dict(health["notification_delivery"])
            if isinstance(health.get("notification_delivery"), Mapping)
            else {}
        ),
        "research_only": True,
        "no_order_execution": True,
        "manual_review_required": True,
    }


def _human_review_service() -> HumanReviewScreeningService:
    value = current_app.extensions.get("decision_support_human_review")
    if not isinstance(value, HumanReviewScreeningService) and not (
        callable(getattr(value, "snapshot", None))
        and callable(getattr(value, "append_feedback", None))
        and callable(getattr(value, "validate_chart_lock", None))
    ):
        raise DecisionSupportError("human_review_screening_unavailable")
    return value


def _realtime_only_human_review_snapshot(
    *,
    requested_source: str,
    reason_code: str,
) -> dict[str, object]:
    """Keep the independent realtime inbox reviewable when formal evidence fails.

    The formal candidate archive remains fail-closed: no candidate, feedback path,
    paper intent, order, or fill is synthesized from an invalid report.  Realtime
    notifications have their own durable, REVIEW_REQUIRED contract and therefore
    must not disappear merely because an unrelated historical bundle is stale.
    """

    return {
        "schema": HUMAN_REVIEW_WEB_SCHEMA,
        "source_kind": "realtime",
        "requested_source": requested_source,
        "source_options": [],
        "source_content_sha256": None,
        "decision_core_id": None,
        "decision_source_snapshot_id": None,
        "review_queue": [],
        "review_queue_count": 0,
        "reviewed_candidate_count": 0,
        "formal_review_available": False,
        "formal_review_unavailable_reason": reason_code,
        "paper_observation_eligible": False,
        "paper_observation_reason": "FORMAL_REVIEW_SOURCE_UNAVAILABLE",
        "virtual_intent_count": 0,
        "virtual_pending_intent_count": 0,
        "virtual_cancelled_intent_count": 0,
        "virtual_operations_cancelled_intent_count": 0,
        "virtual_reserved_sell_quantity": 0,
        "virtual_fill_count": 0,
        "virtual_open_position_count": 0,
        "highest_status": "REVIEW_REQUIRED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "orders_created": 0,
        "fills_created": 0,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


def _human_review_snapshot(source: str) -> dict[str, object]:
    try:
        service = _human_review_service()
        payload = (
            service.snapshot(source=source, include_evidence=False)
            if isinstance(service, HumanReviewScreeningService)
            else service.snapshot(source=source)
        )
    except HumanReviewScreenUnavailable as exc:
        if exc.code == "human_review_source_invalid":
            raise DecisionSupportError(exc.code) from exc
        current_app.logger.warning(
            "formal human review snapshot unavailable; serving independent "
            "realtime review inbox only: %s",
            exc.code,
        )
        payload = _realtime_only_human_review_snapshot(
            requested_source=source,
            reason_code=exc.code,
        )
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != HUMAN_REVIEW_WEB_SCHEMA
        or payload.get("highest_status") != "REVIEW_REQUIRED"
        or payload.get("live_status") != "LIVE_DISABLED"
        or payload.get("human_confirmation_required") is not True
        or payload.get("automated_order_authorized") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or not isinstance(payload.get("review_queue"), list)
    ):
        raise DecisionSupportError("human_review_screening_unavailable")
    output = dict(payload)
    output["realtime_notifications"] = _realtime_review_snapshot()
    output["forward_operations"] = _human_review_forward_operations()
    return output


def _human_review_forward_operations() -> dict[str, object]:
    """在复核页面展示与 ``/readyz`` 完全相同的前向结论。"""

    health_provider = current_app.extensions.get("health_snapshot")
    unavailable = {
        "schema": "chanlun-human-review-forward-operations",
        "session": None,
        "screening_market_data_as_of": None,
        "scheduler": {
            "ready": False,
            "status": "unresolved",
            "reason_code": "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE",
        },
        "qmt_runtime": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "QMT_RUNTIME_OBSERVATION_UNAVAILABLE",
        },
        "archive_gate": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "FORWARD_ARCHIVE_READINESS_UNAVAILABLE",
        },
        "delivery": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "FORWARD_DELIVERY_READINESS_UNAVAILABLE",
        },
        "complete": False,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    if not callable(health_provider):
        return unavailable
    try:
        health, _status_code = health_provider("readyz", "a", None)
        if not isinstance(health, Mapping):
            return unavailable
        components = health.get("components")
        if not isinstance(components, Mapping):
            return unavailable
        screening = components.get("trading_screening")
        market_data_as_of = (
            screening.get("market_data_as_of")
            if isinstance(screening, Mapping)
            else None
        )
        monitor_session = None
        if isinstance(market_data_as_of, str) and market_data_as_of:
            observed = datetime.fromisoformat(market_data_as_of)
            if observed.tzinfo is None:
                raise ValueError("screening market_data_as_of must be timezone-aware")
            monitor_session = observed.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date()
            health, _status_code = health_provider(
                "readyz",
                "a",
                monitor_session,
            )
            components = health.get("components")
            if not isinstance(components, Mapping):
                return unavailable
        scheduler = components.get("forward_scheduler")
        qmt_runtime = components.get("qmt_runtime")
        archive = components.get("forward_archive")
        delivery = components.get("forward_delivery")
        if not isinstance(archive, Mapping) or not isinstance(delivery, Mapping):
            return unavailable
        scheduler_value = (
            dict(scheduler)
            if isinstance(scheduler, Mapping)
            else dict(unavailable["scheduler"])
        )
        qmt_runtime_value = (
            dict(qmt_runtime)
            if isinstance(qmt_runtime, Mapping)
            else dict(unavailable["qmt_runtime"])
        )
        archive_value = dict(archive)
        delivery_value = dict(delivery)
        session_label = (
            monitor_session.isoformat()
            if monitor_session is not None
            else delivery_value.get("session") or archive_value.get("session")
        )
        return {
            "schema": "chanlun-human-review-forward-operations",
            "session": session_label,
            "screening_market_data_as_of": market_data_as_of,
            "scheduler": scheduler_value,
            "qmt_runtime": qmt_runtime_value,
            "archive_gate": archive_value,
            "delivery": delivery_value,
            "complete": bool(delivery_value.get("ready")),
            "highest_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }
    except Exception:
        current_app.logger.exception("human review forward operations failed")
        return unavailable


def _research_audit_root() -> str | Path:
    return current_app.config.get("RESEARCH_AUDIT_ROOT", _REPOSITORY_ROOT)


def _research_audit_snapshot() -> dict[str, object]:
    try:
        return build_research_audit_snapshot(_research_audit_root())
    except ResearchAuditUnavailable as exc:
        try:
            return build_research_audit_status_snapshot(
                _research_audit_root(),
                formal_error_code=exc.code,
                formal_error_details=exc.details,
            )
        except ResearchAuditUnavailable as status_exc:
            raise DecisionSupportError(status_exc.code) from status_exc


@decision_support_bp.get("/decision-support/early-screening")
@login_required
def early_screening():
    return _no_store_html("early_screening.html")


@decision_support_bp.get("/decision-support/early-signals")
@login_required
def early_signals():
    # 默认展示全部合格候选；板块触发子集只能由调用方显式选择。
    scope = str(request.args.get("scope") or "all-qualified").strip().lower()
    if scope not in {"sector-trigger", "all-qualified"}:
        scope = "all-qualified"
    transport = str(request.args.get("transport") or "full").strip().lower()
    if transport not in {"full", _EARLY_SIGNALS_CATALOG_TRANSPORT}:
        raise DecisionSupportError("trading_screening_transport_invalid", 400)
    data = _trading_screening_snapshot(scope=scope)
    cache_revision = _early_signals_response_revision(
        data,
        scope=scope,
        transport=transport,
    )
    if (
        cache_revision is not None
        and request.if_none_match.contains_weak(cache_revision)
    ):
        # 目录化本身也需要遍历所有当前候选。先用轻量语义版本短路，304 路径不再
        # 为浏览器已经持有的相同快照重建两百多万字节的传输文档。
        return _large_json_response(None, cache_revision=cache_revision)
    if transport == _EARLY_SIGNALS_CATALOG_TRANSPORT:
        data = _compact_early_signals_transport(data)
    return _large_json_response(
        _ok(data),
        cache_revision=cache_revision,
    )


@decision_support_bp.get("/decision-support/human-review/data")
@login_required
def human_review_data():
    source = str(request.args.get("source") or "latest").strip().lower()
    return _no_store(make_response(_ok(_human_review_snapshot(source))))


@decision_support_bp.get("/decision-support/human-review/candidate-detail")
@login_required
def human_review_candidate_detail():
    candidate_id = str(request.args.get("candidate_id") or "").strip()
    source_sha256 = str(
        request.args.get("source_content_sha256") or ""
    ).strip()
    provider = getattr(_human_review_service(), "candidate_detail", None)
    if not callable(provider):
        raise DecisionSupportError("human_review_candidate_detail_unavailable")
    try:
        payload = provider(
            candidate_id=candidate_id,
            source_sha256=source_sha256,
        )
    except HumanReviewScreenUnavailable as exc:
        status = 404 if exc.code == "human_review_candidate_not_found" else 400
        raise DecisionSupportError(exc.code, status) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema")
        != "chanlun-human-review-candidate-detail-web"
        or payload.get("candidate_id") != candidate_id
        or payload.get("source_content_sha256") != source_sha256
        or payload.get("highest_status") != "REVIEW_REQUIRED"
        or payload.get("human_confirmation_required") is not True
        or payload.get("automated_order_authorized") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise DecisionSupportError("human_review_candidate_detail_unavailable")
    return _no_store(make_response(_ok(payload)))


@decision_support_bp.post("/decision-support/human-review/feedback")
@login_required
def human_review_feedback():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        raise DecisionSupportError("human_review_feedback_invalid", 400)
    candidate_id = str(payload.get("candidate_id") or "")
    source_sha256 = str(payload.get("source_content_sha256") or "")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise DecisionSupportError("human_review_request_id_required", 400)
    reviewer = str(current_user.get_id() or "authenticated-reviewer")
    try:
        result = _human_review_service().append_feedback(
            candidate_id=candidate_id,
            source_sha256=source_sha256,
            reviewer=reviewer,
            values=payload,
            reviewed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            request_id=request_id,
        )
    except HumanReviewScreenUnavailable as exc:
        status = 409 if exc.code == "human_review_source_not_found" else 400
        raise DecisionSupportError(exc.code, status) from exc
    return _no_store(make_response(_ok(result)))


@decision_support_bp.get("/decision-support/research-audit")
@login_required
def research_audit():
    try:
        audit = build_research_audit_snapshot(_research_audit_root())
    except ResearchAuditUnavailable as exc:
        current_app.logger.warning("research audit unavailable: %s", exc.code)
        try:
            audit_status = build_research_audit_status_snapshot(
                _research_audit_root(),
                formal_error_code=exc.code,
                formal_error_details=exc.details,
            )
        except ResearchAuditUnavailable:
            audit_status = None
        return _no_store_html(
            "research_audit.html",
            audit=None,
            audit_status=audit_status,
            audit_error_code=exc.code,
            audit_error_details=exc.details,
        )
    return _no_store_html(
        "research_audit.html",
        audit=audit,
        audit_status=None,
        audit_error_code=None,
        audit_error_details=None,
    )


@decision_support_bp.get("/decision-support/research-audit/data")
@login_required
def research_audit_data():
    return _no_store(make_response(_ok(_research_audit_snapshot())))


__all__ = ("DecisionSupportError", "decision_support_bp")
