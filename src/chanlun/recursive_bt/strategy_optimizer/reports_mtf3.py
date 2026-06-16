"""strategy_optimizer MTF3 缓存覆盖报告 —— 从 _impl 拆出。

审计 A/US 的 1m5m30m 多周期 K线缓存(chart_cache + bt_data)完整度,产出覆盖
报告 + markdown + 重建命令建议。自包含(仅 stdlib),供 CLI/外部 live_monitor 调用。
"""
from __future__ import annotations

import datetime as _dt
import json
import pickle
from pathlib import Path
from typing import Iterable, Mapping


def build_mtf3_cache_coverage_report(
    markets: Iterable[str] = ("a", "us"),
    *,
    chart_cache_dir: str | Path = "D:/chanlun_pro/chart_cache",
    bt_data_dir: str | Path = "D:/chanlun_pro/bt_data_all_a",
    mtf3_bt_data_dir: str | Path | None = None,
    bt_sample_size: int = 300,
) -> dict:
    from chanlun.recursive_bt.market_runtime import (
        ashare_board,
        list_chart_cache_codes,
        normalize_market,
    )

    market_reports = []
    for market in markets:
        market = normalize_market(market)
        freq_codes = {
            freq: set(list_chart_cache_codes(market, chart_cache_dir, freq))
            for freq in ("1m", "5m", "30m")
        }
        complete_codes = sorted(set.intersection(*freq_codes.values()))
        item = {
            "market": market,
            "chart_cache_dir": str(chart_cache_dir),
            "chart_cache_counts": {
                freq: len(codes) for freq, codes in freq_codes.items()
            },
            "chart_cache_complete_mtf3_count": len(complete_codes),
            "chart_cache_complete_mtf3_codes": complete_codes[:200],
            "chart_cache_complete_mtf3_codes_truncated": len(complete_codes) > 200,
            "chart_cache_status": _mtf3_chart_cache_status(len(complete_codes)),
        }
        if market == "us":
            chart_manifest = _load_us_chart_cache_build_manifest(Path(chart_cache_dir))
            if chart_manifest:
                item["chart_cache_build_manifest"] = chart_manifest
        if market == "a":
            item["bt_data"] = _audit_a_bt_data_mtf3_cache(
                Path(bt_data_dir),
                bt_sample_size=bt_sample_size,
                ashare_board=ashare_board,
            )
            if mtf3_bt_data_dir:
                item["mtf3_bt_data"] = _audit_a_bt_data_mtf3_cache(
                    Path(mtf3_bt_data_dir),
                    bt_sample_size=bt_sample_size,
                    ashare_board=ashare_board,
                )
        item["recommended_next_actions"] = _mtf3_cache_recommended_next_actions(item)
        market_reports.append(item)
    return {
        "version": 1,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "markets": market_reports,
    }


def write_mtf3_cache_coverage_report(
    output_json: str | Path,
    *,
    output_markdown: str | Path | None = None,
    markets: Iterable[str] = ("a", "us"),
    chart_cache_dir: str | Path = "D:/chanlun_pro/chart_cache",
    bt_data_dir: str | Path = "D:/chanlun_pro/bt_data_all_a",
    mtf3_bt_data_dir: str | Path | None = None,
    bt_sample_size: int = 300,
) -> dict:
    report = build_mtf3_cache_coverage_report(
        markets,
        chart_cache_dir=chart_cache_dir,
        bt_data_dir=bt_data_dir,
        mtf3_bt_data_dir=mtf3_bt_data_dir,
        bt_sample_size=bt_sample_size,
    )
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown is not None:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_mtf3_cache_coverage_markdown(report), encoding="utf-8")
    return report


