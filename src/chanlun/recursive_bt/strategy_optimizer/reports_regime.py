"""strategy_optimizer regime/影响评估报告族 —— 从 _impl 拆出。

按行情(bull/range/bear)与买卖点比例评估回测 summary:regime ratio/买点比例/卖出
策略影响、市场压力测试、策略采纳门控等报告 + markdown。依赖 constants+utils+scoring。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Iterable, Mapping

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
    US_MTF3_DEFAULT_SUMMARY,
    US_MTF3_REGIME_WEAK1REDUCE_SUMMARY,
    US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
)
from chanlun.recursive_bt.strategy_optimizer.scoring import (
    load_summary,
)
from chanlun.recursive_bt.strategy_optimizer.utils import (
    _float_first,
)


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
