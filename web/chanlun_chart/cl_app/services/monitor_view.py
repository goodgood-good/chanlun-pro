"""Bounded monitor responses; searching always covers the whole retained list."""

from collections import Counter
from copy import deepcopy
import math

LABELS = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
    "consolidation_up": "向上盘整背驰",
    "consolidation_down": "向下盘整背驰",
    'risk_stop':'结构止损触发','risk_reverse':'反向信号确认',
}
ROW_FIELDS = {
    "id",
    "code",
    "market",
    "name",
    "frequency",
    "point_type",
    "stage",
    "point_status",
    "available_at",
    "source_closed_at",
    "recorded_at",
    "change",
    "anchor_price",
    "signal_type",
    "core_position",
    "comparison_form",
    "macd_area_ratio",
    "macd_area_a",
    "macd_area_c",
    "first_warned_at",
    "invalidation_reason",
    "following_segment_state",
    "following_segment_direction",
    "lower_confirmation_state",
    "lower_frequency",
    "coverage_state",
    "warning_recovery_pending",
    "warning_reactivated",
    'exit_reasons','reference_point_type','stop_price','trigger_price','reverse_point_type','reverse_price',
}
STATE_FIELDS = {
    "enabled",
    "dingtalk_enabled",
    "settings",
    "daily",
    "last_check_at",
    "last_error",
    "watchlist_error",
    "daily_schedule_error",
    "pending_symbols_count",
    "last_batch_completed_at",
    "last_cutoffs_by_market",
    "last_cutoffs",
    "errors",
    "schedule_errors",
    "holding_source_current",
}


def _page_options(name, options):
    try:
        page = max(1, int(options.get(name + "_page", 1)))
        size = int(options.get(name + "_size", 50))
    except (ValueError, TypeError):
        raise ValueError("分页参数不合法") from None
    if size not in (25, 50, 100):
        raise ValueError("每页数量仅支持 25、50 或 100")
    query = str(options.get(name + "_q", "")).strip().casefold()
    if len(query) > 100:
        raise ValueError("搜索内容不能超过 100 个字符")
    return page, size, query


def validate_page_options(options):
    """Validate response options before a control changes durable state."""
    if options is not None:
        for name in ("signals", "events", "pool"):
            _page_options(name, options)


def _page(rows, name, options):
    page, size, query = _page_options(name, options)
    matched = [
        row
        for row in rows
        if not query
        or query
        in " ".join(
            str(v or "")
            for v in (
                row.get("code"),
                row.get("name"),
                row.get("frequency"),
                LABELS.get(row.get("point_type")),
            )
        ).casefold()
    ]
    total = len(matched)
    count = max(1, math.ceil(total / size))
    page = min(page, count)
    projected = [
        {k: v for k, v in row.items() if k in ROW_FIELDS}
        for row in matched[(page - 1) * size : page * size]
    ]
    return deepcopy(projected), {
        "page": page,
        "size": size,
        "pages": count,
        "total": total,
        "overall": len(rows),
        "query": query,
    }


def paged_state(raw, options, revision, source_current):
    state = deepcopy({k: v for k, v in raw.items() if k in STATE_FIELDS})
    checks = raw.get("selection_checks", {})
    signals = [
        row
        for row in raw["signals"].values()
        if row.get("stage") != "invalidated"
        and (
            checks.get(
                f"{row.get('market', 'a')}:{row['code']}:{row['frequency']}", {}
            ).get("revision")
            == revision
            if revision
            else source_current is not False
        )
    ]
    signals.sort(
        key=lambda row: (
            -(row.get("selection_available_at") or row.get("available_at") or 0),
            row["code"],
            row["id"],
        )
    )
    seed = raw.get("seed")
    symbols = seed.get("symbols", []) if seed else []
    frequencies = seed.get("settings", {}).get("frequencies", []) if seed else []
    coverage = Counter()
    failed_symbols = {
        f"{row.get('market', 'a')}:{row.get('code')}"
        for row in [*raw.get("errors", []), *raw.get("schedule_errors", [])]
    }
    pool = []
    for symbol in symbols:
        key = f"{symbol.get('market', 'a')}:{symbol['code']}"
        periods = [checks.get(f"{key}:{frequency}", {}) for frequency in frequencies]
        current = [
            row
            for row in periods
            if row and (not revision or row.get("revision") == revision)
        ]
        health = (
            "limited"
            if key in failed_symbols or any(row.get("error") for row in current)
            else "ready"
            if periods and len(current) == len(periods)
            else "pending"
        )
        coverage[health] += 1
        pool.append({**symbol, "coverage_state": health})
    state["coverage"] = {
        "total": len(pool),
        **{key: coverage[key] for key in ("ready", "limited", "pending")},
    }
    state["pool_markets"] = dict(Counter(row["market"] for row in symbols))
    state["pagination"] = {}
    for name, rows in (("signals", signals), ("events", raw["events"]), ("pool", pool)):
        items, meta = _page(rows, name, options)
        state["pagination"][name] = meta
        if name == "signals":
            state["signals"] = {row["id"]: row for row in items}
        elif name == "events":
            state["events"] = items
        else:
            state["seed"] = (
                {
                    **{
                        k: v
                        for k, v in seed.items()
                        if k
                        in ("run_id", "watchlist_count", "holding_count", "error_count")
                    },
                    "count": len(symbols),
                    "screening_count": len(seed.get("screening_symbols", [])) - len(seed.get('screening_rechecks', {})),
                    "screening_recheck_count": len(seed.get('screening_rechecks', {})),
                    "symbols": items,
                }
                if seed
                else None
            )
    return state