def render_mtf3_cache_coverage_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Chanlun MTF3 Cache Coverage Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "| Market | 1m Codes | 5m Codes | 30m Codes | Complete 1m+5m+30m | Chart Status | BT Files | BT Sample | BT 5m/30m Sample | BT MTF3 Sample | BT Status | MTF3 BT Files | MTF3 BT Sample | MTF3 BT Ready | MTF3 BT Status |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("markets", []) or []:
        counts = item.get("chart_cache_counts") or {}
        bt_data = item.get("bt_data") or {}
        mtf3_bt_data = item.get("mtf3_bt_data") or {}
        lines.append(
            "| {market} | {c1} | {c5} | {c30} | {complete} | {chart_status} | {bt_files} | {bt_sample} | {bt_5m30m} | {bt_mtf3} | {bt_status} | {mtf3_files} | {mtf3_sample} | {mtf3_ready} | {mtf3_status} |".format(
                market=item.get("market", ""),
                c1=int(counts.get("1m") or 0),
                c5=int(counts.get("5m") or 0),
                c30=int(counts.get("30m") or 0),
                complete=int(item.get("chart_cache_complete_mtf3_count") or 0),
                chart_status=item.get("chart_cache_status") or "",
                bt_files=int(bt_data.get("file_count") or 0),
                bt_sample=int(bt_data.get("sample_size") or 0),
                bt_5m30m=int(bt_data.get("sample_5m30m_ready_count") or 0),
                bt_mtf3=int(bt_data.get("sample_mtf3_ready_count") or 0),
                bt_status=bt_data.get("status") or "-",
                mtf3_files=int(mtf3_bt_data.get("file_count") or 0),
                mtf3_sample=int(mtf3_bt_data.get("sample_size") or 0),
                mtf3_ready=int(mtf3_bt_data.get("sample_mtf3_ready_count") or 0),
                mtf3_status=mtf3_bt_data.get("status") or "-",
            )
        )
    lines.extend(["", "## Interpretation", ""])
    lines.append(
        "- `chart_cache` complete MTF3 means the same code has 1m, 5m, and 30m kline cache files, so `live_backtest --source chart_cache --op-level 1m --mid-level 5m --big-level 30m` can build `mid_by_bar` on the fly."
    )
    lines.append(
        "- A-share `bt_data` MTF3 readiness requires sampled cache files to contain both `mid_dir_at` and `mid_by_bar`; files with only `small_by_bar` and `big_dir_at` are treated as 5m/30m-only evidence."
    )
    actions = []
    for item in report.get("markets", []) or []:
        for action in item.get("recommended_next_actions") or []:
            actions.append((item.get("market", ""), action))
    if actions:
        lines.extend(["", "## Recommended Next Actions", ""])
        for market, action in actions:
            command = action.get("command") or ""
            suffix = f": `{command}`" if command else ""
            lines.append(
                f"- {market} {action.get('id')}: {action.get('reason')}{suffix}"
            )
    manifests = []
    for item in report.get("markets", []) or []:
        chart_manifest = item.get("chart_cache_build_manifest") or {}
        if chart_manifest:
            manifests.append((item.get("market", ""), "chart_cache", chart_manifest))
        for key, label in (("bt_data", "bt_data"), ("mtf3_bt_data", "mtf3_bt_data")):
            manifest = ((item.get(key) or {}).get("build_manifest") or {})
            if manifest:
                manifests.append((item.get("market", ""), label, manifest))
    if manifests:
        lines.extend(["", "## Build Manifests", ""])
        for market, label, manifest in manifests:
            levels = manifest.get("levels") or {}
            freqs = manifest.get("frequencies") or []
            level_text = "+".join(str(freq) for freq in freqs) if freqs else (
                f"{levels.get('small_tf') or ''}+{levels.get('mid_tf') or ''}+{levels.get('big_tf') or ''}"
            )
            lines.append(
                "- {market} {source} `{label}` levels={levels} status={status} requested={requested} entries={entries} ok={ok} mid_ok={mid_ok} skip={skip} fail={fail} missing={missing} completed={completed} path=`{path}`".format(
                    market=market,
                    source=label,
                    label=manifest.get("label") or "",
                    status=manifest.get("manifest_status") or "",
                    levels=level_text,
                    requested=int(manifest.get("requested_count") or 0),
                    entries=int(manifest.get("entry_count") or 0),
                    ok=int(manifest.get("ok") or 0),
                    mid_ok=int(manifest.get("ok_has_mid_by_bar") or 0),
                    skip=int(manifest.get("skip") or 0),
                    fail=int(manifest.get("fail") or 0),
                    missing=int(manifest.get("missing_entries") or 0),
                    completed=manifest.get("completed_at") or "",
                    path=manifest.get("path") or "",
                )
            )
    lines.append("")
    return "\n".join(lines)


def _mtf3_chart_cache_status(complete_count: int) -> str:
    if complete_count >= 90:
        return "ready_research_pool"
    if complete_count >= 20:
        return "limited_pool"
    if complete_count > 0:
        return "small_sample_only"
    return "missing"


