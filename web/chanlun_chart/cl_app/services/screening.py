"""Launch one explicit screening job outside the interactive chart process."""

from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import logging
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
from chanlun.persistence.file_lock import interprocess_file_lock
from chanlun.screening.rules import CN, FREQUENCIES, POINT_TYPES, REASON_LABELS, is_forming_observation, point_semantic_reasons, third_buy_entry_precision_reasons
from chanlun.screening.runner import DIRECT_STRATEGY, source_revision, write_json
from chanlun.screening.state_io import read_state_json
from chanlun.screening.cache import REPLAY_CHECKS
from chanlun.screening.evidence import (
    evidence_health, evidence_version, load_evidence, evidence_semantic_catalog,
    evidence_confirmation_catalog, evidence_nested_snapshots, evidence_exit_catalog,
    evidence_operational_macd_risk,
    evidence_u295_lower_structural_witnesses,
    evidence_native_signal_catalog,
    evidence_consolidation_catalog,
)
from chanlun.core.strict_structure.operational_risk import apply_declared_minimum_operating_frequency
from chanlun.core.strict_structure.native_center_signals import NATIVE_SIGNAL_RULE_BASIS
from chanlun.cl_utils.point_exits import with_point_exit_plans
from chanlun.screening.confirmation import confirmation_reasons, is_confirmed
from chanlun.screening.confirmation import native_selection_lifetime_reasons
from chanlun.screening.nesting import STRATEGY, saved_confirmation_valid
from .screening_policy import POLICY_LABELS, POLICY_VERSION, selection_policy_reasons
from .screening_progress import ProgressiveResults, record_lines
from .screening_lifecycle import pid_alive as _pid_alive, reconcile_interrupted
from . import screening_result_storage

_RESULT_CACHE_SCHEMA = screening_result_storage.SCHEMA
_RESULT_CACHE_FILE = screening_result_storage.FILENAME


def _report_start_followup_failure(run_id, phase, error):
    try:
        logging.getLogger(__name__).warning("Screening run %s was launched; %s unavailable (%s)",
                                           run_id, phase, type(error).__name__)
    except Exception:
        # Diagnostics cannot undo an already accepted worker launch.
        pass


class MonitorResultsReadError(ValueError):
    def __init__(self, run_id, detail):
        self.run_id=run_id
        super().__init__('监听结果暂不可读取（'+detail+'）')


def all_periods_failed(result):
    """One failed interval does not invalidate other verified intervals."""
    if not result.get('total') or result.get('results_loading'):
        return False
    if any(result.get(key) for key in ('selected','observations','consolidation_alerts')):
        return False
    if 'verified_periods' in result:
        return not result['verified_periods']
    if type(result.get('successful_period_count')) is int:
        return result['successful_period_count'] == 0
    if 'selected' not in result and result.get('selected_count',0)>0:
        return False
    # Old status files recorded only symbols with any error. A parsed result
    # above can still prove partial success; otherwise keep the prior fallback.
    return result.get('error_count',0)>=result['total']


