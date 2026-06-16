"""Strategy candidate catalog and scoring helpers for recursive Chanlun trading.

The module keeps the current evidence-backed A/US live strategy candidates in
one place.  It also provides a small scoring layer that can rank either
backtest summaries or paper-ledger summaries by return and drawdown.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Mapping, Optional

from chanlun.recursive_bt.strategy_optimizer.utils import (
    _avg,
    _float_first,
    _float_values,
)

from chanlun.recursive_bt.strategy_optimizer.constants import (
    A_ALL_5M30M_DEFAULT_SUMMARY,
    A_ALL_5M30M_REGIME_BEAR3BOOST_SUMMARY,
    A_ALL_5M30M_REGIME_COMBO_B140_SUMMARY,
    A_ALL_5M30M_REGIME_COMBO_SUMMARY,
    A_MTF3_REGIME_BEAR3BOOST_SUMMARY,
    A_MTF3_REGIME_COMBO_B140_SUMMARY,
    A_MTF3_REGIME_COMBO_SUMMARY,
    A_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
    A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    REGIME_ORDER,
    US_2026Q1_MTF3_DEFAULT_SUMMARY,
    US_2026Q1_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
    US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    US_MTF3_DEFAULT_SUMMARY,
    US_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
    US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
)


from chanlun.recursive_bt.strategy_optimizer.models import (
    ScoreWeights,
    ScoredRuntimeSummary,
)


from chanlun.recursive_bt.strategy_optimizer.candidates import (
    a_selection_systems,
    default_strategy_candidates,
)


from chanlun.recursive_bt.strategy_optimizer.scoring import (
    default_runtime_summary_sources,
    discover_backtest_summary_sources,
    load_summary,
    rank_candidates_by_evidence,
    score_runtime_sources,
)


from chanlun.recursive_bt.strategy_optimizer.reports_mtf3 import (
    write_mtf3_cache_coverage_report,
)


from chanlun.recursive_bt.strategy_optimizer.reports_strategy import (
    write_strategy_attribution_report,
)


def build_candidate_report(market: Optional[str] = None) -> dict:
    ranked = rank_candidates_by_evidence(market)
    return {
        "market": market or "all",
        "selection_systems": [asdict(system) for system in a_selection_systems()],
        "candidates": [
            {
                **candidate.as_dict(),
                "score": score.as_dict(),
                "monitor_config": candidate.monitor_config(),
            }
            for candidate, score in ranked
        ],
    }


def build_optimization_report(
    market: Optional[str] = None,
    *,
    include_discovered: bool = True,
    report_dir: str | Path = "D:/chanlun_pro/reports",
    weights: ScoreWeights = ScoreWeights(),
    current_candidate_ids: Mapping[str, str] | None = None,
) -> dict:
    markets = (market,) if market else ("a", "us")
    sources = default_runtime_summary_sources(markets)
    if include_discovered:
        sources.extend(discover_backtest_summary_sources(report_dir, markets))
    runtime, missing = score_runtime_sources(sources, weights=weights)
    candidate_report = build_candidate_report(market)
    best_candidate_by_market = _best_candidate_ids(candidate_report["candidates"])
    best_runtime_by_market = _best_runtime_ids(runtime)
    report = {
        "market": market or "all",
        "weights": asdict(weights),
        "selection_systems": candidate_report["selection_systems"],
        "candidate_ranking": candidate_report["candidates"],
        "runtime_ranking": [item.as_dict() for item in runtime],
        "missing_sources": missing,
        "recommendations": [
            {
                "market": market_key,
                "embedded_candidate": best_candidate_by_market.get(market_key),
                "best_runtime_summary": best_runtime_by_market.get(market_key),
            }
            for market_key in sorted(
                set(best_candidate_by_market) | set(best_runtime_by_market)
            )
        ],
    }
    report["runtime_observations"] = build_runtime_observations(report)
    report["action_suggestions"] = build_action_suggestions(
        report,
        current_candidate_ids=current_candidate_ids,
    )
    return report


def write_optimization_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    output_decision: str | Path | None = None,
    output_decision_state: str | Path | None = None,
    output_runtime_overrides: str | Path | None = None,
    market: Optional[str] = None,
    include_discovered: bool = True,
    report_dir: str | Path = "D:/chanlun_pro/reports",
    weights: ScoreWeights = ScoreWeights(),
    current_candidate_ids: Mapping[str, str] | None = None,
    decision_confirmation_threshold: int = 3,
) -> dict:
    report = build_optimization_report(
        market,
        include_discovered=include_discovered,
        report_dir=report_dir,
        weights=weights,
        current_candidate_ids=current_candidate_ids,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_optimization_markdown(report), encoding="utf-8")
    decision_artifact = build_decision_artifact(report)
    decision_state = None
    if output_decision is not None:
        output_decision = Path(output_decision)
        output_decision.parent.mkdir(parents=True, exist_ok=True)
        output_decision.write_text(
            json.dumps(decision_artifact, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if output_decision_state is not None:
        decision_state = update_decision_state_file(
            output_decision_state,
            decision_artifact,
            confirmation_threshold=decision_confirmation_threshold,
        )
    if output_runtime_overrides is not None:
        if decision_state is None:
            decision_state = build_decision_state(
                decision_artifact,
                confirmation_threshold=decision_confirmation_threshold,
            )
        write_runtime_overrides_file(output_runtime_overrides, decision_state)
    return report


def render_optimization_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Strategy Optimization Report",
        "",
        f"Market: {report.get('market', 'all')}",
        "",
        "## Embedded Candidates",
        "",
        "| Rank | Market | Candidate | Score | Return | Max DD | Trades |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for idx, candidate in enumerate(report.get("candidate_ranking", []) or [], 1):
        score = candidate.get("score") or {}
        lines.append(
            "| {rank} | {market} | {name} | {score:.4f} | {ret:.1%} | {dd:.1%} | {trades} |".format(
                rank=idx,
                market=candidate.get("market", ""),
                name=candidate.get("id", ""),
                score=float(score.get("score") or 0.0),
                ret=float(score.get("total_return") or 0.0),
                dd=float(score.get("max_drawdown") or 0.0),
                trades=int(score.get("trade_count") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## Runtime Summaries",
            "",
            "| Rank | Market | Source | Score | Return | Max DD | Trades | Path |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for idx, item in enumerate(report.get("runtime_ranking", []) or [], 1):
        source = item.get("source") or {}
        score = item.get("score") or {}
        lines.append(
            "| {rank} | {market} | {source_id} | {score:.4f} | {ret:.1%} | {dd:.1%} | {trades} | `{path}` |".format(
                rank=idx,
                market=source.get("market", ""),
                source_id=source.get("id", ""),
                score=float(score.get("score") or 0.0),
                ret=float(score.get("total_return") or 0.0),
                dd=float(score.get("max_drawdown") or 0.0),
                trades=int(score.get("trade_count") or 0),
                path=source.get("path", ""),
            )
        )
    if report.get("action_suggestions"):
        lines.extend(
            [
                "",
                "## Action Suggestions",
                "",
                "| Market | Action | Current | Target | Runtime Ref | Reason |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.get("action_suggestions", []) or []:
            lines.append(
                "| {market} | {action} | {current} | {target} | {runtime} | {reason} |".format(
                    market=item.get("market", ""),
                    action=item.get("action", ""),
                    current=item.get("current_candidate", ""),
                    target=item.get("target_candidate", ""),
                    runtime=item.get("best_runtime_summary", ""),
                    reason=str(item.get("reason", "")).replace("|", "/"),
                )
            )
    if report.get("runtime_observations"):
        lines.extend(
            [
                "",
                "## Runtime Observations",
                "",
                "| Market | Source | Severity | Observation | Reason |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.get("runtime_observations", []) or []:
            lines.append(
                "| {market} | {source} | {severity} | {observation} | {reason} |".format(
                    market=item.get("market", ""),
                    source=item.get("source_id", ""),
                    severity=item.get("severity", ""),
                    observation=item.get("observation", ""),
                    reason=str(item.get("reason", "")).replace("|", "/"),
                )
            )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(
                f"- `{item.get('id')}` ({item.get('kind')}): {item.get('reason')} `{item.get('path')}`"
            )
    lines.append("")
    return "\n".join(lines)


def build_runtime_observations(
    report: Mapping[str, object],
    *,
    return_gap_alert: float = 0.10,
    negative_excess_alert: float = -0.05,
    min_trade_count: int = 20,
) -> list[dict]:
    """Flag runtime drift without changing strategy decisions.

    Observations are deliberately informational. They highlight recent
    live-parity underperformance while leaving action gating to the safer
    candidate/decision-state pipeline.
    """

    top_candidate_by_market: dict[str, Mapping[str, object]] = {}
    for candidate in report.get("candidate_ranking", []) or []:
        if not isinstance(candidate, Mapping):
            continue
        market = str(candidate.get("market") or "").strip().lower()
        if market and market not in top_candidate_by_market:
            top_candidate_by_market[market] = candidate

    observations: list[dict] = []
    for item in report.get("runtime_ranking", []) or []:
        if not isinstance(item, Mapping):
            continue
        source = item.get("source") or {}
        if not isinstance(source, Mapping):
            continue
        market = str(source.get("market") or "").strip().lower()
        source_id = str(source.get("id") or "")
        if source_id != f"{market}_live_parity_backtest":
            continue
        candidate = top_candidate_by_market.get(market)
        if not candidate:
            continue
        score = item.get("score") or {}
        summary = item.get("summary") or {}
        if not isinstance(score, Mapping) or not isinstance(summary, Mapping):
            continue
        trade_count = int(_float_first(score, "trade_count"))
        if trade_count < min_trade_count:
            continue
        candidate_score = candidate.get("score") or {}
        candidate_return = _float_first(candidate_score, "total_return")
        runtime_return = _float_first(score, "total_return")
        runtime_dd = _float_first(score, "max_drawdown")
        excess = _float_first(summary, "excess")
        reasons: list[str] = []
        if candidate_return - runtime_return >= return_gap_alert:
            reasons.append(
                f"runtime return {runtime_return:.1%} trails embedded evidence {candidate_return:.1%}"
            )
        if excess <= negative_excess_alert:
            reasons.append(f"runtime excess is {excess:.1%}")
        if not reasons:
            continue
        observations.append(
            {
                "market": market,
                "source_id": source_id,
                "severity": "watch",
                "observation": "live_parity_runtime_lag",
                "target_candidate": str(candidate.get("id") or ""),
                "runtime_return": runtime_return,
                "candidate_return": candidate_return,
                "runtime_max_drawdown": runtime_dd,
                "runtime_excess": excess,
                "trade_count": trade_count,
                "reason": "; ".join(reasons),
            }
        )
    return observations


def build_action_suggestions(
    report: Mapping[str, object],
    *,
    current_candidate_ids: Mapping[str, str] | None = None,
    drawdown_alert_multiplier: float = 1.25,
    drawdown_alert_floor: float = 0.02,
    score_gap_alert: float = 0.10,
    runtime_gap_min_trades: int = 10,
) -> list[dict]:
    """Turn rankings into concrete keep/switch/degrade suggestions.

    The suggestion layer is deliberately conservative: embedded candidates stay
    authoritative for monitor config, while runtime summaries are used to warn
    about paper-ledger drift and drawdown deterioration.
    """

    current_candidate_ids = {
        str(market).strip().lower(): str(candidate).strip()
        for market, candidate in (current_candidate_ids or {}).items()
    }
    candidates = list(report.get("candidate_ranking", []) or [])
    candidate_by_id = {
        str(candidate.get("id") or ""): candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
    }
    candidates_by_market = _candidate_rankings_by_market(candidates)
    runtime_by_market_id = _runtime_by_market_id(report.get("runtime_ranking", []) or [])

    suggestions: list[dict] = []
    for rec in report.get("recommendations", []) or []:
        if not isinstance(rec, Mapping):
            continue
        market = str(rec.get("market") or "").strip().lower()
        if not market:
            continue
        embedded_id = str(rec.get("embedded_candidate") or "").strip()
        runtime_id = str(rec.get("best_runtime_summary") or "").strip()
        current_id = current_candidate_ids.get(market) or embedded_id
        current_known = current_id in candidate_by_id
        if not current_id:
            current_id = "unknown"
        target_id = embedded_id
        action = "keep_candidate" if current_id == embedded_id else "switch_candidate"
        reason = (
            "current config matches the top embedded candidate"
            if action == "keep_candidate"
            else "current config does not match the top embedded candidate"
        )
        if not current_known and current_id not in {embedded_id, "unknown"}:
            reason = "current config is custom or unmatched against embedded candidates"

        target_candidate = candidate_by_id.get(target_id) or {}
        paper_runtime = runtime_by_market_id.get(market, {}).get(f"{market}_paper_ledger")
        low_dd_candidate = _lowest_drawdown_candidate(
            candidates_by_market.get(market, []),
            exclude_id=target_id,
        )
        if paper_runtime and target_candidate:
            paper_score = paper_runtime.get("score") or {}
            target_score = target_candidate.get("score") or {}
            paper_dd = _float_first(paper_score, "max_drawdown")
            target_dd = _float_first(target_score, "max_drawdown")
            if low_dd_candidate:
                threshold = max(
                    target_dd * drawdown_alert_multiplier,
                    target_dd + drawdown_alert_floor,
                )
                low_dd_score = low_dd_candidate.get("score") or {}
                low_dd = _float_first(low_dd_score, "max_drawdown")
                if target_dd > 0 and low_dd < target_dd and paper_dd > threshold:
                    action = "degrade_candidate"
                    target_id = str(low_dd_candidate.get("id") or target_id)
                    target_candidate = low_dd_candidate
                    reason = (
                        f"paper drawdown {paper_dd:.1%} exceeds alert threshold "
                        f"{threshold:.1%}; prefer lower-drawdown candidate"
                    )
            if action == "keep_candidate":
                best_runtime = runtime_by_market_id.get(market, {}).get(runtime_id)
                paper_trades = int(_float_first(paper_score, "trade_count"))
                if best_runtime and paper_trades >= max(0, runtime_gap_min_trades):
                    paper_runtime_score = _float_first(paper_score, "score")
                    best_runtime_score = _float_first(
                        best_runtime.get("score") or {},
                        "score",
                    )
                    if best_runtime_score - paper_runtime_score > score_gap_alert:
                        action = "review_runtime_gap"
                        reason = (
                            f"paper score trails best runtime summary by "
                            f"{best_runtime_score - paper_runtime_score:.4f}"
                        )

        suggestions.append(
            {
                "market": market,
                "action": action,
                "current_candidate": current_id,
                "target_candidate": target_id,
                "best_runtime_summary": runtime_id,
                "reason": reason,
                "monitor_config": dict(
                    (target_candidate or {}).get("monitor_config") or {}
                ),
            }
        )
    return suggestions


def build_decision_artifact(report: Mapping[str, object]) -> dict:
    """Extract a compact, machine-readable action file from a full report."""

    decisions = []
    for item in report.get("action_suggestions", []) or []:
        if not isinstance(item, Mapping):
            continue
        action = str(item.get("action") or "")
        decisions.append(
            {
                "market": str(item.get("market") or ""),
                "action": action,
                "risk_state": _risk_state_for_action(action),
                "current_candidate": str(item.get("current_candidate") or ""),
                "target_candidate": str(item.get("target_candidate") or ""),
                "best_runtime_summary": str(item.get("best_runtime_summary") or ""),
                "reason": str(item.get("reason") or ""),
                "ready_to_apply": action in {"switch_candidate", "degrade_candidate"},
                "requires_review": action == "review_runtime_gap",
                "monitor_config": dict(item.get("monitor_config") or {}),
            }
        )
    return {
        "version": 1,
        "market": str(report.get("market") or "all"),
        "decisions": decisions,
    }


def build_decision_state(
    decision_artifact: Mapping[str, object],
    previous_state: Mapping[str, object] | None = None,
    *,
    confirmation_threshold: int = 3,
    now: str | None = None,
) -> dict:
    """Track repeated strategy decisions before allowing config changes."""

    now = now or _dt.datetime.now().isoformat(timespec="seconds")
    threshold = max(1, int(confirmation_threshold or 1))
    previous_by_market = {
        str(item.get("market") or ""): item
        for item in (previous_state or {}).get("market_states", []) or []
        if isinstance(item, Mapping)
    }
    market_states = []
    for decision in decision_artifact.get("decisions", []) or []:
        if not isinstance(decision, Mapping):
            continue
        market = str(decision.get("market") or "")
        action = str(decision.get("action") or "")
        target = str(decision.get("target_candidate") or "")
        key = f"{market}|{action}|{target}"
        prev = previous_by_market.get(market) or {}
        same_decision = prev.get("decision_key") == key
        confirmations = int(prev.get("confirmations") or 0) + 1 if same_decision else 1
        ready_to_apply = bool(decision.get("ready_to_apply"))
        requires_review = bool(decision.get("requires_review"))
        apply_allowed = ready_to_apply and confirmations >= threshold
        market_states.append(
            {
                "market": market,
                "decision_key": key,
                "action": action,
                "risk_state": str(decision.get("risk_state") or ""),
                "current_candidate": str(decision.get("current_candidate") or ""),
                "target_candidate": target,
                "confirmations": confirmations,
                "confirmation_threshold": threshold,
                "apply_allowed": apply_allowed,
                "requires_review": requires_review,
                "status": _decision_status(
                    action=action,
                    ready_to_apply=ready_to_apply,
                    requires_review=requires_review,
                    apply_allowed=apply_allowed,
                ),
                "first_seen": str(prev.get("first_seen") or now) if same_decision else now,
                "last_seen": now,
                "reason": str(decision.get("reason") or ""),
                "monitor_config": dict(decision.get("monitor_config") or {}),
            }
        )
    return {
        "version": 1,
        "market": str(decision_artifact.get("market") or "all"),
        "updated_at": now,
        "confirmation_threshold": threshold,
        "market_states": market_states,
        "apply_allowed_count": sum(1 for item in market_states if item["apply_allowed"]),
        "review_required_count": sum(1 for item in market_states if item["requires_review"]),
    }


def update_decision_state_file(
    path: str | Path,
    decision_artifact: Mapping[str, object],
    *,
    confirmation_threshold: int = 3,
    now: str | None = None,
) -> dict:
    path = Path(path)
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    state = build_decision_state(
        decision_artifact,
        previous,
        confirmation_threshold=confirmation_threshold,
        now=now,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def build_runtime_overrides(decision_state: Mapping[str, object]) -> dict:
    """Build next-run monitor overrides from confirmed decision state."""

    overrides = []
    for item in decision_state.get("market_states", []) or []:
        if not isinstance(item, Mapping) or not item.get("apply_allowed"):
            continue
        config = dict(item.get("monitor_config") or {})
        if not config:
            continue
        overrides.append(
            {
                "market": str(item.get("market") or ""),
                "action": str(item.get("action") or ""),
                "risk_state": str(item.get("risk_state") or ""),
                "target_candidate": str(item.get("target_candidate") or ""),
                "decision_key": str(item.get("decision_key") or ""),
                "confirmations": int(item.get("confirmations") or 0),
                "confirmation_threshold": int(item.get("confirmation_threshold") or 0),
                "reason": str(item.get("reason") or ""),
                "monitor_config": config,
            }
        )
    return {
        "version": 1,
        "updated_at": str(decision_state.get("updated_at") or ""),
        "source": "strategy_decision_state",
        "overrides": overrides,
        "override_count": len(overrides),
    }


def write_runtime_overrides_file(
    path: str | Path,
    decision_state: Mapping[str, object],
) -> dict:
    overrides = build_runtime_overrides(decision_state)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    return overrides


def build_bs_point_ratio_state(
    bs_point_report: Mapping[str, object],
    previous_state: Mapping[str, object] | None = None,
    *,
    confirmation_threshold: int = 3,
    now: str | None = None,
) -> dict:
    now = now or _dt.datetime.now().isoformat(timespec="seconds")
    threshold = max(1, int(confirmation_threshold or 1))
    previous_by_key = {
        str(item.get("state_key") or ""): item
        for item in (previous_state or {}).get("ratio_states", []) or []
        if isinstance(item, Mapping)
    }
    ratio_states = []
    for market_report in bs_point_report.get("markets", []) or []:
        if not isinstance(market_report, Mapping):
            continue
        market = str(market_report.get("market") or "")
        for item in market_report.get("ratio_guidance", []) or []:
            if not isinstance(item, Mapping):
                continue
            bs_class = str(item.get("bs_class") or "unknown")
            action = str(item.get("action") or "")
            try:
                multiplier = float(item.get("ratio_multiplier") or 1.0)
            except (TypeError, ValueError):
                multiplier = 1.0
            state_key = f"{market}|{bs_class}"
            decision_key = f"{state_key}|{action}|{multiplier:.4f}"
            prev = previous_by_key.get(state_key) or {}
            same_decision = prev.get("decision_key") == decision_key
            confirmations = int(prev.get("confirmations") or 0) + 1 if same_decision else 1
            ready_to_apply = action in {"allow_boost", "reduce"} and multiplier != 1.0
            apply_allowed = ready_to_apply and confirmations >= threshold
            ratio_states.append(
                {
                    "market": market,
                    "bs_class": bs_class,
                    "state_key": state_key,
                    "decision_key": decision_key,
                    "action": action,
                    "ratio_multiplier": multiplier,
                    "confirmations": confirmations,
                    "confirmation_threshold": threshold,
                    "ready_to_apply": ready_to_apply,
                    "apply_allowed": apply_allowed,
                    "status": _bs_ratio_status(action, ready_to_apply, apply_allowed),
                    "first_seen": str(prev.get("first_seen") or now) if same_decision else now,
                    "last_seen": now,
                    "reason": str(item.get("reason") or ""),
                    "trade_count": int(item.get("trade_count") or 0),
                    "win_rate": float(item.get("win_rate") or 0.0),
                    "avg_return": float(item.get("avg_return") or 0.0),
                    "max_drawdown": float(item.get("max_drawdown") or 0.0),
                }
            )
    return {
        "version": 1,
        "updated_at": now,
        "confirmation_threshold": threshold,
        "ratio_states": ratio_states,
        "apply_allowed_count": sum(1 for item in ratio_states if item["apply_allowed"]),
    }


def update_bs_point_ratio_state_file(
    path: str | Path,
    bs_point_report: Mapping[str, object],
    *,
    confirmation_threshold: int = 3,
    now: str | None = None,
) -> dict:
    path = Path(path)
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    state = build_bs_point_ratio_state(
        bs_point_report,
        previous,
        confirmation_threshold=confirmation_threshold,
        now=now,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def build_bs_point_ratio_overrides(ratio_state: Mapping[str, object]) -> dict:
    grouped: dict[str, dict[str, float]] = {}
    entries = []
    for item in ratio_state.get("ratio_states", []) or []:
        if not isinstance(item, Mapping) or not item.get("apply_allowed"):
            continue
        market = str(item.get("market") or "")
        bs_class = str(item.get("bs_class") or "")
        try:
            multiplier = float(item.get("ratio_multiplier") or 1.0)
        except (TypeError, ValueError):
            multiplier = 1.0
        if not market or bs_class not in {"1", "2", "3"}:
            continue
        grouped.setdefault(market, {})[bs_class] = multiplier
        entries.append(
            {
                "market": market,
                "bs_class": bs_class,
                "action": str(item.get("action") or ""),
                "ratio_multiplier": multiplier,
                "decision_key": str(item.get("decision_key") or ""),
                "confirmations": int(item.get("confirmations") or 0),
                "confirmation_threshold": int(item.get("confirmation_threshold") or 0),
                "reason": str(item.get("reason") or ""),
            }
        )
    return {
        "version": 1,
        "updated_at": str(ratio_state.get("updated_at") or ""),
        "source": "bs_point_ratio_state",
        "overrides": [
            {
                "market": market,
                "bs_point_ratio_multipliers": multipliers,
            }
            for market, multipliers in sorted(grouped.items())
        ],
        "entries": entries,
        "override_count": len(entries),
    }


def write_bs_point_ratio_overrides_file(
    path: str | Path,
    ratio_state: Mapping[str, object],
) -> dict:
    overrides = build_bs_point_ratio_overrides(ratio_state)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    return overrides


def bs_point_ratio_multipliers_for_market(path: str | Path, market: str) -> dict[str, float]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    wanted = str(market).strip().lower()
    for item in data.get("overrides", []) or []:
        if str(item.get("market") or "").strip().lower() == wanted:
            raw = item.get("bs_point_ratio_multipliers") or {}
            return {
                str(key): float(value)
                for key, value in raw.items()
                if str(key) in {"1", "2", "3"}
            }
    return {}


def default_bs_point_ratio_baseline_summary_path(market: str) -> Path:
    from chanlun.recursive_bt.market_runtime import default_backtest_report_paths, normalize_market

    summary, _trades = default_backtest_report_paths(normalize_market(market))
    path = Path(summary)
    suffix = "_summary.json"
    if path.name.endswith(suffix):
        return path.with_name(path.name[: -len(suffix)] + "_no_bs_override_summary.json")
    return path.with_name(path.stem + "_no_bs_override" + path.suffix)


def default_regime_ratio_impact_windows() -> list[dict]:
    """按行情比例乘数候选的默认证据窗口:同窗同参,仅乘数不同。"""
    return [
        {
            "market": "a",
            "candidate": "bear3_boost",
            "window": "a_mtf3_300_1m5m30m",
            "default_summary": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
            "candidate_summary": A_MTF3_REGIME_BEAR3BOOST_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "bear3_boost",
            "window": "a_all_5m30m_max30",
            "default_summary": A_ALL_5M30M_DEFAULT_SUMMARY,
            "candidate_summary": A_ALL_5M30M_REGIME_BEAR3BOOST_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "weak1_reduce",
            "window": "a_mtf3_300_1m5m30m",
            "default_summary": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
            "candidate_summary": A_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "bear3boost_weak1reduce",
            "window": "a_mtf3_300_1m5m30m",
            "default_summary": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
            "candidate_summary": A_MTF3_REGIME_COMBO_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "bear3boost_weak1reduce",
            "window": "a_all_5m30m_max30",
            "default_summary": A_ALL_5M30M_DEFAULT_SUMMARY,
            "candidate_summary": A_ALL_5M30M_REGIME_COMBO_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "combo_bear3x140",
            "window": "a_mtf3_300_1m5m30m",
            "default_summary": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
            "candidate_summary": A_MTF3_REGIME_COMBO_B140_SUMMARY,
        },
        {
            "market": "a",
            "candidate": "combo_bear3x140",
            "window": "a_all_5m30m_max30",
            "default_summary": A_ALL_5M30M_DEFAULT_SUMMARY,
            "candidate_summary": A_ALL_5M30M_REGIME_COMBO_B140_SUMMARY,
        },
        {
            "market": "us",
            "candidate": "weak1_reduce",
            "window": "us_core9_current",
            "default_summary": US_MTF3_DEFAULT_SUMMARY,
            "candidate_summary": US_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
        },
        {
            "market": "us",
            "candidate": "weak1_reduce",
            "window": "us_core9_2026q1",
            "default_summary": US_2026Q1_MTF3_DEFAULT_SUMMARY,
            "candidate_summary": US_2026Q1_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
        },
    ]


def _regime_ratio_window_action(
    delta_return: float,
    delta_drawdown: float,
    *,
    review_dd_tolerance: float = 0.001,
    defensive_dd_gain: float = -0.002,
    defensive_ret_loss_limit: float = -0.02,
) -> str:
    if delta_return > 0 and delta_drawdown <= review_dd_tolerance:
        return "review_regime_ratio"
    if delta_return > 0:
        return "watch_positive_tradeoff"
    if delta_drawdown <= defensive_dd_gain and delta_return >= defensive_ret_loss_limit:
        return "watch_defensive"
    return "keep_default"


def _regime_ratio_verdict(actions: list[str]) -> tuple[str, dict]:
    positive = sum(1 for a in actions if a == "review_regime_ratio")
    tradeoff = sum(1 for a in actions if a == "watch_positive_tradeoff")
    defensive = sum(1 for a in actions if a == "watch_defensive")
    negative = sum(1 for a in actions if a == "keep_default")
    counts = {
        "positive_windows": positive,
        "tradeoff_windows": tradeoff,
        "defensive_windows": defensive,
        "negative_windows": negative,
    }
    if not actions:
        return "evidence_limited", counts
    if negative and (positive or tradeoff):
        return "keep_default", counts
    if positive >= 2:
        return "review_regime_ratio", counts
    if positive == 1:
        return "watch_regime_ratio", counts
    if defensive:
        return "watch_defensive", counts
    if tradeoff:
        return "watch_positive_tradeoff", counts
    return "keep_default", counts


def build_regime_ratio_impact_report(
    windows: Iterable[Mapping] | None = None,
    *,
    review_dd_tolerance: float = 0.001,
    defensive_dd_gain: float = -0.002,
    defensive_ret_loss_limit: float = -0.02,
) -> dict:
    """按行情(bull/range/bear)买点比例乘数候选的 impact 报告。

    每个窗口都是同窗同参对照(仅乘数不同):正向窗口 >= 2 个才给出
    `review_regime_ratio`,单窗口正向只能 `watch_regime_ratio`;
    多窗口方向冲突一律保守回到 `keep_default`。不写 runtime overrides。"""
    from chanlun.recursive_bt.market_runtime import normalize_market

    if windows is None:
        windows = default_regime_ratio_impact_windows()
    rows: list[dict] = []
    missing_sources: list[dict] = []
    grouped: dict[tuple, list] = {}
    for spec in windows:
        market = normalize_market(str(spec.get("market", "a")))
        candidate = str(spec.get("candidate", ""))
        window = str(spec.get("window", ""))
        default_path = Path(spec["default_summary"])
        candidate_path = Path(spec["candidate_summary"])
        default, default_reason = _load_summary_or_reason(default_path)
        cand, cand_reason = _load_summary_or_reason(candidate_path)
        grouped.setdefault((market, candidate), [])
        if default_reason or cand_reason:
            for kind, path, reason in (
                ("default_summary", default_path, default_reason),
                ("candidate_summary", candidate_path, cand_reason),
            ):
                if reason:
                    missing_sources.append(
                        {
                            "market": market,
                            "candidate": candidate,
                            "window": window,
                            "kind": kind,
                            "path": str(path),
                            "reason": reason,
                        }
                    )
            continue
        delta_return = float(cand.get("total") or 0.0) - float(default.get("total") or 0.0)
        delta_drawdown = float(cand.get("max_dd") or 0.0) - float(default.get("max_dd") or 0.0)
        action = _regime_ratio_window_action(
            delta_return,
            delta_drawdown,
            review_dd_tolerance=review_dd_tolerance,
            defensive_dd_gain=defensive_dd_gain,
            defensive_ret_loss_limit=defensive_ret_loss_limit,
        )
        rows.append(
            {
                "market": market,
                "candidate": candidate,
                "window": window,
                "multipliers": dict(cand.get("regime_bs_ratio_multipliers") or {}),
                "default_return": float(default.get("total") or 0.0),
                "candidate_return": float(cand.get("total") or 0.0),
                "delta_return": delta_return,
                "default_drawdown": float(default.get("max_dd") or 0.0),
                "candidate_drawdown": float(cand.get("max_dd") or 0.0),
                "delta_drawdown": delta_drawdown,
                "default_sharpe": float(default.get("sharpe") or 0.0),
                "candidate_sharpe": float(cand.get("sharpe") or 0.0),
                "default_trades": int(default.get("trade_count") or 0),
                "candidate_trades": int(cand.get("trade_count") or 0),
                "action": action,
                "default_summary": str(default_path),
                "candidate_summary": str(candidate_path),
            }
        )
        grouped[(market, candidate)].append(action)
    verdicts = []
    for (market, candidate), actions in sorted(grouped.items()):
        verdict, counts = _regime_ratio_verdict(actions)
        verdicts.append(
            {
                "market": market,
                "candidate": candidate,
                "verdict": verdict,
                "windows": len(actions),
                **counts,
                "note": (
                    "review_regime_ratio is research-grade evidence only; runtime "
                    "overrides still require adoption gate and confirmation state"
                ),
            }
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "review_dd_tolerance": review_dd_tolerance,
        "defensive_dd_gain": defensive_dd_gain,
        "defensive_ret_loss_limit": defensive_ret_loss_limit,
        "windows": rows,
        "verdicts": verdicts,
        "missing_sources": missing_sources,
    }


def write_regime_ratio_impact_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    windows: Iterable[Mapping] | None = None,
) -> dict:
    report = build_regime_ratio_impact_report(windows=windows)
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(
            render_regime_ratio_impact_markdown(report), encoding="utf-8"
        )
    return report


def render_regime_ratio_impact_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Regime Buy-Ratio Impact Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "| Market | Candidate | Window | Multipliers | Default Ret | Candidate Ret | Delta Ret | Default DD | Candidate DD | Delta DD | Default Sharpe | Candidate Sharpe | Action |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("windows", []) or []:
        lines.append(
            "| {market} | {candidate} | {window} | `{multipliers}` | {default_return:.2%} | {candidate_return:.2%} | {delta_return:+.2%} | {default_drawdown:.2%} | {candidate_drawdown:.2%} | {delta_drawdown:+.2%} | {default_sharpe:.2f} | {candidate_sharpe:.2f} | {action} |".format(
                market=row.get("market", ""),
                candidate=row.get("candidate", ""),
                window=row.get("window", ""),
                multipliers=json.dumps(row.get("multipliers") or {}, ensure_ascii=False),
                default_return=float(row.get("default_return") or 0.0),
                candidate_return=float(row.get("candidate_return") or 0.0),
                delta_return=float(row.get("delta_return") or 0.0),
                default_drawdown=float(row.get("default_drawdown") or 0.0),
                candidate_drawdown=float(row.get("candidate_drawdown") or 0.0),
                delta_drawdown=float(row.get("delta_drawdown") or 0.0),
                default_sharpe=float(row.get("default_sharpe") or 0.0),
                candidate_sharpe=float(row.get("candidate_sharpe") or 0.0),
                action=row.get("action", ""),
            )
        )
    lines.extend(
        [
            "",
            "| Market | Candidate | Verdict | Windows | Positive | Tradeoff | Defensive | Negative |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for verdict in report.get("verdicts", []) or []:
        lines.append(
            "| {market} | {candidate} | {verdict} | {windows} | {positive_windows} | {tradeoff_windows} | {defensive_windows} | {negative_windows} |".format(
                market=verdict.get("market", ""),
                candidate=verdict.get("candidate", ""),
                verdict=verdict.get("verdict", ""),
                windows=int(verdict.get("windows") or 0),
                positive_windows=int(verdict.get("positive_windows") or 0),
                tradeoff_windows=int(verdict.get("tradeoff_windows") or 0),
                defensive_windows=int(verdict.get("defensive_windows") or 0),
                negative_windows=int(verdict.get("negative_windows") or 0),
            )
        )
    missing = report.get("missing_sources", []) or []
    if missing:
        lines.extend(["", "## Missing Sources", ""])
        for item in missing:
            lines.append(
                f"- {item.get('market', '')} {item.get('candidate', '')} {item.get('window', '')}: "
                f"{item.get('kind', '')} `{item.get('path', '')}` ({item.get('reason', '')})"
            )
    lines.append("")
    return "\n".join(lines)


def build_bs_point_ratio_impact_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    summary_paths: Mapping[str, str | Path] | None = None,
    baseline_summary_paths: Mapping[str, str | Path] | None = None,
) -> dict:
    from chanlun.recursive_bt.market_runtime import default_backtest_report_paths, normalize_market

    summary_paths = dict(summary_paths or {})
    baseline_summary_paths = dict(baseline_summary_paths or {})
    market_reports = []
    missing_sources = []
    for market in markets:
        market = normalize_market(market)
        default_summary, _trades = default_backtest_report_paths(market)
        current_path = Path(summary_paths.get(market) or default_summary)
        baseline_path = Path(
            baseline_summary_paths.get(market)
            or default_bs_point_ratio_baseline_summary_path(market)
        )
        current, current_reason = _load_summary_or_reason(current_path)
        baseline, baseline_reason = _load_summary_or_reason(baseline_path)
        if current_reason:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "current_summary",
                    "path": str(current_path),
                    "reason": current_reason,
                }
            )
        multipliers = _summary_bs_point_multipliers(current)
        baseline_needed = bool(multipliers)
        if baseline_needed and baseline_reason:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "baseline_summary",
                    "path": str(baseline_path),
                    "reason": baseline_reason,
                }
            )
        market_reports.append(
            _build_bs_point_ratio_impact_market(
                market,
                current_path,
                baseline_path,
                current,
                baseline,
                multipliers,
                current_reason=current_reason,
                baseline_reason=baseline_reason if baseline_needed else "",
            )
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_bs_point_ratio_impact_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    summary_paths: Mapping[str, str | Path] | None = None,
    baseline_summary_paths: Mapping[str, str | Path] | None = None,
) -> dict:
    report = build_bs_point_ratio_impact_report(
        markets,
        summary_paths=summary_paths,
        baseline_summary_paths=baseline_summary_paths,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_bs_point_ratio_impact_markdown(report), encoding="utf-8")
    return report


def render_bs_point_ratio_impact_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Buy Ratio Impact Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "| Market | Multipliers | Base Ret | Current Ret | Delta Ret | Base DD | Current DD | Delta DD | Action |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("markets", []) or []:
        multipliers = item.get("bs_point_ratio_multipliers") or {}
        lines.append(
            "| {market} | `{mult}` | {base_ret:.2%} | {cur_ret:.2%} | {delta_ret:.2%} | {base_dd:.2%} | {cur_dd:.2%} | {delta_dd:.2%} | {action} |".format(
                market=item.get("market", ""),
                mult=json.dumps(multipliers, ensure_ascii=False, sort_keys=True),
                base_ret=float(item.get("baseline_total_return") or 0.0),
                cur_ret=float(item.get("current_total_return") or 0.0),
                delta_ret=float(item.get("delta_total_return") or 0.0),
                base_dd=float(item.get("baseline_max_drawdown") or 0.0),
                cur_dd=float(item.get("current_max_drawdown") or 0.0),
                delta_dd=float(item.get("delta_max_drawdown") or 0.0),
                action=item.get("action") or "",
            )
        )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(
                f"- {item.get('market')} {item.get('kind')}: {item.get('reason')} `{item.get('path')}`"
            )
    lines.append("")
    return "\n".join(lines)


def default_sell_policy_candidate_summary_path(market: str, label: str = "sell12") -> Path:
    from chanlun.recursive_bt.market_runtime import default_backtest_report_paths, normalize_market

    summary, _trades = default_backtest_report_paths(normalize_market(market))
    path = Path(summary)
    suffix = "_summary.json"
    tag = str(label or "candidate").strip().replace(" ", "_")
    if path.name.endswith(suffix):
        return path.with_name(path.name[: -len(suffix)] + f"_{tag}_summary.json")
    return path.with_name(path.stem + f"_{tag}" + path.suffix)


def build_sell_policy_impact_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    summary_paths: Mapping[str, str | Path] | None = None,
    candidate_summary_paths: Mapping[str, str | Path] | None = None,
    candidate_label: str = "sell12",
) -> dict:
    from chanlun.recursive_bt.market_runtime import default_backtest_report_paths, normalize_market

    summary_paths = dict(summary_paths or {})
    candidate_summary_paths = dict(candidate_summary_paths or {})
    market_reports = []
    missing_sources = []
    for market in markets:
        market = normalize_market(market)
        default_summary, _trades = default_backtest_report_paths(market)
        default_path = Path(summary_paths.get(market) or default_summary)
        candidate_path = Path(
            candidate_summary_paths.get(market)
            or default_sell_policy_candidate_summary_path(market, candidate_label)
        )
        default, default_reason = _load_summary_or_reason(default_path)
        candidate, candidate_reason = _load_summary_or_reason(candidate_path)
        if default_reason:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "default_summary",
                    "path": str(default_path),
                    "reason": default_reason,
                }
            )
        if candidate_reason:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "candidate_summary",
                    "path": str(candidate_path),
                    "reason": candidate_reason,
                }
            )
        market_reports.append(
            _build_sell_policy_impact_market(
                market,
                default_path,
                candidate_path,
                default,
                candidate,
                candidate_label=candidate_label,
                default_reason=default_reason,
                candidate_reason=candidate_reason,
            )
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "candidate_label": candidate_label,
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_sell_policy_impact_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    summary_paths: Mapping[str, str | Path] | None = None,
    candidate_summary_paths: Mapping[str, str | Path] | None = None,
    candidate_label: str = "sell12",
) -> dict:
    report = build_sell_policy_impact_report(
        markets,
        summary_paths=summary_paths,
        candidate_summary_paths=candidate_summary_paths,
        candidate_label=candidate_label,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_sell_policy_impact_markdown(report), encoding="utf-8")
    return report


def render_sell_policy_impact_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Sell Policy Impact Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Candidate: {report.get('candidate_label', '')}",
        "",
        "| Market | Default Sell Classes | Candidate Sell Classes | Candidate Sell Ratios | Candidate Scope | Candidate Reentry | Candidate Reentry Scope | Candidate Mid Reentry | Default Ret | Candidate Ret | Delta Ret | Default DD | Candidate DD | Delta DD | Default Trades | Candidate Trades | Action |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("markets", []) or []:
        lines.append(
            "| {market} | `{default_classes}` | `{candidate_classes}` | `{candidate_ratios}` | {candidate_scope} | `{candidate_reentry}` | {candidate_reentry_scope} | `{candidate_mid_reentry}` | {default_ret:.2%} | {candidate_ret:.2%} | {delta_ret:.2%} | {default_dd:.2%} | {candidate_dd:.2%} | {delta_dd:.2%} | {default_trades} | {candidate_trades} | {action} |".format(
                market=item.get("market", ""),
                default_classes=json.dumps(
                    item.get("default_sell_classes") or [], ensure_ascii=False
                ),
                candidate_classes=json.dumps(
                    item.get("candidate_sell_classes") or [], ensure_ascii=False
                ),
                candidate_ratios=json.dumps(
                    item.get("candidate_sell_ratio_overrides") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                candidate_scope=item.get("candidate_sell_ratio_override_scope") or "",
                candidate_reentry=json.dumps(
                    item.get("candidate_after_3sell_reentry_buy_classes") or [],
                    ensure_ascii=False,
                ),
                candidate_reentry_scope=item.get("candidate_after_3sell_reentry_scope") or "all",
                candidate_mid_reentry=json.dumps(
                    item.get("candidate_after_3sell_reentry_mid_buy_classes") or [],
                    ensure_ascii=False,
                ),
                default_ret=float(item.get("default_total_return") or 0.0),
                candidate_ret=float(item.get("candidate_total_return") or 0.0),
                delta_ret=float(item.get("delta_total_return") or 0.0),
                default_dd=float(item.get("default_max_drawdown") or 0.0),
                candidate_dd=float(item.get("candidate_max_drawdown") or 0.0),
                delta_dd=float(item.get("delta_max_drawdown") or 0.0),
                default_trades=int(item.get("default_trade_count") or 0),
                candidate_trades=int(item.get("candidate_trade_count") or 0),
                action=item.get("action") or "",
            )
        )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(
                f"- {item.get('market')} {item.get('kind')}: {item.get('reason')} `{item.get('path')}`"
            )
    lines.append("")
    return "\n".join(lines)


def default_regime_stress_summary_paths(
    markets: Iterable[str] = ("a", "us"),
) -> dict[str, dict[str, str]]:
    from chanlun.recursive_bt.market_runtime import normalize_market

    wanted = {normalize_market(market) for market in markets}
    paths: dict[str, dict[str, str]] = {}
    if "a" in wanted:
        paths["a"] = {
            "default": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
            "sell3_rebuy3": A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
            "sell3_rebuy3_up": A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
            "sell3_rebuy_mid3": A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
        }
    if "us" in wanted:
        paths["us"] = {
            "default": US_MTF3_DEFAULT_SUMMARY,
            "sell3_rebuy3": US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
            "sell3_rebuy_mid3": US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
        }
    return paths


def build_market_regime_stress_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    summary_paths: Mapping[str, Mapping[str, str | Path]] | None = None,
    min_regime_days: int = 10,
) -> dict:
    from chanlun.recursive_bt.market_runtime import normalize_market

    defaults = default_regime_stress_summary_paths(markets)
    custom = summary_paths or {}
    market_reports = []
    missing_sources = []
    for raw_market in markets:
        market = normalize_market(raw_market)
        if market in custom:
            sources: dict[str, str | Path] = dict(custom.get(market) or {})
        else:
            sources = dict(defaults.get(market) or {})
        loaded: dict[str, tuple[Path, dict, str]] = {}
        for label, raw_path in sources.items():
            path = Path(raw_path)
            summary, reason = _load_summary_or_reason(path)
            loaded[label] = (path, summary, reason)
            if reason:
                missing_sources.append(
                    {
                        "market": market,
                        "strategy": label,
                        "path": str(path),
                        "reason": reason,
                    }
                )
        default_summary = loaded.get("default", (Path(""), {}, "missing"))[1]
        rows = []
        for label, (path, summary, reason) in loaded.items():
            rows.extend(
                _build_regime_stress_rows(
                    market,
                    label,
                    path,
                    summary,
                    default_summary,
                    missing_reason=reason,
                    min_regime_days=min_regime_days,
                )
            )
        market_reports.append(
            {
                "market": market,
                "min_regime_days": int(min_regime_days),
                "strategies": [
                    {
                        "label": label,
                        "path": str(path),
                        "missing_reason": reason,
                        "total_return": _float_first(summary, "total", "total_return"),
                        "max_drawdown": _float_first(summary, "max_dd", "max_drawdown"),
                        "trade_count": int(_float_first(summary, "trade_count")),
                        "op_level": str(summary.get("op_level") or ""),
                        "mid_level": str(summary.get("mid_level") or ""),
                        "big_level": str(summary.get("big_level") or ""),
                    }
                    for label, (path, summary, reason) in loaded.items()
                ],
                "rows": rows,
                "best_by_regime": _best_regime_rows(rows),
            }
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "method": "summary.market_regime_segments",
        "regimes": list(REGIME_ORDER),
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_market_regime_stress_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    summary_paths: Mapping[str, Mapping[str, str | Path]] | None = None,
    min_regime_days: int = 10,
) -> dict:
    report = build_market_regime_stress_report(
        markets,
        summary_paths=summary_paths,
        min_regime_days=min_regime_days,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_market_regime_stress_markdown(report), encoding="utf-8")
    return report


def render_market_regime_stress_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Market Regime Stress Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Method: {report.get('method', '')}",
        "",
        "| Market | Regime | Strategy | Days | Ret | Excess | DD | Sharpe | Trades | Delta Ret | Delta Excess | Delta DD | Action |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for market_report in report.get("markets", []) or []:
        for row in market_report.get("rows", []) or []:
            lines.append(
                "| {market} | {regime} | `{strategy}` | {days} | {ret:.2%} | {excess:.2%} | {dd:.2%} | {sharpe:.2f} | {trades} | {delta_ret:.2%} | {delta_excess:.2%} | {delta_dd:.2%} | {action} |".format(
                    market=row.get("market") or "",
                    regime=row.get("regime") or "",
                    strategy=row.get("strategy") or "",
                    days=int(row.get("days") or 0),
                    ret=float(row.get("strategy_return") or 0.0),
                    excess=float(row.get("excess_return") or 0.0),
                    dd=float(row.get("max_drawdown") or 0.0),
                    sharpe=float(row.get("sharpe") or 0.0),
                    trades=int(row.get("trade_count") or 0),
                    delta_ret=float(row.get("delta_strategy_return") or 0.0),
                    delta_excess=float(row.get("delta_excess_return") or 0.0),
                    delta_dd=float(row.get("delta_max_drawdown") or 0.0),
                    action=row.get("action") or "",
                )
            )
    best_rows = []
    for market_report in report.get("markets", []) or []:
        market = market_report.get("market") or ""
        for regime, row in (market_report.get("best_by_regime") or {}).items():
            best_rows.append((market, regime, row))
    if best_rows:
        lines.extend(
            [
                "",
                "## Best By Regime",
                "",
                "| Market | Regime | Strategy | Score | Ret | DD | Excess | Days |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for market, regime, row in best_rows:
            lines.append(
                "| {market} | {regime} | `{strategy}` | {score:.4f} | {ret:.2%} | {dd:.2%} | {excess:.2%} | {days} |".format(
                    market=market,
                    regime=regime,
                    strategy=row.get("strategy") or "",
                    score=float(row.get("regime_score") or 0.0),
                    ret=float(row.get("strategy_return") or 0.0),
                    dd=float(row.get("max_drawdown") or 0.0),
                    excess=float(row.get("excess_return") or 0.0),
                    days=int(row.get("days") or 0),
                )
            )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(
                f"- {item.get('market')} {item.get('strategy')}: {item.get('reason')} `{item.get('path')}`"
            )
    lines.append("")
    return "\n".join(lines)


def _build_regime_stress_rows(
    market: str,
    label: str,
    path: Path,
    summary: Mapping[str, object],
    default_summary: Mapping[str, object],
    *,
    missing_reason: str,
    min_regime_days: int,
) -> list[dict]:
    segments = ((summary.get("market_regime_segments") or {}).get("segments") or {})
    default_segments = (
        (default_summary.get("market_regime_segments") or {}).get("segments") or {}
    )
    rows: list[dict] = []
    for regime in REGIME_ORDER:
        segment = segments.get(regime) or {}
        default_segment = default_segments.get(regime) or {}
        days = int(_float_first(segment, "days"))
        strategy_return = _float_first(segment, "strategy_return")
        benchmark_return = _float_first(segment, "benchmark_return")
        excess_return = _float_first(segment, "excess_return")
        max_drawdown = _float_first(segment, "max_drawdown")
        sharpe = _float_first(segment, "sharpe")
        trade_count = int(_float_first(segment, "trade_count"))
        default_return = _float_first(default_segment, "strategy_return")
        default_excess = _float_first(default_segment, "excess_return")
        default_dd = _float_first(default_segment, "max_drawdown")
        delta_return = strategy_return - default_return
        delta_excess = excess_return - default_excess
        delta_dd = max_drawdown - default_dd
        score = _regime_score(
            strategy_return=strategy_return,
            excess_return=excess_return,
            max_drawdown=max_drawdown,
            days=days,
            min_regime_days=min_regime_days,
        )
        rows.append(
            {
                "market": market,
                "strategy": label,
                "summary_path": str(path),
                "regime": regime,
                "days": days,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "max_drawdown": max_drawdown,
                "sharpe": sharpe,
                "trade_count": trade_count,
                "default_strategy_return": default_return,
                "default_excess_return": default_excess,
                "default_max_drawdown": default_dd,
                "delta_strategy_return": delta_return,
                "delta_excess_return": delta_excess,
                "delta_max_drawdown": delta_dd,
                "regime_score": score,
                "action": _regime_stress_action(
                    label=label,
                    missing_reason=missing_reason,
                    days=days,
                    min_regime_days=min_regime_days,
                    delta_return=delta_return,
                    delta_excess=delta_excess,
                    delta_dd=delta_dd,
                ),
            }
        )
    return rows


def _regime_score(
    *,
    strategy_return: float,
    excess_return: float,
    max_drawdown: float,
    days: int,
    min_regime_days: int,
) -> float:
    if days < min_regime_days:
        return -999.0 + days / max(min_regime_days, 1)
    return strategy_return + 0.5 * excess_return - 2.0 * max_drawdown


def _regime_stress_action(
    *,
    label: str,
    missing_reason: str,
    days: int,
    min_regime_days: int,
    delta_return: float,
    delta_excess: float,
    delta_dd: float,
) -> str:
    eps = 1e-9
    if missing_reason:
        return "missing_summary"
    if days < min_regime_days:
        return "evidence_limited"
    if label == "default":
        return "baseline"
    if (
        delta_return >= -0.005 - eps
        and delta_excess >= -0.005 - eps
        and delta_dd <= -0.002 + eps
    ):
        return "defensive_improvement"
    if (
        delta_return >= 0.005 - eps
        and delta_excess >= 0.005 - eps
        and delta_dd <= 0.002 + eps
    ):
        return "improves_regime"
    if delta_return < -0.02 - eps or delta_excess < -0.02 - eps or delta_dd > 0.002 + eps:
        return "underperforms"
    if delta_dd < -0.002 - eps and delta_return < -0.005 - eps:
        return "defensive_tradeoff"
    return "watch"


def _best_regime_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, dict]:
    best: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if row.get("action") in {"missing_summary", "evidence_limited"}:
            continue
        regime = str(row.get("regime") or "")
        if not regime:
            continue
        if regime not in best or float(row.get("regime_score") or -999) > float(
            best[regime].get("regime_score") or -999
        ):
            best[regime] = row
    return {regime: dict(row) for regime, row in best.items()}


def build_regime_strategy_policy_report(
    report_sources: Mapping[str, Mapping[str, object]],
    *,
    min_supporting_sources: int = 2,
) -> dict:
    evidence_by_key: dict[tuple[str, str], list[dict]] = {}
    for source_label, report in report_sources.items():
        for market_report in report.get("markets", []) or []:
            market = str(market_report.get("market") or "")
            rows = market_report.get("rows", []) or []
            for regime in REGIME_ORDER:
                row = _best_policy_evidence_row(rows, regime)
                if row is None:
                    continue
                evidence_by_key.setdefault((market, regime), []).append(
                    {
                        "source": source_label,
                        "strategy": row.get("strategy") or "",
                        "action": row.get("action") or "",
                        "days": int(row.get("days") or 0),
                        "strategy_return": float(row.get("strategy_return") or 0.0),
                        "excess_return": float(row.get("excess_return") or 0.0),
                        "max_drawdown": float(row.get("max_drawdown") or 0.0),
                        "delta_strategy_return": float(
                            row.get("delta_strategy_return") or 0.0
                        ),
                        "delta_max_drawdown": float(row.get("delta_max_drawdown") or 0.0),
                    }
                )
    policies = []
    for market, regime in sorted(evidence_by_key):
        evidence = evidence_by_key[(market, regime)]
        positive = [
            item
            for item in evidence
            if item["strategy"] != "default"
            and item["action"] in {"improves_regime", "defensive_improvement"}
        ]
        default_count = sum(1 for item in evidence if item["strategy"] == "default")
        if positive:
            strategy_counts: dict[str, int] = {}
            for item in positive:
                strategy_counts[item["strategy"]] = strategy_counts.get(item["strategy"], 0) + 1
            best_strategy, support_count = sorted(
                strategy_counts.items(), key=lambda item: (-item[1], item[0])
            )[0]
            if support_count >= min_supporting_sources:
                policy_action = "review_regime_candidate"
            else:
                policy_action = "watch_regime_candidate"
            recommended_strategy = best_strategy
        elif default_count:
            policy_action = "keep_default"
            recommended_strategy = "default"
            support_count = default_count
        else:
            policy_action = "evidence_limited"
            recommended_strategy = ""
            support_count = 0
        policies.append(
            {
                "market": market,
                "regime": regime,
                "policy_action": policy_action,
                "recommended_strategy": recommended_strategy,
                "supporting_sources": int(support_count),
                "required_supporting_sources": int(min_supporting_sources),
                "evidence": evidence,
                "reason": _regime_policy_reason(
                    policy_action,
                    recommended_strategy=recommended_strategy,
                    support_count=support_count,
                    min_supporting_sources=min_supporting_sources,
                ),
            }
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "min_supporting_sources": int(min_supporting_sources),
        "policies": policies,
        "source_labels": list(report_sources.keys()),
    }


def write_regime_strategy_policy_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    report_sources: Mapping[str, Mapping[str, object]],
    min_supporting_sources: int = 2,
) -> dict:
    report = build_regime_strategy_policy_report(
        report_sources,
        min_supporting_sources=min_supporting_sources,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_regime_strategy_policy_markdown(report), encoding="utf-8")
    return report


def render_regime_strategy_policy_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Regime Strategy Policy Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Min supporting sources: {report.get('min_supporting_sources', '')}",
        "",
        "| Market | Regime | Policy Action | Strategy | Support | Reason |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for item in report.get("policies", []) or []:
        lines.append(
            "| {market} | {regime} | {action} | `{strategy}` | {support}/{required} | {reason} |".format(
                market=item.get("market") or "",
                regime=item.get("regime") or "",
                action=item.get("policy_action") or "",
                strategy=item.get("recommended_strategy") or "",
                support=int(item.get("supporting_sources") or 0),
                required=int(item.get("required_supporting_sources") or 0),
                reason=str(item.get("reason") or "").replace("|", "/"),
            )
        )
    lines.extend(["", "## Evidence", ""])
    for item in report.get("policies", []) or []:
        lines.append(f"### {item.get('market')} {item.get('regime')}")
        for evidence in item.get("evidence", []) or []:
            lines.append(
                "- {source}: `{strategy}` action={action} days={days} ret={ret:.2%} excess={excess:.2%} dd={dd:.2%} delta_ret={delta_ret:.2%} delta_dd={delta_dd:.2%}".format(
                    source=evidence.get("source") or "",
                    strategy=evidence.get("strategy") or "",
                    action=evidence.get("action") or "",
                    days=int(evidence.get("days") or 0),
                    ret=float(evidence.get("strategy_return") or 0.0),
                    excess=float(evidence.get("excess_return") or 0.0),
                    dd=float(evidence.get("max_drawdown") or 0.0),
                    delta_ret=float(evidence.get("delta_strategy_return") or 0.0),
                    delta_dd=float(evidence.get("delta_max_drawdown") or 0.0),
                )
            )
    lines.append("")
    return "\n".join(lines)


def _best_policy_evidence_row(
    rows: Iterable[Mapping[str, object]],
    regime: str,
) -> Mapping[str, object] | None:
    usable = [
        row
        for row in rows
        if row.get("regime") == regime
        and row.get("action") not in {"missing_summary", "evidence_limited"}
    ]
    if not usable:
        return None
    positive = [
        row
        for row in usable
        if row.get("strategy") != "default"
        and row.get("action") in {"improves_regime", "defensive_improvement"}
    ]
    if positive:
        return max(positive, key=lambda row: float(row.get("regime_score") or -999.0))
    default_rows = [row for row in usable if row.get("strategy") == "default"]
    if default_rows:
        return default_rows[0]
    return max(usable, key=lambda row: float(row.get("regime_score") or -999.0))


def _regime_policy_reason(
    policy_action: str,
    *,
    recommended_strategy: str,
    support_count: int,
    min_supporting_sources: int,
) -> str:
    if policy_action == "review_regime_candidate":
        return (
            f"{recommended_strategy} has positive regime evidence from "
            f"{support_count} sources"
        )
    if policy_action == "watch_regime_candidate":
        return (
            f"{recommended_strategy} has positive regime evidence but only "
            f"{support_count}/{min_supporting_sources} supporting sources"
        )
    if policy_action == "keep_default":
        return "default remains the supported regime policy"
    return "not enough usable regime evidence"


def build_strategy_adoption_gate_report(
    coverage_report: Mapping[str, object],
    candidate_reports: Iterable[Mapping[str, object]],
    *,
    min_a_mtf3_symbols: int = 90,
    min_us_mtf3_symbols: int = 9,
    min_a_5m30m_sample: int = 90,
) -> dict:
    coverage_by_market = {
        str(item.get("market") or ""): item
        for item in coverage_report.get("markets", []) or []
        if isinstance(item, Mapping)
    }
    gates = []
    for report in candidate_reports:
        if not isinstance(report, Mapping):
            continue
        candidate_label = str(report.get("candidate_label") or "")
        for market_item in report.get("markets", []) or []:
            if not isinstance(market_item, Mapping):
                continue
            market = str(market_item.get("market") or "")
            coverage = coverage_by_market.get(market, {})
            scope = _candidate_evidence_scope(candidate_label, market_item)
            ready, coverage_reason, coverage_counts = _candidate_evidence_ready(
                market,
                scope,
                coverage,
                min_a_mtf3_symbols=min_a_mtf3_symbols,
                min_us_mtf3_symbols=min_us_mtf3_symbols,
                min_a_5m30m_sample=min_a_5m30m_sample,
            )
            raw_action = str(market_item.get("action") or "")
            gate_action, reason = _strategy_adoption_gate_action(
                raw_action,
                evidence_ready=ready,
                coverage_reason=coverage_reason,
            )
            gates.append(
                {
                    "market": market,
                    "candidate_label": candidate_label,
                    "evidence_scope": scope,
                    "candidate_action": raw_action,
                    "gate_action": gate_action,
                    "evidence_ready": bool(ready),
                    "coverage_reason": coverage_reason,
                    "coverage_counts": coverage_counts,
                    "reason": reason,
                }
            )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "thresholds": {
            "min_a_mtf3_symbols": int(min_a_mtf3_symbols),
            "min_us_mtf3_symbols": int(min_us_mtf3_symbols),
            "min_a_5m30m_sample": int(min_a_5m30m_sample),
        },
        "gates": gates,
    }


def write_strategy_adoption_gate_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    coverage_report: Mapping[str, object],
    candidate_reports: Iterable[Mapping[str, object]],
    min_a_mtf3_symbols: int = 90,
    min_us_mtf3_symbols: int = 9,
    min_a_5m30m_sample: int = 90,
) -> dict:
    report = build_strategy_adoption_gate_report(
        coverage_report,
        candidate_reports,
        min_a_mtf3_symbols=min_a_mtf3_symbols,
        min_us_mtf3_symbols=min_us_mtf3_symbols,
        min_a_5m30m_sample=min_a_5m30m_sample,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_strategy_adoption_gate_markdown(report), encoding="utf-8")
    return report


def render_strategy_adoption_gate_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Strategy Adoption Gate Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "| Market | Candidate | Scope | Candidate Action | Evidence Ready | Gate Action | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.get("gates", []) or []:
        lines.append(
            "| {market} | `{candidate}` | {scope} | {candidate_action} | {ready} | {gate_action} | {reason} |".format(
                market=item.get("market") or "",
                candidate=item.get("candidate_label") or "",
                scope=item.get("evidence_scope") or "",
                candidate_action=item.get("candidate_action") or "",
                ready="yes" if item.get("evidence_ready") else "no",
                gate_action=item.get("gate_action") or "",
                reason=str(item.get("reason") or "").replace("|", "/"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _candidate_evidence_scope(
    candidate_label: str,
    market_item: Mapping[str, object],
) -> str:
    if candidate_label == "a_5m_sell3_rebuy3":
        return "a_5m30m_large_sample"
    candidate_op = str(market_item.get("candidate_op_level") or "").lower()
    candidate_mid = str(market_item.get("candidate_mid_level") or "").lower()
    candidate_big = str(market_item.get("candidate_big_level") or "").lower()
    if candidate_op == "1m" and candidate_mid == "5m" and candidate_big == "30m":
        return "mtf3_1m5m30m"
    if _summary_int_list(market_item, "candidate_after_3sell_reentry_mid_buy_classes"):
        return "mtf3_1m5m30m"
    if "mid3" in candidate_label or "rebuy_mid" in candidate_label:
        return "mtf3_1m5m30m"
    return "summary_only"


def _candidate_evidence_ready(
    market: str,
    scope: str,
    coverage: Mapping[str, object],
    *,
    min_a_mtf3_symbols: int,
    min_us_mtf3_symbols: int,
    min_a_5m30m_sample: int,
) -> tuple[bool, str, dict]:
    chart_complete = int(coverage.get("chart_cache_complete_mtf3_count") or 0)
    bt_data = coverage.get("bt_data") or {}
    mtf3_bt_data = coverage.get("mtf3_bt_data") or {}
    bt_5m30m = int(bt_data.get("sample_5m30m_ready_count") or 0)
    bt_mtf3 = int(bt_data.get("sample_mtf3_ready_count") or 0)
    mtf3_bt_mtf3 = int(mtf3_bt_data.get("sample_mtf3_ready_count") or 0)
    counts = {
        "chart_cache_complete_mtf3_count": chart_complete,
        "bt_sample_5m30m_ready_count": bt_5m30m,
        "bt_sample_mtf3_ready_count": bt_mtf3,
        "mtf3_bt_sample_mtf3_ready_count": mtf3_bt_mtf3,
    }
    if scope == "a_5m30m_large_sample":
        ready = market == "a" and bt_5m30m >= min_a_5m30m_sample
        return (
            ready,
            f"A 5m/30m sample {bt_5m30m}/{min_a_5m30m_sample}",
            counts,
        )
    if scope == "mtf3_1m5m30m":
        if market == "a":
            ready_count = max(chart_complete, bt_mtf3, mtf3_bt_mtf3)
            return (
                ready_count >= min_a_mtf3_symbols,
                f"A MTF3 sample {ready_count}/{min_a_mtf3_symbols}",
                counts,
            )
        if market == "us":
            return (
                chart_complete >= min_us_mtf3_symbols,
                f"US MTF3 sample {chart_complete}/{min_us_mtf3_symbols}",
                counts,
            )
        return False, "unsupported market for MTF3 gate", counts
    return True, "summary-only candidate does not require MTF3 coverage gate", counts


def _strategy_adoption_gate_action(
    candidate_action: str,
    *,
    evidence_ready: bool,
    coverage_reason: str,
) -> tuple[str, str]:
    if candidate_action in {"default_missing", "candidate_missing"}:
        return "blocked_missing_report", "candidate or default summary is missing"
    if candidate_action == "review_sell_policy":
        if evidence_ready:
            return "review_allowed", "candidate action is positive and evidence coverage gate passed"
        return "blocked_evidence", f"candidate action is positive but evidence coverage is insufficient: {coverage_reason}"
    if candidate_action == "keep_default":
        return "keep_default", "candidate underperformed default"
    if candidate_action in {"watch", "watch_drawdown", "watch_defensive"}:
        if evidence_ready:
            return "watch", "candidate evidence is mixed or too small for adoption"
        return "watch_evidence_limited", f"candidate is only watch-level and coverage is limited: {coverage_reason}"
    if candidate_action == "insufficient_sample":
        return "blocked_sample", "candidate report trade sample is too small"
    return "observe", "candidate has no adoption action"


def _build_bs_point_ratio_impact_market(
    market: str,
    current_path: Path,
    baseline_path: Path,
    current: Mapping[str, object],
    baseline: Mapping[str, object],
    multipliers: Mapping[str, float],
    *,
    current_reason: str = "",
    baseline_reason: str = "",
) -> dict:
    current_total = _float_first(current, "total", "total_return")
    current_dd = _float_first(current, "max_dd", "max_drawdown")
    current_excess = _float_first(current, "excess")
    current_trades = int(_float_first(current, "trade_count", "trades", "n"))
    if multipliers:
        baseline_total = _float_first(baseline, "total", "total_return")
        baseline_dd = _float_first(baseline, "max_dd", "max_drawdown")
        baseline_excess = _float_first(baseline, "excess")
        baseline_trades = int(_float_first(baseline, "trade_count", "trades", "n"))
    else:
        baseline_total = current_total
        baseline_dd = current_dd
        baseline_excess = current_excess
        baseline_trades = current_trades
    delta_return = current_total - baseline_total
    delta_dd = current_dd - baseline_dd
    delta_excess = current_excess - baseline_excess
    action, reason = _bs_point_ratio_impact_action(
        bool(multipliers),
        current_reason=current_reason,
        baseline_reason=baseline_reason,
        delta_return=delta_return,
        delta_dd=delta_dd,
    )
    return {
        "market": market,
        "current_summary_path": str(current_path),
        "baseline_summary_path": str(baseline_path),
        "bs_point_ratio_overrides_enabled": bool(
            current.get("bs_point_ratio_overrides_enabled")
        ),
        "bs_point_ratio_multipliers": dict(multipliers),
        "baseline_required": bool(multipliers),
        "current_total_return": current_total,
        "baseline_total_return": baseline_total,
        "delta_total_return": delta_return,
        "current_max_drawdown": current_dd,
        "baseline_max_drawdown": baseline_dd,
        "delta_max_drawdown": delta_dd,
        "current_excess_return": current_excess,
        "baseline_excess_return": baseline_excess,
        "delta_excess_return": delta_excess,
        "current_trade_count": current_trades,
        "baseline_trade_count": baseline_trades,
        "delta_trade_count": current_trades - baseline_trades,
        "action": action,
        "reason": reason,
    }


def _bs_point_ratio_impact_action(
    has_multipliers: bool,
    *,
    current_reason: str,
    baseline_reason: str,
    delta_return: float,
    delta_dd: float,
) -> tuple[str, str]:
    if current_reason:
        return "current_missing", "current summary is missing or unreadable"
    if not has_multipliers:
        return "no_active_override", "no confirmed buy-point ratio multiplier is active"
    if baseline_reason:
        return "baseline_missing", "baseline summary without ratio override is missing"
    if delta_return >= 0.0 and delta_dd <= 0.005:
        return "keep_override", "override improved return with controlled drawdown change"
    if delta_return < -0.002 and delta_dd >= 0.0:
        return "review_disable", "override reduced return without lowering drawdown"
    return "watch", "override impact is mixed or too small to change"


def _summary_bs_point_multipliers(summary: Mapping[str, object]) -> dict[str, float]:
    raw = summary.get("bs_point_ratio_multipliers") or {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        cls = str(key)
        if cls not in {"1", "2", "3"}:
            continue
        try:
            out[cls] = float(value)
        except Exception:
            continue
    return out


def _build_sell_policy_impact_market(
    market: str,
    default_path: Path,
    candidate_path: Path,
    default: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    candidate_label: str,
    default_reason: str = "",
    candidate_reason: str = "",
) -> dict:
    default_total = _float_first(default, "total", "total_return")
    default_dd = _float_first(default, "max_dd", "max_drawdown")
    default_excess = _float_first(default, "excess")
    default_trades = int(_float_first(default, "trade_count", "trades", "n"))
    candidate_total = _float_first(candidate, "total", "total_return")
    candidate_dd = _float_first(candidate, "max_dd", "max_drawdown")
    candidate_excess = _float_first(candidate, "excess")
    candidate_trades = int(_float_first(candidate, "trade_count", "trades", "n"))
    delta_return = candidate_total - default_total
    delta_dd = candidate_dd - default_dd
    delta_excess = candidate_excess - default_excess
    action, reason = _sell_policy_impact_action(
        default_reason=default_reason,
        candidate_reason=candidate_reason,
        default_trade_count=default_trades,
        candidate_trade_count=candidate_trades,
        delta_return=delta_return,
        delta_dd=delta_dd,
    )
    return {
        "market": market,
        "candidate_label": candidate_label,
        "default_summary_path": str(default_path),
        "candidate_summary_path": str(candidate_path),
        "default_sell_classes": _summary_sell_classes(default) or [1, 2, 3],
        "candidate_sell_classes": _summary_sell_classes(candidate),
        "default_sell_ratio_overrides": _summary_sell_ratio_overrides(default),
        "candidate_sell_ratio_overrides": _summary_sell_ratio_overrides(candidate),
        "default_sell_ratio_override_scope": str(
            default.get("sell_ratio_override_scope") or "all"
        ),
        "candidate_sell_ratio_override_scope": str(
            candidate.get("sell_ratio_override_scope") or "all"
        ),
        "default_op_level": str(default.get("op_level") or ""),
        "candidate_op_level": str(candidate.get("op_level") or ""),
        "default_mid_level": str(default.get("mid_level") or ""),
        "candidate_mid_level": str(candidate.get("mid_level") or ""),
        "default_big_level": str(default.get("big_level") or ""),
        "candidate_big_level": str(candidate.get("big_level") or ""),
        "default_after_3sell_reentry_buy_classes": _summary_int_list(
            default,
            "after_3sell_reentry_buy_classes",
        ),
        "candidate_after_3sell_reentry_buy_classes": _summary_int_list(
            candidate,
            "after_3sell_reentry_buy_classes",
        ),
        "default_after_3sell_reentry_mid_buy_classes": _summary_int_list(
            default,
            "after_3sell_reentry_mid_buy_classes",
        ),
        "candidate_after_3sell_reentry_mid_buy_classes": _summary_int_list(
            candidate,
            "after_3sell_reentry_mid_buy_classes",
        ),
        "default_after_3sell_reentry_scope": str(
            default.get("after_3sell_reentry_scope") or "all"
        ),
        "candidate_after_3sell_reentry_scope": str(
            candidate.get("after_3sell_reentry_scope") or "all"
        ),
        "default_total_return": default_total,
        "candidate_total_return": candidate_total,
        "delta_total_return": delta_return,
        "default_max_drawdown": default_dd,
        "candidate_max_drawdown": candidate_dd,
        "delta_max_drawdown": delta_dd,
        "default_excess_return": default_excess,
        "candidate_excess_return": candidate_excess,
        "delta_excess_return": delta_excess,
        "default_trade_count": default_trades,
        "candidate_trade_count": candidate_trades,
        "delta_trade_count": candidate_trades - default_trades,
        "action": action,
        "reason": reason,
    }


def _sell_policy_impact_action(
    *,
    default_reason: str,
    candidate_reason: str,
    default_trade_count: int,
    candidate_trade_count: int,
    delta_return: float,
    delta_dd: float,
) -> tuple[str, str]:
    if default_reason:
        return "default_missing", "default all-sell summary is missing or unreadable"
    if candidate_reason:
        return "candidate_missing", "candidate sell-policy summary is missing or unreadable"
    if candidate_trade_count < 10 <= default_trade_count:
        return "insufficient_sample", "candidate has too few trades for policy change"
    if delta_return >= 0.002 and delta_dd <= 0.005:
        return "review_sell_policy", "candidate improved return with controlled drawdown change"
    if delta_return >= 0.0 and delta_dd > 0.005:
        return "watch_drawdown", "candidate improved return but drawdown rose too much"
    if delta_return >= -0.02 and delta_dd <= -0.01:
        return "watch_defensive", "candidate materially lowered drawdown with modest return give-up"
    if delta_return < -0.002:
        return "keep_default", "candidate reduced return versus default all-sell exits"
    if delta_dd <= -0.003:
        return "watch_defensive", "candidate lowered drawdown but return edge is small"
    return "watch", "candidate impact is mixed or too small to change"


def _summary_sell_classes(summary: Mapping[str, object]) -> list[int]:
    raw = summary.get("sell_classes")
    if not isinstance(raw, (list, tuple)):
        return []
    classes: list[int] = []
    for item in raw:
        try:
            cls = int(item)
        except (TypeError, ValueError):
            continue
        if cls in {1, 2, 3} and cls not in classes:
            classes.append(cls)
    return classes


def _summary_sell_ratio_overrides(summary: Mapping[str, object]) -> dict[str, float]:
    raw = summary.get("sell_ratio_overrides") or {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        cls = str(key)
        if cls not in {"1", "2", "3"}:
            continue
        try:
            out[cls] = float(value)
        except Exception:
            continue
    return out


def _summary_int_list(summary: Mapping[str, object], key: str) -> list[int]:
    raw = summary.get(key) or []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in out:
            out.append(value)
    return out


def _load_summary_or_reason(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, "missing"
    try:
        return load_summary(path), ""
    except Exception as exc:
        return {}, type(exc).__name__


def _bs_ratio_status(action: str, ready_to_apply: bool, apply_allowed: bool) -> str:
    if action in {"keep", "watch"}:
        return action
    if apply_allowed:
        return "apply_allowed"
    if ready_to_apply:
        return "confirming"
    return "observing"


def runtime_override_for_market(path: str | Path, market: str) -> dict:
    entry = runtime_override_entry_for_market(path, market)
    return dict(entry.get("monitor_config") or {}) if entry else {}


def runtime_override_entry_for_market(path: str | Path, market: str) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    market = str(market).strip().lower()
    for item in data.get("overrides", []) or []:
        if str(item.get("market") or "").strip().lower() == market:
            return dict(item)
    return {}


def build_bs_point_attribution_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    trade_paths: Mapping[str, str | Path] | None = None,
    min_trades: int = 10,
) -> dict:
    from chanlun.recursive_bt.market_runtime import default_backtest_report_paths, normalize_market

    trade_paths = dict(trade_paths or {})
    market_reports = []
    missing_sources = []
    for market in markets:
        market = normalize_market(market)
        _summary_path, default_trades = default_backtest_report_paths(market)
        path = Path(trade_paths.get(market) or default_trades)
        if not path.exists():
            missing_sources.append(
                {"market": market, "kind": "trade_csv", "path": str(path), "reason": "missing"}
            )
            rows: list[dict] = []
        else:
            try:
                rows = _load_trade_rows(path)
            except Exception as exc:
                missing_sources.append(
                    {
                        "market": market,
                        "kind": "trade_csv",
                        "path": str(path),
                        "reason": type(exc).__name__,
                    }
                )
                rows = []
        groups = _summarize_bs_point_groups(rows, min_trades=min_trades)
        sell_groups = _summarize_sell_point_groups(rows, min_trades=min_trades)
        market_reports.append(
            {
                "market": market,
                "trade_path": str(path),
                "trade_count": len(rows),
                "groups": groups,
                "ratio_guidance": _bs_ratio_guidance(groups, min_trades=min_trades),
                "sell_groups": sell_groups,
                "sell_ratio_guidance": _sell_ratio_guidance(
                    sell_groups,
                    min_trades=min_trades,
                ),
            }
        )
    return {
        "version": 2,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_bs_point_attribution_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    trade_paths: Mapping[str, str | Path] | None = None,
    min_trades: int = 10,
) -> dict:
    report = build_bs_point_attribution_report(
        markets,
        trade_paths=trade_paths,
        min_trades=min_trades,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_bs_point_attribution_markdown(report), encoding="utf-8")
    return report


def _group_rows_by_key(
    rows: Iterable[Mapping[str, object]],
    key: str,
    *,
    min_trades: int,
) -> list[dict]:
    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        name = str(row.get(key) or "unknown").strip() or "unknown"
        groups.setdefault(name, []).append(row)
    return [
        _summarize_trade_group(name, list(items), min_trades=min_trades)
        for name, items in sorted(groups.items())
    ]


def _layer_guidance(groups: Iterable[Mapping[str, object]], *, min_trades: int) -> list[dict]:
    out: list[dict] = []
    for group in groups:
        layer = str(group.get("bs_class") or "unknown")
        count = int(group.get("trade_count") or 0)
        avg_return = float(group.get("avg_return") or 0.0)
        win_rate = float(group.get("win_rate") or 0.0)
        max_dd = float(group.get("max_drawdown") or 0.0)
        if count < min_trades:
            action = "watch"
            reason = "sample too thin for layer policy adjustment"
        elif avg_return < 0.0 and win_rate < 0.45:
            action = "reduce_layer_risk"
            reason = "negative average return and weak win rate"
        elif avg_return > 0.005 and win_rate >= 0.55 and max_dd <= 0.10:
            action = "keep_or_boost"
            reason = "positive layer edge with controlled drawdown"
        else:
            action = "keep_watch"
            reason = "no confirmed layer edge change"
        out.append(
            {
                "layer": layer,
                "action": action,
                "reason": reason,
                "trade_count": count,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "max_drawdown": max_dd,
            }
        )
    return out


def build_layer_attribution_report(
    summary_path: str | Path,
    trade_path: str | Path | None = None,
    *,
    min_trades: int = 10,
) -> dict:
    summary_path = Path(summary_path)
    trade_path = Path(trade_path) if trade_path is not None else _summary_to_trades_path(summary_path)
    summary: Mapping[str, object] = {}
    rows: list[dict] = []
    missing_sources: list[dict] = []
    if not summary_path.exists():
        missing_sources.append({"kind": "summary_json", "path": str(summary_path), "reason": "missing"})
    else:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            missing_sources.append(
                {"kind": "summary_json", "path": str(summary_path), "reason": type(exc).__name__}
            )
    if not trade_path.exists():
        missing_sources.append({"kind": "trade_csv", "path": str(trade_path), "reason": "missing"})
    else:
        try:
            rows = _load_trade_rows(trade_path)
        except Exception as exc:
            missing_sources.append(
                {"kind": "trade_csv", "path": str(trade_path), "reason": type(exc).__name__}
            )
    entry_layer_groups = _group_rows_by_key(rows, "entry_layer", min_trades=min_trades)
    entry_level_groups = _group_rows_by_key(rows, "entry_level", min_trades=min_trades)
    exit_layer_groups = _group_rows_by_key(rows, "exit_layer", min_trades=min_trades)
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "summary_path": str(summary_path),
        "trade_path": str(trade_path),
        "min_trades": int(min_trades),
        "summary": {
            "total_return": _float_first(summary, "total_return", "total"),
            "buy_hold": _float_first(summary, "buy_hold", "bh"),
            "max_drawdown": _float_first(summary, "max_drawdown", "max_dd"),
            "trade_count": int(_float_first(summary, "trade_count", "n")),
            "signal_event_count": int(_float_first(summary, "signal_event_count")),
            "core_signal_level": int(_float_first(summary, "core_signal_level")),
            "swing_signal_level": int(_float_first(summary, "swing_signal_level")),
        },
        "trade_count": len(rows),
        "entry_layer_groups": entry_layer_groups,
        "entry_level_groups": entry_level_groups,
        "exit_layer_groups": exit_layer_groups,
        "layer_guidance": _layer_guidance(entry_layer_groups, min_trades=min_trades),
        "missing_sources": missing_sources,
    }


def render_layer_attribution_markdown(report: Mapping[str, object]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Chanlun Layer Attribution Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Summary: `{report.get('summary_path', '')}`",
        f"Trades: `{report.get('trade_path', '')}`",
        "",
        "## Replay Summary",
        "",
        "| Total Return | Buy Hold | Max DD | Trades | Signals | Core Level | Swing Level |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| {ret:.2%} | {bh:.2%} | {dd:.2%} | {trades} | {signals} | {core} | {swing} |".format(
            ret=float(summary.get("total_return") or 0.0),
            bh=float(summary.get("buy_hold") or 0.0),
            dd=float(summary.get("max_drawdown") or 0.0),
            trades=int(summary.get("trade_count") or 0),
            signals=int(summary.get("signal_event_count") or 0),
            core=int(summary.get("core_signal_level") or 0),
            swing=int(summary.get("swing_signal_level") or 0),
        ),
        "",
        "## Entry Layers",
        "",
        "| Layer | Trades | Win Rate | Avg Ret | Compound | Max DD | Avg Hold H | Guidance |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    guidance_by_layer = {
        str(item.get("layer") or ""): item
        for item in report.get("layer_guidance", []) or []
    }
    for group in report.get("entry_layer_groups", []) or []:
        layer = str(group.get("bs_class") or "unknown")
        guidance = guidance_by_layer.get(layer, {})
        lines.append(
            "| {layer} | {trades} | {win:.1%} | {avg:.2%} | {comp:.2%} | {dd:.2%} | {hold:.1f} | {action}: {reason} |".format(
                layer=layer,
                trades=int(group.get("trade_count") or 0),
                win=float(group.get("win_rate") or 0.0),
                avg=float(group.get("avg_return") or 0.0),
                comp=float(group.get("compound_return") or 0.0),
                dd=float(group.get("max_drawdown") or 0.0),
                hold=float(group.get("avg_hold_hours") or 0.0),
                action=guidance.get("action") or "watch",
                reason=str(guidance.get("reason") or "").replace("|", "/"),
            )
        )
    lines.extend(
        [
            "",
            "## Entry Levels",
            "",
            "| Level | Trades | Win Rate | Avg Ret | Compound | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for group in report.get("entry_level_groups", []) or []:
        lines.append(
            "| {level} | {trades} | {win:.1%} | {avg:.2%} | {comp:.2%} | {dd:.2%} |".format(
                level=group.get("bs_class") or "unknown",
                trades=int(group.get("trade_count") or 0),
                win=float(group.get("win_rate") or 0.0),
                avg=float(group.get("avg_return") or 0.0),
                comp=float(group.get("compound_return") or 0.0),
                dd=float(group.get("max_drawdown") or 0.0),
            )
        )
    lines.extend(
        [
            "",
            "## Exit Layers",
            "",
            "| Exit Layer | Trades | Win Rate | Avg Ret | Compound | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for group in report.get("exit_layer_groups", []) or []:
        lines.append(
            "| {layer} | {trades} | {win:.1%} | {avg:.2%} | {comp:.2%} | {dd:.2%} |".format(
                layer=group.get("bs_class") or "unknown",
                trades=int(group.get("trade_count") or 0),
                win=float(group.get("win_rate") or 0.0),
                avg=float(group.get("avg_return") or 0.0),
                comp=float(group.get("compound_return") or 0.0),
                dd=float(group.get("max_drawdown") or 0.0),
            )
        )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(f"- `{item.get('path')}`: {item.get('reason')}")
    lines.append("")
    return "\n".join(lines)


def write_layer_attribution_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    summary_path: str | Path,
    trade_path: str | Path | None = None,
    min_trades: int = 10,
) -> dict:
    report = build_layer_attribution_report(
        summary_path,
        trade_path,
        min_trades=min_trades,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_layer_attribution_markdown(report), encoding="utf-8")
    return report


def build_bs_point_regime_attribution_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    summary_paths: Mapping[str, str | Path] | None = None,
    trade_paths: Mapping[str, str | Path] | None = None,
    min_trades: int = 10,
) -> dict:
    from chanlun.recursive_bt.market_runtime import normalize_market

    summary_paths = dict(summary_paths or {})
    trade_paths = dict(trade_paths or {})
    default_summaries = {
        "a": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
        "us": US_MTF3_DEFAULT_SUMMARY,
    }
    market_reports = []
    missing_sources = []
    for market in markets:
        market = normalize_market(market)
        summary_path = Path(summary_paths.get(market) or default_summaries[market])
        trade_path = Path(trade_paths.get(market) or _summary_to_trades_path(summary_path))
        summary: Mapping[str, object] = {}
        if not summary_path.exists():
            missing_sources.append(
                {
                    "market": market,
                    "kind": "summary_json",
                    "path": str(summary_path),
                    "reason": "missing",
                }
            )
        else:
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception as exc:
                missing_sources.append(
                    {
                        "market": market,
                        "kind": "summary_json",
                        "path": str(summary_path),
                        "reason": type(exc).__name__,
                    }
                )
        regime_by_date = _summary_daily_regime_map(summary)
        if summary and not regime_by_date:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "daily_regimes",
                    "path": str(summary_path),
                    "reason": "missing_daily_regimes",
                }
            )
        if not trade_path.exists():
            missing_sources.append(
                {
                    "market": market,
                    "kind": "trade_csv",
                    "path": str(trade_path),
                    "reason": "missing",
                }
            )
            rows: list[dict] = []
        else:
            try:
                rows = _load_trade_rows(trade_path)
            except Exception as exc:
                missing_sources.append(
                    {
                        "market": market,
                        "kind": "trade_csv",
                        "path": str(trade_path),
                        "reason": type(exc).__name__,
                    }
                )
                rows = []
        annotated_rows = [
            _annotate_trade_regime(row, regime_by_date)
            for row in rows
        ]
        buy_groups = _summarize_bs_point_regime_groups(
            annotated_rows,
            min_trades=min_trades,
        )
        sell_groups = _summarize_sell_point_regime_groups(
            annotated_rows,
            min_trades=min_trades,
        )
        market_reports.append(
            {
                "market": market,
                "summary_path": str(summary_path),
                "trade_path": str(trade_path),
                "trade_count": len(rows),
                "daily_regime_count": len(regime_by_date),
                "buy_groups": buy_groups,
                "buy_ratio_guidance": _bs_regime_ratio_guidance(
                    buy_groups,
                    min_trades=min_trades,
                ),
                "sell_groups": sell_groups,
            }
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "method": "trade.entry_date joined to summary.market_regime_segments.daily_regimes",
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_bs_point_regime_attribution_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    summary_paths: Mapping[str, str | Path] | None = None,
    trade_paths: Mapping[str, str | Path] | None = None,
    min_trades: int = 10,
) -> dict:
    report = build_bs_point_regime_attribution_report(
        markets,
        summary_paths=summary_paths,
        trade_paths=trade_paths,
        min_trades=min_trades,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(
            render_bs_point_regime_attribution_markdown(report),
            encoding="utf-8",
        )
    return report


def render_bs_point_regime_attribution_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Buy/Sell Point Regime Attribution Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Method: {report.get('method', '')}",
        "",
        "## Buy Points By Regime",
        "",
        "| Market | Regime | Class | Trades | Win Rate | Avg Ret | Median | Max DD | Guidance |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for market_report in report.get("markets", []) or []:
        market = market_report.get("market", "")
        guidance = {
            (str(item.get("regime") or ""), str(item.get("bs_class") or "")): item
            for item in market_report.get("buy_ratio_guidance", []) or []
        }
        for group in market_report.get("buy_groups", []) or []:
            key = (str(group.get("regime") or ""), str(group.get("bs_class") or ""))
            g = guidance.get(key, {})
            lines.append(
                "| {market} | {regime} | {cls} | {trades} | {win:.1%} | {avg:.2%} | {median:.2%} | {dd:.2%} | {action} {mult:.2f} |".format(
                    market=market,
                    regime=group.get("regime") or "",
                    cls=group.get("bs_class") or "",
                    trades=int(group.get("trade_count") or 0),
                    win=float(group.get("win_rate") or 0.0),
                    avg=float(group.get("avg_return") or 0.0),
                    median=float(group.get("median_return") or 0.0),
                    dd=float(group.get("max_drawdown") or 0.0),
                    action=g.get("action") or "watch",
                    mult=float(g.get("ratio_multiplier") or 1.0),
                )
            )
    lines.extend(
        [
            "",
            "## Sell Points By Regime",
            "",
            "| Market | Regime | Exit Class | Trades | Win Rate | Avg Ret | Post 20 | MFE 20 | MAE 20 | Max DD |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for market_report in report.get("markets", []) or []:
        market = market_report.get("market", "")
        for group in market_report.get("sell_groups", []) or []:
            lines.append(
                "| {market} | {regime} | {cls} | {trades} | {win:.1%} | {avg:.2%} | {post20:.2%} | {mfe20:.2%} | {mae20:.2%} | {dd:.2%} |".format(
                    market=market,
                    regime=group.get("regime") or "",
                    cls=group.get("bs_class") or "",
                    trades=int(group.get("trade_count") or 0),
                    win=float(group.get("win_rate") or 0.0),
                    avg=float(group.get("avg_return") or 0.0),
                    post20=float(group.get("avg_post_exit_ret_20") or 0.0),
                    mfe20=float(group.get("avg_post_exit_mfe_20") or 0.0),
                    mae20=float(group.get("avg_post_exit_mae_20") or 0.0),
                    dd=float(group.get("max_drawdown") or 0.0),
                )
            )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(f"- {item.get('market')} `{item.get('path')}`: {item.get('reason')}")
    lines.append("")
    return "\n".join(lines)


def build_bs_point_regime_policy_report(
    bs_point_regime_report: Mapping[str, object],
    *,
    min_trades: int = 30,
) -> dict:
    policies = []
    for market_report in bs_point_regime_report.get("markets", []) or []:
        market = str(market_report.get("market") or "")
        guidance_by_key = {
            (str(item.get("regime") or ""), str(item.get("bs_class") or "")): item
            for item in market_report.get("buy_ratio_guidance", []) or []
        }
        for group in market_report.get("buy_groups", []) or []:
            regime = str(group.get("regime") or "unknown")
            bs_class = str(group.get("bs_class") or "unknown")
            guidance = guidance_by_key.get((regime, bs_class), {})
            action, reason = _bs_point_regime_policy_action(
                group,
                guidance,
                min_trades=min_trades,
            )
            policies.append(
                {
                    "market": market,
                    "regime": regime,
                    "bs_class": bs_class,
                    "policy_action": action,
                    "reason": reason,
                    "trade_count": int(group.get("trade_count") or 0),
                    "win_rate": float(group.get("win_rate") or 0.0),
                    "avg_return": float(group.get("avg_return") or 0.0),
                    "median_return": float(group.get("median_return") or 0.0),
                    "max_drawdown": float(group.get("max_drawdown") or 0.0),
                    "current_ratio_multiplier": 1.0,
                    "candidate_ratio_multiplier": float(
                        guidance.get("ratio_multiplier") or 1.0
                    ),
                    "guidance_action": str(guidance.get("action") or ""),
                    "source_sample_state": str(group.get("sample_state") or ""),
                }
            )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "method": "conservative policy over strategy_bs_point_regime_attribution_report",
        "min_trades": int(min_trades),
        "policies": sorted(policies, key=_bs_point_regime_policy_sort_key),
        "missing_sources": list(bs_point_regime_report.get("missing_sources", []) or []),
    }


def write_bs_point_regime_policy_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    bs_point_regime_report: Mapping[str, object],
    min_trades: int = 30,
) -> dict:
    report = build_bs_point_regime_policy_report(
        bs_point_regime_report,
        min_trades=min_trades,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(
            render_bs_point_regime_policy_markdown(report),
            encoding="utf-8",
        )
    return report


def render_bs_point_regime_policy_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Buy Point Regime Policy Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Method: {report.get('method', '')}",
        f"Min trades: {report.get('min_trades', '')}",
        "",
        "| Market | Regime | Class | Policy | Trades | Win Rate | Avg Ret | Median | Max DD | Candidate | Reason |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("policies", []) or []:
        lines.append(
            "| {market} | {regime} | {cls} | {policy} | {trades} | {win:.1%} | {avg:.2%} | {median:.2%} | {dd:.2%} | {candidate:.2f} | {reason} |".format(
                market=item.get("market") or "",
                regime=item.get("regime") or "",
                cls=item.get("bs_class") or "",
                policy=item.get("policy_action") or "",
                trades=int(item.get("trade_count") or 0),
                win=float(item.get("win_rate") or 0.0),
                avg=float(item.get("avg_return") or 0.0),
                median=float(item.get("median_return") or 0.0),
                dd=float(item.get("max_drawdown") or 0.0),
                candidate=float(item.get("candidate_ratio_multiplier") or 1.0),
                reason=str(item.get("reason") or "").replace("|", "/"),
            )
        )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(f"- {item.get('market')} `{item.get('path')}`: {item.get('reason')}")
    lines.append("")
    return "\n".join(lines)


def render_bs_point_attribution_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Buy/Sell Point Attribution Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "## Buy Points",
        "",
        "| Market | Trades | Class | Win Rate | Avg Ret | Compound | Max DD | Avg Hold H | Guidance |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for market_report in report.get("markets", []) or []:
        market = market_report.get("market", "")
        trade_count = int(market_report.get("trade_count") or 0)
        guidance_by_class = {
            str(item.get("bs_class") or ""): item
            for item in market_report.get("ratio_guidance", []) or []
        }
        for group in market_report.get("groups", []) or []:
            bs_class = str(group.get("bs_class") or "")
            guidance = guidance_by_class.get(bs_class, {})
            lines.append(
                "| {market} | {trades} | {cls} | {win:.1%} | {avg:.2%} | {comp:.2%} | {dd:.2%} | {hold:.1f} | {action} {mult:.2f} |".format(
                    market=market,
                    trades=trade_count,
                    cls=bs_class,
                    win=float(group.get("win_rate") or 0.0),
                    avg=float(group.get("avg_return") or 0.0),
                    comp=float(group.get("compound_return") or 0.0),
                    dd=float(group.get("max_drawdown") or 0.0),
                    hold=float(group.get("avg_hold_hours") or 0.0),
                    action=guidance.get("action") or "watch",
                    mult=float(guidance.get("ratio_multiplier") or 1.0),
                )
            )
    lines.extend(
        [
            "",
            "## Sell Points",
            "",
            "| Market | Trades | Exit Class | Win Rate | Avg Ret | Post 20 | MFE 20 | MAE 20 | Max DD | Guidance |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for market_report in report.get("markets", []) or []:
        market = market_report.get("market", "")
        trade_count = int(market_report.get("trade_count") or 0)
        guidance_by_class = {
            str(item.get("bs_class") or ""): item
            for item in market_report.get("sell_ratio_guidance", []) or []
        }
        for group in market_report.get("sell_groups", []) or []:
            bs_class = str(group.get("bs_class") or "")
            guidance = guidance_by_class.get(bs_class, {})
            lines.append(
                "| {market} | {trades} | {cls} | {win:.1%} | {avg:.2%} | {post20:.2%} | {mfe20:.2%} | {mae20:.2%} | {dd:.2%} | {action} {ratio:.2f} |".format(
                    market=market,
                    trades=trade_count,
                    cls=bs_class,
                    win=float(group.get("win_rate") or 0.0),
                    avg=float(group.get("avg_return") or 0.0),
                    post20=float(group.get("avg_post_exit_ret_20") or 0.0),
                    mfe20=float(group.get("avg_post_exit_mfe_20") or 0.0),
                    mae20=float(group.get("avg_post_exit_mae_20") or 0.0),
                    dd=float(group.get("max_drawdown") or 0.0),
                    action=guidance.get("action") or "watch",
                    ratio=float(guidance.get("sell_ratio") or 1.0),
                )
            )
    if report.get("missing_sources"):
        lines.extend(["", "## Missing Sources", ""])
        for item in report.get("missing_sources", []) or []:
            lines.append(f"- {item.get('market')} `{item.get('path')}`: {item.get('reason')}")
    lines.append("")
    return "\n".join(lines)


def _summary_to_trades_path(summary_path: str | Path) -> Path:
    path = Path(summary_path)
    name = path.name
    if name.endswith("_summary.json"):
        return path.with_name(name.replace("_summary.json", "_trades.csv"))
    return path.with_suffix(".trades.csv")


def _summary_daily_regime_map(summary: Mapping[str, object]) -> dict[str, str]:
    segments = summary.get("market_regime_segments") or {}
    daily = segments.get("daily_regimes") if isinstance(segments, Mapping) else None
    out: dict[str, str] = {}
    for item in daily or []:
        date = str(item.get("date") or "").strip()
        regime = str(item.get("regime") or "").strip().lower()
        if date and regime in {"bull", "range", "bear"}:
            out[date] = regime
    return out


def _annotate_trade_regime(
    row: Mapping[str, object],
    regime_by_date: Mapping[str, str],
) -> dict:
    out = dict(row)
    entry = _parse_ts(row.get("entry_date"))
    date = str(entry.date()) if entry is not None else ""
    out["market_regime"] = regime_by_date.get(date, "unknown")
    return out


def _summarize_bs_point_regime_groups(
    rows: Iterable[Mapping[str, object]],
    *,
    min_trades: int = 10,
) -> list[dict]:
    by_key: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in rows:
        regime = str(row.get("market_regime") or "unknown")
        bs_class = _trade_bs_class(row)
        by_key.setdefault((regime, bs_class), []).append(row)
    groups = []
    for (regime, bs_class), items in by_key.items():
        group = _summarize_trade_group(bs_class, items, min_trades=min_trades)
        group["regime"] = regime
        groups.append(group)
    return sorted(groups, key=_bs_regime_group_sort_key)


def _summarize_sell_point_regime_groups(
    rows: Iterable[Mapping[str, object]],
    *,
    min_trades: int = 10,
) -> list[dict]:
    by_key: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in rows:
        regime = str(row.get("market_regime") or "unknown")
        bs_class = _trade_exit_bs_class(row)
        by_key.setdefault((regime, bs_class), []).append(row)
    groups = []
    for (regime, bs_class), items in by_key.items():
        group = _summarize_trade_group(bs_class, items, min_trades=min_trades)
        group["regime"] = regime
        groups.append(group)
    return sorted(groups, key=_bs_regime_group_sort_key)


def _bs_regime_ratio_guidance(
    groups: Iterable[Mapping[str, object]],
    *,
    min_trades: int,
) -> list[dict]:
    out = []
    for group in groups:
        regime = str(group.get("regime") or "unknown")
        bs_class = str(group.get("bs_class") or "unknown")
        count = int(group.get("trade_count") or 0)
        avg_return = float(group.get("avg_return") or 0.0)
        win_rate = float(group.get("win_rate") or 0.0)
        max_dd = float(group.get("max_drawdown") or 0.0)
        if regime == "unknown" or count < min_trades or bs_class == "unknown":
            action = "watch"
            multiplier = 1.0
            reason = "sample too thin or missing regime labels"
        elif avg_return < 0.0 and win_rate < 0.45:
            action = "reduce_in_regime"
            multiplier = 0.75
            reason = "negative average return and weak win rate in this regime"
        elif avg_return > 0.005 and win_rate >= 0.58 and max_dd <= 0.08:
            action = "allow_regime_boost"
            multiplier = 1.10
            reason = "positive average return, strong win rate, controlled regime drawdown"
        else:
            action = "keep"
            multiplier = 1.0
            reason = "no confirmed regime-specific edge change"
        out.append(
            {
                "regime": regime,
                "bs_class": bs_class,
                "action": action,
                "ratio_multiplier": multiplier,
                "reason": reason,
                "trade_count": count,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "max_drawdown": max_dd,
            }
        )
    return sorted(out, key=_bs_regime_group_sort_key)


def _bs_regime_group_sort_key(item: Mapping[str, object]) -> tuple[int, int, str]:
    regime_order = {"bull": 0, "range": 1, "bear": 2, "unknown": 3}
    regime = str(item.get("regime") or "unknown")
    bs_order, bs_name = _bs_group_sort_key(item)
    return (regime_order.get(regime, 99), bs_order, bs_name)


def _bs_point_regime_policy_action(
    group: Mapping[str, object],
    guidance: Mapping[str, object],
    *,
    min_trades: int,
) -> tuple[str, str]:
    regime = str(group.get("regime") or "unknown")
    bs_class = str(group.get("bs_class") or "unknown")
    count = int(group.get("trade_count") or 0)
    avg_return = float(group.get("avg_return") or 0.0)
    win_rate = float(group.get("win_rate") or 0.0)
    guidance_action = str(guidance.get("action") or "")
    if regime == "unknown" or bs_class not in {"1", "2", "3"} or count < min_trades:
        return (
            "evidence_limited",
            "sample too thin or missing regime labels; keep current ratio",
        )
    if guidance_action == "allow_regime_boost":
        return (
            "review_regime_ratio_boost",
            "positive regime edge; requires dedicated ratio-impact backtest before live override",
        )
    if guidance_action == "reduce_in_regime":
        return (
            "review_regime_ratio_reduce",
            "weak regime edge; requires dedicated ratio-impact backtest before live override",
        )
    if avg_return > 0.0 and win_rate >= 0.50:
        return (
            "watch_positive_regime_edge",
            "positive regime evidence but not enough to change ratio without impact test",
        )
    return (
        "keep_current_ratio",
        "no confirmed regime-specific ratio edge",
    )


def _bs_point_regime_policy_sort_key(item: Mapping[str, object]) -> tuple[int, int, int, str]:
    market_order = {"a": 0, "us": 1}
    regime_order = {"bull": 0, "range": 1, "bear": 2, "unknown": 3}
    bs_order, bs_name = _bs_group_sort_key(item)
    return (
        market_order.get(str(item.get("market") or ""), 99),
        regime_order.get(str(item.get("regime") or "unknown"), 99),
        bs_order,
        bs_name,
    )


def _load_trade_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return [dict(row) for row in csv.DictReader(fp)]


def _summarize_bs_point_groups(
    rows: Iterable[Mapping[str, object]],
    *,
    min_trades: int = 10,
) -> list[dict]:
    by_class: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        bs_class = _trade_bs_class(row)
        by_class.setdefault(bs_class, []).append(row)
    groups = [
        _summarize_trade_group(bs_class, items, min_trades=min_trades)
        for bs_class, items in by_class.items()
    ]
    return sorted(groups, key=_bs_group_sort_key)


def _summarize_sell_point_groups(
    rows: Iterable[Mapping[str, object]],
    *,
    min_trades: int = 10,
) -> list[dict]:
    by_class: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        bs_class = _trade_exit_bs_class(row)
        by_class.setdefault(bs_class, []).append(row)
    groups = [
        _summarize_trade_group(bs_class, items, min_trades=min_trades)
        for bs_class, items in by_class.items()
    ]
    return sorted(groups, key=_bs_group_sort_key)


def _summarize_trade_group(
    bs_class: str,
    rows: list[Mapping[str, object]],
    *,
    min_trades: int,
) -> dict:
    returns = [_float_first(row, "ret", "return", "pnl_pct") for row in rows]
    wins = sum(1 for value in returns if value > 0)
    hold_hours = [_trade_hold_hours(row) for row in rows]
    hold_hours = [value for value in hold_hours if value >= 0]
    post5 = _float_values(rows, "post_exit_ret_5")
    post20 = _float_values(rows, "post_exit_ret_20")
    post60 = _float_values(rows, "post_exit_ret_60")
    mfe20 = _float_values(rows, "post_exit_mfe_20")
    mae20 = _float_values(rows, "post_exit_mae_20")
    return {
        "bs_class": bs_class,
        "trade_count": len(rows),
        "sample_state": "enough" if len(rows) >= min_trades else "thin",
        "win_rate": wins / len(rows) if rows else 0.0,
        "avg_return": sum(returns) / len(returns) if returns else 0.0,
        "median_return": _median(returns),
        "compound_return": _compound_values(returns),
        "max_drawdown": _return_sequence_max_drawdown(returns),
        "best_return": max(returns) if returns else 0.0,
        "worst_return": min(returns) if returns else 0.0,
        "avg_hold_hours": sum(hold_hours) / len(hold_hours) if hold_hours else 0.0,
        "exit_reasons": _top_exit_reasons(rows),
        "post_exit_sample_count": len(post20),
        "avg_post_exit_ret_5": _avg(post5),
        "avg_post_exit_ret_20": _avg(post20),
        "avg_post_exit_ret_60": _avg(post60),
        "avg_post_exit_mfe_20": _avg(mfe20),
        "avg_post_exit_mae_20": _avg(mae20),
    }


def _bs_ratio_guidance(groups: Iterable[Mapping[str, object]], *, min_trades: int) -> list[dict]:
    out = []
    for group in groups:
        bs_class = str(group.get("bs_class") or "unknown")
        count = int(group.get("trade_count") or 0)
        avg_return = float(group.get("avg_return") or 0.0)
        win_rate = float(group.get("win_rate") or 0.0)
        max_dd = float(group.get("max_drawdown") or 0.0)
        if count < min_trades or bs_class == "unknown":
            action = "watch"
            multiplier = 1.0
            reason = "sample too thin for ratio adjustment"
        elif avg_return < 0.0 and win_rate < 0.45:
            action = "reduce"
            multiplier = 0.75
            reason = "negative average return and weak win rate"
        elif avg_return > 0.005 and win_rate >= 0.58 and max_dd <= 0.10:
            action = "allow_boost"
            multiplier = 1.10
            reason = "positive average return, strong win rate, controlled drawdown"
        else:
            action = "keep"
            multiplier = 1.0
            reason = "no confirmed edge change"
        out.append(
            {
                "bs_class": bs_class,
                "action": action,
                "ratio_multiplier": multiplier,
                "reason": reason,
                "trade_count": count,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "max_drawdown": max_dd,
            }
        )
    return sorted(out, key=_bs_group_sort_key)


def _sell_ratio_guidance(groups: Iterable[Mapping[str, object]], *, min_trades: int) -> list[dict]:
    out = []
    for group in groups:
        bs_class = str(group.get("bs_class") or "unknown")
        count = int(group.get("trade_count") or 0)
        post_samples = int(group.get("post_exit_sample_count") or 0)
        post20 = float(group.get("avg_post_exit_ret_20") or 0.0)
        mfe20 = float(group.get("avg_post_exit_mfe_20") or 0.0)
        if (
            bs_class in {"1", "2", "3"}
            and count >= min_trades
            and post_samples >= min_trades
            and post20 > 0.005
            and mfe20 > 0.010
        ):
            action = "review_scale_out"
            sell_ratio = 1.0
            reason = "post-exit drift stayed positive; run partial-exit backtest before changing live ratio"
        elif bs_class in {"1", "2", "3", "big_down"}:
            action = "keep_full_exit"
            sell_ratio = 1.0
            reason = "Chanlun sell/exit signals remain full exits until partial-exit evidence exists"
        elif bs_class == "final":
            action = "close_only"
            sell_ratio = 1.0
            reason = "forced final close is attribution-only and not a live sell signal"
        elif count < min_trades:
            action = "watch"
            sell_ratio = 1.0
            reason = "sample too thin for sell ratio adjustment"
        else:
            action = "watch"
            sell_ratio = 1.0
            reason = "unclassified sell path is not eligible for partial exits"
        out.append(
            {
                "bs_class": bs_class,
                "action": action,
                "sell_ratio": sell_ratio,
                "reason": reason,
                "trade_count": count,
                "win_rate": float(group.get("win_rate") or 0.0),
                "avg_return": float(group.get("avg_return") or 0.0),
                "max_drawdown": float(group.get("max_drawdown") or 0.0),
            }
        )
    return sorted(out, key=_bs_group_sort_key)


def _trade_bs_class(row: Mapping[str, object]) -> str:
    raw = str(row.get("bs_type") or row.get("bs") or "").strip().lower()
    for cls in ("1", "2", "3"):
        if raw.startswith(cls):
            return cls
    return "unknown"


def _trade_exit_bs_class(row: Mapping[str, object]) -> str:
    raw = str(
        row.get("exit_bs_type")
        or row.get("sell_bs_type")
        or row.get("exit_bs")
        or ""
    ).strip().lower()
    for cls in ("1", "2", "3"):
        if raw.startswith(cls):
            return cls
    reason = str(row.get("reason") or "").strip().lower()
    if "big_level_down" in reason or "大级别" in reason or "turned down" in reason:
        return "big_down"
    if "final" in reason or "收尾" in reason:
        return "final"
    if "sell" in reason or "卖点" in reason:
        return "sell_unknown"
    return "unknown"


def _bs_group_sort_key(item: Mapping[str, object]) -> tuple[int, str]:
    order = {"1": 0, "2": 1, "3": 2, "big_down": 3, "sell_unknown": 4, "final": 5, "unknown": 6}
    cls = str(item.get("bs_class") or "unknown")
    return (order.get(cls, 99), cls)


def _trade_hold_hours(row: Mapping[str, object]) -> float:
    entry = _parse_ts(row.get("entry_date"))
    exit_ = _parse_ts(row.get("exit_date"))
    if entry is None or exit_ is None:
        return -1.0
    return max((exit_ - entry).total_seconds() / 3600.0, 0.0)


def _parse_ts(value: object) -> _dt.datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return _dt.datetime.fromisoformat(text.split("+", 1)[0])
        except ValueError:
            return None


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _compound_values(values: Iterable[float]) -> float:
    total = 1.0
    seen = False
    for value in values:
        total *= 1.0 + float(value)
        seen = True
    return total - 1.0 if seen else 0.0


def _return_sequence_max_drawdown(values: Iterable[float]) -> float:
    curve = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in values:
        curve *= 1.0 + float(value)
        peak = max(peak, curve)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - curve / peak)
    return max_dd


def _top_exit_reasons(rows: Iterable[Mapping[str, object]], limit: int = 3) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def match_candidate_from_monitor_config(
    config: Mapping[str, object],
    market: Optional[str] = None,
) -> str:
    """Return the embedded candidate id that matches a live monitor config."""

    market = str(market or config.get("market") or "").strip().lower() or None
    config_norm = {str(key): _normalize_config_value(value) for key, value in config.items()}
    keys = (
        "max_pos",
        "op_level",
        "mid_level",
        "big_level",
        "mid_gate",
        "regime_mode",
        "nest_mode",
        "trend_3boost",
    )
    for candidate in default_strategy_candidates(market):
        candidate_config = candidate.monitor_config()
        if all(
            key not in config_norm
            or config_norm[key] == _normalize_config_value(candidate_config.get(key))
            for key in keys
        ):
            return candidate.id
    return ""


def _candidate_rankings_by_market(
    candidates: Iterable[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    out: dict[str, list[Mapping[str, object]]] = {}
    for candidate in candidates:
        market = str(candidate.get("market") or "").strip().lower()
        if market:
            out.setdefault(market, []).append(candidate)
    return out


def _runtime_by_market_id(
    runtime: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, Mapping[str, object]]]:
    out: dict[str, dict[str, Mapping[str, object]]] = {}
    for item in runtime:
        source = item.get("source") or {}
        market = str(source.get("market") or "").strip().lower()
        source_id = str(source.get("id") or "").strip()
        if market and source_id:
            out.setdefault(market, {})[source_id] = item
    return out


def _lowest_drawdown_candidate(
    candidates: Iterable[Mapping[str, object]],
    *,
    exclude_id: str = "",
) -> Mapping[str, object] | None:
    best = None
    best_dd = None
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        if exclude_id and candidate_id == exclude_id:
            continue
        score = candidate.get("score") or {}
        dd = _float_first(score, "max_drawdown")
        if best is None or dd < best_dd:
            best = candidate
            best_dd = dd
    return best


def _normalize_config_value(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = str(value or "").strip().lower()
    if text in {"true", "false"}:
        return text == "true"
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _risk_state_for_action(action: str) -> str:
    return {
        "keep_candidate": "ok",
        "switch_candidate": "switch_ready",
        "degrade_candidate": "risk_reduce",
        "review_runtime_gap": "review",
    }.get(action, "unknown")


def _decision_status(
    *,
    action: str,
    ready_to_apply: bool,
    requires_review: bool,
    apply_allowed: bool,
) -> str:
    if action == "keep_candidate":
        return "stable"
    if requires_review:
        return "review_required"
    if apply_allowed:
        return "apply_allowed"
    if ready_to_apply:
        return "confirming"
    return "observing"


def _best_candidate_ids(candidates: Iterable[Mapping[str, object]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for candidate in candidates:
        market = str(candidate.get("market") or "")
        if market and market not in out:
            out[market] = str(candidate.get("id") or "")
    return out


def _best_runtime_ids(runtime: Iterable[ScoredRuntimeSummary]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in runtime:
        market = item.source.market
        if market and market not in out:
            out[market] = item.source.id
    return out


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build Chanlun strategy optimization report")
    parser.add_argument("--market", choices=("a", "us"))
    parser.add_argument("--report-dir", default="D:/chanlun_pro/reports")
    parser.add_argument(
        "--output-json",
        default="D:/chanlun_pro/reports/strategy_optimization_report.json",
    )
    parser.add_argument(
        "--output-markdown",
        default="D:/chanlun_pro/reports/strategy_optimization_report.md",
    )
    parser.add_argument(
        "--output-decision",
        default="D:/chanlun_pro/reports/strategy_decision.json",
    )
    parser.add_argument(
        "--output-decision-state",
        default="D:/chanlun_pro/reports/strategy_decision_state.json",
    )
    parser.add_argument(
        "--output-runtime-overrides",
        default="D:/chanlun_pro/reports/strategy_runtime_overrides.json",
    )
    parser.add_argument(
        "--output-attribution-json",
        default="D:/chanlun_pro/reports/strategy_attribution_report.json",
    )
    parser.add_argument(
        "--output-attribution-markdown",
        default="D:/chanlun_pro/reports/strategy_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json",
    )
    parser.add_argument(
        "--output-bs-point-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-ratio-state",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_state.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-overrides",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-impact-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_impact_report.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_impact_report.md",
    )
    parser.add_argument(
        "--output-bs-point-regime-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.json",
    )
    parser.add_argument(
        "--output-bs-point-regime-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-regime-policy-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json",
    )
    parser.add_argument(
        "--output-bs-point-regime-policy-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.md",
    )
    parser.add_argument("--bs-point-regime-policy-min-trades", type=int, default=30)
    parser.add_argument(
        "--output-regime-ratio-impact-json",
        default="D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.json",
    )
    parser.add_argument(
        "--output-regime-ratio-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.md",
    )
    parser.add_argument(
        "--output-market-regime-stress-json",
        default="D:/chanlun_pro/reports/strategy_market_regime_stress_report.json",
    )
    parser.add_argument(
        "--output-market-regime-stress-markdown",
        default="D:/chanlun_pro/reports/strategy_market_regime_stress_report.md",
    )
    parser.add_argument(
        "--output-us-2026q1-regime-stress-json",
        default=(
            "D:/chanlun_pro/reports/"
            "strategy_market_regime_stress_us_2026q1_report.json"
        ),
    )
    parser.add_argument(
        "--output-us-2026q1-regime-stress-markdown",
        default=(
            "D:/chanlun_pro/reports/"
            "strategy_market_regime_stress_us_2026q1_report.md"
        ),
    )
    parser.add_argument(
        "--output-regime-policy-json",
        default="D:/chanlun_pro/reports/strategy_regime_policy_report.json",
    )
    parser.add_argument(
        "--output-regime-policy-markdown",
        default="D:/chanlun_pro/reports/strategy_regime_policy_report.md",
    )
    parser.add_argument("--regime-policy-min-supporting-sources", type=int, default=2)
    parser.add_argument(
        "--us-2026q1-mtf3-default-summary",
        default=US_2026Q1_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-2026q1-mtf3-sell3-rebuy3-summary",
        default=US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-2026q1-mtf3-sell3-rebuy-mid3-summary",
        default=US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument("--market-regime-min-days", type=int, default=10)
    parser.add_argument(
        "--output-sell-policy-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell_policy_impact_report.json",
    )
    parser.add_argument(
        "--output-sell-policy-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell_policy_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3half-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3half_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3half-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3half_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3half-up-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3half_up_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3half-up-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3half_up_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-candidate-summary",
        default=A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy3-default-summary",
        default=US_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy3-candidate-summary",
        default=US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-sell3-rebuy3-up-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-up-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-up-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-up-candidate-summary",
        default=A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-sell3-rebuy-mid3-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy-mid3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy-mid3-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy-mid3-candidate-summary",
        default=A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy-mid3-default-summary",
        default=US_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy-mid3-candidate-summary",
        default=US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-a-5m-sell3-rebuy3-impact-json",
        default="D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.json",
    )
    parser.add_argument(
        "--output-a-5m-sell3-rebuy3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.md",
    )
    parser.add_argument(
        "--a-5m-sell3-rebuy3-default-summary",
        default="D:/chanlun_pro/reports/a_bt_all_a_5m30m_default_summary.json",
    )
    parser.add_argument(
        "--a-5m-sell3-rebuy3-candidate-summary",
        default="D:/chanlun_pro/reports/a_bt_all_a_5m30m_sell3_rebuy3_summary.json",
    )
    parser.add_argument(
        "--output-mtf3-cache-coverage-json",
        default="D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.json",
    )
    parser.add_argument(
        "--output-mtf3-cache-coverage-markdown",
        default="D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.md",
    )
    parser.add_argument(
        "--output-strategy-adoption-gate-json",
        default="D:/chanlun_pro/reports/strategy_adoption_gate_report.json",
    )
    parser.add_argument(
        "--output-strategy-adoption-gate-markdown",
        default="D:/chanlun_pro/reports/strategy_adoption_gate_report.md",
    )
    parser.add_argument(
        "--mtf3-cache-chart-cache-dir",
        default="D:/chanlun_pro/chart_cache",
    )
    parser.add_argument(
        "--mtf3-cache-bt-data-dir",
        default="D:/chanlun_pro/bt_data_all_a",
    )
    parser.add_argument(
        "--mtf3-cache-mtf3-bt-data-dir",
        default="D:/chanlun_pro/bt_data_mtf3_all_a",
    )
    parser.add_argument("--mtf3-cache-bt-sample-size", type=int, default=300)
    parser.add_argument(
        "--runtime-override-audit",
        default="D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl",
    )
    parser.add_argument(
        "--no-attribution-baseline",
        action="store_true",
        help="Do not create baseline paper-ledger snapshots for attribution",
    )
    parser.add_argument("--decision-confirmation-threshold", type=int, default=3)
    parser.add_argument("--bs-point-ratio-confirmation-threshold", type=int, default=3)
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Only use default paper ledgers and default live-parity summaries",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = make_arg_parser().parse_args(argv)
    write_optimization_report(
        args.output_json,
        output_markdown=args.output_markdown,
        output_decision=args.output_decision,
        output_decision_state=args.output_decision_state,
        output_runtime_overrides=args.output_runtime_overrides,
        market=args.market,
        include_discovered=not args.no_discover,
        report_dir=args.report_dir,
        decision_confirmation_threshold=args.decision_confirmation_threshold,
    )
    write_strategy_attribution_report(
        args.output_attribution_json,
        output_markdown=args.output_attribution_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        audit_path=args.runtime_override_audit,
        ensure_baseline_ledgers=not args.no_attribution_baseline,
    )
    bs_point_report = write_bs_point_attribution_report(
        args.output_bs_point_json,
        output_markdown=args.output_bs_point_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    bs_ratio_state = update_bs_point_ratio_state_file(
        args.output_bs_point_ratio_state,
        bs_point_report,
        confirmation_threshold=args.bs_point_ratio_confirmation_threshold,
    )
    write_bs_point_ratio_overrides_file(
        args.output_bs_point_ratio_overrides,
        bs_ratio_state,
    )
    write_bs_point_ratio_impact_report(
        args.output_bs_point_ratio_impact_json,
        output_markdown=args.output_bs_point_ratio_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    bs_point_regime_report = write_bs_point_regime_attribution_report(
        args.output_bs_point_regime_json,
        output_markdown=args.output_bs_point_regime_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    write_bs_point_regime_policy_report(
        args.output_bs_point_regime_policy_json,
        output_markdown=args.output_bs_point_regime_policy_markdown,
        bs_point_regime_report=bs_point_regime_report,
        min_trades=args.bs_point_regime_policy_min_trades,
    )
    market_regime_report = write_market_regime_stress_report(
        args.output_market_regime_stress_json,
        output_markdown=args.output_market_regime_stress_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        min_regime_days=args.market_regime_min_days,
    )
    write_regime_ratio_impact_report(
        args.output_regime_ratio_impact_json,
        output_markdown=args.output_regime_ratio_impact_markdown,
    )
    wrote_us_2026q1_regime_stress = False
    us_2026q1_regime_report = None
    if args.market in (None, "", "us"):
        us_2026q1_regime_report = write_market_regime_stress_report(
            args.output_us_2026q1_regime_stress_json,
            output_markdown=args.output_us_2026q1_regime_stress_markdown,
            markets=("us",),
            summary_paths={
                "us": {
                    "default": args.us_2026q1_mtf3_default_summary,
                    "sell3_rebuy3": args.us_2026q1_mtf3_sell3_rebuy3_summary,
                    "sell3_rebuy_mid3": args.us_2026q1_mtf3_sell3_rebuy_mid3_summary,
                }
            },
            min_regime_days=args.market_regime_min_days,
        )
        wrote_us_2026q1_regime_stress = True
    regime_policy_sources = {"primary": market_regime_report}
    if us_2026q1_regime_report is not None:
        regime_policy_sources["us_2026q1_weak"] = us_2026q1_regime_report
    write_regime_strategy_policy_report(
        args.output_regime_policy_json,
        output_markdown=args.output_regime_policy_markdown,
        report_sources=regime_policy_sources,
        min_supporting_sources=args.regime_policy_min_supporting_sources,
    )
    write_sell_policy_impact_report(
        args.output_sell_policy_impact_json,
        output_markdown=args.output_sell_policy_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    write_sell_policy_impact_report(
        args.output_sell3half_impact_json,
        output_markdown=args.output_sell3half_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        candidate_label="sell3half",
    )
    write_sell_policy_impact_report(
        args.output_sell3half_up_impact_json,
        output_markdown=args.output_sell3half_up_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        candidate_label="sell3half_up",
    )
    sell3_rebuy3_report = write_sell_policy_impact_report(
        args.output_sell3_rebuy3_impact_json,
        output_markdown=args.output_sell3_rebuy3_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        summary_paths={
            "a": args.a_mtf3_sell3_rebuy3_default_summary,
            "us": args.us_mtf3_sell3_rebuy3_default_summary,
        },
        candidate_summary_paths={
            "a": args.a_mtf3_sell3_rebuy3_candidate_summary,
            "us": args.us_mtf3_sell3_rebuy3_candidate_summary,
        },
        candidate_label="sell3_rebuy3",
    )
    sell3_rebuy3_up_report = None
    if args.market in (None, "", "a"):
        sell3_rebuy3_up_report = write_sell_policy_impact_report(
            args.output_sell3_rebuy3_up_impact_json,
            output_markdown=args.output_sell3_rebuy3_up_impact_markdown,
            markets=("a",),
            summary_paths={"a": args.a_mtf3_sell3_rebuy3_up_default_summary},
            candidate_summary_paths={
                "a": args.a_mtf3_sell3_rebuy3_up_candidate_summary,
            },
            candidate_label="sell3_rebuy3_up",
        )
    sell3_rebuy_mid3_report = write_sell_policy_impact_report(
        args.output_sell3_rebuy_mid3_impact_json,
        output_markdown=args.output_sell3_rebuy_mid3_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        summary_paths={
            "a": args.a_mtf3_sell3_rebuy_mid3_default_summary,
            "us": args.us_mtf3_sell3_rebuy_mid3_default_summary,
        },
        candidate_summary_paths={
            "a": args.a_mtf3_sell3_rebuy_mid3_candidate_summary,
            "us": args.us_mtf3_sell3_rebuy_mid3_candidate_summary,
        },
        candidate_label="sell3_rebuy_mid3",
    )
    adoption_candidate_reports = [sell3_rebuy3_report, sell3_rebuy_mid3_report]
    if sell3_rebuy3_up_report is not None:
        adoption_candidate_reports.insert(1, sell3_rebuy3_up_report)
    wrote_a_5m_sell3_rebuy3 = False
    if args.market in (None, "", "a"):
        a_5m_sell3_rebuy3_report = write_sell_policy_impact_report(
            args.output_a_5m_sell3_rebuy3_impact_json,
            output_markdown=args.output_a_5m_sell3_rebuy3_impact_markdown,
            markets=("a",),
            summary_paths={"a": args.a_5m_sell3_rebuy3_default_summary},
            candidate_summary_paths={
                "a": args.a_5m_sell3_rebuy3_candidate_summary,
            },
            candidate_label="a_5m_sell3_rebuy3",
        )
        adoption_candidate_reports.append(a_5m_sell3_rebuy3_report)
        wrote_a_5m_sell3_rebuy3 = True
    mtf3_coverage_report = write_mtf3_cache_coverage_report(
        args.output_mtf3_cache_coverage_json,
        output_markdown=args.output_mtf3_cache_coverage_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        chart_cache_dir=args.mtf3_cache_chart_cache_dir,
        bt_data_dir=args.mtf3_cache_bt_data_dir,
        mtf3_bt_data_dir=args.mtf3_cache_mtf3_bt_data_dir or None,
        bt_sample_size=args.mtf3_cache_bt_sample_size,
    )
    write_strategy_adoption_gate_report(
        args.output_strategy_adoption_gate_json,
        output_markdown=args.output_strategy_adoption_gate_markdown,
        coverage_report=mtf3_coverage_report,
        candidate_reports=adoption_candidate_reports,
    )
    print(f"summary={args.output_json}")
    print(f"markdown={args.output_markdown}")
    print(f"decision={args.output_decision}")
    print(f"decision_state={args.output_decision_state}")
    print(f"runtime_overrides={args.output_runtime_overrides}")
    print(f"attribution={args.output_attribution_json}")
    print(f"attribution_markdown={args.output_attribution_markdown}")
    print(f"bs_point_attribution={args.output_bs_point_json}")
    print(f"bs_point_attribution_markdown={args.output_bs_point_markdown}")
    print(f"bs_point_ratio_state={args.output_bs_point_ratio_state}")
    print(f"bs_point_ratio_overrides={args.output_bs_point_ratio_overrides}")
    print(f"bs_point_ratio_impact={args.output_bs_point_ratio_impact_json}")
    print(f"bs_point_ratio_impact_markdown={args.output_bs_point_ratio_impact_markdown}")
    print(f"bs_point_regime={args.output_bs_point_regime_json}")
    print(f"bs_point_regime_markdown={args.output_bs_point_regime_markdown}")
    print(f"bs_point_regime_policy={args.output_bs_point_regime_policy_json}")
    print(
        "bs_point_regime_policy_markdown="
        f"{args.output_bs_point_regime_policy_markdown}"
    )
    print(f"regime_ratio_impact={args.output_regime_ratio_impact_json}")
    print(
        "regime_ratio_impact_markdown="
        f"{args.output_regime_ratio_impact_markdown}"
    )
    print(f"market_regime_stress={args.output_market_regime_stress_json}")
    print(
        "market_regime_stress_markdown="
        f"{args.output_market_regime_stress_markdown}"
    )
    if wrote_us_2026q1_regime_stress:
        print(f"us_2026q1_regime_stress={args.output_us_2026q1_regime_stress_json}")
        print(
            "us_2026q1_regime_stress_markdown="
            f"{args.output_us_2026q1_regime_stress_markdown}"
        )
    print(f"regime_policy={args.output_regime_policy_json}")
    print(f"regime_policy_markdown={args.output_regime_policy_markdown}")
    print(f"sell_policy_impact={args.output_sell_policy_impact_json}")
    print(f"sell_policy_impact_markdown={args.output_sell_policy_impact_markdown}")
    print(f"sell3half_impact={args.output_sell3half_impact_json}")
    print(f"sell3half_impact_markdown={args.output_sell3half_impact_markdown}")
    print(f"sell3half_up_impact={args.output_sell3half_up_impact_json}")
    print(f"sell3half_up_impact_markdown={args.output_sell3half_up_impact_markdown}")
    print(f"sell3_rebuy3_impact={args.output_sell3_rebuy3_impact_json}")
    print(f"sell3_rebuy3_impact_markdown={args.output_sell3_rebuy3_impact_markdown}")
    print(f"sell3_rebuy3_up_impact={args.output_sell3_rebuy3_up_impact_json}")
    print(
        "sell3_rebuy3_up_impact_markdown="
        f"{args.output_sell3_rebuy3_up_impact_markdown}"
    )
    print(f"sell3_rebuy_mid3_impact={args.output_sell3_rebuy_mid3_impact_json}")
    print(f"sell3_rebuy_mid3_impact_markdown={args.output_sell3_rebuy_mid3_impact_markdown}")
    if wrote_a_5m_sell3_rebuy3:
        print(f"a_5m_sell3_rebuy3_impact={args.output_a_5m_sell3_rebuy3_impact_json}")
        print(
            "a_5m_sell3_rebuy3_impact_markdown="
            f"{args.output_a_5m_sell3_rebuy3_impact_markdown}"
        )
    print(f"mtf3_cache_coverage={args.output_mtf3_cache_coverage_json}")
    print(f"mtf3_cache_coverage_markdown={args.output_mtf3_cache_coverage_markdown}")
    print(f"strategy_adoption_gate={args.output_strategy_adoption_gate_json}")
    print(f"strategy_adoption_gate_markdown={args.output_strategy_adoption_gate_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