def _mtf3_cache_recommended_next_actions(item: Mapping[str, object]) -> list[dict]:
    market = str(item.get("market") or "")
    complete_count = int(item.get("chart_cache_complete_mtf3_count") or 0)
    actions: list[dict] = []
    if market == "a":
        bt_data = item.get("bt_data") or {}
        mtf3_bt_data = item.get("mtf3_bt_data") or bt_data
        bt_status = str(mtf3_bt_data.get("status") or "")
        mtf3_ready = int(mtf3_bt_data.get("sample_mtf3_ready_count") or 0)
        manifest = mtf3_bt_data.get("build_manifest") or {}
        if manifest and (
            int(manifest.get("fail") or 0) > 0
            or int(manifest.get("missing_entries") or 0) > 0
            or manifest.get("manifest_status") == "incomplete"
        ):
            actions.append(
                {
                    "id": "retry_or_continue_a_mtf3_cache",
                    "reason": (
                        "A-share MTF3 build manifest has failures, missing entries, "
                        "or is incomplete; rerun the build command to retry unfinished symbols"
                    ),
                    "command": _manifest_retry_command(manifest),
                }
            )
        if max(complete_count, mtf3_ready) < 90 and bt_status in {
            "5m30m_only", "mixed_mtf3", "mtf3_ready", "missing",
        }:
            actions.append(
                {
                    "id": "build_a_mtf3_research_cache",
                    "reason": (
                        "A-share 1m+5m+30m evidence is below research-pool size; "
                        "build a first SH/SZ MTF3 sample before promoting 1m live rules"
                    ),
                    "command": (
                        "PYTHONPATH=src python -m chanlun.recursive_bt.fetch "
                        "mtf3_all_a 300 shsz"
                    ),
                }
            )
            actions.append(
                {
                    "id": "backtest_a_mtf3_reentry_candidate",
                    "reason": (
                        "After the MTF3 cache exists, rerun the 1m execution "
                        "5m-three-buy reentry candidate on the wider A-share sample"
                    ),
                    "command": (
                        "PYTHONPATH=src python -m chanlun.recursive_bt.live_backtest "
                        "--market a --source bt_data --bt-data "
                        "D:/chanlun_pro/bt_data_mtf3_all_a --bt-pool-mode "
                        "walk_forward --selection-scan-limit 300 --selection-sample-mode "
                        "stratified --selection-board-filter shsz --op-level 1m "
                        "--mid-level 5m --big-level 30m --mid-gate soft "
                        "--max-pos 30 --require tech,fund,value "
                        "--after-3sell-reentry-mid-buy-classes 3"
                    ),
                }
            )
    elif market == "us":
        manifest = item.get("chart_cache_build_manifest") or {}
        if manifest and (
            int(manifest.get("fail") or 0) > 0
            or int(manifest.get("missing_entries") or 0) > 0
            or manifest.get("manifest_status") == "incomplete"
        ):
            actions.append(
                {
                    "id": "retry_us_core_mtf3_cache",
                    "reason": (
                        "US core-9 MTF3 chart-cache build manifest has failures, "
                        "missing entries, or is incomplete; rerun the bounded build"
                    ),
                    "command": _default_us_mtf3_cache_command(),
                }
            )
        if complete_count < 9:
            actions.append(
                {
                    "id": "expand_us_core_mtf3_cache",
                    "reason": (
                        "US MTF3 chart-cache evidence is below the core-9 universe; "
                        "refresh at least SPY, QQQ, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA"
                    ),
                    "command": _default_us_mtf3_cache_command(),
                }
            )
    return actions


def _default_us_mtf3_cache_command() -> str:
    end_dt = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - _dt.timedelta(days=60)
    start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "PYTHONPATH=src python -m chanlun.recursive_bt.us_mtf3_cache "
        f'--start-date "{start}" --end-date "{end}"'
    )


