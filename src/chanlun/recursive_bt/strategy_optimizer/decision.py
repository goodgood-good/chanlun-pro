"""strategy_optimizer 优化报告 + 决策/覆盖 主线。

汇总候选/运行时评分为优化报告,推导决策工件/状态与 runtime/买点比例覆盖文件,
并按 monitor_config 反查候选。供 CLI/外部 live_monitor 调用。依赖 candidates+
models+scoring+utils。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Mapping, Optional

from chanlun.recursive_bt.strategy_optimizer.candidates import (
    a_selection_systems,
    default_strategy_candidates,
)
from chanlun.recursive_bt.strategy_optimizer.models import (
    ScoreWeights,
    ScoredRuntimeSummary,
)
from chanlun.recursive_bt.strategy_optimizer.scoring import (
    default_runtime_summary_sources,
    discover_backtest_summary_sources,
    rank_candidates_by_evidence,
    score_runtime_sources,
)
from chanlun.recursive_bt.strategy_optimizer.utils import (
    _float_first,
)


def _atomic_write_json(path: Path, obj) -> None:
    """原子写 JSON: 先写 .tmp 再 os.replace, 防写一半被 kill 截断(与 ledger/dedup/manifest 原子写一致)。

    Round11 B3: 决策/覆盖状态文件原用直接 write_text(与 ledger 等的 tmp+os.replace 不一致),
    中途被 kill 会留截断文件。读端 json.loads 套 try/except 回落 {} 虽 fail-safe(降风险 override
    被静默丢), 但与全仓原子写纪律对齐更稳健。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        "> ⚠ 下表 Return/Score 为 **in-sample 排名上界**(无 OOS / 无多重比较校正,审计 F-CRIT-1);",
        "> batch 来源候选另含确定性未来函数(D3-CRIT-1)。非实盘可达,勿外推为真实收益。",
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
        if source_id != f"{market}_recursive_backtest":
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
                "observation": "recursive_backtest_runtime_lag",
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
    _atomic_write_json(path, state)
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
    _atomic_write_json(path, overrides)
    return overrides


def _bs_ratio_status(action: str, ready_to_apply: bool, apply_allowed: bool) -> str:
    if action in {"keep", "watch"}:
        return action
    if apply_allowed:
        return "apply_allowed"
    if ready_to_apply:
        return "confirming"
    return "observing"


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
    _atomic_write_json(path, state)
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
    _atomic_write_json(path, overrides)
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
