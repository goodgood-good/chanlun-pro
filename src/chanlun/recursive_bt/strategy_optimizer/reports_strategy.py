"""strategy_optimizer 策略归因 / 权益曲线报告。

按 runtime override 审计事件 + 权益曲线切分市场策略区段,产出策略归因报告
(总/分段收益、回撤、夏普)+ markdown。供 CLI/外部 live_monitor 调用。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Iterable, Mapping, Optional

from chanlun.recursive_bt.strategy_optimizer.candidates import (
    default_strategy_candidates,
)


def load_runtime_override_audit_events(
    path: str | Path = "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl",
    *,
    market: Optional[str] = None,
) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    wanted = str(market).strip().lower() if market else ""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("event") != "runtime_override_applied":
            continue
        if wanted and str(event.get("market") or "").strip().lower() != wanted:
            continue
        events.append(event)
    return sorted(events, key=lambda item: str(item.get("time") or ""))


def build_strategy_attribution_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    audit_path: str | Path = "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl",
    ledger_paths: Mapping[str, str | Path] | None = None,
    ensure_baseline_ledgers: bool = False,
) -> dict:
    from chanlun.recursive_bt.engine.market_runtime import default_ledger_path, normalize_market

    ledger_paths = dict(ledger_paths or {})
    market_reports = []
    missing_sources = []
    for market in markets:
        market = normalize_market(market)
        ledger_path = Path(ledger_paths.get(market) or default_ledger_path(market))
        if ensure_baseline_ledgers:
            ensure_paper_ledger_baseline(ledger_path, market)
        curve, reason = _load_equity_curve(ledger_path)
        events = load_runtime_override_audit_events(audit_path, market=market)
        if reason:
            missing_sources.append(
                {
                    "market": market,
                    "kind": "paper_ledger",
                    "path": str(ledger_path),
                    "reason": reason,
                }
            )
        segments = (
            _build_market_strategy_segments(market, curve, events)
            if curve
            else []
        )
        market_reports.append(
            {
                "market": market,
                "ledger_path": str(ledger_path),
                "audit_path": str(audit_path),
                "audit_events": len(events),
                "segments": segments,
                "summary": _summarize_segments(segments, curve),
            }
        )
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "audit_path": str(audit_path),
        "markets": market_reports,
        "missing_sources": missing_sources,
    }


def write_strategy_attribution_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    audit_path: str | Path = "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl",
    ledger_paths: Mapping[str, str | Path] | None = None,
    ensure_baseline_ledgers: bool = False,
) -> dict:
    report = build_strategy_attribution_report(
        markets,
        audit_path=audit_path,
        ledger_paths=ledger_paths,
        ensure_baseline_ledgers=ensure_baseline_ledgers,
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
        output_markdown.write_text(
            render_strategy_attribution_markdown(report),
            encoding="utf-8",
        )
    return report


def ensure_paper_ledger_baseline(
    path: str | Path,
    market: str,
    *,
    now: str | None = None,
) -> dict | None:
    from chanlun.recursive_bt.sim.paper import PaperBroker

    now = now or _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
    broker = PaperBroker(str(path), market=market)
    snapshot = broker.ensure_baseline_snapshot(now)
    if snapshot is not None:
        broker.save()
    return snapshot


def render_strategy_attribution_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun Strategy Attribution Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        f"Audit: `{report.get('audit_path', '')}`",
        "",
        "| Market | Segments | Return | Max DD | Audit Events |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for market_report in report.get("markets", []) or []:
        summary = market_report.get("summary") or {}
        lines.append(
            "| {market} | {segments} | {ret:.2%} | {dd:.2%} | {events} |".format(
                market=market_report.get("market", ""),
                segments=int(summary.get("segment_count") or 0),
                ret=float(summary.get("total_return") or 0.0),
                dd=float(summary.get("max_drawdown") or 0.0),
                events=int(market_report.get("audit_events") or 0),
            )
        )
    for market_report in report.get("markets", []) or []:
        if not market_report.get("segments"):
            continue
        lines.extend(["", f"## {str(market_report.get('market', '')).upper()} Segments", ""])
        lines.extend(
            [
                "| Strategy | Start | End | Return | Max DD | Snapshots | Trades |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for segment in market_report.get("segments", []) or []:
            lines.append(
                "| {strategy} | {start} | {end} | {ret:.2%} | {dd:.2%} | {snapshots} | {trades} |".format(
                    strategy=segment.get("target_candidate", ""),
                    start=segment.get("start_time", ""),
                    end=segment.get("end_time", ""),
                    ret=float(segment.get("total_return") or 0.0),
                    dd=float(segment.get("max_drawdown") or 0.0),
                    snapshots=int(segment.get("snapshots") or 0),
                    trades=int(segment.get("trade_count_delta") or 0),
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


def _load_equity_curve(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], type(exc).__name__
    raw_curve = data.get("equity_curve") or []
    curve = []
    for point in raw_curve:
        if not isinstance(point, Mapping):
            continue
        try:
            equity = float(point.get("equity") or 0.0)
        except Exception:
            equity = 0.0
        if equity <= 0:
            continue
        curve.append(
            {
                "time": str(point.get("time") or ""),
                "equity": equity,
                "trades": int(float(point.get("trades") or 0)),
            }
        )
    curve = sorted(curve, key=lambda item: item["time"])
    return curve, "" if curve else "empty_equity_curve"


def _norm_ts(value) -> str:
    """isoformat 时间归一(R1-F2-1): 审计事件默认 'T' 分隔、权益快照空格分隔,
    裸字符串比较时 'T'(0x54)>' '(0x20) 使同日事件恒大于同日快照 → 分段边界滑到
    次日。位置 10 精确替换 'T'→空格,两种格式归一后字典序==时间序。"""
    s = str(value or "")
    if len(s) > 10 and s[10] == "T":
        return s[:10] + " " + s[11:]
    return s


def _build_market_strategy_segments(
    market: str,
    curve: list[dict],
    events: list[dict],
) -> list[dict]:
    default_candidate = _default_candidate_id(market)
    stage_events = [
        {
            "time": "",
            "action": "baseline",
            "risk_state": "baseline",
            "target_candidate": default_candidate,
            "decision_key": f"{market}|baseline|{default_candidate}",
            "reason": "initial paper ledger stage",
            "applied_config": {},
        }
    ] + [
        {
            "time": _norm_ts(event.get("time")),  # R1-F2-1 归一
            "action": str(event.get("action") or ""),
            "risk_state": str(event.get("risk_state") or ""),
            "target_candidate": str(event.get("target_candidate") or ""),
            "decision_key": str(event.get("decision_key") or ""),
            "reason": str(event.get("reason") or ""),
            "applied_config": dict(event.get("applied_config") or {}),
        }
        for event in events
    ]
    stage_events = sorted(stage_events, key=lambda item: item["time"])
    segments: list[dict] = []
    current_stage = stage_events[0]
    current_points: list[dict] = []
    event_idx = 1
    for point in curve:
        point_time = _norm_ts(point.get("time"))  # R1-F2-1 归一
        while event_idx < len(stage_events) and stage_events[event_idx]["time"] <= point_time:
            if current_points:
                segments.append(_segment_from_points(market, current_stage, current_points))
                current_points = []
            current_stage = stage_events[event_idx]
            event_idx += 1
        current_points.append(point)
    if current_points:
        segments.append(_segment_from_points(market, current_stage, current_points))
    return segments


def _segment_from_points(
    market: str,
    stage: Mapping[str, object],
    points: list[dict],
) -> dict:
    equities = [float(point.get("equity") or 0.0) for point in points]
    start = equities[0]
    end = equities[-1]
    trades_start = int(points[0].get("trades") or 0)
    trades_end = int(points[-1].get("trades") or 0)
    return {
        "market": market,
        "action": str(stage.get("action") or ""),
        "risk_state": str(stage.get("risk_state") or ""),
        "target_candidate": str(stage.get("target_candidate") or ""),
        "decision_key": str(stage.get("decision_key") or ""),
        "reason": str(stage.get("reason") or ""),
        "applied_config": dict(stage.get("applied_config") or {}),
        "start_time": str(points[0].get("time") or ""),
        "end_time": str(points[-1].get("time") or ""),
        "start_equity": start,
        "end_equity": end,
        "total_return": end / start - 1 if start > 0 else 0.0,
        "max_drawdown": _max_drawdown(equities),
        "snapshots": len(points),
        "trades_start": trades_start,
        "trades_end": trades_end,
        "trade_count_delta": max(trades_end - trades_start, 0),
    }


def _summarize_segments(segments: list[dict], curve: list[dict]) -> dict:
    if not segments:
        return {
            "segment_count": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "snapshots": 0,
            "trade_count_delta": 0,
        }
    start = float(segments[0].get("start_equity") or 0.0)
    end = float(segments[-1].get("end_equity") or 0.0)
    return {
        "segment_count": len(segments),
        "total_return": end / start - 1 if start > 0 else 0.0,
        # C1(R3): 全曲线口径最大回撤。原取各段自身 dd 的 max, 会漏掉跨段回撤(峰在
        # 前段、谷在后段, 每段各自把 peak 重置为本段首值)→系统性低估。curve 即全部段
        # 点按时序拼接, 一次性算全局最大回撤。
        "max_drawdown": _max_drawdown(
            [float(point.get("equity") or 0.0) for point in curve]
        ),
        "snapshots": sum(int(segment.get("snapshots") or 0) for segment in segments),
        "trade_count_delta": sum(int(segment.get("trade_count_delta") or 0) for segment in segments),
    }


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, 1.0 - value / peak)
    return max_dd


def _default_candidate_id(market: str) -> str:
    candidates = default_strategy_candidates(market)
    return candidates[0].id if candidates else "default"