def _load_us_chart_cache_build_manifest(chart_cache_dir: Path) -> dict:
    path = chart_cache_dir / "_us_mtf3_build_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "path": str(path),
            "read_error": type(exc).__name__,
        }
    counts = data.get("counts") or {}
    entries = [item for item in data.get("entries", []) or [] if isinstance(item, Mapping)]
    codes = [str(code) for code in data.get("codes", []) or [] if str(code)]
    freqs = [str(freq) for freq in data.get("frequencies", []) or [] if str(freq)]
    requested_count = len(codes) * len(freqs)
    entry_count = len(entries)
    missing_entries = max(requested_count - entry_count, 0)
    completed_at = str(data.get("completed_at") or "")
    if not completed_at:
        manifest_status = "incomplete"
    elif int(counts.get("fail") or 0) > 0 or missing_entries > 0:
        manifest_status = "completed_with_gaps"
    else:
        manifest_status = "completed"
    ok_by_code: dict[str, set[str]] = {}
    failed_codes = []
    for item in entries:
        code = str(item.get("code") or "")
        freq = str(item.get("frequency") or "")
        if item.get("status") == "ok" and code and freq:
            ok_by_code.setdefault(code, set()).add(freq)
        elif item.get("status") == "fail" and code:
            failed_codes.append(code)
    ok_complete_code_count = sum(1 for seen in ok_by_code.values() if set(freqs) <= seen)
    return {
        "path": str(path),
        "label": str(data.get("label") or ""),
        "started_at": str(data.get("started_at") or ""),
        "completed_at": completed_at,
        "manifest_status": manifest_status,
        "requested_count": requested_count,
        "entry_count": entry_count,
        "missing_entries": missing_entries,
        "ok": int(counts.get("ok") or 0),
        "skip": int(counts.get("skip") or 0),
        "fail": int(counts.get("fail") or 0),
        "frequencies": freqs,
        "start_date": str(data.get("start_date") or ""),
        "end_date": str(data.get("end_date") or ""),
        "cache_version": str(data.get("cache_version") or ""),
        "cache_tag": str(data.get("cache_tag") or ""),
        "ok_complete_code_count": ok_complete_code_count,
        "failed_codes_sample": failed_codes[:20],
    }


def _manifest_retry_command(manifest: Mapping[str, object]) -> str:
    label = str(manifest.get("label") or "")
    metadata = manifest.get("metadata") or {}
    if label == "mtf3_all_a":
        limit = metadata.get("limit")
        board_filter = str(metadata.get("board_filter") or "shsz")
        sample_mode = str(metadata.get("sample_mode") or "stratified")
        parts = ["PYTHONPATH=src python -m chanlun.recursive_bt.fetch", "mtf3_all_a"]
        if limit:
            parts.append(str(limit))
        if board_filter:
            parts.append(board_filter)
        if sample_mode and sample_mode != "stratified":
            parts.append(sample_mode)
        return " ".join(parts)
    if label == "mtf3":
        sector = str(metadata.get("sector") or "上证50")
        return f"PYTHONPATH=src python -m chanlun.recursive_bt.fetch mtf3 {sector}"
    return "PYTHONPATH=src python -m chanlun.recursive_bt.fetch mtf3_all_a 300 shsz"


def _audit_a_bt_data_mtf3_cache(
    bt_data_dir: Path,
    *,
    bt_sample_size: int,
    ashare_board,
) -> dict:
    files = sorted(bt_data_dir.glob("*.pkl")) if bt_data_dir.exists() else []
    board_counts: dict[str, int] = {}
    for path in files:
        board = ashare_board(path.stem)
        board_counts[board] = board_counts.get(board, 0) + 1
    sample_files = _sample_bt_data_paths_by_board(
        files,
        bt_sample_size,
        ashare_board=ashare_board,
    )
    field_counts = {
        "small_by_bar": 0,
        "big_dir_at": 0,
        "mid_dir_at": 0,
        "mid_by_bar": 0,
    }
    sample_5m30m_ready_count = 0
    sample_mtf3_ready_count = 0
    unreadable: list[str] = []
    missing_mid_by_bar: list[str] = []
    for path in sample_files:
        try:
            with path.open("rb") as fp:
                data = pickle.load(fp)
        except Exception:
            unreadable.append(path.stem)
            continue
        if not isinstance(data, Mapping):
            unreadable.append(path.stem)
            continue
        for key in field_counts:
            if key in data:
                field_counts[key] += 1
        if "small_by_bar" in data and "big_dir_at" in data:
            sample_5m30m_ready_count += 1
        if "mid_dir_at" in data and "mid_by_bar" in data:
            sample_mtf3_ready_count += 1
        elif len(missing_mid_by_bar) < 20:
            missing_mid_by_bar.append(path.stem)
    status = _bt_data_mtf3_status(
        sample_size=len(sample_files),
        sample_5m30m_ready_count=sample_5m30m_ready_count,
        sample_mtf3_ready_count=sample_mtf3_ready_count,
    )
    return {
        "dir": str(bt_data_dir),
        "file_count": len(files),
        "board_counts": board_counts,
        "build_manifest": _load_bt_data_build_manifest(bt_data_dir),
        "requested_sample_size": int(bt_sample_size),
        "sample_size": len(sample_files),
        "sample_codes": [path.stem for path in sample_files[:200]],
        "sample_codes_truncated": len(sample_files) > 200,
        "field_counts": field_counts,
        "sample_5m30m_ready_count": sample_5m30m_ready_count,
        "sample_mtf3_ready_count": sample_mtf3_ready_count,
        "missing_mid_by_bar_sample": missing_mid_by_bar,
        "unreadable_sample": unreadable[:20],
        "status": status,
    }


