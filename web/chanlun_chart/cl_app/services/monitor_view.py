"""Bounded monitor responses; searching always covers the whole retained list."""

from collections import Counter
from copy import deepcopy
import math
from .monitor_scope import independent_monitor_periods

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
POINT_TYPES = ('1buy', '2buy', '3buy', '1sell', '2sell', '3sell')
ROW_FIELDS = {
    'warning_state', 'warning_assessment', 'candidate_updated', 'candidate_previous_price',
    'candidate_updated_at', 'preview_trigger_active', 'invalidation_reason_code', 'invalidation_checked_at',
    'lower_check_state', 'lower_assessment', 'lower_evidence_type',
    'center_sequence',
    'timeframe_policy', 'operating_role', 'background_30m',
    "holding",
    "data_freshness",
    "expected_closed_at",
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
    "point_confirmed_at",
    "external_notifications",
    "notification_suppressed_reason",
    "notification_expires_at",
    'preview_trigger','estimated_area_c','estimate_factor',
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
    "coverage_message",
    "coverage_reasons",
    "minute_monitoring",
    "warning_recovery_pending",
    "warning_reactivated",
    'exit_reasons','reference_point_type','stop_price','trigger_price','reverse_point_type','reverse_price',
}
STATE_FIELDS = {
    'source_waiting_periods',
    'suspended_periods',
    'quote_only_symbols',
    "enabled",
    "dingtalk_enabled",
    "point_notifications",
    "point_notification_version",
    "point_notification_error",
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
    scope = options.get(name + "_scope", "all")
    if scope not in ("all", "holdings"):
        raise ValueError("标的范围仅支持全部或我的持仓")
    point_type = options.get(name + '_point_type', 'all')
    if point_type not in ('all', *POINT_TYPES):
        raise ValueError('买卖点筛选不合法')
    return page, size, query, scope, point_type


def validate_page_options(options):
    """Validate response options before a control changes durable state."""
    if options is not None:
        for name in ("signals", "events", "pool"):
            _page_options(name, options)


def _page(rows, name, options):
    page, size, query, scope, point_type = _page_options(name, options)
    matched = [
        row
        for row in rows
        if (scope != "holdings" or row.get("holding"))
        and (point_type == 'all' or row.get('point_type') == point_type)
        and (not query
        or query
        in " ".join(
            str(v or "")
            for v in (
                row.get("code"),
                row.get("name"),
                row.get("frequency"),
                LABELS.get(row.get("point_type")),
            )
        ).casefold())
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
        "scope": scope,
        'point_type': point_type,
    }


def period_check_state(check, revision, expected):
    if check.get('error'):
        return 'limited'
    if (not check or revision and check.get('revision') != revision
            or expected is None or check.get('cutoff') is None):
        return 'pending'
    return 'ready' if check['cutoff'] == expected else 'updating'


def paged_state(raw, options, revision, source_current, *, expected_cutoffs=None, minute_scope=None):
    state = deepcopy({k: v for k, v in raw.items() if k in STATE_FIELDS})
    checks = raw.get("selection_checks", {})
    held = set(raw.get("holding_symbols", []))
    expected_cutoffs = expected_cutoffs or {}
    def freshness(market, code, frequency):
        paused=raw.get('suspended_periods',{}).get(f'{market}:{code}',{}).get(frequency)
        if paused:
            return 'suspended' if paused.get('state')=='suspended' else 'scheduled'
        if f'{market}:{code}:{frequency}' in raw.get('source_waiting_periods',{}):
            return 'pending'
        if not revision and source_current is not True:
            return 'pending'
        key=f'{market}:{code}:{frequency}'
        return period_check_state(checks.get(key,{}),revision,expected_cutoffs.get(key))
    signals = [
        {**row, "holding": f"{row.get('market', 'a')}:{row['code']}" in held,
         'data_freshness':freshness(row.get('market','a'),row['code'],row['frequency']),
         'expected_closed_at':expected_cutoffs.get(f"{row.get('market','a')}:{row['code']}:{row['frequency']}")}
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
            not row["holding"],
            -(row.get("selection_available_at") or row.get("available_at") or 0),
            row["code"],
            row["id"],
        )
    )
    seed = raw.get("seed")
    symbols = seed.get("symbols", []) if seed else []
    frequencies = seed.get("settings", {}).get("frequencies", []) if seed else []
    state['monitor_frequencies'] = list(raw.get('monitor_frequencies') or frequencies)
    coverage = Counter()
    failed_symbols = {
        f"{row.get('market', 'a')}:{row.get('code')}"
        for row in [*raw.get("errors", []), *raw.get("schedule_errors", [])]
    }
    contract_errors = {}
    for issue in [*raw.get('errors', []), *raw.get('schedule_errors', [])]:
        reasons = set(issue.get('reasons', [])) & {'SESSION_CALENDAR_UNAVAILABLE', 'BAR_TIME_LABEL_UNAVAILABLE'}
        if reasons:
            key = f"{issue.get('market', 'a')}:{issue.get('code')}"
            contract_errors.setdefault(key, []).append((issue.get('frequency'), reasons))
    pool = []
    for symbol in symbols:
        key = f"{symbol.get('market', 'a')}:{symbol['code']}"
        minute = (minute_scope or {}).get('per_symbol', {}).get(key, {})
        periods = []
        enabled_periods = independent_monitor_periods(raw, key)
        for frequency in enabled_periods:
            value = freshness(symbol.get('market','a'),symbol['code'],frequency)
            if (frequency == '1m' and value == 'updating'
                    and minute.get('classification') == 'background'
                    and minute.get('due') is False and minute.get('clock_known') is True):
                value = 'scheduled'
            periods.append(value)
        health = (
            "limited"
            if key in failed_symbols or 'limited' in periods
            else 'pending' if not periods or 'pending' in periods
            else 'updating' if 'updating' in periods
            else 'scheduled' if 'scheduled' in periods
            else 'suspended' if 'suspended' in periods
            else 'ready'
        )
        blocked = set()
        coverage_reasons = set()
        for frequency, reasons in contract_errors.get(key, []):
            blocked.update(enabled_periods if frequency is None else {frequency})
            coverage_reasons.update(reasons)
        if enabled_periods and enabled_periods <= blocked:
            health = 'unsupported'
        paused=raw.get('suspended_periods',{}).get(key,{})
        paused_message=''
        if paused:
            fact=next(iter(paused.values()))
            paused_message=(f"停牌至 {fact.get('resume_label','')}，等待复牌后的有效K线"
                if any(v.get('state')=='suspended' for v in paused.values()) else
                '复牌后等待首根完整K线：'+'、'.join(sorted(paused)))
        coverage[health] += 1
        pool.append({**symbol, "coverage_state": health, "holding": key in held,
                     'minute_monitoring': minute.get('classification'),
                     **({'coverage_message':paused_message} if paused_message else {}),
                     **({'coverage_reasons': sorted(coverage_reasons),
                         'coverage_message': '；'.join(label for reason, label in (
                             ('SESSION_CALENDAR_UNAVAILABLE', '缺少已核实的交易时段'),
                             ('BAR_TIME_LABEL_UNAVAILABLE', '分钟K线时间口径尚未核实')) if reason in coverage_reasons)}
                        if coverage_reasons else {})})
    pool.sort(key=lambda row: not row["holding"])
    holding_coverage = Counter(row['coverage_state'] for row in pool if row['holding'])
    risk_bound, risk_attention = set(), set()
    for plan in raw.get('holding_risk_plans', {}).values():
        if not isinstance(plan, dict) or not plan.get('active'):
            continue
        symbol = f"{plan.get('market', 'a')}:{plan.get('code')}"
        if symbol not in held:
            continue
        risk_bound.add(symbol)
        if plan.get('status') != 'active' or plan.get('reverse_check_pending'):
            risk_attention.add(symbol)
    state['holding_summary'] = {
        'count': len(held), 'signals': sum(row['holding'] for row in signals),
        **{key: holding_coverage[key] for key in ('ready','updating','limited','pending')},
        'source_current': raw.get('holding_source_current', True),
        'risk_bound_symbols': len(risk_bound), 'risk_unbound_symbols': len(held - risk_bound),
        'risk_attention_symbols': len(risk_attention),
    }
    state["coverage"] = {
        "total": len(pool),
        **{key: coverage[key] for key in ("ready", "updating", "limited", "pending", "scheduled")},
    }
    if coverage['unsupported']:
        state['coverage']['unsupported'] = coverage['unsupported']
    if holding_coverage['unsupported']:
        state['holding_summary']['unsupported'] = holding_coverage['unsupported']
    if coverage['suspended']:
        state['coverage']['suspended']=coverage['suspended']
    if holding_coverage['suspended']:
        state['holding_summary']['suspended']=holding_coverage['suspended']
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
