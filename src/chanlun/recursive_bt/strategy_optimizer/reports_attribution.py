"""strategy_optimizer 买卖点/层级/regime 归因报告 + trade 明细分析 —— 从 _impl 拆出。

读交易明细 csv,按买卖点类别/层级/行情分组统计收益与比例指导,产出归因报告
+ markdown。trade 分析 helper(_summarize_trade_group 等)专为本族服务。
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path
from typing import Iterable, Mapping

from chanlun.recursive_bt.strategy_optimizer.constants import (
    A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    US_MTF3_DEFAULT_SUMMARY,
)
from chanlun.recursive_bt.strategy_optimizer.utils import (
    _avg,
    _float_first,
    _float_values,
)


def build_bs_point_attribution_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    trade_paths: Mapping[str, str | Path] | None = None,
    min_trades: int = 10,
) -> dict:
    from chanlun.recursive_bt.engine.market_runtime import default_backtest_report_paths, normalize_market

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
    from chanlun.recursive_bt.engine.market_runtime import normalize_market

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
