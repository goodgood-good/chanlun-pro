"""Launch one explicit screening job outside the interactive chart process."""

from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from chanlun import config
from chanlun.screening.rules import CN, POINT_TYPES, REASON_LABELS, is_forming_observation, point_semantic_reasons, trading_context
from chanlun.screening.runner import source_revision, write_json
from chanlun.screening.evidence import (
    evidence_health, evidence_version, load_evidence, evidence_semantic_catalog,
    evidence_confirmation_catalog, evidence_nested_snapshots, evidence_exit_catalog,
)
from chanlun.cl_utils.point_exits import with_point_exit_plans
from chanlun.screening.confirmation import confirmation_reasons, is_confirmed
from chanlun.screening.nesting import STRATEGY, saved_confirmation_valid
from .screening_policy import POLICY_LABELS, POLICY_VERSION, selection_policy_reasons


def validate_settings(body, *, max_codes=100):
    if not isinstance(body, dict):
        raise ValueError("请求必须为 JSON 对象")
    allowed = {"scope", "codes", "frequencies", "point_types", "recent_sessions",
               "max_anchor_sessions", "exclude_st", "workers", "max_anchor_gain_pct",
               "include_weak_second", "strategy", "confirmation_frequency", "symbols"}
    if set(body) - allowed:
        raise ValueError("请求包含未知选股条件")
    if body.get("strategy", STRATEGY) != STRATEGY or body.get("confirmation_frequency", "1m") != "1m":
        raise ValueError("当前选股采用 5m 买卖点与 1m 区间套确认")
    scope = body.get("scope")
    if scope not in {"all_a", "codes", "all_a_watchlist", "watchlist", "symbols"}:
        raise ValueError("请指定行情源 A 股股票池或明确的股票代码")
    result = {"scope": scope}
    for field, options, default in (("frequencies", ("5m",), ["5m"]),
                                     ("point_types", POINT_TYPES, list(POINT_TYPES))):
        value = body.get(field, default)
        if (not isinstance(value, list) or not value or len(value) > len(options)
                or any(type(x) is not str or x not in options for x in value)
                or len(set(value)) != len(value)):
            raise ValueError(f"{field} 不合法")
        result[field] = value
    result["strategy"] = STRATEGY
    result["confirmation_frequency"] = "1m"
    for field, default, limit in (("recent_sessions", 5, 20), ("max_anchor_sessions", 20, 60),
                                  ("workers", 12, 12)):
        value = body.get(field, default)
        if type(value) is not int or not 1 <= value <= limit:
            raise ValueError(f"{field} 必须是 1–{limit} 的整数")
        result[field] = value
    result["exclude_st"] = body.get("exclude_st", True)
    for field, default in (("include_weak_second", False),):
        result[field] = body.get(field, default)
        if type(result[field]) is not bool:
            raise ValueError(f"{field} 必须为布尔值")
    gain = body.get("max_anchor_gain_pct", 10)
    if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 100:
        raise ValueError("距买卖点的顺向涨跌幅上限必须是 0–100 的百分数")
    result["max_anchor_gain_pct"] = gain
    if type(result["exclude_st"]) is not bool:
        raise ValueError("exclude_st 必须为布尔值")
    codes = body.get("codes", [])
    if (not isinstance(codes, list) or len(codes) > max_codes
            or any(type(c) is not str or not re.fullmatch(r"(?:SH|SZ|BJ)\.\d{6}", c) for c in codes)
            or (scope == "codes" and not codes) or (scope != "codes" and codes)):
        raise ValueError(f"指定范围需填写 1–{max_codes} 个 SH./SZ./BJ. 股票代码，全市场无需代码")
    result["codes"] = list(dict.fromkeys(codes))
    symbols = body.get("symbols", [])
    if scope == "symbols":
        from chanlun.screening.markets import validate_symbol
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= max_codes
                or any(not isinstance(s, dict) or set(s) - {"market", "code", "name", "origin"}
                       or not validate_symbol(s.get("market"), s.get("code"))
                       or not isinstance(s.get("name", ""), str) or len(s.get("name", "")) > 200 for s in symbols)):
            raise ValueError("跨市场标的列表不合法")
        result["symbols"] = list({(s["market"], s["code"]): s for s in symbols}.values())
    elif symbols:
        raise ValueError("仅指定跨市场范围可携带标的列表")
    return result


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_state_json(path):
    """Read committed state despite a brief Windows atomic-replace lock."""
    for attempt in range(15):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 14:
                raise
            time.sleep(min(0.05 * 2 ** attempt, 0.5))