def _load_bt_data_build_manifest(bt_data_dir: Path) -> dict:
    path = bt_data_dir / "_build_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "path": str(path),
            "read_error": type(exc).__name__,
        }
    counts = data.get("counts") or {}
    levels = data.get("levels") or {}
    entries = [item for item in data.get("entries", []) or [] if isinstance(item, Mapping)]
    failed_codes = [str(item.get("code") or "") for item in entries if item.get("status") == "fail"]
    skipped_codes = [str(item.get("code") or "") for item in entries if item.get("status") == "skip"]
    ok_mid_count = sum(
        1 for item in entries
        if item.get("status") == "ok" and bool(item.get("has_mid_by_bar"))
    )
    requested_count = int(data.get("requested_count") or 0)
    entry_count = len(entries)
    missing_entries = max(requested_count - entry_count, 0)
    completed_at = str(data.get("completed_at") or "")
    if not completed_at:
        manifest_status = "incomplete"
    elif int(counts.get("fail") or 0) > 0 or missing_entries > 0:
        manifest_status = "completed_with_gaps"
    else:
        manifest_status = "completed"
    return {
        "path": str(path),
        "label": str(data.get("label") or ""),
        "started_at": str(data.get("started_at") or ""),
        "completed_at": completed_at,
        "manifest_status": manifest_status,
        "requested_count": requested_count,
        "entry_count": entry_count,
        "missing_entries": missing_entries,
        "ok": int(counts.get("ok") or 0),
        "skip": int(counts.get("skip") or 0),
        "fail": int(counts.get("fail") or 0),
        "ok_has_mid_by_bar": ok_mid_count,
        "failed_codes_sample": [code for code in failed_codes if code][:20],
        "skipped_codes_sample": [code for code in skipped_codes if code][:20],
        "levels": {
            "small_tf": levels.get("small_tf"),
            "mid_tf": levels.get("mid_tf"),
            "big_tf": levels.get("big_tf"),
        },
        "metadata": dict(data.get("metadata") or {}),
    }


def _sample_bt_data_paths_by_board(
    files: list[Path],
    bt_sample_size: int,
    *,
    ashare_board,
) -> list[Path]:
    if bt_sample_size <= 0 or bt_sample_size >= len(files):
        return files
    groups: dict[str, list[Path]] = {}
    for path in files:
        board = ashare_board(path.stem)
        groups.setdefault(board, []).append(path)
    for group in groups.values():
        group.sort()
    out: list[Path] = []
    boards = sorted(groups)
    idx = 0
    while len(out) < bt_sample_size and boards:
        board = boards[idx % len(boards)]
        group = groups[board]
        if group:
            out.append(group.pop(0))
        boards = [key for key in boards if groups[key]]
        idx += 1
    return out


def _bt_data_mtf3_status(
    *,
    sample_size: int,
    sample_5m30m_ready_count: int,
    sample_mtf3_ready_count: int,
) -> str:
    if sample_size <= 0:
        return "missing"
    if sample_mtf3_ready_count == sample_size:
        return "mtf3_ready"
    if sample_mtf3_ready_count > 0:
        return "mixed_mtf3"
    if sample_5m30m_ready_count > 0:
        return "5m30m_only"
    return "unusable"