def _result_reader_revision():
    """Version only code that interprets an already saved screening run.

    A generator change makes the run stale. The result cache also includes
    source_current so a previously verified payload cannot republish that run.
    Disk storage has its own cache key; its maintenance cannot change which
    saved signals are accepted or force the live pool through revalidation.
    """
    project = Path(__file__).resolve().parents[4]
    files = [Path(__file__).resolve(), Path(__file__).with_name("screening_policy.py"),
             Path(__file__).with_name("screening_progress.py"),
             Path(__file__).with_name("historical_native_pool.py")]
    files.extend(project / "src" / "chanlun" / "screening" / name for name in (
        "rules.py", "confirmation.py", "nesting.py", "evidence.py", "cache.py", "markets.py",
        "lower_first_timeline.py",
    ))
    files.extend((project / "src" / "chanlun" / "cl_utils" / "point_exits.py",
                  project / "src" / "chanlun" / "core" / "strict_structure" / "unit_adapter.py",
                  project / "src" / "chanlun" / "core" / "strict_structure" / "operational_risk.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(project).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


_RESULT_READER_REVISION = _result_reader_revision()


def validate_settings(body, *, max_codes=100):
    if not isinstance(body, dict):
        raise ValueError("请求必须为 JSON 对象")
    allowed = {"scope", "codes", "frequencies", "point_types", "recent_sessions", "alert_mode",
               "max_anchor_sessions", "exclude_st", "workers", "max_anchor_gain_pct",
               "include_weak_second", "strategy", "confirmation_frequency",
               "structural_levels", "symbols", "minimum_operating_frequency"}
    if set(body) - allowed:
        raise ValueError("请求包含未知选股条件")
    strategy = body.get("strategy", DIRECT_STRATEGY)
    if body.get("alert_mode") not in (None, "native_center_signals"):
        raise ValueError("unknown alert mode")
    if strategy != DIRECT_STRATEGY:
        raise ValueError("旧的 5m+1m 组合筛选已停用；请选择本周期中枢提醒")
    if strategy == STRATEGY and body.get("confirmation_frequency", "1m") != "1m":
        raise ValueError("当前组合策略采用 5m 买卖点与区间内 1m 结构确认")
    if strategy == DIRECT_STRATEGY and body.get("confirmation_frequency") is not None:
        raise ValueError("直接结构筛选不使用第二周期确认")
    scope = body.get("scope")
    if scope not in {"all_a", "codes", "all_a_watchlist", "watchlist", "symbols"}:
        raise ValueError("请指定行情源 A 股股票池或明确的股票代码")
    result = {"scope": scope}
    for field, options, default in (("frequencies", ("5m",) if strategy == STRATEGY else FREQUENCIES,
                                     ["5m"]),
                                     ("point_types", POINT_TYPES, list(POINT_TYPES))):
        value = body.get(field, default)
        if (not isinstance(value, list) or not value or len(value) > len(options)
                or any(type(x) is not str or x not in options for x in value)
                or len(set(value)) != len(value)):
            raise ValueError(f"{field} 不合法")
        result[field] = value
    result["strategy"] = strategy
    if strategy == DIRECT_STRATEGY:
        result["alert_mode"] = "native_center_signals"
    if strategy == STRATEGY:
        result["confirmation_frequency"] = "1m"
    levels = body.get("structural_levels", [0])
    if (not isinstance(levels, list) or not levels or len(levels) > 50
            or any(type(level) is not int or not 0 <= level < 50 for level in levels)
            or len(set(levels)) != len(levels)
            or levels != [0]):
        raise ValueError("结构级别列表无效或与 5m+1m 组合策略不兼容")
    result["structural_levels"] = levels
    minimum = body.get("minimum_operating_frequency")
    if minimum is not None:
        if (type(minimum) is not str or minimum not in FREQUENCIES
                or strategy == STRATEGY and minimum not in {"1m", "5m"}
                or strategy == DIRECT_STRATEGY and result["frequencies"] != [minimum]):
            raise ValueError("最低操作周期必须是本轮实际计算的单一物理周期")
        result["minimum_operating_frequency"] = minimum
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


class ScreeningManager:
    def __init__(self, root=None, *, now=None, max_codes=100, warm_results=True, reuse_workers=False):
        self._root = root
        self._max_codes = max_codes
        self._warm_results = warm_results
        self._reuse_workers = bool(reuse_workers and os.name == "nt")
        if self._reuse_workers and warm_results:
            raise ValueError("Reusable monitor workers cannot wait for page-result warmup")
        self._lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._results_key = None
        self._results_future = None
        self._progressive_results = None
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
        state = reconcile_interrupted(directory,state,alive=_pid_alive)
        if (state["status"] in {"starting", "running"}
                and time.time() - state.get("updated_at", state.get("started_at", 0)) > 30
                and not state.get("worker_pid")):
            # Legacy startup records have no provable process owner. They can
            # be retried, but remain protected from automatic deletion.
            state = {**state, "status": "interrupted", "error": "选股工作进程已退出，可重新运行"}
        state["reason_labels"] = {**REASON_LABELS, **POLICY_LABELS}
        state["selection_policy_version"] = POLICY_VERSION
        state["result_reader_revision"] = _RESULT_READER_REVISION
        state["cancel_requested"] = (directory / "cancel").exists()
        state["evidence_version"] = evidence_version(directory)
        checked = time.monotonic()
        if self._revision_checked is None or checked - self._revision_checked >= 5:
            self._revision = source_revision()
            self._revision_checked = checked
        state["source_current"] = bool(state.get("source_revision")) and state["source_revision"] == self._revision
        state["current_source_revision"] = self._revision
        # A source change stops live reuse, but a completed native-center run
        # remains reviewable at its frozen cutoff after validating its proof.
        settings = state.get("settings", {})
        state["frozen_native_review"] = bool(
            state["source_current"] is False and state.get("status") == "completed"
            and isinstance(state.get("source_revision"), str)
            and re.fullmatch(r"[0-9a-f]{64}", state["source_revision"])
            and settings.get("strategy") == DIRECT_STRATEGY
            and settings.get("alert_mode") == "native_center_signals"
        )
        state.update(self._freshness(state))
        return state

    def _freshness(self, state):
        now = self._now()
        result = {"freshness_checked_at": int(now.timestamp()), "data_current": None, "cutoff_current": None,
                  "input_revision_state": "not_rechecked",
                   "freshness_state": "pending", "expected_cutoffs": {}}
        market_cutoffs = state.get("market_cutoffs")
        if isinstance(market_cutoffs, dict):
            from chanlun.screening.markets import closed_bar_cutoff
            finished = state.get("status") in {"completed", "cancelled", "interrupted", "failed"}
            if not market_cutoffs:
                return {**result, "freshness_state": "unknown" if finished else "pending",
                        "freshness_message": "本轮未取得可核对的行情截止记录。" if finished else "正在等待各市场的行情截止记录。"}
            expected_markets, checks = {}, {}
            for market, periods in market_cutoffs.items():
                checks[market] = {}
                if not isinstance(periods, dict) or not periods:
                    checks[market]["unknown"] = {"state": "unknown", "message": "市场周期记录缺失或无效"}
                    continue
                code = state.get("market_clock_symbols", {}).get(market)
                for frequency, cutoff in periods.items():
                    check = {"source_closed_at": cutoff}
                    checks[market][frequency] = check
                    if type(cutoff) is not int:
                        check.update(state="pending" if cutoff is None and not finished else "unknown",
                                     message="尚未取得有效行情截止时间")
                        continue
                    try:
                        if market != "a" and not code:
                            raise ValueError("缺少用于核对市场收盘时刻的标的")
                        expected = closed_bar_cutoff(market, code, now, frequency)
                        if type(expected) is not int or cutoff > expected:
                            raise ValueError("保存截止时间晚于当前可核对时刻，或市场收盘时刻无效")
                    except (ValueError, OSError) as exc:
                        check.update(state="unknown", message=str(exc))
                        continue
                    expected_markets.setdefault(market, {})[frequency] = expected
                    check.update(state="cutoff_current" if cutoff == expected else "stale", expected_closed_at=expected)
            states = {check["state"] for periods in checks.values() for check in periods.values()}
            current = False if "stale" in states else True if states == {"cutoff_current"} else None
            freshness = ("stale" if current is False else "cutoff_current" if current is True
                         else "pending" if "pending" in states else "partial" if "cutoff_current" in states else "unknown")
            messages = []
            if "stale" in states:
                messages.append("部分市场已有新的已收盘 K 线，本轮结果仅代表保存时刻。")
            elif "cutoff_current" in states:
                messages.append("已分别核对有有效截止时间的市场，复核图使用本轮保存行情。")
            if "pending" in states:
                messages.append("仍有市场正在等待行情截止记录。")
            if "unknown" in states:
                messages.append("部分市场时效无法核对，请查看对应市场状态及数据诊断。")
            return {**result, "cutoff_current": current, "data_current": False if current is False else None,
                    "expected_market_cutoffs": expected_markets,
                    "market_freshness": checks, "freshness_state": freshness,
                    "freshness_message": "".join(messages)}
        cutoffs = state.get("cutoffs")
        if not cutoffs:
            return result
        try:
            from chanlun.screening.markets import closed_bar_cutoff
            expected = {f: closed_bar_cutoff("a", "", now, f) for f in cutoffs}
            if state.get("settings", {}).get("strategy") == STRATEGY and "5m" in expected:
                expected["1m"] = closed_bar_cutoff("a", "", datetime.fromtimestamp(expected["5m"], CN), "1m")
        except (ValueError, OSError) as exc:
            return {**result, "freshness_state": "unknown",
                    "freshness_message": f"无法核对结果时效：{exc}"}
        current = all(cutoffs[f] == t for f, t in expected.items())
        return {**result, "cutoff_current": current, "data_current": None if current else False,
                "expected_cutoffs": expected, "freshness_state": "cutoff_current" if current else "stale",
                "freshness_message": ("收盘截止时间一致；尚未重新核对全体历史行情修订。复核图使用本次保存的行情。"
                                      if current else "已有新的收盘 K 线，本结果仅代表原截止时刻，请重新运行选股")}

    def results(self, *, background=False, persist=False, project=None, available=False):
        directory = self._directory()
        state = self._status(directory)
        # Reuse only the same committed rows and evidence-file versions. The
        # status/freshness fields are still read on every request.
        key = (str(directory), state.get("completed"), state.get("evidence_version"), POLICY_VERSION,
               state.get("source_current"),
               state.get("frozen_native_review"),
               state.get("current_source_revision"),
               _RESULT_READER_REVISION,
               json.dumps({k: state.get(k) for k in ("settings", "cutoffs", "source_revision")}, sort_keys=True))
        if directory is not None and (
                state.get("status") in {"starting", "running"} and state.get("source_current")
                or self._progressive_results is not None and self._progressive_results.matches(directory, state)
                and self._progressive_results.persisted_key != key):
            with self._results_lock:
                if self._progressive_results is None:
                    self._progressive_results = ProgressiveResults(self._read_results)
                reader = self._progressive_results
                prior_key, prior_future = self._results_key, self._results_future
            payload = reader.results(directory, state, background=background, available=available)
            if (state.get("status") == "completed" and not payload["results_loading"]
                    and reader.persisted_key != key):
                context = reader.key(directory, state)
                with reader.lock:
                    guard, snapshot, persisted = reader.guard, reader.payload, reader.persisted_key
                    # Returned lists still reference their immutable published
                    # snapshot here; caller projection/deepcopy happens below.
                    owns_payload = (reader.context == context and (
                        payload.get("processed_symbols") is snapshot.get("processed_symbols")
                        or payload.get("results_through") == 0 and guard is None and not snapshot))
                self._save_results_cache(directory, state, key, payload)
                with self._results_lock, reader.lock:
                    if (owns_payload and reader.context == context and reader.guard is guard
                            and reader.payload is snapshot and reader.persisted_key == persisted
                            and self._results_key == prior_key and self._results_future is prior_future):
                        reader.persisted_key = key
                        self._results_key, self._results_future = key, Future()
                        self._results_future.set_result(payload)
            view = payload if project is None else project(payload)
            return {**deepcopy(view), **state, **{name: payload[name] for name in (
                "results_through", "results_loading", "results_available", "results_partial")}}
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
                try:
                    cached = self._load_results_cache(directory, state, key)
                    if cached is not None:
                        future.set_result(cached)
                    elif background:
                        threading.Thread(target=self._prepare_results, args=(future, directory, state),
                                         name="screening-results", daemon=True).start()
                    else:
                        self._prepare_results(future, directory, state)
                except Exception as error:
                    # The reservation exists before cache I/O/thread creation.
                    # Release it and wake its waiters if initialization fails.
                    with self._results_lock:
                        if self._results_future is future:
                            self._results_future = None
                            self._results_key = None
                    if not future.done():
                        future.set_exception(error)
                    raise
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
            if persist:
                self._save_results_cache(directory, state, key, payload)
            view = payload if project is None else project(payload)
            return {**deepcopy(view), **state, "results_loading": False}

    @staticmethod
    def _results_cache_key(key):
        return screening_result_storage.cache_key(key)

    def _load_results_cache(self, directory, state, key):
        return screening_result_storage.load(directory, state, self._results_cache_key(key))

    def _save_results_cache(self, directory, state, key, payload):
        screening_result_storage.save(directory, state, self._results_cache_key(key), payload)

    def _prepare_results(self, future, directory, state):
        try:
            payload = self._read_results(directory, state)
            with self._results_lock:
                key = self._results_key if self._results_future is future else None
            if key is not None:
                self._save_results_cache(directory, state, key, payload)
            future.set_result(payload)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)

    def committed_results(self, after_completed=0, *, limit=16):
        """Read only the next committed symbols for the live monitor.

        Worker progress is published after its JSONL rows and evidence are
        saved. Never infer a missing signal from symbols still being computed.
        """
        directory = self._directory()
        state = self._status(directory)
        completed = state.get("completed", 0)
        pinned = {**state, "completed": min(completed, after_completed + limit),
                  "frozen_native_review": False}
        try:
            result = self._read_results(directory, pinned, after_completed=after_completed)
            if state.get('source_current') is not False and result.get('results_through')!=pinned['completed']:
                raise ValueError('committed record count mismatch')
        except (OSError,ValueError,TypeError,KeyError,AttributeError,EOFError) as exc:
            raise MonitorResultsReadError(state.get('run_id'),type(exc).__name__) from exc
        return {**result, "completed": completed}

    def _read_results(self, directory, state, *, after_completed=0, record_offset=0):
        frozen_native_review = state.get("frozen_native_review") is True
        if state.get("source_current") is False and not frozen_native_review:
            # The former recursive-point run must not be republished as live
            # center reminders or reparsed on every workbench refresh.
            return {**state, "selected": [], "observations": [], "entry_waiting": [], "consolidation_alerts": [],
                    "recent_rejections": [], "processed_symbols": [],
                    "results_through": after_completed, "errors": [],
                    "rejection_counts": {}, "observation_count": 0,
                    "selected_symbols": 0}
        selected, observations, entry_waiting, recent_rejections, errors, counts = [], [], [], [], [], {}
        consolidation_alerts, verified_periods = [], []
        historical_recheck_periods = []
        u295_lower_structural_witnesses = []
        processed_symbols, results_through = [], after_completed
        semantic_exclusions = 0
        lifetime_exclusions = 0
        policy_exclusions = 0
        if directory is not None and (directory / "results.jsonl").exists():
            # Append-only JSONL can end in an in-flight partial line.
            committed = state.get("completed")
            for index, line in record_lines(directory, committed, after_completed, record_offset):
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
                    errors.append({"code": item["code"], "market": item.get("market", "a"), "error": item["error"],
                                   **({"reasons": item["reasons"]} if item.get("reasons") else {})})
                for row in item["rows"]:
                    header = {k: row[k] for k in ("code", "market", "name", "frequency", "source_closed_at", "input_fingerprint",
                                                  "latest_close", "structure_price_quantum") if k in row}
                    if (not item.get("error") and not row.get("error") and not row.get("data_errors")
                            and type(row.get("source_closed_at")) is int):
                        verified_periods.append({**header, "consolidation_checks": row.get("consolidation_checks", {}),
                             'timings_ms':row.get('timings_ms',{}),'replays_reused':row.get('replays_reused',0),
                             'processing_timeline':row.get('processing_timeline',{}),
                            'evidence':row.get('evidence'), 'price_basis_revision':row.get('price_basis_revision'),
                            "consolidation_checked": row.get("consolidation_checked") is True
                                and not row.get("consolidation_error") and not row.get("consolidation_rejections")})
                    nested = state.get("settings", {}).get("strategy") == STRATEGY
                    if nested:
                        header.update(strategy=STRATEGY, confirmation_evidence=row.get("confirmation_evidence", {}))
                    if not frozen_native_review and (row.get("consolidation_error") or row.get("consolidation_rejections")):
                        errors.append({**header, "signal_type": "consolidation_divergence",
                                       "reasons": ["CONSOLIDATION_EVIDENCE_MISSING"], "error": row.get("consolidation_error")})
                    if (not frozen_native_review and row.get("consolidation_alerts")
                            and not item.get("error") and not row.get("data_errors") and not row.get("error")):
                        try:
                            manifest = row.get("evidence", {})
                            health = evidence_health(directory, row["code"], row["frequency"], manifest)
                            catalog, source_valid = evidence_consolidation_catalog(
                                directory, row["code"], row["frequency"], manifest,
                                source_closed_at=row.get("source_closed_at"))
                            if not health["complete"] or not health["verified"]:
                                raise ValueError("consolidation evidence unavailable")
                            from chanlun.screening.evidence import evidence_consolidation_lifetimes
                            lifetimes = evidence_consolidation_lifetimes(
                                directory, row["code"], row["frequency"], manifest,
                                source_closed_at=row.get("source_closed_at"))
                            verified = []
                            for candidate in row["consolidation_alerts"]:
                                alert = candidate["alert"]
                                from chanlun.core.strict_structure.native_consolidation_preview import PREVIEW_AUDIT
                                audit_keys = PREVIEW_AUDIT if alert.get("status") == "warning" else REPLAY_CHECKS
                                if (catalog.get(alert["alert_id"]) != alert or not source_valid.get(alert["alert_id"])
                                        or candidate.get("reasons") != []
                                        or not all(candidate.get("audit", {}).get(key) is True for key in audit_keys)):
                                    raise ValueError("consolidation record disagrees with frozen proof")
                                if lifetimes.get(alert["alert_id"]):
                                    continue
                                verified.append({**header, **candidate, "evidence_verified": True, "evidence": manifest})
                            consolidation_alerts.extend(verified)
                        except (OSError, ValueError, KeyError, TypeError):
                            errors.append({**header, "signal_type": "consolidation_divergence",
                                           "reasons": ["CONSOLIDATION_EVIDENCE_MISSING"]})
                    if (
                        state.get("settings", {}).get("strategy") == DIRECT_STRATEGY
                        and row.get("frequency") == "1m"
                        and not item.get("error")
                        and not row.get("data_errors")
                        and not row.get("error")
                        and row.get("u295_lower_structural_witnesses")
                    ):
                        try:
                            verified = evidence_u295_lower_structural_witnesses(
                                directory, row["code"], row.get("evidence", {}),
                                known_from=row["u295_witness_known_from"],
                            )
                            if list(verified) != row["u295_lower_structural_witnesses"]:
                                raise ValueError("U295 frozen witness disagrees with saved record")
                            u295_lower_structural_witnesses.extend(
                                {**header, **witness, "evidence": row["evidence"],
                                 "evidence_verified": True}
                                for witness in verified
                            )
                        except (OSError, ValueError, KeyError, TypeError):
                            errors.append({**header,
                                           "reasons": ["U295_WITNESS_EVIDENCE_MISMATCH"]})
                    if not item.get("error") and not row.get("data_errors") and not row.get("error"):
                        native_signals = row.get("selected", [])
                        if frozen_native_review and native_signals:
                            from .historical_native_pool import historical_native_period
                            try:
                                period = historical_native_period(directory, row, state.get("source_revision"))
                                if period is not None:
                                    historical_recheck_periods.append(period)
                            except (OSError, ValueError, KeyError, TypeError):
                                pass  # No old-rule signal or unverifiable symbol is republished.
                        if frozen_native_review and (not native_signals or not all(
                            candidate.get("point", {}).get("evidence_scope") == "native_center_signal"
                            and candidate.get("point", {}).get("rule_basis") == NATIVE_SIGNAL_RULE_BASIS
                            for candidate in native_signals
                        )):
                            if native_signals:
                                errors.append({**header, "reasons": ["STRUCTURE_MISMATCH"],
                                               "error": "保存结果缺少当前本周期中枢规则证据"})
                            for reason, count in row.get("reason_counts", {}).items():
                                counts[reason] = counts.get(reason, 0) + count
                            # Historical review never enters the retired
                            # recursive/observation result reader below.
                            continue
                        if native_signals and all(
                            candidate.get("point", {}).get("evidence_scope") == "native_center_signal"
                            for candidate in native_signals
                        ):
                            manifest = row.get("evidence", {})
                            try:
                                health = evidence_health(
                                    directory, row["code"], row["frequency"], manifest,
                                )
                                point_catalog, center_catalog, source_valid = evidence_native_signal_catalog(
                                    directory, row["code"], row["frequency"], manifest,
                                    source_closed_at=row.get("source_closed_at"),
                                )
                                if not health["complete"] or not health["verified"]:
                                    raise ValueError("native center signal evidence is incomplete")
                                exit_plans = evidence_exit_catalog(directory, row["code"], row["frequency"], manifest)
                                eligible_native = []
                                following = evidence_confirmation_catalog(directory, row["code"], row["frequency"], manifest)
                                for candidate in native_signals:
                                    point = candidate["point"]
                                    if (point.get("status") != "confirmed"
                                            or point.get("formal_point_issued") is not True
                                            or point.get("rule_basis") != NATIVE_SIGNAL_RULE_BASIS
                                            or candidate.get("reasons") != []
                                            or not all(candidate.get("audit", {}).get(key) is True
                                                       for key in REPLAY_CHECKS)
                                             or point_catalog.get(point.get("point_id")) != point
                                             or source_valid.get(point.get("point_id")) is not True
                                            or center_catalog.get(point.get("center_id")) != candidate.get("center")):
                                        raise ValueError("native center signal proof mismatch")
                                    lifetime = native_selection_lifetime_reasons(
                                        point, point_catalog.values(), following, source_valid)
                                    if lifetime:
                                        lifetime_exclusions += 1
                                        recent_rejections.append({**header, **candidate, "reasons": lifetime})
                                        for reason in lifetime:
                                            counts[reason] = counts.get(reason, 0) + 1
                                        continue
                                    eligible_native.append({
                                        **header, **candidate,
                                        "strategy": "native_center_signals",
                                        "evidence": manifest,
                                        "evidence_verified": True,
                                        "evidence_available": True,
                                        "evidence_chart_available": manifest.get("chart_saved") is True,
                                        "frozen_native_review": frozen_native_review,
                                        "confirmation_segment": following[point["point_id"]],
                                        "exit_plan": exit_plans.get(point["point_id"]),
                                    })
                                selected.extend(eligible_native)
                            except (OSError, ValueError, KeyError, TypeError):
                                errors.append({**header, "reasons": ["EVIDENCE_MISSING"]})
                            recent_rejections.extend({**header, **candidate}
                                                     for candidate in row.get("recent_rejections", ()))
                            for reason, count in row.get("reason_counts", {}).items():
                                counts[reason] = counts.get(reason, 0) + count
                            continue
                        legacy_observations = "observations" not in row
                        waiting = [s for s in row.get("observations", row["recent_rejections"])
                                   if is_forming_observation(s)]
                        eligible, eligible_waiting, eligible_entry_waiting = [], [], []
                        entry_candidates = [s for s in row.get("recent_rejections", ())
                                            if s.get("reasons") == ["THIRD_ENTRY_PRECISION_PENDING"]
                                            and is_confirmed(s.get("point", {}))
                                            and s["point"].get("point_type") == "3buy"
                                            and s["point"].get("structural_level", 0) > 0]
                        candidates = [*row["selected"], *waiting,
                                      *({**s, "entry_waiting": True} for s in entry_candidates)]
                        minimum_period = state.get("settings", {}).get("minimum_operating_frequency")
                        operational_risk = None
                        if minimum_period is not None and candidates:
                            risk_frequency = row.get("operational_risk_evidence_frequency")
                            operational_risk = {
                                "rule_id": "U176", "declared_minimum_frequency": minimum_period,
                                "physical_frequency": risk_frequency,
                                "risk_filter_state": "pending_frozen_evidence",
                                "formal_structure_independent": True, "tradable": False,
                            }
                            if risk_frequency == minimum_period:
                                manifest = (row.get("confirmation_evidence", {})
                                            if nested and risk_frequency == "1m"
                                            else row.get("evidence", {}))
                                try:
                                    fact, risk_cutoff = evidence_operational_macd_risk(
                                        directory, row["code"], risk_frequency, manifest,
                                    )
                                    verified_risk = apply_declared_minimum_operating_frequency(
                                        fact, physical_frequency=risk_frequency,
                                        minimum_frequency=minimum_period,
                                        source_closed_at=risk_cutoff,
                                    )
                                    if verified_risk == row.get("operational_macd_risk"):
                                        operational_risk = verified_risk
                                except (OSError, ValueError, KeyError, TypeError):
                                    pass
                        point_catalog, center_catalog, precision_catalog = {}, {}, {}
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
                        if any(s["point"].get("point_type") in {"1buy", "2buy", "1sell", "2sell"}
                               or s["point"].get("point_type") in {"3buy", "3sell"} and is_confirmed(s["point"])
                               for s in candidates):
                            try:
                                point_catalog, center_catalog, precision_catalog = evidence_semantic_catalog(
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
                            entry_candidate = s.get("entry_waiting") is True
                            reasons = point_semantic_reasons(s["point"], point_catalog, center_catalog)
                            if (is_confirmed(s["point"])
                                    and s["point"].get("point_type") in {
                                        "1buy", "1sell", "2buy", "2sell"
                                    }
                                    and point_catalog.get(s["point"].get("point_id")) != s["point"]):
                                reasons.append("STRUCTURE_MISMATCH")
                            if (s["point"].get("point_type") in {"3buy", "3sell"}
                                    and not is_confirmed(s["point"]) and not waiting_candidate):
                                reasons.append("NOT_CONFIRMED")
                            if (is_confirmed(s["point"]) and s["point"].get("point_type") in {"3buy", "3sell"}):
                                saved_point = point_catalog.get(s["point"].get("point_id"))
                                saved_center = center_catalog.get(s["point"].get("center_id"))
                                if (saved_point != s["point"]
                                        or (s.get("center") is not None
                                            and s["center"] != saved_center)):
                                    reasons.append("STRUCTURE_MISMATCH")
                                s = {**s, "center": saved_center}
                            if (is_confirmed(s["point"]) and s["point"].get("point_type") == "3buy"
                                    and s["point"].get("structural_level", 0) > 0):
                                saved_precision = precision_catalog.get(s["point"].get("point_id"))
                                if s.get("entry_precision_evidence") != saved_precision:
                                    reasons.append("THIRD_ENTRY_PRECISION_EVIDENCE_MISMATCH")
                                reasons.extend(third_buy_entry_precision_reasons(
                                    s["point"], precision_catalog, row.get("source_closed_at")))
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
                                     ((s.get("nested_confirmation") or {}).get("point") or {}).get("point_id")),
                                 "operational_macd_risk": operational_risk}
                            if is_confirmed(s["point"]):
                                s = {**s, "confirmation_segment": confirmations.get(s["point"]["point_id"], {"state": "unavailable"})}
                            if entry_candidate and reasons == ["THIRD_ENTRY_PRECISION_PENDING"]:
                                eligible_entry_waiting.append({**s, "center": center_catalog.get(s["point"].get("center_id")),
                                                               "selection_status": "entry_pending",
                                                               "decision_reasons": reasons})
                            elif reasons:
                                recent_rejections.append({**header, **s, "reasons": reasons})
                                for reason in reasons:
                                    counts[reason] = counts.get(reason, 0) + 1
                            elif waiting_candidate:
                                eligible_waiting.append(s)
                            else:
                                eligible.append(s)
                        waiting = eligible_waiting
                        complete = True
                        if eligible or waiting or eligible_entry_waiting:
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
                            if eligible_entry_waiting and header["evidence_verified"]:
                                entry_waiting.extend({**header, **s} for s in eligible_entry_waiting)
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
        if state.get("completed") is not None and results_through != state["completed"]:
            raise ValueError("已完成进度对应的结果尚未完整写入，请稍后刷新")
        selected.sort(key=lambda s: (-s.get("selection_available_at", s["point"]["available_at"]), s["code"], s["frequency"]))
        observations.sort(key=lambda s: (-s.get("selection_available_at", s["point"]["available_at"]), s["code"], s["frequency"]))
        entry_waiting.sort(key=lambda s: (-s.get("selection_available_at", s["point"]["available_at"]), s["code"], s["frequency"]))
        u295_lower_structural_witnesses.sort(
            key=lambda item: (-item["known_at"], item["code"], item["point_id"])
        )
        failed_symbols = {(error.get("market", "a"), error["code"]) for error in errors
                          if not error.get("frequency") and error.get("signal_type") != "consolidation_divergence"}
        failed_periods = {(error.get("market", "a"), error["code"], error["frequency"]) for error in errors
                         if error.get("frequency") and error.get("signal_type") != "consolidation_divergence"}
        verified_periods = [period for period in verified_periods
                            if (period.get("market", "a"), period["code"]) not in failed_symbols
                            and (period.get("market", "a"), period["code"], period["frequency"]) not in failed_periods]
        return {**state, "selected": selected, "observations": observations, "entry_waiting": entry_waiting,
                "historical_recheck_periods": historical_recheck_periods,
                "consolidation_alerts": consolidation_alerts,
                "verified_periods": verified_periods,
                "u295_lower_structural_witnesses": u295_lower_structural_witnesses,
                "u295_lower_structural_witness_count": len(u295_lower_structural_witnesses),
                "recent_rejections": recent_rejections,
                "processed_symbols": processed_symbols, "results_through": results_through,
                "errors": errors, "rejection_counts": counts,
                "semantic_exclusion_count": semantic_exclusions,
                "lifetime_exclusion_count": lifetime_exclusions,
                "selection_policy_exclusion_count": policy_exclusions,
                "observation_count": len(observations),
                "selected_symbols": len({s["code"] for s in selected})}

    def evidence(self, source, code, frequency, point_id, *, market="a"):
        result = self.results(available=True)
        if result.get("run_id") != source:
            raise ValueError("该结果已被新一轮选股替换，请刷新选股页面")
        candidate = next((row for row in [*result["selected"], *result["observations"],
                                          *result.get("entry_waiting", [])]
                          if row["code"] == code and row.get("market", "a") == market and (row["frequency"] == frequency
                              or frequency == "1m" and row.get("nested_confirmation", {}).get("strategy") == STRATEGY)
                          and row["point"]["point_id"] == point_id and row.get("evidence_available")), None)
        if candidate is None:
            raise ValueError("本次结果中没有该候选，或它的证据已不可用")
        lower = frequency != candidate["frequency"]
        manifest = candidate.get("confirmation_evidence" if lower else "evidence", {})
        frame, snapshot, health = load_evidence(self.root / source, code, frequency, manifest)
        expected_point = candidate["nested_confirmation"].get("point") if lower else candidate["point"]
        saved_point = next((p for level in snapshot["levels"]
                            if expected_point and level.get("structural_level", 0) == expected_point.get("structural_level", 0)
                            for p in level["points"] if expected_point and p["point_id"] == expected_point["point_id"]), None)
        if saved_point != expected_point or snapshot["source_closed_at"] != candidate["source_closed_at"]:
            raise ValueError("候选记录与保存的结构证据不一致")
        if not lower and is_confirmed(expected_point) and expected_point.get("point_type") in {"3buy", "3sell"}:
            saved_center = next((center for level in snapshot["levels"]
                                 for center in level.get("centers", ())
                                 if center["center_id"] == expected_point.get("center_id")), None)
            if saved_center != candidate.get("center"):
                raise ValueError("候选记录与保存的中枢证据不一致")
        if (not lower and is_confirmed(expected_point) and expected_point.get("point_type") == "3buy"
                and expected_point.get("structural_level", 0) > 0):
            saved_precision = next((state for level in snapshot["levels"]
                                    for state in level.get("third_point_entry_precision_rule_states", ())
                                    if state["point_id"] == point_id), None)
            precision_reasons = third_buy_entry_precision_reasons(
                expected_point, {point_id: saved_precision} if saved_precision else {},
                snapshot["source_closed_at"])
            expected_reasons = (["THIRD_ENTRY_PRECISION_PENDING"]
                                if candidate.get("entry_waiting") else [])
            if (saved_precision != candidate.get("entry_precision_evidence")
                    or precision_reasons != expected_reasons):
                raise ValueError("候选记录与保存的三买入场证据不一致")
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
        frozen_structure = {k: v for k, v in snapshot.items() if k != "chart"}
        if payload["candidate"].get("operational_macd_risk") is not None:
            frozen_structure["screening_operational_macd_risk"] = payload["candidate"]["operational_macd_risk"]
        return {"s": "ok", **chart, "update": not first, "full_snapshot": authoritative,
                "history_floor": bars[0][0], "history_complete": True,
                "strict_structure_mode": "replace" if authoritative else "unchanged",
                **({"strict_structure": frozen_structure}
                   if authoritative else {})}

    def start(self, body, *, external_notifications=True, force_rebuild=False, monitor_periods=None, holding_snapshot=None):
        if type(external_notifications) is not bool:
            raise ValueError("外部通知选项必须是布尔值")
        if type(force_rebuild) is not bool:
            raise ValueError("重新计算选项必须是布尔值")
        settings = validate_settings(body, max_codes=self._max_codes)
        if holding_snapshot is not None:
            from chanlun.screening.markets import validate_symbol
            if (settings['scope']!='symbols' or not isinstance(holding_snapshot,list) or len(holding_snapshot)>10000
                    or any(not isinstance(key,str) or ':' not in key or not validate_symbol(*key.split(':',1)) for key in holding_snapshot)):
                raise ValueError('已核验持仓快照不合法')
        if monitor_periods:
            allowed={f"{s['market']}:{s['code']}" for s in settings.get('symbols',[])}
            if (settings['scope']!='symbols' or set(monitor_periods)-allowed
                    or any(not isinstance(v,list) or not v or set(v)-set(FREQUENCIES) for v in monitor_periods.values())):
                raise ValueError('持仓跟踪周期不合法')
            settings['symbols']=[{**s,'_monitor_periods':sorted(set(settings['frequencies'])|set(monitor_periods.get(f"{s['market']}:{s['code']}",[])))}
                                 for s in settings['symbols']]
        if self._reuse_workers and settings['scope'] != 'symbols':
            raise ValueError('Reusable monitoring requires an explicit symbol list')
        dispatch_lock = (interprocess_file_lock(self.root / 'worker_dispatch.lock', timeout_seconds=15)
                         if self._reuse_workers else nullcontext())
        with self._lock, dispatch_lock:
            if self.status()["status"] in {"starting", "running"}:
                raise RuntimeError("已有选股任务在执行，请等待完成或取消")
            run_id = uuid.uuid4().hex
            directory = self.root / run_id
            request = {"run_id": run_id, "observed_at": self._now().isoformat(),
                       "settings": settings}
            if self._reuse_workers:
                from .screening_worker_runtime import runtime_request
                request['worker_runtime'] = runtime_request()
            if holding_snapshot is not None:
                request['holding_snapshot']={'schema':'monitor-holdings-v1','keys':holding_snapshot}
            if not external_notifications:
                # Run-local delivery policy, independent of stock selection
                # settings and the user's normal notification preference.
                request["external_notifications"] = False
            if force_rebuild:
                request["force_rebuild"] = True
            write_json(directory / "request.json", request)
            launch_state = {**request, "status": "starting", "started_at": time.time(), "completed": 0, "total": 0}
            write_json(directory / "status.json", launch_state)
            write_json(self.root / "latest.json", {"run_id": run_id})
            project = Path(__file__).resolve().parents[4]
            env = {**os.environ, "PYTHONPATH": str(project / "src"), "PYTHONIOENCODING": "utf-8",
                   "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            try:
                if self._reuse_workers:
                    from .screening_worker_runtime import dispatch
                    process = dispatch(self.root, directory, request, project=project, env=env,
                                       popen=subprocess.Popen,
                                       creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                    if process is not None:
                        self._process = process
                else:
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
            launched_process = self._process
        if self._warm_results:
            try:
                threading.Thread(target=self._warm_after_run, args=(directory, launched_process),
                                 name="screening-result-warmup", daemon=True).start()
            except Exception as error:
                _report_start_followup_failure(run_id, 'result warmup', error)
        try:
            return self._status(directory)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError) as error:
            # The process/host has accepted this run. Its status file belongs
            # to that worker; preserve ownership without rewriting its progress.
            _report_start_followup_failure(run_id, 'status read', error)
            return {**launch_state, 'status_unavailable': True,
                    'error': '任务已启动，详细状态暂不可读取，请稍后刷新'}


    def _warm_after_run(self, directory, process):
        """Prepare the verified page result after the worker commits its run."""
        try:
            process.wait()
            state = read_state_json(directory / "status.json")
            if state.get("status") == "completed" and self._directory() == directory:
                self.results(persist=True)
        except Exception:
            # The page can still build or retry the result on demand.
            pass

    def referenced_runs(self):
        """Retain both the selected run and any frozen result reader in use."""
        paths=[self._directory()]
        with self._results_lock:
            if self._results_key:
                paths.append(self._results_key[0])
            reader=self._progressive_results
            if reader is not None:
                for context in (reader.context,reader.requested_context):
                    if context:paths.append(context[0])
        root=self.root.resolve()
        return {Path(path).name for path in paths if path and Path(path).resolve().parent==root
                and re.fullmatch(r'[a-f0-9]{32}',Path(path).name)}

    def release_workers(self):
        if self._reuse_workers:
            from .screening_worker_runtime import release
            with interprocess_file_lock(self.root / 'worker_dispatch.lock', timeout_seconds=15):
                release(self.root)

    def cancel(self):
        with self._lock:
            directory = self._directory()
            if directory and self._status(directory)["status"] in {"starting", "running"}:
                (directory / "cancel").touch()
        return self._status(directory)


manager = ScreeningManager()