class ScreeningManager:
    def __init__(self, root=None, *, now=None, max_codes=100):
        self._root = root
        self._max_codes = max_codes
        self._lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._results_key = None
        self._results_future = None
        self._process = None
        self._revision = None
        self._revision_checked = None
        self._now = now or (lambda: datetime.now(CN))

    @property
    def root(self):
        return Path(self._root) if self._root else config.get_data_path() / "screening"

    def _directory(self):
        latest = self.root / "latest.json"
        if not latest.exists():
            return None
        run_id = read_state_json(latest)["run_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("选股任务标识无效")
        return self.root / run_id

    def status(self):
        return self._status(self._directory())

    def _status(self, directory):
        """Read one pinned run; a concurrent start may change latest.json."""
        if directory is None:
            return {"status": "idle", "selected": [], "reason_labels": {**REASON_LABELS, **POLICY_LABELS},
                    "selection_policy_version": POLICY_VERSION}
        state = read_state_json(directory / "status.json")
        if state.get("run_id") != directory.name:
            raise ValueError("选股任务状态与目录不一致")
        if (state["status"] in {"starting", "running"}
                and time.time() - state.get("updated_at", state.get("started_at", 0)) > 30
                and not _pid_alive(state.get("worker_pid"))):
            state = {**state, "status": "interrupted", "error": "选股工作进程已退出，可重新运行"}
        state["reason_labels"] = {**REASON_LABELS, **POLICY_LABELS}
        state["selection_policy_version"] = POLICY_VERSION
        state["cancel_requested"] = (directory / "cancel").exists()
        state["evidence_version"] = evidence_version(directory)
        checked = time.monotonic()
        if self._revision_checked is None or checked - self._revision_checked >= 5:
            self._revision = source_revision()
            self._revision_checked = checked
        state["source_current"] = bool(state.get("source_revision")) and state["source_revision"] == self._revision
        state.update(self._freshness(state))
        return state

    def _freshness(self, state):
        now = self._now()
        result = {"freshness_checked_at": int(now.timestamp()), "data_current": None, "cutoff_current": None,
                  "input_revision_state": "not_rechecked",
                  "freshness_state": "pending", "expected_cutoffs": {}}
        cutoffs = state.get("cutoffs")
        if not cutoffs:
            return result
        try:
            expected = {f: trading_context(now, f, 1, 1)["cutoff"] for f in cutoffs}
            if state.get("settings", {}).get("strategy") == STRATEGY and "5m" in expected:
                expected["1m"] = trading_context(datetime.fromtimestamp(expected["5m"], CN), "1m", 1, 1)["cutoff"]
        except (ValueError, OSError) as exc:
            return {**result, "freshness_state": "unknown",
                    "freshness_message": f"无法核对结果时效：{exc}"}
        current = all(cutoffs[f] == t for f, t in expected.items())
        return {**result, "cutoff_current": current, "data_current": None if current else False,
                "expected_cutoffs": expected, "freshness_state": "cutoff_current" if current else "stale",
                "freshness_message": ("收盘截止时间一致；尚未重新核对全体历史行情修订。复核图使用本次保存的行情。"
                                      if current else "已有新的收盘 K 线，本结果仅代表原截止时刻，请重新运行选股")}

    def results(self, *, background=False):
        directory = self._directory()
        state = self._status(directory)
        # Reuse only the same committed rows and evidence-file versions. The
        # status/freshness fields are still read on every request.
        key = (str(directory), state.get("completed"), state.get("evidence_version"), POLICY_VERSION,
               json.dumps({k: state.get(k) for k in ("settings", "cutoffs", "source_revision")}, sort_keys=True))
        pending = {**state, "results_loading": True, "selected": [], "observations": [],
                   "recent_rejections": [], "errors": [], "rejection_counts": {}}
        while True:
            launch = False
            with self._results_lock:
                future = self._results_future
                matching = future is not None and self._results_key == key
                if not matching and (future is None or future.done()):
                    future = Future()
                    self._results_future, self._results_key = future, key
                    matching = launch = True
            if not matching:
                # A changed run/version waits for the one active reader, rather
                # than spawning another full-market evidence read on every poll.
                if background:
                    return pending
                try:
                    future.result()
                except Exception:
                    pass
                continue
            if launch:
                if background:
                    threading.Thread(target=self._prepare_results, args=(future, directory, state),
                                     name="screening-results", daemon=True).start()
                else:
                    self._prepare_results(future, directory, state)
            if background and not future.done():
                return pending
            try:
                payload = future.result()
            except Exception:
                with self._results_lock:
                    if self._results_future is future:
                        self._results_future = None
                        self._results_key = None
                raise
            return {**deepcopy(payload), **state, "results_loading": False}

    def _prepare_results(self, future, directory, state):
        try:
            future.set_result(self._read_results(directory, state))
        except BaseException as exc:
            future.set_exception(exc)

    def committed_results(self, after_completed=0, *, limit=16):
        """Read only the next committed symbols for the live monitor.

        Worker progress is published after its JSONL rows and evidence are
        saved. Never infer a missing signal from symbols still being computed.
        """
        directory = self._directory()
        state = self._status(directory)
        completed = state.get("completed", 0)
        pinned = {**state, "completed": min(completed, after_completed + limit)}
        result = self._read_results(directory, pinned, after_completed=after_completed)
        return {**result, "completed": completed}

    def _read_results(self, directory, state, *, after_completed=0):
        selected, observations, recent_rejections, errors, counts = [], [], [], [], {}
        processed_symbols, results_through = [], after_completed
        semantic_exclusions = 0
        lifetime_exclusions = 0
        policy_exclusions = 0
        if directory is not None and (directory / "results.jsonl").exists():
            # Append-only JSONL can end in an in-flight partial line.
            committed = state.get("completed")
            for index, line in enumerate((directory / "results.jsonl").read_bytes().splitlines(keepends=True)):
                # The worker publishes its progress only after appending rows.
                # Rows appended after this status snapshot belong to the next poll.
                if committed is not None and index >= committed:
                    break
                if index < after_completed:
                    continue
                # A concurrent append may end halfway through a Chinese UTF-8
                # character. Decode only newline-terminated, committed records.
                if not line.endswith(b"\n"):
                    continue
                item = json.loads(line)
                processed_symbols.append({"market": item.get("market", "a"), "code": item["code"]})
                results_through = index + 1
                if item.get("error"):
                    errors.append({"code": item["code"], "market": item.get("market", "a"), "error": item["error"]})
                for row in item["rows"]:
                    header = {k: row[k] for k in ("code", "market", "name", "frequency", "source_closed_at", "input_fingerprint") if k in row}
                    nested = state.get("settings", {}).get("strategy") == STRATEGY
                    if nested:
                        header.update(strategy=STRATEGY, confirmation_evidence=row.get("confirmation_evidence", {}))
                    if not item.get("error") and not row.get("data_errors") and not row.get("error"):
                        legacy_observations = "observations" not in row
                        waiting = [s for s in row.get("observations", row["recent_rejections"])
                                   if is_forming_observation(s)]
                        eligible, eligible_waiting = [], []
                        candidates = [*row["selected"], *waiting]
                        point_catalog, center_catalog = {}, {}
                        confirmations = {}
                        exit_plans, lower_exit_plans = {}, {}
                        paired = None
                        if nested and candidates:
                            try:
                                paired = evidence_nested_snapshots(directory, row["code"], row.get("evidence", {}),
                                                                   row.get("confirmation_evidence", {}))
                            except (OSError, ValueError, KeyError, TypeError):
                                pass
                        if any(is_confirmed(s["point"]) for s in candidates):
                            try:
                                confirmations = evidence_confirmation_catalog(
                                    directory, row["code"], row["frequency"], row.get("evidence", {}))
                            except (OSError, ValueError, KeyError, TypeError):
                                pass
                        if any(s["point"].get("point_type") in {"1buy", "2buy", "1sell", "2sell"} for s in candidates):
                            try:
                                point_catalog, center_catalog = evidence_semantic_catalog(
                                    directory, row["code"], row["frequency"], row.get("evidence", {}))
                            except (OSError, ValueError, KeyError, TypeError):
                                # Old waiting records may have no frozen proof. Missing
                                # parents cannot be treated as confirmed buying evidence.
                                pass
                        if candidates:
                            try:
                                exit_plans = evidence_exit_catalog(directory, row["code"], row["frequency"], row.get("evidence", {}))
                                if paired is not None:
                                    lower_exit_plans = evidence_exit_catalog(directory, row["code"], "1m", row.get("confirmation_evidence", {}))
                            except (OSError, ValueError, KeyError, TypeError):
                                pass
                        for s in candidates:
                            waiting_candidate = is_forming_observation(s)
                            reasons = point_semantic_reasons(s["point"], point_catalog, center_catalog)
                            if nested and (row["frequency"] != "5m" or paired is None
                                           or not saved_confirmation_valid(s, *paired)):
                                reasons.append("NESTED_EVIDENCE_MISSING")
                            lifetime_reasons = confirmation_reasons(s["point"], confirmations)
                            policy_reasons = selection_policy_reasons(s["point"])
                            semantic_exclusions += bool(reasons)
                            lifetime_exclusions += bool(lifetime_reasons)
                            reasons.extend(lifetime_reasons)
                            reasons.extend(policy_reasons)
                            policy_exclusions += bool(policy_reasons)
                            s = {**s, "exit_plan": exit_plans.get(s["point"].get("point_id")),
                                 "confirmation_exit_plan": lower_exit_plans.get(
                                     ((s.get("nested_confirmation") or {}).get("point") or {}).get("point_id"))}
                            if is_confirmed(s["point"]):
                                s = {**s, "confirmation_segment": confirmations.get(s["point"]["point_id"], {"state": "unavailable"})}
                            if reasons:
                                recent_rejections.append({**header, **s, "reasons": reasons})
                                for reason in reasons:
                                    counts[reason] = counts.get(reason, 0) + 1
                            elif waiting_candidate:
                                eligible_waiting.append(s)
                            else:
                                eligible.append(s)
                        waiting = eligible_waiting
                        complete = True
                        if eligible or waiting:
                            health = evidence_health(directory, row["code"], row["frequency"], row.get("evidence", {}))
                            complete = health["complete"]
                            header["evidence"] = row.get("evidence", {})
                            header["evidence_verified"] = health["verified"]
                            header["evidence_available"] = complete
                            header["evidence_chart_available"] = complete and row.get("evidence", {}).get("chart_saved") is True
                            if not complete and (eligible or not legacy_observations):
                                errors.append({**header, "reasons": ["EVIDENCE_MISSING"]})
                        if complete:
                            selected.extend({**header, **s} for s in eligible)
                        if complete or legacy_observations:
                            # Older runs kept these points only in rejection
                            # records. Expose them as observations without
                            # claiming the newer dependency checks were run.
                            observations.extend({**header, **s, "observation_validation":
                                                 "legacy_not_rechecked" if legacy_observations else "checked"}
                                                for s in waiting)
                    recent_rejections.extend({**header, **s} for s in row["recent_rejections"])
                    if row.get("data_errors"):
                        errors.append({**header, "reasons": row["data_errors"], "error": row.get("error"),
                                       "data_quality": row.get("data_quality")})
                    for reason, count in row["reason_counts"].items():
                        counts[reason] = counts.get(reason, 0) + count
        selected.sort(key=lambda s: (-s.get("selection_available_at", s["point"]["available_at"]), s["code"], s["frequency"]))
        observations.sort(key=lambda s: (-s.get("selection_available_at", s["point"]["available_at"]), s["code"], s["frequency"]))
        return {**state, "selected": selected, "observations": observations, "recent_rejections": recent_rejections,
                "processed_symbols": processed_symbols, "results_through": results_through,
                "errors": errors, "rejection_counts": counts,
                "semantic_exclusion_count": semantic_exclusions,
                "lifetime_exclusion_count": lifetime_exclusions,
                "selection_policy_exclusion_count": policy_exclusions,
                "observation_count": len(observations),
                "selected_symbols": len({s["code"] for s in selected})}

    def evidence(self, source, code, frequency, point_id, *, market="a"):
        result = self.results()
        if result.get("run_id") != source:
            raise ValueError("该结果已被新一轮选股替换，请刷新选股页面")
        candidate = next((row for row in [*result["selected"], *result["observations"]]
                          if row["code"] == code and row.get("market", "a") == market and (row["frequency"] == frequency
                              or frequency == "1m" and row.get("nested_confirmation", {}).get("strategy") == STRATEGY)
                          and row["point"]["point_id"] == point_id and row.get("evidence_available")), None)
        if candidate is None:
            raise ValueError("本次结果中没有该候选，或它的证据已不可用")
        lower = frequency != candidate["frequency"]
        manifest = candidate.get("confirmation_evidence" if lower else "evidence", {})
        frame, snapshot, health = load_evidence(self.root / source, code, frequency, manifest)
        expected_point = candidate["nested_confirmation"].get("point") if lower else candidate["point"]
        saved_point = next((p for level in snapshot["levels"] if level["structural_level"] == 0
                            for p in level["points"] if expected_point and p["point_id"] == expected_point["point_id"]), None)
        if saved_point != expected_point or snapshot["source_closed_at"] != candidate["source_closed_at"]:
            raise ValueError("候选记录与保存的结构证据不一致")
        bars = [[int(row.date.timestamp()), float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume)]
                for row in frame.itertuples(index=False)]
        return {"source": source, "candidate": candidate, "focus_point": expected_point,
                "bars": bars, "snapshot": snapshot,
                "metadata": {**health, "source_revision": result.get("source_revision"),
                             "source_current": result["source_current"], "cutoff_current": result["cutoff_current"],
                             "source_closed_at": snapshot["source_closed_at"], "input_revision_state": "not_rechecked"}}

    def evidence_history(self, source, code, frequency, point_id, *, first, start, end, market="a"):
        """Adapt saved chart geometry to the shared TradingView history contract.

        This path never calls a market feed or recomputes historical structure.
        It intentionally cannot fall back to live data on a missing proof.
        """
        payload = self.evidence(source, code, frequency, point_id, market=market)
        snapshot, bars = with_point_exit_plans(payload["snapshot"]), payload["bars"]
        geometry = snapshot.get("chart")
        if not geometry:
            raise ValueError("本轮旧结果未保存完整画线数据，请切换当前行情图表")
        # Intraday QMT timestamps label candle closes. The shared datafeed
        # converts them to chart starts exactly as on the analysis page.
        duration = {"1m": 60, "5m": 300, "30m": 1800}[frequency]
        indices = [i for i, bar in enumerate(bars) if first or start <= bar[0] - duration < end]
        if not indices:
            return {"s": "no_data"}
        chart = {key: [bars[i][column] for i in indices]
                 for column, key in enumerate(("t", "o", "h", "l", "c", "v"))}
        for key, values in geometry.items():
            if key.startswith(("macd_", "higher_macd_")):
                if values and len(values) != len(bars):
                    raise ValueError("保存的指标与行情长度不一致")
                chart[key] = [values[i] for i in indices] if values else []
            else:
                chart[key] = values
        authoritative = first or indices[-1] == len(bars) - 1
        return {"s": "ok", **chart, "update": not first, "full_snapshot": authoritative,
                "history_floor": bars[0][0], "history_complete": True,
                "strict_structure_mode": "replace" if authoritative else "unchanged",
                **({"strict_structure": {k: v for k, v in snapshot.items() if k != "chart"}}
                   if authoritative else {})}

    def start(self, body, *, external_notifications=True, force_rebuild=False):
        if type(external_notifications) is not bool:
            raise ValueError("外部通知选项必须是布尔值")
        if type(force_rebuild) is not bool:
            raise ValueError("重新计算选项必须是布尔值")
        settings = validate_settings(body, max_codes=self._max_codes)
        with self._lock:
            if self.status()["status"] in {"starting", "running"}:
                raise RuntimeError("已有选股任务在执行，请等待完成或取消")
            run_id = uuid.uuid4().hex
            directory = self.root / run_id
            request = {"run_id": run_id, "observed_at": self._now().isoformat(),
                       "settings": settings}
            if not external_notifications:
                # Run-local delivery policy, independent of stock selection
                # settings and the user's normal notification preference.
                request["external_notifications"] = False
            if force_rebuild:
                request["force_rebuild"] = True
            write_json(directory / "request.json", request)
            write_json(directory / "status.json", {**request, "status": "starting",
                       "started_at": time.time(), "completed": 0, "total": 0})
            write_json(self.root / "latest.json", {"run_id": run_id})
            project = Path(__file__).resolve().parents[4]
            env = {**os.environ, "PYTHONPATH": str(project / "src"), "PYTHONIOENCODING": "utf-8",
                   "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            try:
                with (directory / "worker.log").open("ab") as log:
                    self._process = subprocess.Popen(
                        [sys.executable, "-m", "chanlun.screening.runner", "--run-dir", str(directory)],
                        cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                        if os.name == "nt" else 0,
                    )
            except Exception as exc:
                write_json(directory / "status.json", {**request, "status": "failed", "error": str(exc)})
                raise
        return self._status(directory)

    def cancel(self):
        with self._lock:
            directory = self._directory()
            if directory and self._status(directory)["status"] in {"starting", "running"}:
                (directory / "cancel").touch()
        return self._status(directory)


manager = ScreeningManager()
