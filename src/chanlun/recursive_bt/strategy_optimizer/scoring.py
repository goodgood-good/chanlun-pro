"""strategy_optimizer 评分层。

候选/运行时摘要评分 + 来源发现:score_summary 核心打分,rank_* 排序,
load_summary/summary_from_paper_ledger 读取,default/discover_*_summary_sources
枚举来源,score_runtime_sources 落地评分。依赖 models + utils + candidates。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Optional

from chanlun.recursive_bt.strategy_optimizer.candidates import (
    default_strategy_candidates,
)
from chanlun.recursive_bt.strategy_optimizer.models import (
    RuntimeSummarySource,
    ScoreWeights,
    ScoredRuntimeSummary,
    StrategyCandidate,
    StrategyScore,
)
from chanlun.recursive_bt.strategy_optimizer.utils import (
    _float_first,
    _path_key,
)


def score_summary(
    summary: Mapping[str, object],
    *,
    candidate_id: str = "",
    market: str = "",
    source: str = "",
    weights: ScoreWeights = ScoreWeights(),
) -> StrategyScore:
    total_return = _float_first(summary, "total_return", "total")
    max_drawdown = _float_first(summary, "max_drawdown", "max_dd")
    sharpe = _float_first(summary, "sharpe")
    trade_count = int(_float_first(summary, "trade_count", "trades", "n"))
    low_trade_penalty = 0.0
    if weights.trade_floor > 0 and trade_count < weights.trade_floor:
        low_trade_penalty = (
            (weights.trade_floor - trade_count)
            / weights.trade_floor
            * weights.low_trade_penalty
        )
    score = (
        weights.return_weight * total_return
        - weights.drawdown_penalty * max_drawdown
        + weights.sharpe_weight * sharpe
        - low_trade_penalty
    )
    return StrategyScore(
        candidate_id=candidate_id,
        market=market,
        score=score,
        total_return=total_return,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        trade_count=trade_count,
        source=source,
    )


def rank_candidates_by_evidence(
    market: Optional[str] = None,
    *,
    weights: ScoreWeights = ScoreWeights(),
) -> list[tuple[StrategyCandidate, StrategyScore]]:
    scored = []
    for candidate in default_strategy_candidates(market):
        score = score_summary(
            candidate.evidence_summary or {},
            candidate_id=candidate.id,
            market=candidate.market,
            source="embedded_evidence",
            weights=weights,
        )
        scored.append((candidate, score))
    return sorted(scored, key=lambda item: item[1].score, reverse=True)


def rank_summary_records(
    records: Iterable[tuple[str, str, Mapping[str, object]]],
    *,
    weights: ScoreWeights = ScoreWeights(),
) -> list[StrategyScore]:
    scores = [
        score_summary(
            summary,
            candidate_id=candidate_id,
            market=market,
            source="runtime_summary",
            weights=weights,
        )
        for candidate_id, market, summary in records
    ]
    return sorted(scores, key=lambda item: item.score, reverse=True)


def load_summary(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _infer_market_from_summary_path(path: Path) -> str:
    try:
        data = load_summary(path)
        market = str(data.get("market") or "").strip().lower()
        if market in {"a", "us"}:
            return market
    except Exception:
        pass
    name = path.name.lower()
    if name.startswith("us_") or "_us_" in name or "us-" in name:
        return "us"
    return "a"


def summary_from_paper_ledger(path: str | Path) -> dict:
    data = load_summary(path)
    if _paper_ledger_is_baseline_only(data) or _paper_ledger_has_no_activity(data):
        return {}
    summary = data.get("summary")
    return summary if isinstance(summary, dict) else {}


def _paper_ledger_has_no_activity(data: Mapping[str, object]) -> bool:
    trades = data.get("trades") or []
    positions = data.get("positions") or {}
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    try:
        summary_trades = int(
            summary.get("trades")
            or summary.get("trade_count")
            or summary.get("n_trades")
            or 0
        )
    except (TypeError, ValueError):
        summary_trades = 0
    return not trades and summary_trades <= 0 and not positions


def _paper_ledger_is_baseline_only(data: Mapping[str, object]) -> bool:
    trades = data.get("trades") or []
    curve = data.get("equity_curve") or []
    if trades or not isinstance(curve, list) or not curve:
        return False
    return all(
        isinstance(point, Mapping)
        and bool(point.get("baseline"))
        and str(point.get("reason") or "") == "ledger_baseline"
        for point in curve
    )


def default_runtime_summary_sources(
    markets: Iterable[str] = ("a", "us"),
) -> list[RuntimeSummarySource]:
    from chanlun.recursive_bt.engine.market_runtime import (
        default_backtest_report_paths,
        default_ledger_path,
        normalize_market,
    )

    sources: list[RuntimeSummarySource] = []
    for market in markets:
        market = normalize_market(market)
        sources.append(
            RuntimeSummarySource(
                id=f"{market}_paper_ledger",
                market=market,
                kind="paper_ledger",
                path=default_ledger_path(market),
            )
        )
        summary_path, _trades_path = default_backtest_report_paths(market)
        sources.append(
            RuntimeSummarySource(
                id=f"{market}_live_parity_backtest",
                market=market,
                kind="backtest_summary",
                path=summary_path,
            )
        )
    return sources


def discover_backtest_summary_sources(
    report_dir: str | Path = "D:/chanlun_pro/reports",
    markets: Iterable[str] = ("a", "us"),
) -> list[RuntimeSummarySource]:
    wanted = {str(market).strip().lower() for market in markets}
    out: list[RuntimeSummarySource] = []
    for path in sorted(Path(report_dir).glob("*_summary.json")):
        market = _infer_market_from_summary_path(path)
        if market not in wanted:
            continue
        out.append(
            RuntimeSummarySource(
                id=path.stem.removesuffix("_summary"),
                market=market,
                kind="backtest_summary",
                path=str(path),
            )
        )
    return out


def score_runtime_sources(
    sources: Iterable[RuntimeSummarySource],
    *,
    weights: ScoreWeights = ScoreWeights(),
) -> tuple[list[ScoredRuntimeSummary], list[dict]]:
    scored: list[ScoredRuntimeSummary] = []
    missing: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        key = (source.market, source.kind, _path_key(source.path))
        if key in seen:
            continue
        seen.add(key)
        path = Path(source.path)
        if not path.exists():
            missing.append({**source.as_dict(), "reason": "missing"})
            continue
        try:
            summary = (
                summary_from_paper_ledger(path)
                if source.kind == "paper_ledger"
                else load_summary(path)
            )
        except Exception as exc:
            missing.append({**source.as_dict(), "reason": type(exc).__name__})
            continue
        if not summary:
            reason = "empty_summary"
            if source.kind == "paper_ledger":
                try:
                    ledger_data = load_summary(path)
                    if _paper_ledger_is_baseline_only(ledger_data):
                        reason = "baseline_only"
                    elif _paper_ledger_has_no_activity(ledger_data):
                        reason = "no_activity"
                except Exception:
                    pass
            missing.append({**source.as_dict(), "reason": reason})
            continue
        score = score_summary(
            summary,
            candidate_id=source.id,
            market=source.market,
            source=source.kind,
            weights=weights,
        )
        scored.append(ScoredRuntimeSummary(source=source, score=score, summary=summary))
    return (
        sorted(scored, key=lambda item: item.score.score, reverse=True),
        missing,
    )
